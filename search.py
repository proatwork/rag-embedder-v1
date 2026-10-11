"""Search the embedded chunks: a question in, the closest chunks out. Or look an article up by its number.

Usage (run with the virtual environment's interpreter):
    venvs/bin/python search.py --chunks-dir docs_processed/chunks --vectors-dir docs_processed/vectors "perete antifoc"
    venvs/bin/python search.py --chunks-dir CHUNKS --vectors-dir VECTORS --top 10 --doc "P118-1/2025" "perete antifoc"
    venvs/bin/python search.py --chunks-dir CHUNKS --article 2.3.2.1.2                   # no server needed

--chunks-dir is the output of json_to_chunks.py and --vectors-dir that of embed_chunks.py, made from those chunks.
If the chunks were written again since, the script stops and says which file to embed again. The question is embedded
by the server (--server), with the model the vectors were made with, and its "ş ţ" are turned into "ș ț" as in the
chunks.
Each result shows its score (1 is identical), the chunk id, its citation with the norm's status in words, and the breadcrumb.
The pictures of a chunk are listed under it as `image:` lines with the path of the file.
"""

import argparse
import os
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np

import pipeline_common
from embed_chunks import (
    KEY_HASHES, KEY_IDS, KEY_MODEL, KEY_VECTORS, Embedder, add_server_argument, read_chunks, text_hash, texts_to_embed,
)
from json_to_chunks import (
    AMENDED_BY_KEY, CONFIG_FILE, GAZETTE_KEY, ID_SEPARATOR, LINE_SEPARATOR, RANGE_SEPARATOR, REPEALED_ON_KEY,
    STATUS_IN_FORCE, STATUS_REPEALED, STATUS_UNVERIFIED, WHITESPACE, load_config, norm_fields,
)
from pipeline_common import JSONL_SUFFIX, NPZ_SUFFIX, ConversionError

DEFAULT_TOP = 5
PREVIEW_CHARS = 300  # of a chunk's text, under each result
IMAGE_CAPTION_CHARS = 80
LOCATOR_CHARS = 70  # of a heading or a table's title, in a citation
HEADINGS_CITED = 2  # a chunk without a number is cited by its last headings: "ANEXA NR. 15, Proprietățile dioxidului de carbon"
CAPTIONS_CITED = 3  # of a chunk's figures; the rest are counted, not quoted
# A heading that says where in the norm one is: an annex, a chapter, a part, a section, or a numbered title.
ANCHOR_HEADING = re.compile(r"\s*(?:anexa|capitolul|partea|sec[țţt]iunea|\d)", re.IGNORECASE)
POINT_NUMBER = re.compile(r"\d+\.\d")  # how the label of a point starts: "7.10"; a bare "7" opens a chapter
# The number of an article or point in what someone wrote for it: "Art. 2.3.1.", "pct. 7.10", "A.10. 1.1.1" (of an annex).
# The letter of an annex stands alone: the "a." that ends a word ("anexa. 7.10") is not one.
ARTICLE_NUMBER = re.compile(r"(?:\bA\.\s*)?\d+(?:\.\s*\d+)*", re.IGNORECASE)
NUMBER_PART_SEPARATOR = "."  # between the parts of an article's number
BY_NUMBER = "by number"  # shown in place of a score for a chunk that was looked up by its number, not searched for
ELLIPSIS = "…"
LIST_SEPARATOR = ", "

