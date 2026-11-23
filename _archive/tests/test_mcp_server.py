"""Tests of the search as an MCP server (mcp_server.py).

No embedding server and no model is used: the index is the handful of chunks of test_search_tool.py, and the embedding
call is replaced as there. The MCP side is real: the tool is listed and called by the mcp package's own client, in the
same process and over HTTP on a port of this machine, with the key and without it.
Run from the project folder: venvs/bin/python -m unittest discover -s tests -v
"""

import asyncio
import contextlib
import io
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import httpx2
import uvicorn
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client

import ask
import mcp_server
import search
import search_tool
import settings
from pipeline_common import ConversionError
from search_tool import ARG_ARTICLE, ARG_COUNT, ARG_NORM, ARG_QUERY, EXCERPT_SEPARATOR, MAX_SEARCHES, TOOL_NAME
from test_search_tool import (
    ABOUT_DOORS, ABOUT_WALLS, CONFIG, DOOR, INDEX, MODEL, NO_ANSWER, NORM, OTHER_NORM, OTHER_TOOL, POINT, POINT_NUMBER, QUESTIONS, SERVER,
    UNKNOWN_NUMBER, WALL, WALL_NUMBER, chunk, embedding, for_article, for_query,
)

RULES = "How to search and cite."
KEY = "k" * mcp_server.MIN_KEY_CHARS
OTHER_KEY = "x" * mcp_server.MIN_KEY_CHARS
PUBLIC_HOST = "Norms.Example.com"  # a name the proxy may be reached under, written as someone may type it
OTHER_SITE = "https://other-site.example"
LOCAL_URL = "http://127.0.0.1:{port}" + mcp_server.MCP_PATH
ACCEPT = {"Accept": "application/json, text/event-stream"}  # what an MCP client says it takes
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
AUTO = "auto"  # the client's mode: the newest protocol the server speaks
LEGACY = "legacy"  # the client's mode for the handshake of the protocol versions before 2026
OK = 200
INTERNAL_ADDRESS = "http://10.0.0.5:1234/v1/embeddings"  # what an error inside a search may name
STARTED_POLL_SECONDS = 0.01
STOP_SECONDS = 5


def state(index: search.Index = INDEX, count: int = search.DEFAULT_TOP) -> mcp_server.SearchState:
    return mcp_server.SearchState(index, CONFIG, SERVER, count, RULES)


def bearer(key: str) -> dict[str, str]:
    return {ask.AUTHORIZATION: ask.BEARER + key}


def given(*records: dict) -> list[str]:
    """The excerpts one call is answered with for `records`: numbered from 1, their pictures not shown to the reader."""
    return search_tool.excerpts(list(enumerate(records, start=1)), CONFIG, figures_shown=False)


def searched(what: str, *records: dict) -> str:
    """A whole reply to a call that found `records`."""
    found = search_tool.FOUND_ONE if len(records) == 1 else search_tool.FOUND_MANY.format(count=len(records))
    return EXCERPT_SEPARATOR.join((search_tool.RESULT_HEADER_UNLIMITED.format(what=what, found=found), *given(*records)))


def text_of(result) -> str:
    """The text of a tool's result, which is all it holds."""
    (content,) = result.content
    return content.text


def causes(error: BaseException) -> list[BaseException]:
    """The errors a group of errors holds, however deep; an error that is no group is its own cause."""
    return [cause for inner in error.exceptions for cause in causes(inner)] if isinstance(error, BaseExceptionGroup) else [error]


