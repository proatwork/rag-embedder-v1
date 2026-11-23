"""Tests of the model that searches for itself in ask.py: the round with a chat server that takes tools, the loop that
answers the model's calls, what ask() gives back and ask.py prints, and what the model is told about the tool.

A stand-in OpenAI-style server on this machine plays the chat server, and a scripted function plays the model where no
server is needed; the embedding call is replaced. No real server and no model is used.
Run from the project folder: venvs/bin/python -m unittest discover -s tests -v
"""

import contextlib
import copy
import inspect
import io
import itertools
import json
import re
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import numpy as np

import ask
import search_tool
from embed_chunks import API_ERROR, API_MODEL, Embedder
from json_to_chunks import ARTICLE_PREFIX, KIND_ARTICLE, LINE_SEPARATOR, STATUS_IN_FORCE
from pipeline_common import ConversionError
from search import STATUS_LABEL, Index, citation, score_note
from search_tool import ARG_ARTICLE, ARG_COUNT, ARG_NORM, ARG_QUERY, EXCERPT_SEPARATOR, MAX_SEARCHES, TOOL_NAME, Search, SearchSession

DOC_KEY = "doc"
CONFIG = {DOC_KEY: {}}
EMBEDDING_MODEL = "embedding-model"
EMBEDDING_SERVER = "http://embedding.invalid"  # never asked: the embedding call is replaced
NO_ANSWER = "no answer from the embedding server"
CHAT_MODEL = "chat-model"
WRITTEN_BY = f"Written by {CHAT_MODEL} from:"  # what ask.py prints above the chunks of an answer
KEY = "secret-key-123"
ECHO_PATH = "/echo"  # where the stand-in refuses the request and repeats the key it was given
NOTHING_TO_SAY = "the stand-in has no reply left"
NO_TOOLS_TAKEN = "this model does not support tools"  # what the stand-in says instead, to a request that carries tools
FINISH_STOP = "stop"
NORM = "T 1/2025"
UNKNOWN_NORM = "X 9"
WALL_NUMBER = "2.3.1"
DOOR_NUMBER = "2.3.2"
STAIR_NUMBER = "4.1"
UNKNOWN_NUMBER = "99.9"
OTHER_TOOL = "read_norm"
FIRST_ID = "abc"  # of the calls a chat server sends
SECOND_ID = "def"
RULES = "The rules."
QUESTION = "Ce rezistență la foc are un perete antifoc?"
ANSWER = f"EI 180 ({NORM}, {ARTICLE_PREFIX}{WALL_NUMBER}) [1]"
ANSWER_WITHOUT_SEARCHING = "EI 180, din câte știu."
TOP = 1  # chunks a search gives: each question below is closest to one chunk
SHORT_LIMIT = 2  # searches, for a session that is to reach its limit soon
TOKEN = re.compile(r"\{[a-z_]+\}")  # how a token of an instructions file looks


def chunk(number: str) -> dict:
    """A chunk as json_to_chunks.py writes it, with what a search and a citation read of it: the article `number`."""
    label = ARTICLE_PREFIX + number
    text = f"{label}. Text."
    return {
        "id": f"{NORM}:{number}", "kind": KIND_ARTICLE, "doc_key": DOC_KEY, "doc_code": NORM, "edition_year": 2025, "status": STATUS_IN_FORCE,
        "label": label, "articles": [number], "text": text, "embed_text": f"{NORM} > {label}{LINE_SEPARATOR}{text}", "flags": [],
    }


WALL, DOOR, STAIR = RECORDS = [chunk(number) for number in (WALL_NUMBER, DOOR_NUMBER, STAIR_NUMBER)]
INDEX = Index(RECORDS, np.eye(len(RECORDS), dtype=np.float32), EMBEDDING_MODEL)  # every chunk has a direction of its own
ABOUT_WALLS = "rezistența la foc a pereților antifoc"
ABOUT_DOORS = "uși în pereți antifoc"
# What the model searches for finds WALL and DOOR; the question's own words are closest to STAIR, which only the single search gives.
VECTORS = {ABOUT_WALLS: INDEX.vectors[0], ABOUT_DOORS: INDEX.vectors[1], QUESTION: INDEX.vectors[2]}
MESSAGES = [{ask.API_ROLE: ask.ROLE_SYSTEM, ask.API_CONTENT: RULES}, {ask.API_ROLE: ask.ROLE_USER, ask.API_CONTENT: QUESTION}]
CALL_IDS = (f"call-{number}" for number in itertools.count(1))  # of the calls a scripted model makes; no two are the same
CALL_AS_JSON = json.dumps({ask.API_NAME: TOOL_NAME, ask.API_ARGUMENTS: {ARG_QUERY: ABOUT_WALLS}}, ensure_ascii=False)
WRITTEN_CALLS = (  # what a model sends as its answer when its call was not recognised as one
    f"<tool_call>{LINE_SEPARATOR}{CALL_AS_JSON}{LINE_SEPARATOR}</tool_call>", f"Caut în norme. <tool_call>{CALL_AS_JSON}</tool_call>", CALL_AS_JSON,
    f"  {LINE_SEPARATOR}{CALL_AS_JSON}", json.dumps(json.loads(CALL_AS_JSON), indent=2),
    json.dumps({ask.API_ARGUMENTS: {ARG_ARTICLE: WALL_NUMBER}, ask.API_NAME: TOOL_NAME}), json.dumps({ARG_QUERY: ABOUT_WALLS, ARG_NORM: NORM}),
    json.dumps({ARG_ARTICLE: WALL_NUMBER}), json.dumps({ARG_NORM: NORM, ARG_ARTICLE: WALL_NUMBER}), json.dumps({ARG_COUNT: 8, ARG_QUERY: ABOUT_WALLS}),
    f"```json{LINE_SEPARATOR}{CALL_AS_JSON}{LINE_SEPARATOR}```", f"```{LINE_SEPARATOR}{CALL_AS_JSON}{LINE_SEPARATOR}```",
)


def embedding(vectors: dict[str, np.ndarray]):
    """Replaces the embedding server: every question gets the vector `vectors` holds for it, and no other can be asked."""
    return mock.patch.object(Embedder, "encode", autospec=True, side_effect=lambda embedder, texts: np.array([vectors[text] for text in texts]))