# What is said about a chunk to the model and to the reader. Romanian, because answers quote it; everything else in the
# code, the config and the chunks stays English. To change the language, change these.
POINT_PREFIX = "pct. "  # before a point that the norm gives only a number ("7.10"); articles come as "Art. 2.3.1" already
PARAGRAPH_PREFIX = "alin. "  # before a numbered paragraph of an article or point, "(2)"; only the instructions use it
TITLE_SEPARATOR = " — "
EDITION = "ed. {year}"
PAGE_LABEL = "PDF p."  # the page of the PDF file, which is not always the number printed on it
PDF_PAGE = PAGE_LABEL + " {first}"
PDF_PAGES = PDF_PAGE + RANGE_SEPARATOR + "{last}"
STATUS_LABEL = "Statut"
STATUS_WORDS = {STATUS_IN_FORCE: "în vigoare", STATUS_UNVERIFIED: "neverificat", STATUS_REPEALED: "abrogat"}
REPEALED_FROM = " din {day}"
DAY_FORMAT = "%d.%m.%Y"
AMENDED_MARK = "modificat ulterior"  # the instructions name it
AMENDED = "text original, " + AMENDED_MARK + " prin: {acts}"
ACT_SEPARATOR = "; "
FIGURES_LABEL = "Figuri"  # the instructions name it
FIGURES_SHOWN = "afișate utilizatorului în sursele de sub răspuns"
CAPTIONS_LABEL = "legende"
MORE_CAPTIONS = "și alte {count}"
IMAGE_FILE_KEY = "file"  # added to each image of a chunk on loading: its path from where the script runs
EMBED_AGAIN = "run embed_chunks.py again"
CHUNK_AGAIN = "run json_to_chunks.py again, then embed_chunks.py (it will reuse the vectors)"


@dataclass
class Index:
    """The embedded chunks and their vectors, row for row."""
    records: list[dict]
    vectors: np.ndarray
    model: str


def add_chunks_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--chunks-dir", type=Path, required=True, help="folder with the .jsonl files of json_to_chunks.py")
    parser.add_argument("--vectors-dir", type=Path, help="folder with the .npz files of embed_chunks.py")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="per-document settings (default: %(default)s)")
    add_server_argument(parser)


def result_count(value: str) -> int:
    """The value of --top."""
    count = int(value)
    if count < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return count


def add_result_arguments(parser: argparse.ArgumentParser, purpose: str) -> None:
    """The options that pick the chunks a question gets; `purpose` says what is done with them."""
    parser.add_argument("--top", type=result_count, default=DEFAULT_TOP, help=f"how many results to {purpose} (default: %(default)s)")
    parser.add_argument("--doc", help="only this norm, by its code (for example \"P118-1/2025\")")


def load_chunks(chunks_dir: Path) -> dict[Path, list[dict]]:
    """Every chunk file of the folder with its chunks, the ones that are not embedded too. The path of each image is
    relative to the chunk folder; `file` is added to it, which can be opened from where the script runs."""
    chunk_files = {path: read_chunks(path) for path in pipeline_common.collect_files(chunks_dir, JSONL_SUFFIX)}
    for chunks in chunk_files.values():
        for record in chunks:
            for image in record.get("images", []):
                image[IMAGE_FILE_KEY] = os.path.normpath(chunks_dir / image["path"])
    return chunk_files


def load_index(chunk_files: dict[Path, list[dict]], vectors_dir: Path, config: dict) -> Index:
    """Join every chunk to its vector. Stops when a file has no vectors, or vectors made from other text than the
    chunks hold now, or from another model than the rest."""
    records: list[dict] = []
    parts: list[np.ndarray] = []
    models: dict[str, str] = {}
    for source, chunks in chunk_files.items():
        path = pipeline_common.output_path(vectors_dir, source.stem, NPZ_SUFFIX)
        if not pipeline_common.has_content(path):
            sys.exit(f"no vectors for {source.name} in {vectors_dir}: {EMBED_AGAIN}")
        try:
            kept, texts = texts_to_embed(chunks, config)
            with np.load(path) as stored:
                ids, hashes, vectors, model = stored[KEY_IDS].tolist(), stored[KEY_HASHES].tolist(), stored[KEY_VECTORS], str(stored[KEY_MODEL])
        except ConversionError as error:
            sys.exit(f"{source.name}: {error}")
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
            sys.exit(f"cannot read {path}: {error}")
        if ids != [record["id"] for record in kept] or hashes != [text_hash(text) for text in texts]:
            sys.exit(f"{source.name} changed after its vectors were made: {EMBED_AGAIN}")
        stale = stale_norm_fields(chunks, config)
        if stale:
            sys.exit(f"{source.name} was chunked before the config file changed ({', '.join(stale)}): {CHUNK_AGAIN}")
        records += kept
        parts.append(vectors)
        models[model] = source.name
    if len(models) > 1:
        sys.exit("the vectors come from different models (" + ", ".join(f"{model}: {name}" for model, name in models.items()) + f"): {EMBED_AGAIN}")
    ids = [record["id"] for record in records]
    if len(set(ids)) != len(ids):
        sys.exit("two chunks share an id: the chunk files do not belong together")
    return Index(records, np.concatenate(parts), next(iter(models)))