class ReadKeyTest(unittest.TestCase):
    def test_the_key_is_read_from_the_environment_without_the_space_around_it(self):
        self.assertEqual(mcp_server.read_key({mcp_server.KEY_VARIABLE: f" {KEY}\n"}), KEY)

    def test_no_key_a_short_key_and_a_key_no_header_can_carry_are_refused_with_the_way_to_make_one(self):
        refused = ({}, {mcp_server.KEY_VARIABLE: ""}, {mcp_server.KEY_VARIABLE: KEY[:-1]}, {mcp_server.KEY_VARIABLE: " " * mcp_server.MIN_KEY_CHARS}, {mcp_server.KEY_VARIABLE: "ș" * mcp_server.MIN_KEY_CHARS})
        for environment in refused:
            with self.assertRaisesRegex(ConversionError, f"set {mcp_server.KEY_VARIABLE}.*openssl rand"):
                mcp_server.read_key(environment)

    def test_a_refused_key_is_not_repeated_in_the_message(self):
        with self.assertRaises(ConversionError) as raised:
            mcp_server.read_key({mcp_server.KEY_VARIABLE: "secret-but-short"})
        self.assertNotIn("secret-but-short", str(raised.exception))


class ToolTest(unittest.TestCase):
    def test_the_tool_is_the_one_a_chat_model_is_given_with_the_rules_after_its_description(self):
        tool = mcp_server.tool(state())
        function = search_tool.tool_schema([NORM, OTHER_NORM], search.DEFAULT_TOP, {NORM: "Titlu", OTHER_NORM: "Titlu"})[search_tool.SCHEMA_FUNCTION]
        self.assertEqual(tool.name, TOOL_NAME)
        self.assertEqual(tool.input_schema, function[search_tool.SCHEMA_PARAMETERS])
        self.assertEqual(tool.input_schema["properties"][ARG_NORM]["enum"], [NORM, OTHER_NORM])
        self.assertEqual(tool.description, search_tool.TOOL_DESCRIPTION + EXCERPT_SEPARATOR + RULES)

    def test_the_tool_says_it_changes_nothing(self):
        annotations = mcp_server.tool(state()).annotations
        self.assertTrue(annotations.read_only_hint)
        self.assertFalse(annotations.open_world_hint)

    def test_the_count_a_call_gets_is_the_states(self):
        self.assertIn("default 3", mcp_server.tool(state(count=3)).input_schema["properties"][ARG_COUNT]["description"])


class RulesFileTest(unittest.TestCase):
    """The rules file that is shipped (mcp-instructions.md), as the server reads it."""

    def setUp(self):
        self.rules = mcp_server.load_rules()

    def test_every_token_of_the_file_is_replaced(self):
        raw = mcp_server.RULES_FILE.read_text(encoding=ask.TEXT_ENCODING)
        used = [token for token in ask.SEARCH_TOKENS if token in raw]
        self.assertTrue(set(used) & set(ask.TOOL_TOKENS))  # the file names the tool's arguments by their tokens
        self.assertFalse([token for token in used if token in self.rules])
        self.assertNotRegex(self.rules, r"\{[a-z_]+\}")  # nor is a token left that nothing replaces

    def test_it_names_the_arguments_and_the_words_of_the_excerpts(self):
        for words in (f"`{ARG_QUERY}`", f"`{ARG_NORM}`", f"`{ARG_ARTICLE}`", search.STATUS_LABEL, search.FIGURES_LABEL, search.AMENDED_MARK, *search.STATUS_WORDS.values()):
            self.assertIn(words, self.rules)

    def test_it_does_not_speak_of_a_limit_or_of_numbers_that_run_on(self):
        self.assertNotIn(str(MAX_SEARCHES) + " searches", self.rules)
        self.assertNotIn("run on", self.rules)
        self.assertIn("do not cite an excerpt by its number", self.rules)


class LoadStateTest(unittest.TestCase):
    def loaded(self, rules: str) -> mcp_server.SearchState:
        """The state as the command line loads it, with `rules` as the rules file and the index of this file."""
        with TemporaryDirectory() as folder:
            path = Path(folder) / "rules.md"
            path.write_text(rules, encoding=ask.TEXT_ENCODING)
            args = mock.Mock(config=None, chunks_dir=Path(folder), vectors_dir=Path(folder), rules=path, server=SERVER, top=3)
            with mock.patch.object(mcp_server, "load_config", return_value=CONFIG), mock.patch.object(mcp_server, "load_chunks", return_value={}), \
                    mock.patch.object(mcp_server, "load_index", return_value=INDEX):
                return mcp_server.load_state(args)

    def test_the_rules_are_read_with_the_tokens_of_the_tool_and_of_the_excerpts(self):
        loaded = self.loaded("Search with `{arg_query}`; read \"{status_label}\".")
        self.assertEqual(loaded.rules, f'Search with `{ARG_QUERY}`; read "{search.STATUS_LABEL}".')
        self.assertEqual((loaded.index, loaded.embedding_server, loaded.count), (INDEX, SERVER, 3))

    def test_an_empty_rules_file_stops_the_start(self):
        with self.assertRaisesRegex(ConversionError, "is empty"):
            self.loaded("")


