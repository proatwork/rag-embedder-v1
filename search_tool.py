"""The search as a tool a language model calls: search_norms(query, norm, article, count).

Instead of being handed the chunks closest to the user's own words, the model asks for them itself, in its words, as
often as it needs (up to MAX_SEARCHES for one question), or fetches an article by its number. Every call is answered
with numbered excerpts, each under its citation and status, exactly as ask.py pastes them. The numbers run on from one
search to the next and a chunk found again keeps its number, so that [n] in an answer names one chunk.

This module knows nothing of chat servers: ask.py and the chatbot hand the tool to a model (tool_schema, and
SearchSession.reply for every call it makes), and anything else that can call a function could do the same.

From Python:
    session = SearchSession(index, config, server)       # index and config as search.load_index and load_config give them
    text = session.reply('{"query": "perete antifoc"}')  # what the model is given back for one call
    session.sources                                      # the chunks so far with their scores, numbered from 1 in this order
    session.searches                                     # the calls so far, as they were run
    session.refused                                      # whether a call came after the limit and was told so
"""

import json
from dataclasses import dataclass, field

from embed_chunks import Embedder, texts_to_embed
from json_to_chunks import LINE_SEPARATOR
from pipeline_common import ConversionError
from search import (
    DEFAULT_TOP, LIST_SEPARATOR, STATUS_LABEL, Index, article_number, citation, figures_note, in_doc, nearest, query_vectors,
    reference, status_words, with_number,
)

TOOL_NAME = "search_norms"
# The tool's parameters. English, as all names a program reads; what the tool gives back quotes the norms in Romanian.
ARG_QUERY = "query"
ARG_NORM = "norm"
ARG_ARTICLE = "article"
ARG_COUNT = "count"
MAX_COUNT = 20  # the most excerpts one call may ask for
MAX_SEARCHES = 5  # calls for one question; each is another round with the model, and adds its excerpts to the prompt
TOOL_DESCRIPTION = (
    "Search the Romanian fire-safety norms. Returns numbered excerpts, each under its citation and the status of its norm. "
    f"Give `{ARG_QUERY}` to search by meaning, or `{ARG_ARTICLE}` to fetch an article or point by its number."
)
QUERY_HELP = "What to look for, in Romanian, in the words a norm would use; complete on its own, without relying on the conversation."
NORM_HELP = "Search this norm only (its code). Leave out to search all of them, unless you know which norm holds the answer."
NORMS_ARE = " The norms: {norms}."
NORM_WITH_TITLE = "{code} ({title})"
NORM_SEPARATOR = "; "
ARTICLE_HELP = f'The number of an article or point to fetch as it is, for example "2.3.2.1.2" or "7.10". With it, `{ARG_QUERY}` is not used.'
COUNT_HELP = "How many excerpts to return (default {count})."

EXCERPT_HEADER = "[{number}] {citation}"
NOTE_SEPARATOR = " · "  # between the status and the figures, on the line under the citation
EXCERPT_SEPARATOR = LINE_SEPARATOR * 2
# What a call is answered with, around the excerpts. English, like the rest of what frames the excerpts for the model.
RESULT_HEADER = "Search {number} of at most {limit}, {what}: {found}"
FOR_QUERY = 'for "{query}"'
FOR_ARTICLE = "the article or point {article}"
IN_NORM = " in {norm}"
FOUND_ONE = "1 excerpt"
FOUND_MANY = "{count} excerpts"
REPEATS = " ({count} given before, under the same number)"
NOT_SHOWN = "; {count} more with that number are not shown: ask with a larger `" + ARG_COUNT + f"` (at most {MAX_COUNT}), or search with `{ARG_QUERY}` for the part you need"
GIVEN_ABOVE = "[{number}] {reference}: given above, under this number."
LAST_SEARCH = "This was the last search for this question: answer now from the excerpts you have, and say what they leave unanswered."
NOT_RUN = "Search {number} was not run: {problem}"
LIMIT_REACHED = "the limit of {limit} searches for one question is reached. Answer now from the excerpts you have, and say what they leave unanswered."
BAD_ARGUMENTS = f"the arguments must be a JSON object with `{ARG_QUERY}` or `{ARG_ARTICLE}`."
NOTHING_ASKED = f"give `{ARG_QUERY}` (what to look for) or `{ARG_ARTICLE}` (a number to fetch)."
NO_SUCH_NORM = "there is no norm with code {norm!r}. The norms are: {codes}."
ONLY_NORM = "only {doc} can be searched in this conversation."
NO_SUCH_NUMBER = "no excerpt holds the number {article}{where}. Search with `" + ARG_QUERY + "` instead."
BAD_COUNT = f"`{ARG_COUNT}` must be a whole number from 1 to {MAX_COUNT}."
NO_SUCH_TOOL = f"there is no tool {{name!r}}. The only tool is {TOOL_NAME}."


class BadCall(Exception):
    """A call of the tool that cannot be run; the message says why, for the model."""


