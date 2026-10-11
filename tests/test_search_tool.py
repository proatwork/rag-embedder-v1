"""Tests of the search as a tool a language model calls (search_tool.py), and of the parts of search.py it is built on.

No server and no model is used: the index is a handful of chunks written here, and the embedding call is replaced, so
that each question gets a vector chosen here.
Run from the project folder: venvs/bin/python -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import unittest
from unittest import mock

import numpy as np

import ask
import search
import search_tool
from embed_chunks import Embedder
from json_to_chunks import ARTICLE_PREFIX, KIND_ARTICLE, LINE_SEPARATOR, STATUS_IN_FORCE
from pipeline_common import ConversionError
from search_tool import ARG_ARTICLE, ARG_COUNT, ARG_NORM, ARG_QUERY, EXCERPT_SEPARATOR, LAST_SEARCH, MAX_COUNT, MAX_SEARCHES, TOOL_NAME, Search, SearchSession

DOC_KEY = "doc"
CONFIG = {DOC_KEY: {}}
MODEL = "embedding-model"
SERVER = "http://embedding.invalid"  # never asked: the embedding call is replaced
NO_ANSWER = "no answer from the embedding server"
NORM = "T 1/2025"
OTHER_NORM = "T 2/2013"
UNKNOWN_NORM = "X 9"
TITLE = "Titlu"
EDITION = 2025
OTHER_TOOL = "read_norm"
WALL_NUMBER = "2.3.1"
DOOR_NUMBER = "2.3.2"
POINT_NUMBER = "7.10"
ANNEX_NUMBER = "A.10.1.1.1"
PIECES_NUMBER = "5.1"
PARENT_NUMBER = "2.3"  # how the numbers of WALL and DOOR start; no chunk holds it as its own
UNKNOWN_NUMBER = "99.9"
PIECE_COUNT = 4  # of the article that is cut into pieces
SCORE_STEP = 0.125  # between the chunks a question is close to; a power of two, so that the scores are exact
SHORT_LIMIT = 2  # searches, for a session that is to reach its limit soon


def chunk(number: str, label: str | None = None, norm: str = NORM, piece: int = 0, **fields) -> dict:
    """A chunk as json_to_chunks.py writes it, with what a search reads of it: piece `piece` of the article or point
    `number` of `norm`, cited by `label` (an article of that number when none is given)."""
    label = label or ARTICLE_PREFIX + number
    text = f"{label}. Piece {piece}."
    record = {
        "id": f"{norm}:{number}:{piece}", "kind": KIND_ARTICLE, "doc_key": DOC_KEY, "doc_code": norm, "doc_title": TITLE, "edition_year": EDITION,
        "status": STATUS_IN_FORCE, "heading_path": [], "label": label, "caption": "", "articles": [number], "pdf_pages": None,
        "text": text, "embed_text": f"{norm} > {label}{LINE_SEPARATOR}{text}", "flags": [], "images": [],
    }
    record.update(fields)
    return record


WALL = chunk(WALL_NUMBER)
DOOR = chunk(DOOR_NUMBER)
POINT = chunk(POINT_NUMBER, POINT_NUMBER)  # a point, which the norm gives only a number
ANNEX = chunk(ANNEX_NUMBER, "A.10. 1.1.1")  # an annex's own numbering, as the norm writes it
PIECES = [chunk(PIECES_NUMBER, piece=piece) for piece in range(PIECE_COUNT)]  # one article in several chunks
ELSEWHERE = chunk(WALL_NUMBER, norm=OTHER_NORM)  # the number of WALL, in the other norm
RECORDS = [WALL, DOOR, POINT, ANNEX, *PIECES, ELSEWHERE]
INDEX = search.Index(RECORDS, np.eye(len(RECORDS), dtype=np.float32), MODEL)  # every chunk has a direction of its own


def score(place: int) -> float:
    """The score of the chunk a question is close to in `place` (0 is the closest)."""
    return 1 - place * SCORE_STEP


def towards(*records: dict) -> np.ndarray:
    """The vector of a question that is closest to the first of `records`, then to the next, and as far from all the
    others."""
    vector = np.zeros(len(RECORDS), dtype=np.float32)
    for place, record in enumerate(records):
        vector[RECORDS.index(record)] = score(place)
    return vector


ABOUT_WALLS = "pereți antifoc"
ABOUT_DOORS = "uși rezistente la foc"
DOORS_AS_TYPED = "uşi rezistente la foc"  # with the cedilla an old keyboard gives; the chunks have the comma
QUESTIONS = {ABOUT_WALLS: towards(WALL, DOOR, ELSEWHERE), ABOUT_DOORS: towards(DOOR, POINT, WALL)}
FOUND = 3  # how many chunks each question is close to
BAD_COUNTS = (0, -1, MAX_COUNT + 1, str(MAX_COUNT + 1), "many", "2.5", 2.5, True, False, [2], {}, float("inf"), float("nan"))
BAD_CALLS = (  # the arguments of a call that cannot be run, and what the model is told
    ("{not json", search_tool.BAD_ARGUMENTS), ("[1]", search_tool.BAD_ARGUMENTS), ('"perete"', search_tool.BAD_ARGUMENTS), (None, search_tool.BAD_ARGUMENTS),
    ([ABOUT_WALLS], search_tool.BAD_ARGUMENTS), (7, search_tool.BAD_ARGUMENTS),
    ("", search_tool.NOTHING_ASKED), ({}, search_tool.NOTHING_ASKED), ({ARG_QUERY: "  ", ARG_ARTICLE: ""}, search_tool.NOTHING_ASKED),
    ({ARG_QUERY: None, ARG_ARTICLE: None}, search_tool.NOTHING_ASKED), ({ARG_QUERY: 7}, search_tool.NOTHING_ASKED),
    ({ARG_NORM: NORM, ARG_COUNT: 3}, search_tool.NOTHING_ASKED),
    ({ARG_QUERY: ABOUT_WALLS, ARG_NORM: UNKNOWN_NORM}, search_tool.NO_SUCH_NORM.format(norm=UNKNOWN_NORM, codes=f"{NORM}, {OTHER_NORM}")),
    ({ARG_ARTICLE: WALL_NUMBER, ARG_NORM: UNKNOWN_NORM}, search_tool.NO_SUCH_NORM.format(norm=UNKNOWN_NORM, codes=f"{NORM}, {OTHER_NORM}")),
    ({ARG_ARTICLE: UNKNOWN_NUMBER}, search_tool.NO_SUCH_NUMBER.format(article=UNKNOWN_NUMBER, where="")),
    ({ARG_ARTICLE: PARENT_NUMBER}, search_tool.NO_SUCH_NUMBER.format(article=PARENT_NUMBER, where="")),
    ({ARG_ARTICLE: POINT_NUMBER, ARG_NORM: OTHER_NORM}, search_tool.NO_SUCH_NUMBER.format(article=POINT_NUMBER, where=f" in {OTHER_NORM}")),
    ({ARG_ARTICLE: "anexa"}, search_tool.NO_SUCH_NUMBER.format(article="anexa", where="")),
    *(({ARG_QUERY: ABOUT_WALLS, ARG_COUNT: count}, search_tool.BAD_COUNT) for count in BAD_COUNTS),
    ({ARG_ARTICLE: POINT_NUMBER, ARG_COUNT: 0}, search_tool.BAD_COUNT),
    (json.dumps({ARG_QUERY: ABOUT_WALLS, ARG_COUNT: float("inf")}), search_tool.BAD_COUNT),  # JSON text may say Infinity
    (f'{{"{ARG_QUERY}": "{ABOUT_WALLS}", "{ARG_COUNT}": 1e999}}', search_tool.BAD_COUNT),  # a number too large to be one
)


def embedding(vectors: dict[str, np.ndarray]):
    """Replaces the embedding server: every question gets the vector `vectors` holds for it, and no other can be asked."""
    return mock.patch.object(Embedder, "encode", autospec=True, side_effect=lambda embedder, texts: np.array([vectors[text] for text in texts]))


def session(**options) -> SearchSession:
    """A new session over the chunks of this file; `options` as SearchSession takes them."""
    return SearchSession(INDEX, CONFIG, SERVER, **options)


def given(*numbered: tuple[int, dict]) -> list[str]:
    """The excerpts a call is answered with for chunks under their numbers."""
    return search_tool.excerpts(list(numbered), CONFIG)


def above(number: int, record: dict) -> str:
    """What stands in place of an excerpt that was given before."""
    return search_tool.GIVEN_ABOVE.format(number=number, reference=search.reference(record))


def said(number: int, what: str, found: str, limit: int = MAX_SEARCHES) -> str:
    """The line above the excerpts of call `number`, as the model reads it: `what` was looked for, and how much was `found`."""
    return f"Search {number} of at most {limit}, {what}: {found}"


def for_query(query: str) -> str:
    return f'for "{query}"'


def for_article(number: str) -> str:
    return f"the article or point {number}"


def answered(*parts: str) -> str:
    """A whole reply to a call: the line that says what was searched, then what is under it."""
    return EXCERPT_SEPARATOR.join(parts)


def not_run(problem: str, number: int = 1) -> str:
    """The reply to call `number` when it could not be run."""
    return search_tool.NOT_RUN.format(number=number, problem=problem)


def chunks_of(sources: list[tuple[float | None, dict]]) -> list[dict]:
    return [record for _, record in sources]


def parameters(schema: dict) -> dict:
    """The parameters of the tool a schema describes, by name."""
    return schema["function"]["parameters"]["properties"]


def printed(results: list[tuple[float | None, dict]]) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        search.print_results(results)
    return out.getvalue()


class EmbeddingReplaced(unittest.TestCase):
    """Tests in which the embedding server is replaced (see embedding); `encode` keeps what it was asked."""

    def setUp(self):
        patcher = embedding(QUESTIONS)
        self.encode = patcher.start()
        self.addCleanup(patcher.stop)


class ArticleNumberTest(unittest.TestCase):
    def test_the_number_is_taken_out_of_what_someone_wrote_for_it(self):
        for written, number in (
            ("Art. 2.3.1.", "2.3.1"), ("art 2.3.1", "2.3.1"), ("pct. 7.10", "7.10"), ("  7.10 ", "7.10"), ("2.3.2.1.2", "2.3.2.1.2"), ("articolul 5", "5"),
            ("pct. 7.10 alin. (2)", "7.10"), ("2. 3. 1", "2.3.1"),
        ):
            self.assertEqual(search.article_number(written), number, written)

    def test_a_number_of_an_annex_keeps_its_letter_as_the_chunks_have_it(self):
        for written in ("A.10. 1.1.1", "A.10.1.1.1", "a.10.1.1.1", "pct. A. 10. 1.1.1"):
            self.assertEqual(search.article_number(written), ANNEX_NUMBER, written)

    def test_of_several_numbers_the_one_with_the_most_parts_is_meant(self):
        for written in (f"P118/2-2013, pct. {POINT_NUMBER}", f"{POINT_NUMBER} din P118/2-2013", f"art. {POINT_NUMBER} alin. (2) lit. 3"):
            self.assertEqual(search.article_number(written), POINT_NUMBER, written)

    def test_a_word_that_ends_in_a_is_not_the_letter_of_an_annex(self):
        for written in (f"anexa. {POINT_NUMBER}", f"norma.{POINT_NUMBER}"):
            self.assertEqual(search.article_number(written), POINT_NUMBER, written)

    def test_what_holds_no_number_is_given_back_trimmed(self):
        self.assertEqual(search.article_number("  anexa  "), "anexa")
        self.assertEqual(search.article_number(""), "")

    def test_the_chunks_that_hold_a_number_come_in_the_order_of_the_norms(self):
        self.assertEqual(search.with_number(RECORDS, WALL_NUMBER), [WALL, ELSEWHERE])
        self.assertEqual(search.with_number(RECORDS, PIECES_NUMBER), PIECES)
        self.assertEqual(search.with_number(RECORDS, UNKNOWN_NUMBER), [])

    def test_a_number_is_matched_whole(self):
        self.assertEqual(search.with_number(RECORDS, PARENT_NUMBER), [])
        both = chunk(WALL_NUMBER, articles=[WALL_NUMBER, DOOR_NUMBER])  # a chunk that holds two articles
        self.assertEqual(search.with_number([both], DOOR_NUMBER), [both])


class NearestTest(EmbeddingReplaced):
    def test_the_closest_chunks_come_first_with_their_scores(self):
        self.assertEqual(search.nearest(INDEX, QUESTIONS[ABOUT_WALLS], FOUND), [(score(0), WALL), (score(1), DOOR), (score(2), ELSEWHERE)])

    def test_top_is_how_many_are_given(self):
        self.assertEqual(search.nearest(INDEX, QUESTIONS[ABOUT_WALLS], 1), [(score(0), WALL)])
        self.assertEqual(len(search.nearest(INDEX, QUESTIONS[ABOUT_WALLS], len(RECORDS) + 1)), len(RECORDS))

    def test_with_a_norm_only_its_chunks_are_given_and_its_code_is_matched_in_any_case(self):
        self.assertEqual(search.nearest(INDEX, QUESTIONS[ABOUT_WALLS], FOUND, OTHER_NORM.lower()), [(score(2), ELSEWHERE)])

    def test_chunks_as_far_from_the_question_keep_the_order_of_the_norms(self):
        self.assertEqual(chunks_of(search.nearest(INDEX, towards(), 2)), [WALL, DOOR])

    def test_a_question_is_spelled_as_the_chunks_are_before_it_is_embedded(self):
        vectors = search.query_vectors(Embedder(MODEL, SERVER), [DOORS_AS_TYPED, ABOUT_WALLS])
        self.assertEqual(self.encode.call_args.args[1], [ABOUT_DOORS, ABOUT_WALLS])
        np.testing.assert_array_equal(vectors, [QUESTIONS[ABOUT_DOORS], QUESTIONS[ABOUT_WALLS]])


class ScoreNoteTest(unittest.TestCase):
    def test_a_score_has_three_decimals_and_a_chunk_looked_up_by_its_number_has_none(self):
        self.assertEqual(search.score_note(0.87654), "0.877")
        self.assertEqual(search.score_note(1), "1.000")
        self.assertEqual(search.score_note(None), search.BY_NUMBER)

    def test_results_are_printed_whether_they_were_searched_for_or_looked_up(self):
        shown = printed([(score(1), WALL), (None, POINT)])
        self.assertIn(f"1. {score(1):.3f}  {WALL['id']}", shown)
        self.assertIn(f"2. {search.BY_NUMBER}  {POINT['id']}", shown)
        self.assertNotIn("None", shown)


class ExcerptTest(unittest.TestCase):
    def test_an_excerpt_is_its_number_and_citation_then_the_status_then_the_text(self):
        self.assertEqual(
            search_tool.excerpt(3, WALL, "The text."),
            f"[3] {NORM}, {ARTICLE_PREFIX}{WALL_NUMBER} — {TITLE} (ed. {EDITION}){LINE_SEPARATOR}Statut: în vigoare{LINE_SEPARATOR}The text.",
        )

    def test_the_figures_of_a_chunk_follow_the_status_on_its_line(self):
        pictured = chunk(WALL_NUMBER, images=[{"caption": "Figura 1", "page": 4}])
        status_line = search_tool.excerpt(1, pictured, "The text.").split(LINE_SEPARATOR)[1]
        self.assertEqual(status_line, f"{search.STATUS_LABEL}: {search.status_words(pictured)}{search_tool.NOTE_SEPARATOR}{search.figures_note(pictured)}")

    def test_chunks_are_given_as_they_were_embedded_under_the_numbers_they_come_with(self):
        self.assertEqual(
            search_tool.excerpts([(4, POINT), (9, WALL)], CONFIG),
            [search_tool.excerpt(4, POINT, POINT["embed_text"]), search_tool.excerpt(9, WALL, WALL["embed_text"])],
        )
        self.assertEqual(search_tool.excerpts([], CONFIG), [])


class ToolSchemaTest(unittest.TestCase):
    def test_the_schema_describes_one_function_with_the_four_parameters(self):
        schema = search_tool.tool_schema([NORM, OTHER_NORM])
        self.assertEqual((schema["type"], schema["function"]["name"]), ("function", TOOL_NAME))
        self.assertEqual(schema["function"]["description"], search_tool.TOOL_DESCRIPTION)
        self.assertEqual(set(parameters(schema)), {ARG_QUERY, ARG_NORM, ARG_ARTICLE, ARG_COUNT})
        self.assertEqual(json.loads(json.dumps(schema)), schema)  # it can be sent as it is

    def test_no_parameter_is_required_because_a_call_gives_a_query_or_an_article(self):
        self.assertNotIn("required", search_tool.tool_schema([NORM])["function"]["parameters"])

    def test_the_norms_that_can_be_searched_are_listed(self):
        self.assertEqual(parameters(search_tool.tool_schema([NORM, OTHER_NORM]))[ARG_NORM]["enum"], [NORM, OTHER_NORM])

    def test_count_says_its_default_and_its_bounds(self):
        count = parameters(search_tool.tool_schema([NORM], 7))[ARG_COUNT]
        self.assertEqual((count["minimum"], count["maximum"]), (1, MAX_COUNT))
        self.assertEqual(count["description"], search_tool.COUNT_HELP.format(count=7))
        self.assertIn(str(search.DEFAULT_TOP), parameters(search_tool.tool_schema([NORM]))[ARG_COUNT]["description"])

    def test_a_session_offers_the_norms_of_its_chunks_and_its_own_count(self):
        schema = session(count=7).schema()
        self.assertEqual(parameters(schema)[ARG_NORM]["enum"], sorted([NORM, OTHER_NORM]))
        self.assertEqual(schema, search_tool.tool_schema([NORM, OTHER_NORM], 7, {NORM: TITLE, OTHER_NORM: TITLE}))

    def test_the_model_is_told_what_each_norm_is_about_when_that_is_known(self):
        told = parameters(search_tool.tool_schema([NORM, OTHER_NORM], titles={NORM: TITLE, OTHER_NORM: ""}))[ARG_NORM]["description"]
        self.assertIn(search_tool.NORM_WITH_TITLE.format(code=NORM, title=TITLE), told)
        self.assertNotIn(OTHER_NORM, told)  # a norm without a title is in the list of codes only
        self.assertEqual(parameters(search_tool.tool_schema([NORM]))[ARG_NORM]["description"], search_tool.NORM_HELP)

    def test_a_session_for_one_norm_offers_only_that_norm(self):
        self.assertEqual(parameters(session(doc=OTHER_NORM).schema())[ARG_NORM]["enum"], [OTHER_NORM])


class SearchReplyTest(EmbeddingReplaced):
    def test_a_search_is_answered_with_numbered_excerpts_under_a_line_that_says_what_was_searched(self):
        reply = session(count=FOUND).reply({ARG_QUERY: ABOUT_WALLS})
        self.assertEqual(reply, answered(said(1, for_query(ABOUT_WALLS), f"{FOUND} excerpts"), *given((1, WALL), (2, DOOR), (3, ELSEWHERE))))

    def test_an_excerpt_has_its_number_and_citation_then_the_status_then_the_text(self):
        reply = session(count=1).reply({ARG_QUERY: ABOUT_WALLS})
        excerpt = LINE_SEPARATOR.join((f"[1] {NORM}, {ARTICLE_PREFIX}{WALL_NUMBER} — {TITLE} (ed. {EDITION})", "Statut: în vigoare", WALL["embed_text"]))
        self.assertEqual(reply, answered(said(1, for_query(ABOUT_WALLS), "1 excerpt"), excerpt))

    def test_the_excerpts_are_exactly_those_the_model_is_handed_without_the_tool(self):
        searching = session(count=FOUND)
        under_the_header = searching.reply({ARG_QUERY: ABOUT_WALLS}).split(EXCERPT_SEPARATOR, 1)[1]
        pasted = ask.build_messages("Ce?", searching.sources, CONFIG, "Rules.")[-1][ask.API_CONTENT]
        self.assertIn(EXCERPT_SEPARATOR + under_the_header + EXCERPT_SEPARATOR, pasted)

    def test_the_arguments_may_be_json_text_or_the_object_itself(self):
        arguments = {ARG_QUERY: ABOUT_WALLS, ARG_COUNT: FOUND}
        self.assertEqual(session().reply(json.dumps(arguments, ensure_ascii=False)), session().reply(arguments))
        self.assertEqual(session().reply(json.dumps(arguments)), session().reply(arguments))  # with "ț" escaped

    def test_the_numbers_run_on_from_one_search_to_the_next(self):
        searching = session(count=2)
        searching.reply({ARG_QUERY: ABOUT_WALLS})
        reply = searching.reply({ARG_ARTICLE: POINT_NUMBER})
        self.assertEqual(reply, answered(said(2, for_article(POINT_NUMBER), "1 excerpt"), *given((3, POINT))))
        self.assertEqual(chunks_of(searching.sources), [WALL, DOOR, POINT])

    def test_a_chunk_found_again_keeps_its_number_and_is_listed_as_given_above(self):
        searching = session(count=FOUND)
        searching.reply({ARG_QUERY: ABOUT_WALLS})
        reply = searching.reply({ARG_QUERY: ABOUT_DOORS})
        header = said(2, for_query(ABOUT_DOORS), f"{FOUND} excerpts (2 given before, under the same number)")
        self.assertEqual(reply, answered(header, above(2, DOOR), *given((4, POINT)), above(1, WALL)))
        self.assertIn(f"[2] {NORM}, {ARTICLE_PREFIX}{DOOR_NUMBER}: given above, under this number.", reply)
        self.assertNotIn(DOOR["text"], reply)

    def test_the_sources_are_all_the_chunks_so_far_in_the_order_of_their_numbers_with_their_scores(self):
        searching = session(count=FOUND)
        searching.reply({ARG_QUERY: ABOUT_WALLS})
        searching.reply({ARG_QUERY: ABOUT_DOORS})
        self.assertEqual(searching.sources, [(score(0), WALL), (score(1), DOOR), (score(2), ELSEWHERE), (score(1), POINT)])

    def test_every_search_is_kept_as_it_was_run(self):
        searching = session(count=FOUND)
        searching.reply({ARG_QUERY: f"  {ABOUT_WALLS} "})
        searching.reply({ARG_QUERY: ABOUT_DOORS, ARG_NORM: NORM})
        self.assertEqual(searching.searches, [
            Search(1, query=ABOUT_WALLS, numbers=[1, 2, 3], new=3), Search(2, query=ABOUT_DOORS, norm=NORM, numbers=[2, 4, 1], new=1),
        ])

    def test_count_is_how_many_excerpts_a_call_gets_and_may_come_as_text(self):
        for count in (2, "2", " 2 ", 2.0):
            searching = session()
            searching.reply({ARG_QUERY: ABOUT_WALLS, ARG_COUNT: count})
            self.assertEqual(chunks_of(searching.sources), [WALL, DOOR], repr(count))

    def test_a_call_that_names_no_count_gets_the_sessions_own(self):
        for arguments in ({ARG_QUERY: ABOUT_WALLS}, {ARG_QUERY: ABOUT_WALLS, ARG_COUNT: None}, {ARG_QUERY: ABOUT_WALLS, ARG_COUNT: ""}):
            searching = session(count=1)
            searching.reply(arguments)
            self.assertEqual(chunks_of(searching.sources), [WALL], arguments)
        default = session()
        default.reply({ARG_QUERY: ABOUT_WALLS})
        self.assertEqual(len(default.sources), search.DEFAULT_TOP)

    def test_the_largest_count_is_taken(self):
        searching = session()
        searching.reply({ARG_QUERY: ABOUT_WALLS, ARG_COUNT: MAX_COUNT})
        self.assertEqual(len(searching.sources), len(RECORDS))

    def test_norm_limits_a_search_to_one_norm_and_is_matched_in_any_case(self):
        searching = session(count=FOUND)
        reply = searching.reply({ARG_QUERY: ABOUT_WALLS, ARG_NORM: f" {OTHER_NORM.lower()} "})
        self.assertEqual(reply, answered(said(1, f"{for_query(ABOUT_WALLS)} in {OTHER_NORM}", "1 excerpt"), *given((1, ELSEWHERE))))
        self.assertEqual(searching.sources, [(score(2), ELSEWHERE)])
        self.assertEqual(searching.searches[0].norm, OTHER_NORM)


class ArticleReplyTest(EmbeddingReplaced):
    def test_an_article_is_fetched_by_its_number_however_it_is_written(self):
        for written, number, held_by in (
            ("Art. 2.3.1.", WALL_NUMBER, [WALL, ELSEWHERE]), ("art 2.3.1", WALL_NUMBER, [WALL, ELSEWHERE]), ("pct. 7.10", POINT_NUMBER, [POINT]),
            ("7.10", POINT_NUMBER, [POINT]), ("A.10. 1.1.1", ANNEX_NUMBER, [ANNEX]), ("a.10.1.1.1", ANNEX_NUMBER, [ANNEX]),
        ):
            searching = session()
            reply = searching.reply({ARG_ARTICLE: written})
            self.assertEqual(reply.split(EXCERPT_SEPARATOR)[1:], given(*enumerate(held_by, start=1)), written)
            self.assertTrue(reply.startswith(f"Search 1 of at most {MAX_SEARCHES}, {for_article(number)}: {len(held_by)} excerpt"), reply)
            self.assertEqual(searching.searches, [Search(1, article=number, numbers=list(range(1, len(held_by) + 1)), new=len(held_by))], written)

    def test_a_chunk_fetched_by_its_number_has_no_score_and_no_server_is_asked(self):
        searching = session()
        searching.reply({ARG_ARTICLE: WALL_NUMBER})
        self.assertEqual(searching.sources, [(None, WALL), (None, ELSEWHERE)])
        self.encode.assert_not_called()

    def test_with_an_article_the_query_is_not_used(self):
        searching = session()
        reply = searching.reply({ARG_QUERY: ABOUT_DOORS, ARG_ARTICLE: POINT_NUMBER})
        self.assertEqual(chunks_of(searching.sources), [POINT])
        self.assertEqual(searching.searches, [Search(1, article=POINT_NUMBER, numbers=[1], new=1)])
        self.assertNotIn(ABOUT_DOORS, reply)
        self.encode.assert_not_called()

    def test_with_a_norm_only_its_article_is_fetched(self):
        searching = session()
        reply = searching.reply({ARG_ARTICLE: WALL_NUMBER, ARG_NORM: OTHER_NORM})
        self.assertEqual(reply, answered(said(1, f"{for_article(WALL_NUMBER)} in {OTHER_NORM}", "1 excerpt"), *given((1, ELSEWHERE))))

    def test_an_article_in_more_pieces_than_count_says_how_many_are_not_shown(self):
        searching = session()
        reply = searching.reply({ARG_ARTICLE: PIECES_NUMBER, ARG_COUNT: 2})
        header = said(1, for_article(PIECES_NUMBER), f"2 excerpts; {PIECE_COUNT - 2} more with that number are not shown: ask with a larger `{ARG_COUNT}` (at most {MAX_COUNT}), or search with `{ARG_QUERY}` for the part you need")
        self.assertEqual(reply, answered(header, *given((1, PIECES[0]), (2, PIECES[1]))))

    def test_asked_with_a_larger_count_the_other_pieces_come_under_new_numbers(self):
        searching = session()
        searching.reply({ARG_ARTICLE: PIECES_NUMBER, ARG_COUNT: 2})
        reply = searching.reply({ARG_ARTICLE: PIECES_NUMBER, ARG_COUNT: PIECE_COUNT})
        header = said(2, for_article(PIECES_NUMBER), f"{PIECE_COUNT} excerpts (2 given before, under the same number)")
        self.assertEqual(reply, answered(header, above(1, PIECES[0]), above(2, PIECES[1]), *given((3, PIECES[2]), (4, PIECES[3]))))
        self.assertEqual(chunks_of(searching.sources), PIECES)

    def test_a_chunk_fetched_by_its_number_and_then_found_by_a_search_keeps_its_number_and_has_no_score(self):
        searching = session(count=FOUND)
        searching.reply({ARG_ARTICLE: DOOR_NUMBER})
        reply = searching.reply({ARG_QUERY: ABOUT_WALLS})
        self.assertEqual(reply.split(EXCERPT_SEPARATOR)[1:], [*given((2, WALL)), above(1, DOOR), *given((3, ELSEWHERE))])
        self.assertEqual(searching.sources, [(None, DOOR), (score(0), WALL), (score(2), ELSEWHERE)])


class BadCallTest(EmbeddingReplaced):
    def test_a_call_that_cannot_be_run_is_answered_with_the_reason_and_nothing_is_searched(self):
        for arguments, problem in BAD_CALLS:
            searching = session()
            self.assertEqual(searching.reply(arguments), not_run(problem), repr(arguments))
            self.assertEqual((searching.sources, len(searching.searches), searching.searches[0].problem), ([], 1, problem), repr(arguments))
        self.encode.assert_not_called()

    def test_the_reasons_say_what_to_do_instead(self):
        self.assertEqual(
            session().reply({ARG_ARTICLE: POINT_NUMBER, ARG_NORM: OTHER_NORM}),
            f"Search 1 was not run: no excerpt holds the number {POINT_NUMBER} in {OTHER_NORM}. Search with `{ARG_QUERY}` instead.",
        )
        self.assertIn(f"The norms are: {NORM}, {OTHER_NORM}.", session().reply({ARG_QUERY: ABOUT_WALLS, ARG_NORM: UNKNOWN_NORM}))
        self.assertIn(f"from 1 to {MAX_COUNT}", session().reply({ARG_QUERY: ABOUT_WALLS, ARG_COUNT: 0}))

    def test_a_call_of_another_tool_is_told_the_only_tool(self):
        searching = session()
        reply = searching.reply({ARG_QUERY: ABOUT_WALLS}, OTHER_TOOL)
        self.assertEqual(reply, f"Search 1 was not run: there is no tool '{OTHER_TOOL}'. The only tool is {TOOL_NAME}.")
        self.assertEqual((searching.sources, searching.searches[0].problem), ([], search_tool.NO_SUCH_TOOL.format(name=OTHER_TOOL)))
        self.encode.assert_not_called()

    def test_a_call_that_was_not_run_counts_as_a_call_and_takes_no_excerpt_number(self):
        searching = session(count=1)
        searching.reply("{not json")
        reply = searching.reply({ARG_QUERY: ABOUT_WALLS})
        self.assertEqual(reply, answered(said(2, for_query(ABOUT_WALLS), "1 excerpt"), *given((1, WALL))))
        self.assertEqual([search.number for search in searching.searches], [1, 2])

    def test_a_call_that_was_not_run_still_says_what_it_asked_for(self):
        for arguments, query, article, norm in (
            ({ARG_QUERY: ABOUT_WALLS, ARG_COUNT: 0}, ABOUT_WALLS, "", ""), ({ARG_QUERY: ABOUT_WALLS, ARG_NORM: UNKNOWN_NORM}, ABOUT_WALLS, "", ""),
            ({ARG_ARTICLE: f"pct. {POINT_NUMBER}", ARG_NORM: UNKNOWN_NORM}, "", POINT_NUMBER, ""),
            ({ARG_QUERY: ABOUT_WALLS, ARG_ARTICLE: POINT_NUMBER, ARG_COUNT: 0}, "", POINT_NUMBER, ""),
            ({ARG_ARTICLE: UNKNOWN_NUMBER, ARG_NORM: NORM.lower()}, "", UNKNOWN_NUMBER, NORM),
        ):
            searching = session()
            searching.reply(arguments)
            asked = searching.searches[0]
            self.assertEqual((asked.query, asked.article, asked.norm, bool(asked.problem)), (query, article, norm, True), arguments)


class OneNormTest(EmbeddingReplaced):
    """A session made with `doc` searches that norm only, whatever a call asks for."""

    def test_the_norm_of_a_session_is_matched_in_any_case(self):
        self.assertEqual(session(doc=f" {OTHER_NORM.lower()}").doc, OTHER_NORM)
        self.assertIsNone(session(doc=None).doc)
        self.assertIsNone(session(doc="").doc)

    def test_a_norm_no_chunk_has_is_an_error_that_names_the_norms_there_are(self):
        with self.assertRaisesRegex(ConversionError, f"{UNKNOWN_NORM}.*{NORM}, {OTHER_NORM}"):
            session(doc=UNKNOWN_NORM)

    def test_a_call_that_names_no_norm_searches_the_sessions_own(self):
        searching = session(doc=OTHER_NORM, count=FOUND)
        reply = searching.reply({ARG_QUERY: ABOUT_WALLS})
        self.assertEqual(reply, answered(said(1, f"{for_query(ABOUT_WALLS)} in {OTHER_NORM}", "1 excerpt"), *given((1, ELSEWHERE))))

    def test_an_article_is_fetched_from_the_sessions_norm_only(self):
        searching = session(doc=OTHER_NORM)
        searching.reply({ARG_ARTICLE: WALL_NUMBER})
        self.assertEqual(searching.sources, [(None, ELSEWHERE)])
        not_there = search_tool.NO_SUCH_NUMBER.format(article=POINT_NUMBER, where=f" in {OTHER_NORM}")
        self.assertEqual(searching.reply({ARG_ARTICLE: POINT_NUMBER}), not_run(not_there, 2))

    def test_a_call_for_the_sessions_norm_is_run_in_whatever_case_it_names_it(self):
        searching = session(doc=OTHER_NORM)
        searching.reply({ARG_QUERY: ABOUT_WALLS, ARG_NORM: OTHER_NORM.lower()})
        self.assertEqual(chunks_of(searching.sources), [ELSEWHERE])

    def test_a_call_for_another_norm_is_told_which_one_can_be_searched(self):
        searching = session(doc=OTHER_NORM)
        reply = searching.reply({ARG_QUERY: ABOUT_WALLS, ARG_NORM: NORM})
        self.assertEqual(reply, f"Search 1 was not run: only {OTHER_NORM} can be searched in this conversation.")
        self.assertEqual((searching.sources, searching.searches[0].problem), ([], search_tool.ONLY_NORM.format(doc=OTHER_NORM)))
        self.encode.assert_not_called()


class LimitTest(EmbeddingReplaced):
    def test_the_last_search_ends_with_the_note_that_it_was_the_last(self):
        searching = session()
        replies = [searching.reply({ARG_QUERY: ABOUT_WALLS}) for _ in range(MAX_SEARCHES)]
        for reply in replies[:-1]:
            self.assertNotIn(LAST_SEARCH, reply)
        self.assertTrue(replies[-1].startswith(said(MAX_SEARCHES, for_query(ABOUT_WALLS), "")), replies[-1])
        self.assertTrue(replies[-1].endswith(EXCERPT_SEPARATOR + LAST_SEARCH), replies[-1])

    def test_a_last_call_that_cannot_be_run_ends_with_the_note_too(self):
        other_tool = ({ARG_QUERY: ABOUT_WALLS}, OTHER_TOOL, search_tool.NO_SUCH_TOOL.format(name=OTHER_TOOL))
        for arguments, name, problem in (({}, TOOL_NAME, search_tool.NOTHING_ASKED), other_tool):
            searching = session(max_searches=SHORT_LIMIT)
            searching.reply({ARG_QUERY: ABOUT_WALLS})
            self.assertEqual(searching.reply(arguments, name), answered(not_run(problem, SHORT_LIMIT), LAST_SEARCH), name)

    def test_a_call_over_the_limit_is_refused_and_nothing_is_searched(self):
        searching = session(count=1)
        for _ in range(MAX_SEARCHES):
            searching.reply({ARG_QUERY: ABOUT_WALLS})
        self.encode.reset_mock()
        reply = searching.reply({ARG_QUERY: ABOUT_DOORS})
        self.assertEqual(reply, not_run(search_tool.LIMIT_REACHED.format(limit=MAX_SEARCHES), MAX_SEARCHES + 1))
        self.assertIn(f"the limit of {MAX_SEARCHES} searches for one question is reached", reply)
        self.assertEqual((chunks_of(searching.sources), len(searching.searches)), ([WALL], MAX_SEARCHES + 1))
        self.encode.assert_not_called()

    def test_over_the_limit_every_call_is_told_the_limit_whatever_else_is_wrong_with_it(self):
        refused = not_run(search_tool.LIMIT_REACHED.format(limit=SHORT_LIMIT), SHORT_LIMIT + 1)
        for arguments, name in (
            ({ARG_QUERY: ABOUT_WALLS}, OTHER_TOOL), ("{not json", TOOL_NAME), ({ARG_ARTICLE: UNKNOWN_NUMBER}, TOOL_NAME), ({ARG_ARTICLE: POINT_NUMBER}, TOOL_NAME),
        ):
            searching = session(max_searches=SHORT_LIMIT)
            for _ in range(SHORT_LIMIT):
                searching.reply({ARG_QUERY: ABOUT_WALLS})
            self.assertEqual(searching.reply(arguments, name), refused, repr(arguments))

    def test_calls_that_were_not_run_use_up_the_limit_as_the_others_do(self):
        searching = session(max_searches=SHORT_LIMIT)
        searching.reply({})
        searching.reply({ARG_QUERY: ABOUT_WALLS}, OTHER_TOOL)
        self.assertEqual(searching.reply({ARG_QUERY: ABOUT_WALLS}), not_run(search_tool.LIMIT_REACHED.format(limit=SHORT_LIMIT), SHORT_LIMIT + 1))
        self.assertEqual(searching.sources, [])

    def test_a_session_has_refused_once_a_call_came_after_the_limit(self):
        searching = session(max_searches=SHORT_LIMIT)
        states = [searching.refused]
        for _ in range(SHORT_LIMIT + 2):
            searching.reply({ARG_QUERY: ABOUT_WALLS})
            states.append(searching.refused)
        self.assertEqual(states, [False] * (SHORT_LIMIT + 1) + [True] * 2)

    def test_a_call_gets_no_more_excerpts_than_the_most_a_call_may_ask_for(self):
        self.assertEqual(session(count=MAX_COUNT + 30).count, MAX_COUNT)

    def test_arguments_nested_too_deep_to_read_are_a_wrong_call_not_a_crash(self):
        searching = session()
        self.assertIn(search_tool.BAD_ARGUMENTS, searching.reply("[" * 5000))

    def test_a_norm_that_is_not_text_is_refused_not_searched_as_all_norms(self):
        for norm in ([NORM], 5, {"code": NORM}):
            searching = session()
            self.assertIn("there is no norm with code", searching.reply({ARG_QUERY: ABOUT_WALLS, ARG_NORM: norm}), norm)
            self.assertEqual(searching.sources, [])

    def test_the_limit_is_the_one_the_session_was_given(self):
        searching = session(max_searches=1)
        reply = searching.reply({ARG_QUERY: ABOUT_WALLS})
        self.assertTrue(reply.startswith(said(1, for_query(ABOUT_WALLS), "", limit=1)), reply)
        self.assertTrue(reply.endswith(LAST_SEARCH), reply)
        self.assertIn("the limit of 1 searches", searching.reply({ARG_QUERY: ABOUT_WALLS}))


class EmbeddingServerTest(unittest.TestCase):
    """The embedding server cannot be used: whatever asks it is told so by an error, and the program is not stopped."""

    def setUp(self):
        patcher = mock.patch("embed_chunks.post_json", autospec=True, side_effect=ConversionError(NO_ANSWER))
        self.post = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_search_raises_the_servers_error(self):
        searching = session()
        with self.assertRaisesRegex(ConversionError, NO_ANSWER):
            searching.reply({ARG_QUERY: ABOUT_WALLS})
        self.post.assert_called_once()
        self.assertEqual(searching.sources, [])
        self.assertIn(NO_ANSWER, searching.searches[0].problem)  # the call is kept as one that was not run

    def test_the_vectors_of_questions_raise_it_too(self):
        with self.assertRaisesRegex(ConversionError, NO_ANSWER):
            search.query_vectors(Embedder(MODEL, SERVER), [ABOUT_WALLS])

    def test_a_session_its_schema_and_an_article_need_no_server(self):
        searching = session()
        searching.schema()
        searching.reply({ARG_ARTICLE: POINT_NUMBER})
        searching.reply({ARG_QUERY: ABOUT_WALLS, ARG_NORM: UNKNOWN_NORM})
        self.assertEqual(chunks_of(searching.sources), [POINT])
        self.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