class MainTest(unittest.TestCase):
    """The command line, up to where the server would start."""

    def run_main(self, environment: dict[str, str], *options: str) -> mock.Mock:
        """Runs main with `options` in `environment`, without a settings file; the state is not loaded and nothing is
        served. Returns the mock that stands for uvicorn.run."""
        with mock.patch.object(mcp_server.sys, "argv", ["mcp_server.py", *options]), mock.patch.dict(os.environ, {settings.FILE_VARIABLE: "", **environment}, clear=True), \
                mock.patch.object(mcp_server, "load_state", return_value=state()) as self.load_state, \
                mock.patch.object(mcp_server.uvicorn, "run") as served, contextlib.redirect_stdout(io.StringIO()) as self.output:
            mcp_server.main()
        return served

    def test_without_a_key_nothing_is_loaded_and_nothing_served(self):
        with self.assertRaisesRegex(SystemExit, mcp_server.KEY_VARIABLE):
            self.run_main({})
        self.load_state.assert_not_called()

    def test_the_key_of_the_settings_file_is_enough(self):
        with TemporaryDirectory() as folder:
            file = Path(folder) / "settings.env"
            file.write_text(f"{mcp_server.KEY_VARIABLE}={KEY}\n", encoding=settings.TEXT_ENCODING)
            served = self.run_main({settings.FILE_VARIABLE: str(file)})
        self.assertEqual(served.call_args.args[0].key, KEY.encode())

    def test_the_environment_wins_over_the_settings_file(self):
        with TemporaryDirectory() as folder:
            file = Path(folder) / "settings.env"
            file.write_text(f"{mcp_server.KEY_VARIABLE}={OTHER_KEY}\n", encoding=settings.TEXT_ENCODING)
            served = self.run_main({settings.FILE_VARIABLE: str(file), mcp_server.KEY_VARIABLE: KEY})
        self.assertEqual(served.call_args.args[0].key, KEY.encode())

    def test_with_a_key_the_server_listens_on_this_machine_only_behind_the_key(self):
        served = self.run_main({mcp_server.KEY_VARIABLE: KEY}, "--port", "9000")
        (app,), options = served.call_args
        self.assertIsInstance(app, mcp_server.KeyGate)
        self.assertEqual(app.key, KEY.encode())
        self.assertEqual(options, {"host": mcp_server.HOST, "port": 9000})
        self.assertNotIn(KEY, self.output.getvalue())


