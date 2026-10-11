"""Score the search against questions whose answer is known: is the right article among the first results?

Usage (run with the virtual environment's interpreter):
    venvs/bin/python eval_retrieval.py --chunks-dir docs_processed/chunks --vectors-dir docs_processed/vectors --questions eval-questions.jsonl
    venvs/bin/python eval_retrieval.py --chunks-dir CHUNKS --vectors-dir VECTORS --questions FILE --strip-diacritics
    venvs/bin/python eval_retrieval.py --chunks-dir CHUNKS --questions FILE --check-questions     # no server needed

The questions file has one JSON object per line:
    {"question": "...", "doc": "P118-1/2025", "articles": ["2.3.2.1.2"], "author": "claude"}
    {"question": "...", "doc": "P118-1/2025", "ids": ["p118-1-2025:sec-18-tab6"]}
"doc" is the norm's code. "articles" lists the article or paragraph numbers that answer the question: a result counts
when it is a chunk of that norm holding one of them, or a table of such a chunk. "ids" is for text that has no number
and for one particular table: a chunk id without its last part (the piece number), which then stands for all its
pieces, or a whole chunk id for one piece of a long article (one definition among many). Either must match exactly.
"author" is optional and gives a line of its own in the scores.

Every question is searched in all norms. Printed: how often the answer is the first result, among the first 5 and
the first 10, and the mean of 1/rank over the first 10 (MRR), in total, per norm and per author; then every question
with the rank of its answer, and for the ones not found the results that came first.
A question naming an article or id that no embedded chunk has stops the run: it is a mistake in the file, not a miss.
"""

import argparse
import json
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from embed_chunks import SKIPPED_KINDS, Embedder
from json_to_chunks import LINE_SEPARATOR, TEXT_ENCODING, load_config
from search import Index, add_chunks_arguments, embed_queries, load_chunks, load_index, ranked, unit_id

CUTOFFS = (1, 5, 10)  # "found" means the answer is among this many first results
MRR_DEPTH = 10  # an answer further down counts as zero in the mean of 1/rank
SHOWN_INSTEAD = 3  # results listed for a question whose answer was not found
NO_AUTHOR = "-"
ROW_ALL = "all"


@dataclass
class Question:
    line: int  # in the questions file
    text: str
    doc: str
    articles: frozenset[str]
    ids: frozenset[str]
    author: str


def strip_diacritics(text: str) -> str:
    """The text as typed on a keyboard without Romanian letters: "ă â î ș ț" become "a a i s t"."""
    return "".join(char for char in unicodedata.normalize("NFD", text) if not unicodedata.combining(char))


def read_questions(path: Path) -> list[Question]:
    try:
        lines = path.read_text(encoding=TEXT_ENCODING).splitlines()
    except OSError as error:
        sys.exit(f"cannot read the questions file {path}: {error}")
    questions = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            question = Question(
                number, entry["question"], entry["doc"], frozenset(entry.get("articles", [])), frozenset(entry.get("ids", [])),
                entry.get("author", NO_AUTHOR),
            )
        except (ValueError, KeyError, TypeError) as error:
            sys.exit(f"{path.name} line {number}: {type(error).__name__}: {error}")
        if not question.articles and not question.ids:
            sys.exit(f"{path.name} line {number}: needs \"articles\" or \"ids\"")
        questions.append(question)
    if not questions:
        sys.exit(f"no questions in {path}")
    return questions


def is_answer(record: dict, question: Question, by_id: dict[str, dict]) -> bool:
    """Whether a chunk is one the question accepts: named by id, or of the question's norm and holding one of its
    numbers, itself or through the article it belongs to (a table)."""
    if record["id"] in question.ids or unit_id(record) in question.ids:
        return True
    if record["doc_code"] != question.doc:
        return False
    parent = by_id.get(record["parent_id"])
    numbers = set(record["articles"]) | set(parent["articles"] if parent else [])
    return bool(numbers & question.articles)


def check_questions(questions: list[Question], embedded: list[dict], by_id: dict[str, dict], name: str) -> None:
    """Stop at a question that no embedded chunk can answer: an unknown norm, article number or id."""
    problems = []
    codes = {record["doc_code"] for record in embedded}
    units = {unit_id(record) for record in embedded} | {record["id"] for record in embedded}
    for question in questions:
        where = f"{name} line {question.line}"
        if question.doc not in codes:
            problems.append(f"{where}: no norm with code {question.doc!r}; known: {', '.join(sorted(codes))}")
            continue
        for number in sorted(question.articles):
            alone = Question(question.line, question.text, question.doc, frozenset([number]), frozenset(), question.author)
            if not any(is_answer(record, alone, by_id) for record in embedded):
                problems.append(f"{where}: no embedded chunk of {question.doc} holds number {number!r}")
        problems += [f"{where}: no embedded chunk has id {given!r}" for given in sorted(question.ids - units)]
    if problems:
        sys.exit(LINE_SEPARATOR.join(problems))