def new_session(**options) -> SearchSession:
    """A new session over the chunks of this file, which gives TOP chunks a search; `options` as SearchSession takes them."""
    return SearchSession(INDEX, CONFIG, EMBEDDING_SERVER, TOP, **options)


def result_of(call_id: str, arguments: dict | str) -> dict:
    """The message that carries what the first call of a question gave, for a call with `arguments`."""
    return {ask.API_ROLE: ask.ROLE_TOOL, ask.API_TOOL_CALL_ID: call_id, ask.API_CONTENT: new_session().reply(arguments)}


def chunks_of(sources: list[tuple[float | None, dict]]) -> list[dict]:
    return [record for _, record in sources]


def calling(*arguments: dict, name: str = TOOL_NAME) -> ask.Reply:
    """A reply of the model that says nothing and makes one call of `name` for each of `arguments`."""
    return ask.Reply("", [ask.ToolCall(next(CALL_IDS), name, json.dumps(asked, ensure_ascii=False)) for asked in arguments])


def searching_for(*queries: str) -> ask.Reply:
    """A reply of the model that searches for each of `queries`."""
    return calling(*({ARG_QUERY: query} for query in queries))


def answering(text: str = ANSWER) -> ask.Reply:
    """A reply of the model that makes no call."""
    return ask.Reply(text, [])


def call_sent(arguments: dict | str, call_id: str | None = None, name: str = TOOL_NAME) -> dict:
    """A call as a chat server sends it: `arguments` as JSON text, as most servers do, unless they are given as text
    already; without `call_id`, as a server that gives a call no id."""
    function = {ask.API_NAME: name, ask.API_ARGUMENTS: arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)}
    return {ask.API_TYPE: ask.TYPE_FUNCTION, ask.API_FUNCTION: function, **({ask.API_ID: call_id} if call_id else {})}


def choice(text: str | None = "", calls: list | None = None, finish: str = FINISH_STOP) -> dict:
    """What a chat server answers with: a message of the model with `text` and, when given, `calls`."""
    message = {ask.API_ROLE: ask.ROLE_ASSISTANT, ask.API_CONTENT: text, **({} if calls is None else {ask.API_TOOL_CALLS: calls})}
    return {ask.API_MESSAGE: message, ask.API_FINISH: finish}


SEARCHING = choice(calls=[call_sent({ARG_QUERY: ABOUT_WALLS}, FIRST_ID)])  # the model searches, as a chat server sends it


def refusal(reason: str) -> dict:
    """What a chat server answers with when it refuses a request."""
    return {API_ERROR: {"message": reason}}


def prompt_part(role: str, text: str = "") -> str:
    """One part of a prompt as --show-prompt prints it: its heading, then the text."""
    return ask.PROMPT_PART.format(role=role) + LINE_SEPARATOR + text


def roles(messages: list[dict]) -> list[str]:
    return [message[ask.API_ROLE] for message in messages]


class Scripted:
    """A model that sends the replies it was made with, one each time it is asked, and keeps what it was sent: the
    messages as they were then, and the tools."""

    def __init__(self, *replies: ask.Reply):
        self.replies = list(replies)
        self.sent = []

    def __call__(self, messages: list[dict], tools: list[dict]) -> ask.Reply:
        self.sent.append((copy.deepcopy(messages), tools))
        return self.replies.pop(0)