class CallTest(unittest.IsolatedAsyncioTestCase):
    """The tool called by an MCP client in the same process."""

    async def call(self, arguments: dict, name: str = TOOL_NAME, index: search.Index = INDEX):
        """The result of one call, by a client of its own (a client is opened and closed in the test that uses it)."""
        async with Client(mcp_server.build_server(state(index))) as client:
            return await client.call_tool(name, arguments)

    async def test_the_client_is_told_of_one_tool_and_where_its_rules_are(self):
        async with Client(mcp_server.build_server(state())) as client:
            listed = await client.list_tools()
            self.assertEqual([tool.name for tool in listed.tools], [TOOL_NAME])
            self.assertEqual(listed.tools[0].description, mcp_server.tool(state()).description)
            self.assertEqual(client.instructions, mcp_server.SERVER_INSTRUCTIONS)
            self.assertEqual((client.server_info.name, client.server_info.version), (mcp_server.SERVER_NAME, mcp_server.SERVER_VERSION))

    async def test_an_article_is_fetched_by_its_number_without_the_embedding_server(self):
        with mock.patch("embed_chunks.post_json", autospec=True, side_effect=ConversionError(NO_ANSWER)) as post:
            result = await self.call({ARG_ARTICLE: POINT_NUMBER})
        self.assertFalse(result.is_error)
        self.assertEqual(text_of(result), searched(for_article(POINT_NUMBER), POINT))
        post.assert_not_called()

    async def test_a_search_by_meaning_gives_the_closest_chunks_numbered_from_1(self):
        with embedding(QUESTIONS):
            result = await self.call({ARG_QUERY: ABOUT_WALLS, ARG_COUNT: 2})
        self.assertFalse(result.is_error)
        self.assertEqual(text_of(result), searched(for_query(ABOUT_WALLS), WALL, DOOR))

    async def test_every_call_stands_alone_and_none_is_refused_for_coming_late(self):
        async with Client(mcp_server.build_server(state())) as client:  # one client, as one conversation of Claude is
            with embedding(QUESTIONS):
                first = await client.call_tool(TOOL_NAME, {ARG_QUERY: ABOUT_WALLS, ARG_COUNT: 1})
                replies = [await client.call_tool(TOOL_NAME, {ARG_QUERY: ABOUT_WALLS, ARG_COUNT: 1}) for _ in range(MAX_SEARCHES + 1)]
                other = await client.call_tool(TOOL_NAME, {ARG_QUERY: ABOUT_DOORS, ARG_COUNT: 1})
        self.assertEqual(text_of(first), searched(for_query(ABOUT_WALLS), WALL))
        self.assertEqual({text_of(reply) for reply in replies}, {text_of(first)})  # the chunk found again is given again, as [1]
        self.assertEqual(text_of(other), searched(for_query(ABOUT_DOORS), DOOR))
        self.assertFalse(any(reply.is_error for reply in (first, other, *replies)))

    async def test_the_embedding_server_is_asked_from_another_thread_than_the_one_that_serves(self):
        asked_from = []

        def encode(embedder, texts):
            asked_from.append(threading.current_thread())
            return [QUESTIONS[text] for text in texts]

        with mock.patch.object(search_tool.Embedder, "encode", autospec=True, side_effect=encode):
            result = await self.call({ARG_QUERY: ABOUT_WALLS, ARG_COUNT: 1})
        self.assertFalse(result.is_error)
        self.assertEqual(len(asked_from), 1)
        self.assertIsNot(asked_from[0], threading.current_thread())

    async def test_a_call_that_cannot_be_run_is_an_error_that_says_why(self):
        problem = search_tool.NO_SUCH_NUMBER.format(article=UNKNOWN_NUMBER, where="")
        for arguments, said in (({ARG_ARTICLE: UNKNOWN_NUMBER}, problem), ({}, search_tool.NOTHING_ASKED), ({ARG_QUERY: ABOUT_WALLS, ARG_COUNT: 0}, search_tool.BAD_COUNT)):
            result = await self.call(arguments)
            self.assertTrue(result.is_error)
            self.assertEqual(text_of(result), search_tool.NOT_RUN_UNLIMITED.format(problem=said))

    async def test_another_tool_is_an_error_that_names_the_one_there_is(self):
        result = await self.call({ARG_QUERY: ABOUT_WALLS}, OTHER_TOOL)
        self.assertTrue(result.is_error)
        self.assertIn(search_tool.NO_SUCH_TOOL.format(name=OTHER_TOOL), text_of(result))

    async def test_without_the_embedding_server_a_search_is_an_error_that_keeps_its_address_to_this_server(self):
        output = io.StringIO()
        with mock.patch("embed_chunks.post_json", autospec=True, side_effect=ConversionError(f"{NO_ANSWER} at {SERVER}")), contextlib.redirect_stderr(output):
            result = await self.call({ARG_QUERY: ABOUT_WALLS})
        self.assertTrue(result.is_error)
        self.assertEqual(text_of(result), mcp_server.SEARCH_UNAVAILABLE)
        self.assertNotIn(SERVER, text_of(result))
        self.assertIn(SERVER, output.getvalue())  # the reason is in this server's own output

    async def test_the_pictures_of_an_excerpt_are_said_not_to_be_shown(self):
        pictured = chunk(WALL_NUMBER, images=[{"caption": "Figura 1", "page": 4}])
        result = await self.call({ARG_ARTICLE: WALL_NUMBER}, index=search.Index([pictured], INDEX.vectors[:1, :1], MODEL))
        self.assertIn(search.FIGURES_NOT_SHOWN, text_of(result))
        self.assertNotIn(search.FIGURES_SHOWN, text_of(result))