@dataclass
class Search:
    """One call of the tool as it was run: what was asked, and what it gave."""
    number: int  # among the calls of one question, from 1
    query: str = ""
    article: str = ""  # the number looked up, when the call was for one
    norm: str = ""  # the norm searched; empty for all of them
    numbers: list[int] = field(default_factory=list)  # the excerpts given back, by their numbers in SearchSession.sources
    new: int = 0  # how many of them were not given before in this question
    problem: str = ""  # why nothing was searched: the call was wrong, or over the limit


def excerpt(number: int, record: dict, text: str) -> str:
    """One chunk as the language model is given it: its number and citation, then a line with the status of its norm
    and with its figures when it has any, then `text`."""
    notes = NOTE_SEPARATOR.join(note for note in (f"{STATUS_LABEL}: {status_words(record)}", figures_note(record)) if note)
    return LINE_SEPARATOR.join((EXCERPT_HEADER.format(number=number, citation=citation(record)), notes, text))


def excerpts(numbered: list[tuple[int, dict]], config: dict) -> list[str]:
    """Chunks with their numbers as the language model is given them (see excerpt). A chunk is given as it was embedded;
    the embedded text itself does not change."""
    _, texts = texts_to_embed([record for _, record in numbered], config)
    return [excerpt(number, record, text) for (number, record), text in zip(numbered, texts)]


def tool_schema(codes: list[str], count: int = DEFAULT_TOP, titles: dict[str, str] | None = None) -> dict:
    """The tool as an OpenAI-style chat request describes it (an entry of `tools`); `codes` are the norms that can be
    searched, `count` how many excerpts a call gets when it does not say. With `titles` (by code), the model is told
    what each norm is about, so that it can tell which one to search."""
    named = [NORM_WITH_TITLE.format(code=code, title=titles[code]) for code in codes if titles and titles.get(code)]
    norm_help = NORM_HELP + (NORMS_ARE.format(norms=NORM_SEPARATOR.join(named)) if named else "")
    return {"type": "function", "function": {"name": TOOL_NAME, "description": TOOL_DESCRIPTION, "parameters": {
        "type": "object",
        "properties": {
            ARG_QUERY: {"type": "string", "description": QUERY_HELP},
            ARG_NORM: {"type": "string", "enum": codes, "description": norm_help},
            ARG_ARTICLE: {"type": "string", "description": ARTICLE_HELP},
            ARG_COUNT: {"type": "integer", "minimum": 1, "maximum": MAX_COUNT, "description": COUNT_HELP.format(count=count)},
        },
    }}}


def text_argument(arguments: dict, name: str) -> str:
    """An argument that is text, trimmed; empty when it is left out, null or anything else."""
    value = arguments.get(name)
    return value.strip() if isinstance(value, str) else ""