def stale_norm_fields(chunks: list[dict], config: dict) -> list[str]:
    """The fields of a norm (its name, title, edition, status and what goes with it) that the chunks of one file carry
    differently from the config, or not at all. A chunk is cited from what it carries: with this, a status changed in
    the config cannot be given to anyone as it was before."""
    stale: set[str] = set()
    for doc_key in dict.fromkeys(record["doc_key"] for record in chunks):
        first = next(record for record in chunks if record["doc_key"] == doc_key)
        expected = norm_fields(config[doc_key]) if doc_key in config else {}
        stale.update(key for key, value in expected.items() if key not in first or first[key] != value)
    return sorted(stale)


def query_vectors(embedder: Embedder, queries: list[str]) -> np.ndarray:
    """The vectors of questions, spelled the way the chunks are. Raises ConversionError when the server cannot be used."""
    return embedder.encode([pipeline_common.normalize_romanian(query) for query in queries])


def embed_queries(embedder: Embedder, queries: list[str]) -> np.ndarray:
    """As query_vectors, but stops the program when the server cannot be used."""
    try:
        return query_vectors(embedder, queries)
    except ConversionError as error:
        sys.exit(str(error))


def ranked(index: Index, query_vector: np.ndarray) -> list[tuple[float, dict]]:
    """Every embedded chunk with its score for one question, the closest first."""
    scores = index.vectors @ query_vector
    return [(float(scores[row]), index.records[row]) for row in np.argsort(-scores, kind="stable")]


def nearest(index: Index, query_vector: np.ndarray, top: int, doc: str | None = None) -> list[tuple[float, dict]]:
    """The `top` chunks closest to a question's vector with their scores, the closest first; with `doc`, of that norm
    only. Stops at a code no chunk has (see in_doc)."""
    wanted = {record["id"] for record in in_doc(index.records, doc)}
    return [(score, record) for score, record in ranked(index, query_vector) if record["id"] in wanted][:top]


def closest(index: Index, embedder: Embedder, query: str, top: int, doc: str | None = None) -> list[tuple[float, dict]]:
    """The `top` chunks closest to a question with their scores, the closest first; with `doc`, of that norm only.
    Stops the program when the embedding server cannot be used."""
    return nearest(index, embed_queries(embedder, [query])[0], top, doc)


def article_number(text: str) -> str:
    """The number in what someone wrote for an article or point, as the chunks keep it: "Art. 2.3.1." and "pct 2.3.1"
    give "2.3.1", "A.10. 1.1.1" gives "A.10.1.1.1". Of several numbers, the one with the most parts is meant
    ("P118/2-2013, pct. 7.10" gives "7.10"), the first of those that have as many. What holds no number is given back trimmed."""
    numbers = [WHITESPACE.sub("", found.group()).upper() for found in ARTICLE_NUMBER.finditer(text)]
    return max(numbers, key=lambda number: number.count(NUMBER_PART_SEPARATOR)) if numbers else text.strip()


def with_number(records: list[dict], number: str) -> list[dict]:
    """The chunks that hold the article or point `number` (see article_number), in the order of the norms."""
    return [record for record in records if number in record["articles"]]


def unit_id(record: dict) -> str:
    """A chunk's id without its piece number: the same for all pieces of one article or table."""
    return record["id"].rsplit(ID_SEPARATOR, 1)[0]


def breadcrumb(record: dict) -> str:
    return record["embed_text"].split(LINE_SEPARATOR, 1)[0]