class HttpTest(unittest.IsolatedAsyncioTestCase):
    """The server over HTTP, as the proxy reaches it: on a free port of this machine, behind the key."""

    async def asyncSetUp(self):
        self.server = uvicorn.Server(uvicorn.Config(mcp_server.http_app(state(), KEY), host=mcp_server.HOST, port=0, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        while not self.server.started:
            await asyncio.sleep(STARTED_POLL_SECONDS)
        self.url = LOCAL_URL.format(port=self.server.servers[0].sockets[0].getsockname()[1])
        self.http = await self.enterAsyncContext(httpx2.AsyncClient())

    async def asyncTearDown(self):
        self.server.should_exit = True
        await asyncio.to_thread(self.thread.join, STOP_SECONDS)

    async def post(self, headers: dict[str, str]):
        return await self.http.post(self.url, json=PING, headers={**ACCEPT, **headers})

    @contextlib.asynccontextmanager
    async def client(self, key: str = KEY, mode: str = AUTO):
        """An MCP client that sends `key` with every request."""
        async with httpx2.AsyncClient(headers=bearer(key)) as http, Client(streamable_http_client(self.url, http_client=http), mode=mode) as client:
            yield client


class KeyTest(HttpTest):
    async def test_a_request_without_the_key_is_refused_and_told_how_to_send_it(self):
        refused = (
            {}, bearer(OTHER_KEY), bearer(KEY[:-1]), bearer(KEY + "k"), {ask.AUTHORIZATION: ask.BEARER.strip()}, {ask.AUTHORIZATION: KEY},
            {ask.AUTHORIZATION: f"Basic {KEY}"}, {"X-Api-Key": KEY},
        )
        for headers in refused:
            reply = await self.post(headers)
            self.assertEqual(reply.status_code, mcp_server.UNAUTHORIZED, headers)
            self.assertEqual(reply.headers["www-authenticate"], ask.BEARER.strip())
            self.assertEqual(reply.text, mcp_server.NO_KEY_REPLY)
            self.assertNotIn(KEY, reply.text)

    async def test_every_method_and_path_is_behind_the_key(self):
        for method, url in (("GET", self.url), ("DELETE", self.url), ("GET", self.url.removesuffix(mcp_server.MCP_PATH) + "/"), ("OPTIONS", self.url)):
            reply = await self.http.request(method, url)
            self.assertEqual(reply.status_code, mcp_server.UNAUTHORIZED, (method, url))

    async def test_the_key_opens_it_and_the_scheme_may_be_written_in_any_case(self):
        for headers in (bearer(KEY), {ask.AUTHORIZATION: f"bearer {KEY}"}, {ask.AUTHORIZATION: f"BEARER  {KEY}"}):
            reply = await self.post(headers)
            self.assertEqual(reply.status_code, OK, headers)
            self.assertEqual(reply.json()["id"], PING["id"])

    async def test_the_key_is_the_only_gate_whatever_name_or_page_the_request_comes_under(self):
        port = self.url.split(":")[2].split("/")[0]
        for named in ({"Host": PUBLIC_HOST}, {"Host": PUBLIC_HOST.lower() + ":443"}, {"Host": f"localhost:{port}"}, {"Origin": OTHER_SITE}, {"Host": PUBLIC_HOST, "Origin": "https://claude.ai"}):
            self.assertEqual((await self.post({**bearer(KEY), **named})).status_code, OK, named)
            self.assertEqual((await self.post(named)).status_code, mcp_server.UNAUTHORIZED, named)

    async def test_a_client_with_the_key_lists_and_calls_the_tool_in_both_protocols(self):
        for mode in (AUTO, LEGACY):
            async with self.client(mode=mode) as client:
                self.assertEqual([tool.name for tool in (await client.list_tools()).tools], [TOOL_NAME], mode)
                by_number = await client.call_tool(TOOL_NAME, {ARG_ARTICLE: POINT_NUMBER})
                with embedding(QUESTIONS):
                    by_meaning = await client.call_tool(TOOL_NAME, {ARG_QUERY: ABOUT_WALLS, ARG_COUNT: 1})
            self.assertEqual(text_of(by_number), searched(for_article(POINT_NUMBER), POINT), mode)
            self.assertEqual(text_of(by_meaning), searched(for_query(ABOUT_WALLS), WALL), mode)

    async def test_a_client_with_another_key_is_told_only_that_the_server_refused(self):
        for mode in (AUTO, LEGACY):
            with self.assertRaises(BaseException) as raised:
                async with self.client(OTHER_KEY, mode) as client:
                    await client.list_tools()
            self.assertTrue(all(isinstance(cause, MCPError) for cause in causes(raised.exception)), (mode, raised.exception))

    async def test_whatever_fails_inside_a_search_the_client_is_told_only_that_it_failed(self):
        failure = RuntimeError(f"boom at {INTERNAL_ADDRESS}")
        for mode in (AUTO, LEGACY):
            output = io.StringIO()
            with mock.patch.object(search_tool.Embedder, "encode", autospec=True, side_effect=failure), contextlib.redirect_stderr(output):
                async with self.client(mode=mode) as client:
                    result = await client.call_tool(TOOL_NAME, {ARG_QUERY: ABOUT_WALLS})
                    still_there = await client.call_tool(TOOL_NAME, {ARG_ARTICLE: POINT_NUMBER})
            self.assertTrue(result.is_error, mode)
            self.assertEqual(text_of(result), mcp_server.SEARCH_FAILED, mode)
            self.assertIn(INTERNAL_ADDRESS, output.getvalue())  # the reason is in this server's own output
            self.assertFalse(still_there.is_error, mode)  # and the server goes on


class KeyGateTest(unittest.IsolatedAsyncioTestCase):
    """The gate by itself, for the connections that are not HTTP requests."""

    async def passed(self, scope: dict) -> tuple[bool, list[dict]]:
        """Whether the gate hands `scope` on, and what it sends back by itself."""
        reached, sent = [], []

        async def app(scope, receive, send):
            reached.append(scope)

        async def send(message):
            sent.append(message)

        await mcp_server.KeyGate(app, KEY)(scope, None, send)
        return bool(reached), sent

    async def test_the_server_starting_and_stopping_passes_without_a_key(self):
        self.assertEqual(await self.passed({"type": mcp_server.SCOPE_LIFESPAN}), (True, []))

    async def test_a_websocket_without_the_key_is_closed_and_one_with_it_passes(self):
        authorization = ask.AUTHORIZATION.lower().encode()
        self.assertEqual(await self.passed({"type": "websocket", "headers": []}), (False, [{"type": mcp_server.WEBSOCKET_CLOSE, "code": mcp_server.POLICY_VIOLATION}]))
        self.assertEqual(await self.passed({"type": "websocket", "headers": [(authorization, (ask.BEARER + KEY).encode())]}), (True, []))

    async def test_a_header_that_is_not_plain_text_is_refused_not_an_error(self):
        passed, sent = await self.passed({"type": mcp_server.SCOPE_HTTP, "headers": [(ask.AUTHORIZATION.lower().encode(), ask.BEARER.encode() + "ș".encode() * mcp_server.MIN_KEY_CHARS)]})
        self.assertFalse(passed)
        self.assertEqual(sent[0]["status"], mcp_server.UNAUTHORIZED)


del HttpTest  # only its subclasses hold tests


if __name__ == "__main__":
    unittest.main()