class StandIn(ThreadingHTTPServer):
    """Answers every POST like an OpenAI-style chat server: with the next of the `choices` it was made with, under the
    name of the model it was asked for."""

    def __init__(self, *choices: dict):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.choices = list(choices)  # still to be answered with, in order
        self.seen = []  # the body of every request

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append(body)
        if self.path == ECHO_PATH:
            status, reply = 400, refusal(f"bad key {self.headers.get(ask.AUTHORIZATION)}")
        elif not self.server.choices:
            status, reply = 500, refusal(NO_TOOLS_TAKEN if ask.API_TOOLS in body else NOTHING_TO_SAY)
        else:
            status, reply = 200, {API_MODEL: body[API_MODEL], ask.API_CHOICES: [self.server.choices.pop(0)]}
        data = json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class NoRealServer(unittest.TestCase):
    """Tests that reach no real server: the embedding server is replaced (see embedding; `encode` keeps what it was
    asked), and `serve` starts the stand-in for the chat server."""

    def setUp(self):
        patcher = embedding(VECTORS)
        self.encode = patcher.start()
        self.addCleanup(patcher.stop)

    def serve(self, *choices: dict) -> StandIn:
        """A chat server that answers with `choices`, one a request, until the test is over."""
        api = StandIn(*choices)
        threading.Thread(target=api.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(api.server_close)
        self.addCleanup(api.shutdown)
        return api


class ToolCallsTest(unittest.TestCase):
    def test_a_message_without_calls_has_none(self):
        for message in ({}, {ask.API_TOOL_CALLS: None}, {ask.API_TOOL_CALLS: []}, {ask.API_CONTENT: ANSWER}):
            self.assertEqual(ask.tool_calls_of(message), [], message)

    def test_a_call_is_its_id_the_tool_and_the_arguments_as_they_were_written(self):
        for written in (CALL_AS_JSON, "{not json", ""):
            self.assertEqual(ask.tool_calls_of({ask.API_TOOL_CALLS: [call_sent(written, FIRST_ID)]}), [ask.ToolCall(FIRST_ID, TOOL_NAME, written)])

    def test_an_id_that_is_given_is_not_one_another_call_of_the_message_has(self):
        taken = ask.CALL_ID.format(number=1)
        calls = ask.tool_calls_of({ask.API_TOOL_CALLS: [call_sent({}), call_sent({}, taken)]})
        self.assertEqual([call.id for call in calls], [ask.CALL_ID.format(number=2), taken])

    def test_a_call_without_an_id_is_given_one(self):
        nameless = call_sent({}, name=OTHER_TOOL)
        calls = ask.tool_calls_of({ask.API_TOOL_CALLS: [nameless, {**nameless, ask.API_ID: ""}, {**nameless, ask.API_ID: 7}, call_sent({}, FIRST_ID)]})
        self.assertEqual([call.id for call in calls], [*(ask.CALL_ID.format(number=number) for number in (1, 2, 3)), FIRST_ID])

    def test_arguments_sent_as_an_object_become_json_text(self):
        arguments = {ARG_QUERY: ABOUT_WALLS, ARG_COUNT: 3}
        sent = call_sent({})
        sent[ask.API_FUNCTION][ask.API_ARGUMENTS] = arguments
        written = ask.tool_calls_of({ask.API_TOOL_CALLS: [sent]})[0].arguments
        self.assertEqual(json.loads(written), arguments)
        self.assertIn(ABOUT_WALLS, written)  # not escaped

    def test_a_call_without_arguments_has_an_empty_object(self):
        for function in ({ask.API_NAME: TOOL_NAME}, {ask.API_NAME: TOOL_NAME, ask.API_ARGUMENTS: None}):
            self.assertEqual(ask.tool_calls_of({ask.API_TOOL_CALLS: [{ask.API_FUNCTION: function}]})[0].arguments, "{}")

    def test_what_is_no_call_of_a_function_is_left_out(self):
        no_functions = [{ask.API_FUNCTION: function} for function in (None, {}, {ask.API_NAME: 5}, {ask.API_ARGUMENTS: "{}"})]
        junk = ["text", None, {}, *no_functions]
        self.assertEqual(ask.tool_calls_of({ask.API_TOOL_CALLS: junk}), [])
        self.assertEqual(ask.tool_calls_of({ask.API_TOOL_CALLS: [*junk, call_sent({})]}), [ask.ToolCall(ask.CALL_ID.format(number=1), TOOL_NAME, "{}")])

    def test_a_reply_that_made_calls_is_sent_back_as_the_assistants_message_with_those_calls(self):
        reply = ask.Reply("Caut.", [ask.ToolCall(FIRST_ID, TOOL_NAME, CALL_AS_JSON), ask.ToolCall(SECOND_ID, OTHER_TOOL, "{}")])
        message = ask.calls_message(reply)
        self.assertEqual(message, {ask.API_ROLE: ask.ROLE_ASSISTANT, ask.API_CONTENT: "Caut.", ask.API_TOOL_CALLS: [
            {ask.API_ID: FIRST_ID, ask.API_TYPE: ask.TYPE_FUNCTION, ask.API_FUNCTION: {ask.API_NAME: TOOL_NAME, ask.API_ARGUMENTS: CALL_AS_JSON}},
            {ask.API_ID: SECOND_ID, ask.API_TYPE: ask.TYPE_FUNCTION, ask.API_FUNCTION: {ask.API_NAME: OTHER_TOOL, ask.API_ARGUMENTS: "{}"}},
        ]})
        self.assertEqual(ask.tool_calls_of(message), reply.calls)


class ChatReplyTest(NoRealServer):
    TOOLS = [search_tool.tool_schema([NORM])]

    def test_the_tools_are_sent_only_when_there_are_some(self):
        api = self.serve(choice(ANSWER), choice(ANSWER), choice(ANSWER))
        ask.chat_reply(api.url, CHAT_MODEL, MESSAGES, tools=self.TOOLS)
        ask.chat_reply(api.url, CHAT_MODEL, MESSAGES)
        ask.chat_reply(api.url, CHAT_MODEL, MESSAGES, tools=[])
        with_tools, without, with_none = api.seen
        self.assertEqual((with_tools[ask.API_TOOLS], with_tools[ask.API_MESSAGES], with_tools[API_MODEL]), (self.TOOLS, MESSAGES, CHAT_MODEL))
        self.assertNotIn(ask.API_TOOLS, without)
        self.assertNotIn(ask.API_TOOLS, with_none)

    def test_complete_is_the_round_without_tools(self):
        api = self.serve(choice(ANSWER))
        self.assertEqual(ask.complete(api.url, CHAT_MODEL, MESSAGES), (ANSWER, False))
        self.assertNotIn(ask.API_TOOLS, api.seen[0])

    def test_the_calls_of_a_reply_are_read(self):
        api = self.serve(choice(calls=[call_sent(CALL_AS_JSON, FIRST_ID), call_sent({ARG_ARTICLE: WALL_NUMBER}, SECOND_ID)]))
        reply = ask.chat_reply(api.url, CHAT_MODEL, MESSAGES, tools=self.TOOLS)
        calls = [ask.ToolCall(FIRST_ID, TOOL_NAME, CALL_AS_JSON), ask.ToolCall(SECOND_ID, TOOL_NAME, json.dumps({ARG_ARTICLE: WALL_NUMBER}))]
        self.assertEqual(reply, ask.Reply("", calls))

    def test_a_call_without_an_id_gets_one_and_arguments_sent_as_an_object_become_json_text(self):
        sent = call_sent({})
        sent[ask.API_FUNCTION][ask.API_ARGUMENTS] = {ARG_QUERY: ABOUT_WALLS}
        api = self.serve(choice(calls=[sent]))
        call = ask.chat_reply(api.url, CHAT_MODEL, MESSAGES, tools=self.TOOLS).calls[0]
        self.assertEqual((call.id, json.loads(call.arguments)), (ask.CALL_ID.format(number=1), {ARG_QUERY: ABOUT_WALLS}))

    def test_a_reply_that_makes_a_call_needs_no_text(self):
        without_content = choice(calls=[call_sent({})])
        del without_content[ask.API_MESSAGE][ask.API_CONTENT]
        silent = [*(choice(text, [call_sent({})]) for text in ("", None, " \n", "<think>Caut în norme.")), without_content]
        api = self.serve(*silent)
        for sent in silent:
            reply = ask.chat_reply(api.url, CHAT_MODEL, MESSAGES, tools=self.TOOLS)
            self.assertEqual((reply.text, len(reply.calls)), ("", 1), sent)

    def test_what_the_model_says_beside_its_calls_comes_without_its_thinking(self):
        api = self.serve(choice("<think>Ce caut?</think>\nCaut în norme.", [call_sent({})]))
        reply = ask.chat_reply(api.url, CHAT_MODEL, MESSAGES, tools=self.TOOLS)
        self.assertEqual((reply.text, len(reply.calls)), ("Caut în norme.", 1))

    def test_neither_an_answer_nor_a_call_is_an_error(self):
        without_content = choice()
        del without_content[ask.API_MESSAGE][ask.API_CONTENT]
        empty = [choice(""), choice(None), choice("", []), choice("<think>Mă gândesc"), choice("", ["text", {ask.API_FUNCTION: {}}]), without_content]
        api = self.serve(*empty)
        for sent in empty:
            with self.assertRaisesRegex(ConversionError, f"{CHAT_MODEL} gave no answer$", msg=sent):
                ask.chat_reply(api.url, CHAT_MODEL, MESSAGES, tools=self.TOOLS)

    def test_a_reply_that_ran_out_of_room_says_so(self):
        cut = [choice(ANSWER, finish=ask.FINISH_LENGTH), choice("", [call_sent({})], ask.FINISH_LENGTH), choice("<think>Mă gândesc", finish=ask.FINISH_LENGTH)]
        api = self.serve(*cut)
        self.assertEqual(ask.chat_reply(api.url, CHAT_MODEL, MESSAGES, tools=self.TOOLS), ask.Reply(ANSWER, [], True))
        self.assertTrue(ask.chat_reply(api.url, CHAT_MODEL, MESSAGES, tools=self.TOOLS).cut)
        with self.assertRaises(ConversionError) as caught:
            ask.chat_reply(api.url, CHAT_MODEL, MESSAGES, tools=self.TOOLS)
        self.assertEqual(str(caught.exception), f"{CHAT_MODEL} gave no answer{ask.NO_ROOM}")

    def test_a_refusal_of_the_tools_points_to_the_single_search(self):
        api = self.serve()
        with self.assertRaisesRegex(ConversionError, NO_TOOLS_TAKEN) as with_tools:
            ask.chat_reply(api.url, CHAT_MODEL, MESSAGES, tools=self.TOOLS)
        with self.assertRaisesRegex(ConversionError, NOTHING_TO_SAY) as without:
            ask.chat_reply(api.url, CHAT_MODEL, MESSAGES)
        self.assertTrue(str(with_tools.exception).endswith(ask.NO_TOOLS_HINT), with_tools.exception)
        self.assertIn("single search", ask.NO_TOOLS_HINT)
        self.assertNotIn(ask.NO_TOOLS_HINT, str(without.exception))

    def test_a_key_the_server_echoes_in_its_error_is_hidden_with_tools_too(self):
        api = self.serve()
        with self.assertRaises(ConversionError) as caught:
            ask.chat_reply(api.url, CHAT_MODEL, MESSAGES, ECHO_PATH, KEY, self.TOOLS)
        message = str(caught.exception)
        self.assertIn(f"bad key {ask.BEARER}{ask.KEY_HIDDEN}", message)
        self.assertNotIn(KEY, message)
        self.assertNotIn(ask.NO_TOOLS_HINT, message)  # a refusal that is not about the tools: the single search would not help


class AnswerWithToolsTest(NoRealServer):
    def setUp(self):
        super().setUp()
        self.session = new_session()
        self.messages = ask.tool_messages(QUESTION, RULES)

    def answer(self, *replies: ask.Reply, **options) -> ask.Reply | None:
        """What answer_with_tools gives for a model that sends `replies`, one each time it is asked; the model is kept
        as `model`."""
        self.model = Scripted(*replies)
        return ask.answer_with_tools(self.session, self.messages, self.model, **options)

    def results(self) -> list[dict]:
        """The messages that carry what the calls gave, in order."""
        return [message for message in self.messages if message[ask.API_ROLE] == ask.ROLE_TOOL]

    def test_the_reply_after_a_search_is_the_answer(self):
        final = answering()
        self.assertIs(self.answer(searching_for(ABOUT_WALLS), final), final)
        self.assertEqual(self.session.sources, [(1.0, WALL)])

    def test_a_call_is_sent_back_to_the_model_and_after_it_what_it_gave(self):
        searching = searching_for(ABOUT_WALLS)
        self.answer(searching, answering())
        call = searching.calls[0]
        self.assertEqual(self.messages[:2], ask.tool_messages(QUESTION, RULES))
        self.assertEqual(self.messages[2:], [
            {ask.API_ROLE: ask.ROLE_ASSISTANT, ask.API_CONTENT: "", ask.API_TOOL_CALLS: [
                {ask.API_ID: call.id, ask.API_TYPE: ask.TYPE_FUNCTION, ask.API_FUNCTION: {ask.API_NAME: TOOL_NAME, ask.API_ARGUMENTS: call.arguments}},
            ]},
            result_of(call.id, call.arguments),
        ])

    def test_the_model_is_sent_the_tool_every_time_with_everything_said_so_far(self):
        self.answer(searching_for(ABOUT_WALLS), searching_for(ABOUT_DOORS), answering())
        self.assertEqual([roles(messages) for messages, _ in self.model.sent], [roles(self.messages[:2]), roles(self.messages[:4]), roles(self.messages)])
        self.assertEqual(self.model.sent[-1][0], self.messages)
        for _, tools in self.model.sent:
            self.assertEqual(tools, [self.session.schema()])

    def test_the_searches_of_several_rounds_add_up(self):
        self.answer(searching_for(ABOUT_WALLS), searching_for(ABOUT_DOORS), answering())
        self.assertEqual(roles(self.messages[2:]), [ask.ROLE_ASSISTANT, ask.ROLE_TOOL] * 2)
        self.assertEqual(chunks_of(self.session.sources), [WALL, DOOR])
        self.assertIn(f"[2] {citation(DOOR)}", self.results()[1][ask.API_CONTENT])

    def test_calls_made_in_one_reply_are_each_answered_in_their_order(self):
        searching = searching_for(ABOUT_WALLS, ABOUT_DOORS)
        final = answering()
        self.assertIs(self.answer(searching, final), final)
        said, *results = self.messages[2:]
        self.assertEqual([call[ask.API_ID] for call in said[ask.API_TOOL_CALLS]], [call.id for call in searching.calls])
        self.assertEqual([(result[ask.API_ROLE], result[ask.API_TOOL_CALL_ID]) for result in results], [(ask.ROLE_TOOL, call.id) for call in searching.calls])
        self.assertIn(f"[1] {citation(WALL)}", results[0][ask.API_CONTENT])
        self.assertIn(f"[2] {citation(DOOR)}", results[1][ask.API_CONTENT])
        self.assertEqual((chunks_of(self.session.sources), len(self.model.sent)), ([WALL, DOOR], 2))

    def test_searched_is_told_of_every_call_after_it_was_answered(self):
        told = []
        self.answer(
            calling({ARG_QUERY: ABOUT_WALLS}, {}), searching_for(ABOUT_DOORS), answering(),
            searched=lambda search: told.append((search, list(search.numbers), self.messages[-1][ask.API_TOOL_CALL_ID])),
        )
        self.assertEqual([search for search, _, _ in told], self.session.searches)
        self.assertEqual([numbers for _, numbers, _ in told], [[1], [], [2]])
        self.assertEqual([call_id for _, _, call_id in told], [result[ask.API_TOOL_CALL_ID] for result in self.results()])
        self.assertEqual(told[1][0].problem, search_tool.NOTHING_ASKED)

    def test_a_call_that_cannot_be_run_is_answered_with_the_reason_and_the_model_goes_on(self):
        final = answering()
        self.assertIs(self.answer(calling({ARG_QUERY: ABOUT_WALLS}, name=OTHER_TOOL), calling("not an object"), searching_for(ABOUT_WALLS), final), final)
        problems = [search_tool.NO_SUCH_TOOL.format(name=OTHER_TOOL), search_tool.BAD_ARGUMENTS]
        not_run = [search_tool.NOT_RUN.format(number=number, problem=problem) for number, problem in enumerate(problems, start=1)]
        self.assertEqual([result[ask.API_CONTENT] for result in self.results()[:2]], not_run)
        self.assertEqual(chunks_of(self.session.sources), [WALL])

    def test_a_model_that_never_searched_is_not_taken_at_its_word(self):
        self.assertIsNone(self.answer(answering(ANSWER_WITHOUT_SEARCHING)))
        self.assertEqual((len(self.model.sent), self.messages, self.session.searches), (1, ask.tool_messages(QUESTION, RULES), []))

    def test_a_call_written_as_text_is_not_an_answer_before_a_search_or_after_one(self):
        for written in WRITTEN_CALLS:
            self.assertRegex(written, ask.WRITTEN_CALL)
            self.session, self.messages = new_session(), ask.tool_messages(QUESTION, RULES)
            self.assertIsNone(self.answer(answering(written)), written)
            self.assertIsNone(self.answer(searching_for(ABOUT_WALLS), answering(written)), written)

    def test_an_answer_that_only_speaks_of_braces_or_names_the_tool_is_an_answer(self):
        for text in (ANSWER, f"Am căutat cu {TOOL_NAME}: {ANSWER}", '{EI 180} este "clasa" [1]'):
            self.session, self.messages = new_session(), ask.tool_messages(QUESTION, RULES)
            self.assertEqual(self.answer(searching_for(ABOUT_WALLS), answering(text)), answering(text), text)

    def test_as_many_calls_as_the_limit_are_run_and_the_last_is_told_it_is_the_last(self):
        final = answering()
        self.assertIs(self.answer(*[searching_for(ABOUT_WALLS)] * MAX_SEARCHES, final), final)
        results = [result[ask.API_CONTENT] for result in self.results()]
        self.assertEqual(len(results), MAX_SEARCHES)
        self.assertEqual([result.endswith(search_tool.LAST_SEARCH) for result in results], [False] * (MAX_SEARCHES - 1) + [True])
        self.assertEqual((len(self.session.searches), self.session.refused), (MAX_SEARCHES, False))

    def test_a_call_over_the_limit_is_refused_and_a_model_that_answers_then_is_taken_at_its_word(self):
        self.session = new_session(max_searches=SHORT_LIMIT)
        final = answering()
        self.assertIs(self.answer(searching_for(ABOUT_WALLS, ABOUT_DOORS, ABOUT_WALLS), final), final)
        refusal = search_tool.NOT_RUN.format(number=SHORT_LIMIT + 1, problem=search_tool.LIMIT_REACHED.format(limit=SHORT_LIMIT))
        self.assertEqual(self.results()[-1][ask.API_CONTENT], refusal)
        self.assertEqual((chunks_of(self.session.sources), len(self.results())), ([WALL, DOOR], SHORT_LIMIT + 1))
        self.assertIsNone(self.model.sent[-1][1])  # told the limit, it was asked for its answer without the tool

    def test_of_the_calls_of_one_reply_only_one_past_the_limit_is_answered(self):
        self.session = new_session(max_searches=SHORT_LIMIT)
        final = answering()
        self.assertIs(self.answer(searching_for(*[ABOUT_WALLS] * (SHORT_LIMIT + 5)), final), final)
        self.assertEqual((len(self.session.searches), len(self.results())), (SHORT_LIMIT + 1, SHORT_LIMIT + 1))
        self.assertEqual(len(self.messages[-SHORT_LIMIT - 2][ask.API_TOOL_CALLS]), SHORT_LIMIT + 1)  # every call sent back has its result

    def test_arguments_that_are_not_a_json_object_are_sent_back_as_an_empty_one(self):
        for written in ("{not json", "[1, 2]", "[" * 5000):
            reply = ask.Reply("", [ask.ToolCall(FIRST_ID, TOOL_NAME, written)])
            self.assertEqual(ask.calls_message(reply)[ask.API_TOOL_CALLS][0][ask.API_FUNCTION][ask.API_ARGUMENTS], ask.NO_ARGUMENTS, written[:20])

    def test_a_model_that_calls_again_after_it_was_told_the_limit_is_stopped(self):
        self.session = new_session(max_searches=SHORT_LIMIT)
        replies = [searching_for(ABOUT_WALLS), searching_for(ABOUT_DOORS), searching_for(ABOUT_WALLS), searching_for(ABOUT_DOORS), answering()]
        self.assertIsNone(self.answer(*replies))
        self.assertEqual(self.model.replies, [answering()])  # it was not asked again
        self.assertEqual([tools is None for _, tools in self.model.sent], [False, False, False, True])  # its last chance was without the tool
        self.assertEqual((len(self.session.searches), len(self.results())), (SHORT_LIMIT + 1, SHORT_LIMIT + 1))  # its last call was neither run nor answered
        self.assertIn(search_tool.LIMIT_REACHED.format(limit=SHORT_LIMIT), self.messages[-1][ask.API_CONTENT])

    def test_a_model_told_the_limit_for_a_call_of_another_tool_is_stopped_at_its_next_call_too(self):
        self.session = new_session(max_searches=1)
        self.assertIsNone(self.answer(searching_for(ABOUT_WALLS), calling({}, name=OTHER_TOOL), searching_for(ABOUT_DOORS)))
        self.assertIn(search_tool.LIMIT_REACHED.format(limit=1), self.messages[-1][ask.API_CONTENT])

    def test_a_model_whose_only_call_was_not_run_is_not_taken_at_its_word(self):
        self.assertIsNone(self.answer(calling({ARG_ARTICLE: UNKNOWN_NUMBER}), answering(ANSWER_WITHOUT_SEARCHING)))
        self.assertEqual(self.session.sources, [])  # it was given nothing to answer from

    def test_an_embedding_server_that_cannot_be_used_is_an_error(self):
        self.encode.side_effect = ConversionError(NO_ANSWER)
        with self.assertRaisesRegex(ConversionError, NO_ANSWER):
            self.answer(searching_for(ABOUT_WALLS), answering())

    def test_what_asking_the_model_raises_is_raised(self):
        with self.assertRaisesRegex(ConversionError, NOTHING_TO_SAY):
            ask.answer_with_tools(self.session, self.messages, mock.Mock(side_effect=ConversionError(NOTHING_TO_SAY)))


class AskTest(NoRealServer):
    def asked(self, *choices: dict, **options) -> ask.Answer:
        """What ask() gives for the question, with a chat server that answers with `choices` (kept as `api`);
        `options` as ask() takes them."""
        self.api = self.serve(*choices)
        return ask.ask(QUESTION, INDEX, CONFIG, EMBEDDING_SERVER, CHAT_MODEL, TOP, chat_server=self.api.url, **{"instructions": RULES, **options})

    def assert_handed_the_chunks(self, answer: ask.Answer, request: dict) -> None:
        """`answer` was written from the chunks closest to the question's own words, which `request` handed the model
        without the tool."""
        self.assertEqual((answer.text, answer.sources, answer.searches), (ANSWER, [(1.0, STAIR)], []))
        self.assertNotIn(ask.API_TOOLS, request)
        self.assertEqual(request[ask.API_MESSAGES], ask.build_messages(QUESTION, answer.sources, CONFIG, RULES))

    def test_the_answer_comes_with_the_searches_and_all_the_chunks_they_gave(self):
        answer = self.asked(SEARCHING, choice(calls=[call_sent({ARG_ARTICLE: ARTICLE_PREFIX + DOOR_NUMBER}, SECOND_ID)]), choice(ANSWER))
        self.assertEqual((answer.text, answer.model, answer.cut, answer.fell_back), (ANSWER, CHAT_MODEL, False, False))
        self.assertEqual(answer.sources, [(1.0, WALL), (None, DOOR)])
        self.assertEqual(answer.searches, [Search(1, query=ABOUT_WALLS, numbers=[1], new=1), Search(2, article=DOOR_NUMBER, numbers=[2], new=1)])

    def test_the_model_is_sent_the_tool_its_instructions_and_the_question_as_it_was_asked(self):
        self.asked(SEARCHING, choice(ANSWER))
        first, second = self.api.seen
        self.assertEqual(first[ask.API_MESSAGES], ask.tool_messages(QUESTION, ask.tool_instructions(RULES)))
        self.assertEqual(roles(second[ask.API_MESSAGES]), [ask.ROLE_SYSTEM, ask.ROLE_USER, ask.ROLE_ASSISTANT, ask.ROLE_TOOL])
        self.assertEqual(second[ask.API_MESSAGES][-1], result_of(FIRST_ID, {ARG_QUERY: ABOUT_WALLS}))
        for request in self.api.seen:
            self.assertEqual((request[ask.API_TOOLS], request[API_MODEL]), ([new_session().schema()], CHAT_MODEL))

    def test_with_a_norm_only_that_norm_is_offered_and_one_no_chunk_has_is_an_error(self):
        self.asked(SEARCHING, choice(ANSWER), doc=NORM.lower())
        self.assertEqual(self.api.seen[0][ask.API_TOOLS], [search_tool.tool_schema([NORM], TOP)])
        with self.assertRaisesRegex(ConversionError, UNKNOWN_NORM):
            self.asked(choice(ANSWER), doc=UNKNOWN_NORM)
        self.assertEqual(self.api.seen, [])

    def test_the_earlier_exchange_comes_before_the_question(self):
        self.asked(SEARCHING, choice(ANSWER), history=ask.remembered("q1", "a1"))
        self.assertEqual(self.api.seen[0][ask.API_MESSAGES], ask.tool_messages(QUESTION, ask.tool_instructions(RULES), ask.remembered("q1", "a1")))
        self.assertEqual(roles(self.api.seen[0][ask.API_MESSAGES]), [ask.ROLE_SYSTEM, ask.ROLE_USER, ask.ROLE_ASSISTANT, ask.ROLE_USER])

    def test_without_instructions_the_rules_of_the_default_file_follow_the_tool_text(self):
        self.asked(SEARCHING, choice(ANSWER), instructions=None)
        self.assertEqual(self.api.seen[0][ask.API_MESSAGES][0][ask.API_CONTENT], ask.tool_instructions(ask.load_instructions()))

    def test_searched_is_told_of_every_search_of_the_model(self):
        told = []
        answer = self.asked(choice(calls=[call_sent({ARG_QUERY: ABOUT_WALLS}, FIRST_ID), call_sent({}, SECOND_ID)]), choice(ANSWER), searched=told.append)
        self.assertEqual((len(told), told), (2, answer.searches))

    def test_an_answer_that_was_cut_short_says_so(self):
        self.assertTrue(self.asked(SEARCHING, choice(ANSWER, finish=ask.FINISH_LENGTH)).cut)

    def test_a_model_that_answers_without_searching_is_handed_the_chunks_closest_to_the_question(self):
        answer = self.asked(choice(ANSWER_WITHOUT_SEARCHING), choice(ANSWER))
        self.assertTrue(answer.fell_back)
        self.assertIn(ask.API_TOOLS, self.api.seen[0])
        self.assert_handed_the_chunks(answer, self.api.seen[1])

    def test_a_model_that_writes_its_call_as_text_is_handed_the_chunks(self):
        answer = self.asked(SEARCHING, choice(WRITTEN_CALLS[0]), choice(ANSWER))
        self.assertTrue(answer.fell_back)
        self.assert_handed_the_chunks(answer, self.api.seen[-1])

    def test_a_model_that_does_not_stop_searching_is_handed_the_chunks(self):
        answer = self.asked(*[SEARCHING] * (MAX_SEARCHES + 2), choice(ANSWER))
        self.assertTrue(answer.fell_back)
        self.assertEqual(len(self.api.seen), MAX_SEARCHES + 3)
        self.assert_handed_the_chunks(answer, self.api.seen[-1])

    def test_with_single_search_the_model_is_never_offered_the_tool(self):
        answer = self.asked(choice(ANSWER), single_search=True)
        self.assertFalse(answer.fell_back)
        self.assertEqual(len(self.api.seen), 1)
        self.assert_handed_the_chunks(answer, self.api.seen[0])

    def test_an_embedding_server_that_cannot_be_used_is_an_error_with_the_single_search_too(self):
        self.encode.side_effect = ConversionError(NO_ANSWER)  # an error for the caller, not the end of the program
        with self.assertRaisesRegex(ConversionError, NO_ANSWER):
            self.asked(choice(ANSWER), single_search=True)
        self.assertEqual(self.api.seen, [])

    def test_a_chat_server_that_refuses_the_tool_is_an_error_that_points_to_the_single_search(self):
        with self.assertRaisesRegex(ConversionError, re.escape(ask.NO_TOOLS_HINT)):
            self.asked()
        self.assertEqual(len(self.api.seen), 1)

    def test_an_embedding_server_that_cannot_be_used_is_an_error_when_the_model_searches(self):
        self.encode.side_effect = ConversionError(NO_ANSWER)
        with self.assertRaisesRegex(ConversionError, NO_ANSWER):
            self.asked(SEARCHING, choice(ANSWER))


class ToolInstructionsTest(unittest.TestCase):
    def written(self, text: str) -> Path:
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "tool.md"
        path.write_text(text, encoding=ask.TEXT_ENCODING)
        return path

    def test_the_default_file_uses_every_token_of_the_tool(self):
        raw = ask.TOOL_INSTRUCTIONS_FILE.read_text(encoding=ask.TEXT_ENCODING)
        for token in ask.TOOL_TOKENS:
            self.assertIn(token, raw, token)
        self.assertLessEqual(set(TOKEN.findall(raw)), set(ask.TOOL_TOKENS) | set(ask.INSTRUCTION_TOKENS))  # and none that ask.py does not know

    def test_every_token_is_replaced_by_its_value_and_none_is_left(self):
        for rules in (RULES, ask.load_instructions()):
            text = ask.tool_instructions(rules)
            self.assertEqual(TOKEN.findall(text), [])
            for token, value in ask.TOOL_TOKENS.items():
                self.assertIn(value, text, token)

    def test_the_model_is_told_the_tool_its_parameters_and_its_limit_as_the_tool_has_them(self):
        text = ask.tool_instructions(RULES)
        parameters = [f"`{name}`" for name in (ARG_QUERY, ARG_NORM, ARG_ARTICLE)]
        for words in (f"calling the tool {TOOL_NAME}.", *parameters, f"at most {MAX_SEARCHES} searches for one question"):
            self.assertIn(words, text)
        self.assertEqual(ask.TOOL_TOKENS["{max_searches}"], str(search_tool.MAX_SEARCHES))

    def test_the_rules_follow_what_is_said_of_the_tool(self):
        about_the_tool = ask.load_instructions(ask.TOOL_INSTRUCTIONS_FILE, {**ask.INSTRUCTION_TOKENS, **ask.TOOL_TOKENS})
        self.assertEqual(ask.tool_instructions(RULES), about_the_tool + EXCERPT_SEPARATOR + RULES)
        self.assertNotIn(RULES, about_the_tool)

    def test_another_file_may_use_the_tokens_of_the_tool_and_those_of_the_rules(self):
        path = self.written('Call {tool_name} with {arg_query}, {arg_norm} or {arg_article}, {max_searches} times; read "{status_label}"; leave {unknown}.\n')
        said = f'Call {TOOL_NAME} with {ARG_QUERY}, {ARG_NORM} or {ARG_ARTICLE}, {MAX_SEARCHES} times; read "{STATUS_LABEL}"; leave {{unknown}}.'
        self.assertEqual(ask.tool_instructions(RULES, path), said + EXCERPT_SEPARATOR + RULES)

    def test_the_tokens_of_the_tool_are_left_alone_in_the_rules_file(self):
        self.assertEqual(set(ask.TOOL_TOKENS) & set(ask.INSTRUCTION_TOKENS), set())
        self.assertEqual(ask.load_instructions(self.written("Call {tool_name}.")), "Call {tool_name}.")

    def test_a_missing_or_an_empty_file_is_an_error_that_names_it(self):
        with self.assertRaisesRegex(ConversionError, "nope.md"):
            ask.tool_instructions(RULES, Path("nope.md"))
        with self.assertRaisesRegex(ConversionError, "tool.md is empty"):
            ask.tool_instructions(RULES, self.written(" \n"))


class ToolMessagesTest(unittest.TestCase):
    def test_the_model_is_sent_the_instructions_and_the_question_as_it_was_asked(self):
        self.assertEqual(ask.tool_messages(QUESTION, RULES), MESSAGES)
        self.assertEqual(ask.tool_messages(QUESTION, RULES, []), MESSAGES)

    def test_the_newest_of_the_earlier_exchange_comes_between_them(self):
        history = [message for turn in range(ask.MAX_HISTORY_MESSAGES) for message in ask.remembered(f"q{turn}", f"a{turn}")]
        messages = ask.tool_messages(QUESTION, RULES, history)
        self.assertEqual((messages[0], messages[-1]), tuple(MESSAGES))
        self.assertEqual(messages[1:-1], history[-ask.MAX_HISTORY_MESSAGES:])
        self.assertEqual(messages, ask.framed(RULES, history, QUESTION))


class SearchLineTest(NoRealServer):
    def test_a_search_is_shown_with_what_it_looked_for_and_how_much_it_found(self):
        for search, line in (
            (Search(1, query=ABOUT_WALLS, numbers=[1, 2, 3], new=3), f'search 1: "{ABOUT_WALLS}": 3 chunks, 3 new'),
            (Search(2, query=ABOUT_DOORS, norm=NORM, numbers=[2, 4], new=1), f'search 2: "{ABOUT_DOORS}" in {NORM}: 2 chunks, 1 new'),
            (Search(3, article=WALL_NUMBER, numbers=[5], new=0), f"search 3: number {WALL_NUMBER}: 1 chunks, 0 new"),
            (Search(4, article=WALL_NUMBER, norm=NORM, numbers=[5, 6], new=2), f"search 4: number {WALL_NUMBER} in {NORM}: 2 chunks, 2 new"),
        ):
            self.assertEqual(ask.search_line(search), line)

    def test_a_search_that_was_not_run_is_shown_with_the_reason(self):
        for search, line in (
            (Search(5, article=UNKNOWN_NUMBER, norm=NORM, problem=NO_ANSWER), f"search 5: number {UNKNOWN_NUMBER} in {NORM}: not run: {NO_ANSWER}"),
            (Search(6, query=ABOUT_WALLS, problem=search_tool.BAD_COUNT), f'search 6: "{ABOUT_WALLS}": not run: {search_tool.BAD_COUNT}'),
        ):
            self.assertEqual(ask.search_line(search), line)

    def test_the_searches_of_a_session_are_shown_as_they_were_run(self):
        session = new_session()
        for arguments in ({ARG_QUERY: ABOUT_WALLS}, {ARG_ARTICLE: ARTICLE_PREFIX + WALL_NUMBER, ARG_NORM: NORM.lower()}, {ARG_ARTICLE: UNKNOWN_NUMBER}):
            session.reply(arguments)
        self.assertEqual([ask.search_line(search) for search in session.searches], [
            f'search 1: "{ABOUT_WALLS}": 1 chunks, 1 new', f"search 2: number {WALL_NUMBER} in {NORM}: 1 chunks, 0 new",
            f"search 3: number {UNKNOWN_NUMBER}: not run: " + search_tool.NO_SUCH_NUMBER.format(article=UNKNOWN_NUMBER, where=""),
        ])


class CommandLineTest(NoRealServer):
    """What ask.py prints, run over the chunks of this file: with --show-prompt what the model would be sent, and else
    the answer of ask(), which is replaced here, so that no chat server is asked."""

    def run_main(self, *options: str, answer: ask.Answer | None = None) -> tuple[str, dict]:
        """What `ask.py` prints for the question with `options`, and what it gave ask(), by the names of ask()'s
        parameters (nothing with --show-prompt); ask() gives `answer`."""
        arguments = [ask.__file__, "--chunks-dir", "chunks", "--vectors-dir", "vectors", "--top", str(TOP), *options, QUESTION]
        parameters = inspect.signature(ask.ask)
        replaced = {
            "load_config": mock.Mock(return_value=CONFIG), "load_chunks": mock.Mock(return_value={}), "load_index": mock.Mock(return_value=INDEX),
            "ask": mock.create_autospec(ask.ask, return_value=answer),
        }
        out = io.StringIO()
        with mock.patch.object(sys, "argv", arguments), mock.patch.multiple(ask, **replaced), contextlib.redirect_stdout(out):
            ask.main()
        given = replaced["ask"].call_args
        return out.getvalue(), parameters.bind(*given.args, **given.kwargs).arguments if given else {}

    def shown(self, *options: str) -> str:
        """What `ask.py --show-prompt` prints for the question with `options`."""
        return self.run_main("--show-prompt", *options)[0]

    def test_a_model_that_searches_is_shown_its_instructions_the_question_and_the_tool(self):
        shown = self.shown()
        self.assertTrue(shown.startswith(prompt_part(ask.ROLE_SYSTEM, ask.tool_instructions(ask.load_instructions())) + EXCERPT_SEPARATOR), shown)
        self.assertIn(EXCERPT_SEPARATOR + prompt_part(ask.ROLE_USER, QUESTION) + EXCERPT_SEPARATOR + prompt_part(ask.PROMPT_TOOLS), shown)
        self.assertEqual(json.loads(shown.split(prompt_part(ask.PROMPT_TOOLS))[1]), new_session().schema())
        self.encode.assert_not_called()

    def test_with_a_norm_the_tool_that_is_shown_offers_only_that_norm(self):
        self.assertEqual(json.loads(self.shown("--doc", NORM.lower()).split(prompt_part(ask.PROMPT_TOOLS))[1]), search_tool.tool_schema([NORM], TOP))

    def test_with_single_search_the_chunks_are_shown_and_no_tool(self):
        messages = ask.build_messages(QUESTION, [(1.0, STAIR)], CONFIG, ask.load_instructions())
        parts = [prompt_part(message[ask.API_ROLE], message[ask.API_CONTENT]) for message in messages]
        self.assertEqual(self.shown("--single-search"), EXCERPT_SEPARATOR.join(parts) + LINE_SEPARATOR)

    def test_the_answer_is_printed_with_the_searches_and_the_chunks_they_gave(self):
        searches = [Search(1, query=ABOUT_WALLS, numbers=[1], new=1), Search(2, article=DOOR_NUMBER, numbers=[2], new=1)]
        printed, _ = self.run_main(answer=ask.Answer(ANSWER, [(1.0, WALL), (None, DOOR)], CHAT_MODEL, False, searches))
        lines = LINE_SEPARATOR.join(ask.search_line(search) for search in searches)
        self.assertTrue(printed.startswith(EXCERPT_SEPARATOR.join((ANSWER, lines, WRITTEN_BY, f"1. {score_note(1.0)}  {WALL['id']}"))), printed)
        self.assertIn(f"{EXCERPT_SEPARATOR}2. {score_note(None)}  {DOOR['id']}", printed)
        self.assertNotIn(ask.FELL_BACK_NOTE, printed)

    def test_an_answer_written_from_the_chunks_the_model_was_handed_after_all_says_so(self):
        printed, _ = self.run_main(answer=ask.Answer(ANSWER, [(1.0, STAIR)], CHAT_MODEL, False, fell_back=True))
        self.assertTrue(printed.startswith(EXCERPT_SEPARATOR.join((ANSWER, ask.FELL_BACK_NOTE, WRITTEN_BY, ""))), printed)

    def test_single_search_is_passed_on_to_ask_only_when_it_is_given(self):
        answer = ask.Answer(ANSWER, [(1.0, STAIR)], CHAT_MODEL, False)
        for options, single in (((), False), (("--single-search",), True)):
            _, given = self.run_main(*options, answer=answer)
            self.assertEqual((given["question"], given["top"], given["single_search"]), (QUESTION, TOP, single))

    def test_every_search_is_printed_as_it_is_run_apart_from_the_answer(self):
        _, given = self.run_main(answer=ask.Answer(ANSWER, [(1.0, WALL)], CHAT_MODEL, False))
        search = Search(1, query=ABOUT_WALLS, numbers=[1], new=1)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            given["searched"](search)
        self.assertEqual(errors.getvalue(), ask.search_line(search) + LINE_SEPARATOR)


if __name__ == "__main__":
    unittest.main()