def shortened(text: str | None, limit: int) -> str:
    """The start of `text` on one line, at most `limit` characters and a mark that it was cut; empty for no text."""
    line = WHITESPACE.sub(" ", text or "").strip()
    return line[:limit] + ELLIPSIS if len(line) > limit else line


def short_caption(image: dict) -> str:
    """The start of a picture's caption on one line; empty when it has none."""
    return shortened(image.get("caption"), IMAGE_CAPTION_CHARS)


def pages_note(pages: list[int] | None) -> str:
    """The PDF pages a chunk or its pictures are on, as a citation says them; empty when they are not known (a norm
    converted from a Word file has none)."""
    known = sorted({page for page in pages or [] if page})
    if not known:
        return ""
    return (PDF_PAGE if known[0] == known[-1] else PDF_PAGES).format(first=known[0], last=known[-1])


def headings_cited(path: list[str]) -> list[str]:
    """The headings a chunk without a number is cited by: its last one, with the nearest heading above it that says
    where in the norm one is (ANCHOR_HEADING) when the last one does not; the last HEADINGS_CITED when none does."""
    if not path or ANCHOR_HEADING.match(path[-1]):
        return path[-1:]
    anchor = next((heading for heading in reversed(path[:-1]) if ANCHOR_HEADING.match(heading)), None)
    return [anchor, path[-1]] if anchor else path[-HEADINGS_CITED:]


def locator(record: dict) -> str:
    """Where in its norm a chunk is: its article or point ("Art. 2.3.1", "pct. 7.10–7.11"), or the headings it is under
    (see headings_cited) when it has no such number; a table adds its title. Empty for text that stands under no heading."""
    label = record.get("label") or ""
    numbers = record.get("articles") or []
    if not label and numbers:  # a chunk file written before chunks kept their label
        label = numbers[0] if len(numbers) == 1 else numbers[0] + RANGE_SEPARATOR + numbers[-1]
    if POINT_NUMBER.match(label):
        label = POINT_PREFIX + label
    elif label[:1].isdigit():  # a bare number: the opening of a chapter, an item of a list of references
        label = ""
    if not label:
        label = LIST_SEPARATOR.join(shortened(heading, LOCATOR_CHARS) for heading in headings_cited(record.get("heading_path") or []))
    return LIST_SEPARATOR.join(part for part in (label, shortened(record.get("caption"), LOCATOR_CHARS)) if part)


def reference(record: dict) -> str:
    """The short name of a chunk: its norm and where in it, "P118/2-2013, pct. 7.10"."""
    return LIST_SEPARATOR.join(part for part in (record["doc_code"], locator(record)) if part)


def citation(record: dict) -> str:
    """How a chunk is cited in full: "P118/2-2013, pct. 7.10 — <the norm's title> (ed. 2013, <gazette>, PDF p. 36)".
    The gazette and the page are there when they are known."""
    title = TITLE_SEPARATOR + record["doc_title"] if record.get("doc_title") else ""
    details = (EDITION.format(year=record["edition_year"]), record.get(GAZETTE_KEY), pages_note(record.get("pdf_pages")))
    return f"{reference(record)}{title} ({LIST_SEPARATOR.join(part for part in details if part)})"


def status_words(record: dict) -> str:
    """Whether a chunk's norm applies, in words: "în vigoare", "neverificat", "abrogat din 25.08.2026"; then the acts
    that changed it since, when the config names any. A status this script does not know is given as it is."""
    words = STATUS_WORDS.get(record["status"], str(record["status"]))
    if record.get(REPEALED_ON_KEY):
        words += REPEALED_FROM.format(day=date.fromisoformat(record[REPEALED_ON_KEY]).strftime(DAY_FORMAT))
    if record.get(AMENDED_BY_KEY):
        words += ACT_SEPARATOR + AMENDED.format(acts=ACT_SEPARATOR.join(record[AMENDED_BY_KEY]))
    return words


def applies_as_written(record: dict) -> bool:
    """Whether a chunk's norm is in force and unchanged since: then its status needs no remark."""
    return record["status"] == STATUS_IN_FORCE and not record.get(AMENDED_BY_KEY)