def answer_ranks(index: Index, vectors: np.ndarray, questions: list[Question], by_id: dict[str, dict]) -> tuple[list[int], list[list[tuple[float, dict]]]]:
    """Per question: the place of the first accepted chunk among all results, and the first results themselves."""
    ranks, firsts = [], []
    for question, vector in zip(questions, vectors):
        results = ranked(index, vector)
        ranks.append(next(place for place, (_, record) in enumerate(results, start=1) if is_answer(record, question, by_id)))
        firsts.append(results[:SHOWN_INSTEAD])
    return ranks, firsts


def score_row(label: str, ranks: list[int]) -> str:
    found = "".join(f"{sum(rank <= cutoff for rank in ranks) / len(ranks):>8.0%}" for cutoff in CUTOFFS)
    mrr = sum(1 / rank for rank in ranks if rank <= MRR_DEPTH) / len(ranks)
    return f"{label:<22}{len(ranks):>4}{found}{mrr:>8.2f}"


def print_scores(title: str, questions: list[Question], ranks: list[int]) -> None:
    print(f"{LINE_SEPARATOR}{title}")
    print(f"{'':<22}{'n':>4}" + "".join(f"{'top ' + str(cutoff):>8}" for cutoff in CUTOFFS) + f"{'MRR':>8}")
    print(score_row(ROW_ALL, ranks))
    groups: list[tuple[str, Callable[[Question], str]]] = [("norm", lambda question: question.doc), ("author", lambda question: question.author)]
    for name, key in groups:
        values = sorted({key(question) for question in questions})
        if name == "author" and len(values) < 2:
            continue
        print(f"by {name}:")
        for value in values:
            print(score_row(f"  {value}", [rank for question, rank in zip(questions, ranks) if key(question) == value]))


def print_questions(questions: list[Question], ranks: list[int], firsts: list[list[tuple[float, dict]]]) -> None:
    """Every question with the rank of its answer; under a question whose answer was not found, what came first."""
    print(f"{LINE_SEPARATOR}rank of the answer, per question:")
    for question, rank, first in zip(questions, ranks, firsts):
        print(f"{rank:>5}  {question.doc:<12} {question.text}")
        if rank > max(CUTOFFS):
            wanted = ", ".join(sorted(question.articles | question.ids))
            print(f"         wanted {wanted}; came first:")
            for score, record in first:
                print(f"         {score:.3f}  {record['id']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_chunks_arguments(parser)
    parser.add_argument("--questions", type=Path, required=True, help="the questions file, one JSON object per line")
    parser.add_argument("--strip-diacritics", action="store_true", help="also score every question typed without ă â î ș ț")
    parser.add_argument("--check-questions", action="store_true", help="only check that every answer names an embedded chunk; the server is not needed")
    args = parser.parse_args()
    if args.vectors_dir is None and not args.check_questions:
        parser.error("scoring needs --vectors-dir")

    questions = read_questions(args.questions)
    chunk_files = load_chunks(args.chunks_dir)
    by_id = {record["id"]: record for chunks in chunk_files.values() for record in chunks}
    embedded = [record for record in by_id.values() if record["kind"] not in SKIPPED_KINDS]
    check_questions(questions, embedded, by_id, args.questions.name)
    if args.check_questions:
        print(f"{len(questions)} questions, every answer names an embedded chunk")
        return

    index = load_index(chunk_files, args.vectors_dir, load_config(args.config))
    embedder = Embedder(index.model, args.server)
    texts = [question.text for question in questions]
    ranks, firsts = answer_ranks(index, embed_queries(embedder, texts), questions, by_id)
    print(f"{len(questions)} questions against {len(index.records)} chunks, model {index.model}")
    print_scores("as written", questions, ranks)
    if args.strip_diacritics:
        plain_ranks, _ = answer_ranks(index, embed_queries(embedder, [strip_diacritics(text) for text in texts]), questions, by_id)
        print_scores("typed without diacritics", questions, plain_ranks)
    print_questions(questions, ranks, firsts)


if __name__ == "__main__":
    main()