class SearchSession:
    """The searches of one question and the chunks they gave. `server` runs the embedding model; `count` is how many
    excerpts a call gets when it does not say (at most MAX_COUNT); with `doc`, only that norm can be searched, and a
    call that names another is refused. Raises ConversionError for a `doc` no chunk has."""

    def __init__(self, index: Index, config: dict, server: str, count: int = DEFAULT_TOP, doc: str | None = None, max_searches: int = MAX_SEARCHES):
        self.index = index
        self.config = config
        self.embedder = Embedder(index.model, server)
        self.count = min(count, MAX_COUNT)
        self.max_searches = max_searches
        self.titles = {record["doc_code"]: record.get("doc_title") or "" for record in index.records}  # of the norms, by code
        self.codes = sorted(self.titles)
        self.doc = self.known_code(doc) if doc else None
        if doc and self.doc is None:
            raise ConversionError(NO_SUCH_NORM.format(norm=doc, codes=LIST_SEPARATOR.join(self.codes)))
        self.sources: list[tuple[float | None, dict]] = []  # numbered from 1 in this order; a chunk fetched by its number has no score
        self.searches: list[Search] = []
        self.numbers: dict[str, int] = {}  # of the chunks in `sources`, by id

    def schema(self) -> dict:
        """The tool as a chat request describes it (see tool_schema), with the norms this session searches and their titles."""
        return tool_schema([self.doc] if self.doc else self.codes, self.count, self.titles)

    def known_code(self, code: str) -> str | None:
        """The norm's code as the chunks spell it, for a code written in any case; None when there is no such norm."""
        return next((known for known in self.codes if known.casefold() == code.strip().casefold()), None)

    @property
    def refused(self) -> bool:
        """Whether a call came after the limit and was told so: a model that calls again is not going to stop."""
        return len(self.searches) > self.max_searches

    def reply(self, arguments: str | dict, name: str = TOOL_NAME) -> str:
        """What the model is given back for one call: the excerpts found, numbered, under a line that says what was
        searched. `arguments` is the JSON text a model writes for a call, or the object itself; `name` is the tool it
        called. A call that cannot be run (another tool, wrong arguments, an unknown norm or number, over the limit) is
        answered with the reason, for the model to go on from; it counts as a call all the same. Raises ConversionError
        when the embedding server cannot be used."""
        search = Search(len(self.searches) + 1)
        self.searches.append(search)
        try:
            if search.number > self.max_searches:  # before anything else: `refused` says the model was told the limit
                raise BadCall(LIMIT_REACHED.format(limit=self.max_searches))
            if name != TOOL_NAME:
                raise BadCall(NO_SUCH_TOOL.format(name=name))
            found, more = self.find(search, arguments)
        except BadCall as problem:
            search.problem = str(problem)
            not_run = NOT_RUN.format(number=search.number, problem=search.problem)
            return EXCERPT_SEPARATOR.join((not_run, LAST_SEARCH)) if search.number == self.max_searches else not_run
        except ConversionError as problem:  # the embedding server: the call is kept as one that was not run, and the question ends here
            search.problem = str(problem)
            raise
        fresh = [(score, record) for score, record in found if record["id"] not in self.numbers]
        numbered = [(len(self.sources) + place, record) for place, (_, record) in enumerate(fresh, start=1)]
        texts = dict(zip((number for number, _ in numbered), excerpts(numbered, self.config)))
        self.numbers.update((record["id"], number) for number, record in numbered)
        self.sources += fresh
        search.numbers = [self.numbers[record["id"]] for _, record in found]
        search.new = len(fresh)
        parts = [self.header(search, more)]
        parts += [texts.get(number) or GIVEN_ABOVE.format(number=number, reference=reference(self.sources[number - 1][1])) for number in search.numbers]
        if search.number == self.max_searches:
            parts.append(LAST_SEARCH)
        return EXCERPT_SEPARATOR.join(parts)

    def find(self, search: Search, arguments: str | dict) -> tuple[list[tuple[float | None, dict]], int]:
        """The chunks one call asks for with their scores, and how many more hold the number it asked for than it is
        given. Fills `search` with what was asked, before anything about it is refused. Raises BadCall for a call that
        cannot be run."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except (ValueError, RecursionError) as error:  # not JSON, or nested too deep to read
                raise BadCall(BAD_ARGUMENTS) from error
        if not isinstance(arguments, dict):
            raise BadCall(BAD_ARGUMENTS)
        article = text_argument(arguments, ARG_ARTICLE)
        search.article = article_number(article) if article else ""
        search.query = "" if article else text_argument(arguments, ARG_QUERY)
        if not isinstance(arguments.get(ARG_NORM), str | None):  # a list of norms, a number: not searched as if none was named
            raise BadCall(NO_SUCH_NORM.format(norm=arguments[ARG_NORM], codes=LIST_SEPARATOR.join(self.codes)))
        norm = self.norm_asked(text_argument(arguments, ARG_NORM))
        search.norm = norm or ""
        count = self.count_asked(arguments.get(ARG_COUNT))
        if article:
            holding = with_number(in_doc(self.index.records, norm), search.article)
            if not holding:
                raise BadCall(NO_SUCH_NUMBER.format(article=search.article, where=IN_NORM.format(norm=norm) if norm else ""))
            return [(None, record) for record in holding[:count]], max(len(holding) - count, 0)
        if not search.query:
            raise BadCall(NOTHING_ASKED)
        return nearest(self.index, query_vectors(self.embedder, [search.query])[0], count, norm), 0

    def norm_asked(self, norm: str) -> str | None:
        """The norm a call is to search: the session's own when it has one, else the one the call names, else all (None).
        Raises BadCall for a code that is no norm, and for another norm than the session's."""
        code = self.known_code(norm) if norm else None
        if norm and code is None:
            raise BadCall(NO_SUCH_NORM.format(norm=norm, codes=LIST_SEPARATOR.join(self.codes)))
        if self.doc and code not in (None, self.doc):
            raise BadCall(ONLY_NORM.format(doc=self.doc))
        return self.doc or code

    def count_asked(self, count) -> int:
        """How many excerpts a call gets: what it asks for (a model may write the number as text), else the session's
        own. Raises BadCall for anything that is not a whole number from 1 to MAX_COUNT."""
        if count is None or count == "":
            return self.count
        try:
            wanted = int(count)
        except (TypeError, ValueError, OverflowError) as error:  # OverflowError: JSON text may say Infinity, or 1e999
            raise BadCall(BAD_COUNT) from error
        if isinstance(count, bool) or (isinstance(count, float) and wanted != count) or not 1 <= wanted <= MAX_COUNT:
            raise BadCall(BAD_COUNT)
        return wanted

    def header(self, search: Search, more: int) -> str:
        """The line above the excerpts of one call: which call it is, what it looked for, and how much it found."""
        what = FOR_ARTICLE.format(article=search.article) if search.article else FOR_QUERY.format(query=search.query)
        found = FOUND_ONE if len(search.numbers) == 1 else FOUND_MANY.format(count=len(search.numbers))
        repeats = len(search.numbers) - search.new
        return (
            RESULT_HEADER.format(number=search.number, limit=self.max_searches, what=what + (IN_NORM.format(norm=search.norm) if search.norm else ""), found=found)
            + (REPEATS.format(count=repeats) if repeats else "") + (NOT_SHOWN.format(count=more) if more else "")
        )