def figures_note(record: dict) -> str:
    """What is said of a chunk's pictures: how many, on which PDF pages, that the reader is shown them, and the start
    of the captions they have; empty when it has none. Nobody is given the pictures themselves by this."""
    images = record.get("images", [])
    if not images:
        return ""
    pages = pages_note([image.get("page") for image in images])
    note = f"{FIGURES_LABEL}: {len(images)}{' (' + pages + ')' if pages else ''}{LIST_SEPARATOR}{FIGURES_SHOWN}"
    captions = [f'"{caption}"' for image in images if (caption := short_caption(image))]
    if len(captions) > CAPTIONS_CITED:
        captions = captions[:CAPTIONS_CITED] + [MORE_CAPTIONS.format(count=len(captions) - CAPTIONS_CITED)]
    return note + (f"{ACT_SEPARATOR}{CAPTIONS_LABEL}: {LIST_SEPARATOR.join(captions)}" if captions else "")


def describe_images(record: dict) -> str:
    """One line for each picture of a chunk: the file, the PDF page if it has one, and the start of its caption."""
    lines = []
    for image in record.get("images", []):
        page = f"  p. {image['page']}" if image["page"] else ""
        caption = short_caption(image)
        lines.append(f"{LINE_SEPARATOR}   image: {image.get(IMAGE_FILE_KEY, image['path'])}{page}{'  ' + caption if caption else ''}")
    return "".join(lines)


def describe(record: dict) -> str:
    """The lines shown for one chunk, under its id."""
    flags = f"  flags: {', '.join(record['flags'])}" if record["flags"] else ""
    preview = WHITESPACE.sub(" ", record["text"]).strip()
    if len(preview) > PREVIEW_CHARS:
        preview = preview[:PREVIEW_CHARS] + "…"
    return (
        f"{record['id']}  {record['kind']}{flags}"
        f"{LINE_SEPARATOR}   {citation(record)}  [{status_words(record)}]"
        f"{LINE_SEPARATOR}   {breadcrumb(record)}{LINE_SEPARATOR}   {preview}{describe_images(record)}"
    )


def score_note(score: float | None) -> str:
    """A result's score as it is shown; a chunk that was looked up by its number has none."""
    return BY_NUMBER if score is None else f"{score:.3f}"


def print_results(results: list[tuple[float | None, dict]]) -> None:
    for place, (score, record) in enumerate(results, start=1):
        print(f"{place}. {score_note(score)}  {describe(record)}", end=LINE_SEPARATOR * 2)


def in_doc(records: list[dict], doc: str | None) -> list[dict]:
    """The chunks of the norm with code `doc`, or all of them without one. Stops at a code no chunk has."""
    if doc is None:
        return records
    found = [record for record in records if record["doc_code"].casefold() == doc.casefold()]
    if not found:
        sys.exit(f"no norm with code {doc!r}; known: {', '.join(sorted({record['doc_code'] for record in records}))}")
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_chunks_arguments(parser)
    parser.add_argument("query", nargs="?", help="the question to search for")
    parser.add_argument("--article", help="list the chunks of this article or paragraph number instead of searching")
    add_result_arguments(parser, "show")
    args = parser.parse_args()
    if (args.query is None) == (args.article is None):
        parser.error("give a question or --article, one of the two")

    chunk_files = load_chunks(args.chunks_dir)
    if args.article is not None:
        number = article_number(args.article)
        found = with_number(in_doc([record for chunks in chunk_files.values() for record in chunks], args.doc), number)
        if not found:
            sys.exit(f"no chunk holds number {number!r}")
        for record in found:
            print(describe(record), end=LINE_SEPARATOR * 2)
        return

    if args.vectors_dir is None:
        parser.error("a search needs --vectors-dir")
    index = load_index(chunk_files, args.vectors_dir, load_config(args.config))
    print_results(closest(index, Embedder(index.model, args.server), args.query, args.top, args.doc))


if __name__ == "__main__":
    main()
