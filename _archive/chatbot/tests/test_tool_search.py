"""Tests of the chatbot server when the model searches for itself (the request's "search": "model"), against the fake
hosted API of fake_lmstudio.py. No model is ever run, and the embedding server is never asked: the vectors of the
searches are made up here.

Run from the project folder: venvs/bin/python -m unittest discover -s chatbot/tests -v
"""

import json
import socket
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]  # fake_lmstudio.py, server.py (which adds the project folder itself)

import fake_lmstudio  # noqa: E402
import numpy as np  # noqa: E402
import search_tool  # noqa: E402
import server  # noqa: E402
from fake_lmstudio import openai_chunks, tool_call_chunks  # noqa: E402
from search import Index  # noqa: E402
from test_chatbot import KEY, MESSAGES, PREFIX, HostedCase, record  # noqa: E402

ask = server.ask
TOOL = search_tool.TOOL_NAME
TOOL_RULES = "TOOL RULES"
QUESTION = "Ce rezistență?"  # what ServerCase.ask asks
HOSTED_MODEL = "ark-code-latest"
NORM, OTHER_NORM = "P118-1/2025", "P118/2-2013"
CONFIG = {"p118-1": {}, "p118-2": {}}
# Three chunks, and the vectors that order them: a search for WALL finds the first two, one for DOOR the last two.
RECORDS = [
    record(id="p118-1-2025:art-1:0", doc_key="p118-1"),
    record(id="p118-1-2025:art-2.3.1:0", doc_key="p118-1", label="Art. 2.3.1", articles=["2.3.1"], text="Art. 2.3.1. Pereți."),
    record(id="p118-2-2013:art-7:0", doc_key="p118-2", doc_code=OTHER_NORM, label="Art. 7", articles=["7"], text="Art. 7. Uși."),
]
TITLES = {found["doc_code"]: found["doc_title"] for found in RECORDS}  # of the norms, as the tool tells the model
VECTORS = np.array([[1.0, 0.0], [0.6, 0.8], [0.0, 1.0]], dtype=np.float32)
WALL, DOOR = "perete antifoc", "uși rezistente la foc"
QUERY_VECTORS = {WALL: [1.0, 0.0], DOOR: [0.0, 1.0]}
ANSWER = "Răspuns din căutări [1]."
WRITTEN_CALL = '<tool_call>{"name": "search_norms", "arguments": {"query": "perete"}}</tool_call>'
SLOW_SECONDS = 0.6  # how long a search is made to take, for the browser to leave meanwhile
CHAT_PATH = PREFIX + server.OPENAI_CHAT_PATH


def query_vectors(embedder, queries):
    """Stands in for the embedding server: the vector made up for each query (the wall's for any other)."""
    return np.array([QUERY_VECTORS.get(query, QUERY_VECTORS[WALL]) for query in queries], dtype=np.float32)


def calling(*calls, **fields):
    """The chunks of a round in which the model makes `calls` (each the arguments of one call of the tool); their ids
    are c1, c2... in every round."""
    return tool_call_chunks([(f"c{place}", TOOL, arguments) for place, arguments in enumerate(calls, start=1)], **fields)


def names(events):
    return [name for name, _ in events]


def of(events, wanted):
    """The data of the events named `wanted`, in order."""
    return [data for name, data in events if name == wanted]


def answer_of(events):
    return "".join(data["content"] for data in of(events, "message"))


class ToolCase(HostedCase):
    """A chatbot with three chunks to search, asked to let the model of a hosted OpenAI-style API search for itself."""

    def setUp(self):
        super().setUp()
        self.state.index = Index(RECORDS, VECTORS, "m")
        self.state.config = CONFIG
        self.state.tool_instructions = TOOL_RULES
        self.embedding = mock.patch.object(search_tool, "query_vectors", side_effect=query_vectors)
        self.embedded = self.embedding.start()
        self.addCleanup(self.embedding.stop)

    def ask_tools(self, *rounds, **fields):
        """Ask with search "model", the fake playing `rounds` in turn; 2 chunks a search unless `top` says otherwise."""
        self.fake.openai_rounds, self.fake.openai_asked = list(rounds), 0
        return self.ask_hosted(**{"search": server.SEARCH_MODEL, "top": 2, **fields})[1]

    def chat_bodies(self):
        """What was posted to the fake's chat endpoint, request by request."""
        return [body for path, _, body in self.fake.seen if path == CHAT_PATH]


class ToolAnswerTest(ToolCase):
    """The model searches, then answers: what the page is sent, and what the chat server is."""

    def test_a_two_round_answer_is_relayed_in_order(self):
        events = self.ask_tools(calling({"query": WALL}), openai_chunks(ANSWER))
        words = len(ANSWER.split(" "))
        self.assertEqual(names(events), ["status", "reasoning", "message", "search", "sources", "reasoning"] + ["message"] * words + ["done"])
        self.assertEqual(events[0][1], {"state": server.STATUS_WAITING})
        self.assertEqual([data["content"] for data in of(events, "reasoning")], ["caut", "gândesc"])
        self.assertEqual(events[2][1], {"content": fake_lmstudio.BEFORE_CALLS})
        self.assertEqual(events[3][1], {"number": 1, "query": WALL, "article": "", "norm": "", "found": 2, "new": 2, "problem": ""})
        sources = events[4][1]["sources"]
        self.assertEqual([(s["number"], s["id"], s["score"], s["search"]) for s in sources], [(1, RECORDS[0]["id"], 1.0, 1), (2, RECORDS[1]["id"], 0.6, 1)])
        self.assertEqual(answer_of(events).strip(), ANSWER)
        self.assertEqual(events[-1][1]["stats"]["total_output_tokens"], 12)  # the stats of the last round
        self.prepare.assert_not_called()  # nothing was searched for with the question's own words

    def test_the_chat_server_gets_the_tool_and_then_the_call_with_what_it_gave(self):
        self.ask_tools(calling({"query": WALL, "count": 2}), openai_chunks(ANSWER))
        first, second = self.chat_bodies()
        tools = [search_tool.tool_schema([NORM, OTHER_NORM], 2, TITLES)]
        self.assertEqual((first["tools"], second["tools"]), (tools, tools))
        self.assertEqual(first["messages"], [{"role": "system", "content": TOOL_RULES}, {"role": "user", "content": QUESTION}])
        self.assertEqual(second["messages"][:2], first["messages"])
        called, given = second["messages"][2:]
        arguments = json.dumps({"query": WALL, "count": 2}, ensure_ascii=False)
        self.assertEqual(called, {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": TOOL, "arguments": arguments}}]})
        self.assertEqual((given["role"], given["tool_call_id"]), ("tool", "c1"))
        self.assertIn(f'Search 1 of at most {search_tool.MAX_SEARCHES}, for "{WALL}": 2 excerpts', given["content"])
        self.assertIn("[2] P118-1/2025, Art. 2.3.1", given["content"])
        self.assertEqual({authorization for path, authorization, _ in self.fake.seen if path == CHAT_PATH}, {f"Bearer {KEY}"})

    def test_the_history_and_the_fields_of_the_request_reach_the_model_and_the_search(self):
        exchange = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1 [2]"}]
        events = self.ask_tools(calling({"query": WALL}), openai_chunks(ANSWER), history=exchange, top=1, doc=OTHER_NORM)
        first = self.chat_bodies()[0]
        self.assertEqual(first["messages"][1:], [*exchange, {"role": "user", "content": QUESTION}])
        self.assertEqual(first["tools"], [search_tool.tool_schema([OTHER_NORM], 1, TITLES)])
        self.assertEqual((of(events, "search")[0]["norm"], [s["id"] for s in of(events, "sources")[0]["sources"]]), (OTHER_NORM, [RECORDS[2]["id"]]))
        self.assertEqual(self.embedded.call_args.args[0].url, "http://embedding.invalid:1/v1/embeddings")

    def test_the_sources_grow_from_search_to_search_and_say_which_search_gave_each(self):
        events = self.ask_tools(calling({"query": WALL}), calling({"query": DOOR}), openai_chunks(ANSWER))
        self.assertEqual([(s["number"], s["found"], s["new"]) for s in of(events, "search")], [(1, 2, 2), (2, 2, 1)])
        first, second = (data["sources"] for data in of(events, "sources"))
        self.assertEqual([s["id"] for s in first], [RECORDS[0]["id"], RECORDS[1]["id"]])
        self.assertEqual([(s["number"], s["id"], s["search"]) for s in second], [(1, RECORDS[0]["id"], 1), (2, RECORDS[1]["id"], 1), (3, RECORDS[2]["id"], 2)])
        self.assertEqual(names(events).count("done"), 1)
        self.assertEqual(len(self.chat_bodies()), 3)
        self.assertIn("[2] P118-1/2025, Art. 2.3.1: given above, under this number.", self.chat_bodies()[2]["messages"][-1]["content"])

    def test_two_calls_in_one_round_are_both_run_and_answered(self):
        events = self.ask_tools(calling({"query": WALL}, {"query": DOOR}), openai_chunks(ANSWER))
        self.assertEqual(names(events)[:7], ["status", "reasoning", "message", "search", "sources", "search", "sources"])
        self.assertEqual([(s["number"], s["query"]) for s in of(events, "search")], [(1, WALL), (2, DOOR)])
        self.assertEqual([s["search"] for s in of(events, "sources")[-1]["sources"]], [1, 1, 2])
        called, *given = self.chat_bodies()[1]["messages"][2:]
        self.assertEqual([call["id"] for call in called["tool_calls"]], ["c1", "c2"])
        self.assertEqual([json.loads(call["function"]["arguments"])["query"] for call in called["tool_calls"]], [WALL, DOOR])
        self.assertEqual([(message["role"], message["tool_call_id"]) for message in given], [("tool", "c1"), ("tool", "c2")])
        self.assertEqual(answer_of(events).strip(), ANSWER)

    def test_an_article_fetched_by_its_number_is_a_source_without_a_score(self):
        events = self.ask_tools(calling({"article": "Art. 2.3.1"}), openai_chunks(ANSWER))
        self.assertEqual(of(events, "search"), [{"number": 1, "query": "", "article": "2.3.1", "norm": "", "found": 1, "new": 1, "problem": ""}])
        source = of(events, "sources")[0]["sources"][0]
        self.assertEqual((source["id"], source["score"], source["search"]), (RECORDS[1]["id"], None, 1))
        self.embedded.assert_not_called()  # a number is looked up, not searched for by meaning
        self.assertEqual(events[-1][0], "done")

    def test_a_call_that_cannot_be_run_is_told_with_its_problem_and_the_model_goes_on(self):
        events = self.ask_tools(calling({"query": WALL, "norm": "NOPE"}), calling("not json"), calling({"query": WALL}), openai_chunks(ANSWER))
        first, second, third = of(events, "search")
        self.assertEqual((first["number"], first["found"], first["new"]), (1, 0, 0))
        self.assertIn("no norm with code 'NOPE'", first["problem"])
        self.assertIn("JSON object", second["problem"])
        self.assertEqual(third["problem"], "")
        self.assertEqual(names(events).count("sources"), 1)  # only the search that was run found anything
        self.assertNotIn("error", names(events))
        self.assertEqual((events[-1][0], answer_of(events).strip()), ("done", ANSWER))
        self.assertIn("Search 1 was not run", self.chat_bodies()[1]["messages"][-1]["content"])

    def test_another_model_answering_is_noted_once_not_at_every_round(self):
        self.fake.required_key = None
        events = self.ask_tools(calling({"query": WALL}), calling({"query": DOOR}), openai_chunks(ANSWER), key=None)
        self.assertEqual(of(events, "notice"), [{"message": f"answered by {fake_lmstudio.OPENAI_MODEL}, not {HOSTED_MODEL}"}])
        self.assertEqual(names(events)[:2], ["status", "notice"])
        self.assertEqual(events[-1][0], "done")

    def test_the_thinking_a_model_writes_into_its_text_is_not_sent_back_to_it(self):
        thinking = fake_lmstudio.delta_chunk(fake_lmstudio.OPENAI_MODEL, {"content": "<think>mă gândesc</think> Caut. "})
        events = self.ask_tools([thinking, *calling({"query": WALL}, reasoning="")], openai_chunks(ANSWER))
        self.assertEqual(self.chat_bodies()[1]["messages"][2]["content"], "Caut.")
        self.assertEqual(of(events, "message")[0], {"content": "<think>mă gândesc</think> Caut. "})  # the page is sent it as it came

    def test_an_answer_cut_short_in_the_last_round_is_flagged(self):
        events = self.ask_tools(calling({"query": WALL}), openai_chunks(ANSWER, finish=server.FINISH_LENGTH))
        self.assertEqual((events[-1][0], events[-1][1]["stats"]["cut"]), ("done", True))


class FallbackTest(ToolCase):
    """A model that cannot be taken at its word is handed the chunks: the page is told, then answered as without the tool."""

    def assert_fell_back(self, events, requests, toolless=1):
        """The page was told once, then sent what a question searched once gives; the chat server was asked `requests`
        times, the last `toolless` of them without the tool, and the very last with the prompt ask.prepare built."""
        at = names(events).index("fallback")
        self.assertEqual(events[at][1], {"message": ask.FELL_BACK_NOTE})
        self.assertEqual(names(events).count("fallback"), 1)
        self.assertEqual(names(events)[at + 1:at + 4], ["status", "sources", "status"])
        self.assertEqual([events[at + 1][1], events[at + 3][1]], [{"state": server.STATUS_SEARCHING}, {"state": server.STATUS_WAITING}])
        self.assertEqual(events[at + 2][1]["sources"], server.sources_payload(self.sources, self.root))
        self.assertNotIn("search", events[at + 2][1]["sources"][0])
        self.assertEqual((names(events)[-1], names(events).count("done")), ("done", 1))
        self.assertNotIn("done", names(events)[:at])
        self.prepare.assert_called_once()
        self.assertEqual((self.prepare.call_args.args[0], self.prepare.call_args.args[6]), (QUESTION, "RULES"))  # the rules without those of the tool
        bodies = self.chat_bodies()
        self.assertEqual(len(bodies), requests)
        self.assertEqual(["tools" in body for body in bodies], [True] * (requests - toolless) + [False] * toolless)
        self.assertEqual(bodies[-1]["messages"], MESSAGES)

    def test_a_model_that_never_searches_is_handed_the_chunks(self):
        events = self.ask_tools(openai_chunks("Din memorie."), openai_chunks(ANSWER))
        self.assertEqual(names(events)[:2], ["status", "reasoning"])
        self.assertEqual(events[0][1], {"state": server.STATUS_WAITING})
        self.assert_fell_back(events, requests=2)
        self.assertEqual("".join(d["content"] for d in of(events[names(events).index("fallback"):], "message")).strip(), ANSWER)

    def test_a_call_written_as_text_after_a_search_is_not_an_answer(self):
        events = self.ask_tools(calling({"query": WALL}), openai_chunks(WRITTEN_CALL), openai_chunks(ANSWER))
        self.assertEqual(len(of(events, "search")), 1)
        self.assert_fell_back(events, requests=3)

    def test_a_model_that_will_not_stop_searching_is_stopped(self):
        limit = search_tool.MAX_SEARCHES
        events = self.ask_tools(*[calling({"query": WALL})] * (limit + 2), openai_chunks(ANSWER))
        searches = of(events, "search")
        self.assertEqual([s["number"] for s in searches], list(range(1, limit + 2)))  # the one over the limit is told, the next is not run
        self.assertEqual([bool(s["problem"]) for s in searches], [False] * limit + [True])
        self.assertIn(f"limit of {limit} searches", searches[-1]["problem"])
        self.assert_fell_back(events, requests=limit + 3, toolless=2)  # told the limit, it was asked once more without the tool, and called again

    def test_another_model_answering_is_not_noted_again_after_the_fallback(self):
        self.fake.required_key = None
        events = self.ask_tools(openai_chunks("Din memorie."), openai_chunks(ANSWER), key=None)
        self.assertIn("fallback", names(events))
        self.assertEqual(len(of(events, "notice")), 1)


class OldPathTest(ToolCase):
    """Everything but search "model" with an OpenAI-style server is answered as before: one search, one stream."""

    def assert_searched_once(self, events, path):
        self.assertEqual(names(events)[:3], ["status", "sources", "status"])
        self.assertEqual(events[0][1], {"state": server.STATUS_SEARCHING})
        self.assertEqual(events[-1][0], "done")
        for absent in ("search", "fallback"):
            self.assertNotIn(absent, names(events))
        self.prepare.assert_called_once()
        self.embedded.assert_not_called()
        self.assertEqual(self.fake.seen[-1][0], path)
        self.assertNotIn("tools", self.fake.seen[-1][2])
        self.assertEqual(len([body for _, _, body in self.fake.seen if body is not None]), 1)

    def test_lm_studios_own_api_takes_no_tools_so_the_question_is_searched_once(self):
        _, events = self.ask(search=server.SEARCH_MODEL)
        self.assert_searched_once(events, server.NATIVE_CHAT_PATH)

    def test_no_search_field_is_one_search(self):
        _, events = self.ask_hosted()
        self.assert_searched_once(events, CHAT_PATH)

    def test_search_once_is_one_search(self):
        for value in (server.SEARCH_ONCE, "", None):
            self.prepare.reset_mock()
            self.fake.seen.clear()
            _, events = self.ask_hosted(search=value)
            self.assert_searched_once(events, CHAT_PATH)

    def test_a_search_that_is_neither_is_a_400(self):
        for value in ("twice", "MODEL", 5, ["model"]):
            status, payload = self.ask_hosted(search=value)
            self.assertEqual((status, payload["error"]), (400, server.SEARCH_MODE_MESSAGE), value)
        self.assertEqual(server.SEARCH_MODE_MESSAGE, "search must be one of: model, once")

    def test_the_settings_say_how_the_norms_can_be_searched(self):
        settings = json.loads(self.get(server.ROUTE_SETTINGS)[2])
        self.assertEqual((settings["top"], settings["docs"]), (server.DEFAULT_TOP, self.state.docs))  # as before: the page knows the two ways itself

    def test_sources_of_one_search_have_no_search_number_and_a_missing_score_is_null(self):
        payload = server.sources_payload([(None, RECORDS[0]), (0.12345, RECORDS[1])], self.root)
        self.assertEqual([source["score"] for source in payload], [None, 0.123])
        self.assertTrue(all("search" not in source for source in payload))
        searches = [search_tool.Search(1, numbers=[2]), search_tool.Search(2, numbers=[2, 1])]
        numbered = server.sources_payload([(None, RECORDS[0]), (0.5, RECORDS[1]), (0.4, RECORDS[2])], self.root, searches)
        self.assertEqual([source["search"] for source in numbered], [2, 1, None])

    def test_the_tool_instructions_are_the_last_field_of_the_state_and_empty_unless_given(self):
        self.assertEqual(server.AppState(self.state.index, {}, "RULES", [], self.root, "e", "c").tool_instructions, "")
        self.assertEqual(server.AppState(self.state.index, {}, "RULES", [], self.root, "e", "c", "T").tool_instructions, "T")


class ToolFailureTest(ToolCase):
    """What goes wrong while a model searches is an error on the page, and ends the rounds."""

    def test_the_embedding_server_failing_is_an_error_event(self):
        self.embedded.side_effect = server.ConversionError("no answer from http://embedding.invalid:1")
        events = self.ask_tools(calling({"query": WALL}), openai_chunks(ANSWER))
        self.assertEqual(names(events), ["status", "reasoning", "message", "error"])
        self.assertEqual(events[-1][1], {"message": "no answer from http://embedding.invalid:1"})
        self.assertEqual(len(self.chat_bodies()), 1)
        self.prepare.assert_not_called()

    def test_an_embedding_server_that_cannot_be_reached_is_an_error_event_on_the_real_path(self):
        self.embedding.stop()  # the real search.query_vectors: it asks the server named in the request, where nothing listens
        events = self.ask_tools(calling({"query": WALL}), openai_chunks(ANSWER), embedding_server="http://127.0.0.1:1")
        self.embedding.start()
        self.assertEqual(names(events), ["status", "reasoning", "message", "error"])
        self.assertIn("127.0.0.1:1", events[-1][1]["message"])
        self.assertEqual(len(self.chat_bodies()), 1)

    def test_a_norm_the_index_does_not_hold_is_an_error_event(self):
        self.state.docs.append("NP-000")
        events = self.ask_tools(calling({"query": WALL}), doc="NP-000")
        self.assertEqual(names(events), ["status", "error"])
        self.assertIn("no norm with code 'NP-000'", events[-1][1]["message"])
        self.assertEqual(self.chat_bodies(), [])

    def test_an_http_error_of_the_chat_server_in_the_second_round_is_an_error_with_the_hint_and_without_the_key(self):
        events = self.ask_tools(calling({"query": WALL}), (500, f"tools broke for {KEY}"))
        self.assertEqual(names(events), ["status", "reasoning", "message", "search", "sources", "error"])
        message = events[-1][1]["message"]
        self.assertIn("answered 500", message)
        self.assertIn(f"tools broke for {server.REDACTED}", message)
        self.assertTrue(message.endswith(server.NO_TOOLS_HINT))
        self.assertNotIn(KEY, json.dumps(events))
        self.assertEqual(len(self.chat_bodies()), 2)
        self.prepare.assert_not_called()  # an error is not a model that did not search

    def test_a_server_that_refuses_the_tools_at_once_is_an_error_with_the_hint(self):
        events = self.ask_tools((400, "tools are not supported"))
        self.assertEqual(names(events), ["status", "error"])
        self.assertIn("400", events[-1][1]["message"])
        self.assertIn("tools are not supported", events[-1][1]["message"])
        self.assertTrue(events[-1][1]["message"].endswith(server.NO_TOOLS_HINT))

    def test_a_refusal_that_is_not_about_the_tools_gets_no_advice_to_search_once(self):
        events = self.ask_tools((500, "out of memory"))
        self.assertIn("out of memory", events[-1][1]["message"])
        self.assertNotIn(server.NO_TOOLS_HINT, events[-1][1]["message"])

    def test_an_address_that_lacks_lm_studios_v1_is_told_so_on_both_paths(self):
        refusal = f"http://x/chat/completions answered: {server.WRONG_BASE} or method. (POST /chat/completions)"
        for with_tools in (True, False):
            self.assertEqual(server.advised(refusal, with_tools), refusal + server.WRONG_BASE_HINT)
        self.assertEqual(server.advised("answered 401"), "answered 401")

    def test_a_wrong_key_is_an_error_event_without_the_key(self):
        events = self.ask_tools(calling({"query": WALL}), key="sk-WRONG-999")
        self.assertEqual(names(events), ["status", "error"])
        self.assertIn("401", events[-1][1]["message"])
        self.assertNotIn(server.NO_TOOLS_HINT, events[-1][1]["message"])
        self.assertNotIn("sk-WRONG-999", json.dumps(events))

    def test_an_error_the_chat_server_reports_in_its_stream_ends_the_rounds(self):
        failing = [{"error": {"message": f"model crashed with {KEY}"}}]
        for rounds, before in (([failing], []), ([calling({"query": WALL}), failing], ["reasoning", "message", "search", "sources"])):
            self.fake.seen.clear()
            events = self.ask_tools(*rounds, openai_chunks(ANSWER))
            self.assertEqual(names(events), ["status", *before, "error", "done"])  # as when the question is searched once: the error, then the end
            self.assertEqual(events[-2][1], {"message": f"model crashed with {server.REDACTED}"})
            self.assertEqual(len(self.chat_bodies()), len(rounds))  # nothing more was asked
            self.assertNotIn("fallback", names(events))
        self.prepare.assert_not_called()

    def test_an_empty_stream_is_an_error_not_a_fallback(self):
        events = self.ask_tools([])
        self.assertEqual(names(events), ["status", "error"])
        self.assertIn("empty stream", events[-1][1]["message"])

    def leave_after(self, seen: bytes) -> None:
        """Ask as the page does, and close the connection as soon as `seen` has arrived, as its stop button does."""
        body = json.dumps({
            "question": QUESTION, "model": HOSTED_MODEL, "chat_server": self.hosted, "chat_api": server.API_OPENAI, "chat_api_key": KEY,
            "search": server.SEARCH_MODEL,
        }).encode()
        port = self.web.server_address[1]
        with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
            client.sendall(b"POST /api/ask HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (port, len(body), body))
            received = b""
            while seen not in received:
                received += client.recv(4096)

    def test_leaving_during_a_search_ends_the_rounds(self):
        real = search_tool.SearchSession.reply

        def slow(session, arguments, name=TOOL):
            time.sleep(SLOW_SECONDS)
            return real(session, arguments, name)

        self.fake.openai_rounds = [calling({"query": WALL, "norm": "NOPE"}), openai_chunks(ANSWER)]
        with mock.patch.object(search_tool.SearchSession, "reply", slow):
            self.leave_after(b"reasoning")
            time.sleep(SLOW_SECONDS * 3)
        self.assertEqual(self.fake.openai_asked, 1)  # the model was not asked a second time

    def test_leaving_during_a_round_stops_its_stream(self):
        self.fake.openai_rounds = [calling({"query": WALL}, reasoning="x " * 200), openai_chunks(ANSWER)]
        self.fake.delay = 0.02
        self.leave_after(b"reasoning")
        time.sleep(1.0)
        sent = self.fake.events_sent
        time.sleep(0.5)
        self.assertEqual(self.fake.events_sent, sent)  # nothing more is written
        self.assertLess(sent, 100)
        self.assertEqual(self.fake.openai_asked, 1)
        self.embedded.assert_not_called()  # and the call the round would have ended with is not run


class ToolStreamTest(ToolCase):
    """The tool calls of an OpenAI-style reply, streamed in pieces or sent whole, as the events' last one gives them."""

    def end_of(self, chunks):
        events = list(server.openai_events(iter(chunks)))
        self.assertEqual(events[-1]["type"], server.LMS_CHAT_END)
        return events, events[-1]["result"]

    def test_a_call_that_arrives_in_pieces_is_put_together(self):
        arguments = {"query": "perete antifoc rezistență la foc", "count": 10}
        events, result = self.end_of(calling(arguments, reasoning="mă gândesc", usage=False))
        self.assertEqual([e["type"] for e in events], ["chat.start", "reasoning.delta", "reasoning.delta", "message.delta", "chat.end"])
        call, = result[server.KEY_CALLS]
        self.assertEqual((call["id"], call["type"], call["function"]["name"]), ("c1", "function", TOOL))
        self.assertEqual(json.loads(call["function"]["arguments"]), arguments)
        self.assertEqual(ask.tool_calls_of(result), [ask.ToolCall("c1", TOOL, json.dumps(arguments, ensure_ascii=False))])

    def test_a_round_of_calls_is_a_whole_reply_not_an_incomplete_one(self):
        _, result = self.end_of(calling({"query": WALL}))
        stats = result[server.KEY_STATS]
        self.assertEqual((stats[server.STATS_INCOMPLETE], stats[server.STATS_CUT]), (False, False))
        _, unfinished = self.end_of(calling({"query": WALL})[:-2])  # neither the finish marker nor the usage
        self.assertTrue(unfinished[server.KEY_STATS][server.STATS_INCOMPLETE])

    def test_pieces_of_two_calls_are_kept_apart_by_their_index_however_they_are_mixed(self):
        def piece(index, **fields):
            function = {name: fields.pop(name) for name in ("name", "arguments") if name in fields}
            return {"choices": [{"delta": {"tool_calls": [{"index": index, "type": "function", "function": function, **fields}]}}]}

        chunks = [
            piece(1, id="b", name=TOOL, arguments=""), piece(0, id="a", name=TOOL, arguments='{"que'), piece(1, arguments='{"article": '),
            piece(0, arguments='ry": "x"}'), piece(1, arguments='"7"}', id="b", name="late"), {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        _, result = self.end_of(chunks)
        self.assertEqual(ask.tool_calls_of(result), [ask.ToolCall("a", TOOL, '{"query": "x"}'), ask.ToolCall("b", TOOL, '{"article": "7"}')])

    def test_calls_a_server_gives_one_index_are_kept_apart_by_their_ids(self):
        def whole(call_id, arguments):
            return {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": call_id, "function": {"name": TOOL, "arguments": arguments}}]}}]}

        _, result = self.end_of([whole("a", '{"query": "x"}'), whole("b", '{"article": "7"}'), {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}])
        self.assertEqual(ask.tool_calls_of(result), [ask.ToolCall("a", TOOL, '{"query": "x"}'), ask.ToolCall("b", TOOL, '{"article": "7"}')])

    def test_the_arguments_of_a_call_are_gathered_up_to_a_size_only(self):
        piece = {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "a", "function": {"name": TOOL, "arguments": "x" * 1000}}]}}]}
        _, result = self.end_of([piece] * 50)
        self.assertLessEqual(len(ask.tool_calls_of(result)[0].arguments), server.MAX_CALL_CHARS + 1000)

    def test_two_calls_in_one_chunk_are_two_calls(self):
        both = [{"index": 0, "id": "a", "function": {"name": TOOL, "arguments": "{}"}}, {"index": 1, "id": "b", "function": {"name": TOOL, "arguments": "{}"}}]
        _, result = self.end_of([{"choices": [{"delta": {"tool_calls": both}, "finish_reason": "tool_calls"}]}])
        self.assertEqual([call.id for call in ask.tool_calls_of(result)], ["a", "b"])

    def test_odd_pieces_are_skipped_and_the_call_survives(self):
        odd = [None, "x", 5, {"index": "zero"}, {"index": 3, "function": "name"}, {"index": 2, "function": {"name": 7, "arguments": 7}}]
        chunks = [
            {"choices": [{"delta": {"tool_calls": "search"}}]}, {"choices": [{"delta": {"tool_calls": {"index": 0}}}]}, {"choices": [{"delta": {"tool_calls": odd}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 5, "id": "ok", "function": {"name": TOOL, "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]},
        ]
        _, result = self.end_of(chunks)
        self.assertEqual(ask.tool_calls_of(result), [ask.ToolCall("ok", TOOL, "{}")])  # a call without a name is no call

    def test_an_answer_has_no_calls(self):
        _, result = self.end_of(openai_chunks())
        self.assertEqual(result[server.KEY_CALLS], [])

    def test_a_whole_json_reply_carries_its_calls_too(self):
        calls = [
            {"id": "w1", "type": "function", "function": {"name": TOOL, "arguments": '{"query": "x"}'}},
            {"type": "function", "function": {"name": TOOL, "arguments": {"article": "7"}}},  # no id, and the arguments as an object
        ]
        reply = {"model": "resolved", "choices": [{"message": {"content": None, "tool_calls": calls}, "finish_reason": "tool_calls"}], "usage": {"completion_tokens": 9}}
        self.fake.openai_raw = ("application/json", json.dumps(reply))
        events = list(server.stream_chat(self.hosted, HOSTED_MODEL, MESSAGES, server.API_OPENAI, None, KEY, [search_tool.tool_schema([NORM])]))
        result = events[-1]["result"]
        self.assertEqual(ask.tool_calls_of(result), [ask.ToolCall("w1", TOOL, '{"query": "x"}'), ask.ToolCall(ask.CALL_ID.format(number=1), TOOL, '{"article": "7"}')])
        self.assertEqual((result["stats"]["incomplete"], result["stats"]["total_output_tokens"]), (False, 9))

    def test_a_whole_json_reply_with_calls_is_a_round_like_any_other(self):
        call = {"id": "w1", "type": "function", "function": {"name": TOOL, "arguments": json.dumps({"query": WALL})}}
        self.fake.openai_raw = ("application/json", json.dumps({"choices": [{"message": {"content": "", "tool_calls": [call]}, "finish_reason": "tool_calls"}]}))
        real = search_tool.SearchSession.reply

        def answering_next(session, arguments, name=TOOL):
            self.fake.openai_raw = ("application/json", json.dumps({"choices": [{"message": {"content": ANSWER}, "finish_reason": "stop"}]}))
            return real(session, arguments, name)

        with mock.patch.object(search_tool.SearchSession, "reply", answering_next):
            _, events = self.ask_hosted(search=server.SEARCH_MODEL, top=2)
        self.assertEqual(names(events), ["status", "search", "sources", "message", "done"])
        self.assertEqual(answer_of(events), ANSWER)

    def test_the_tools_go_to_an_openai_style_server_only(self):
        tools = [search_tool.tool_schema([NORM])]
        list(server.stream_chat(self.hosted, HOSTED_MODEL, MESSAGES, server.API_OPENAI, None, KEY, tools))
        self.assertEqual(self.fake.last_chat_body["tools"], tools)
        list(server.stream_chat(self.hosted, HOSTED_MODEL, MESSAGES, server.API_OPENAI, None, KEY))
        self.assertNotIn("tools", self.fake.last_chat_body)
        list(server.stream_chat(self.fake.url, fake_lmstudio.MODEL_ID, MESSAGES, server.API_LMSTUDIO, None, None, tools))
        self.assertNotIn("tools", self.fake.last_chat_body)
        self.assertIn("system_prompt", self.fake.last_chat_body)


class FakeByHandTest(ToolCase):
    """The fake as it is run by hand: a request with tools gets a call of the tool, then an answer."""

    def test_a_request_with_tools_is_played_a_search_and_then_an_answer(self):
        self.fake.plays_tools = True
        _, events = self.ask_hosted(search=server.SEARCH_MODEL, top=2)
        self.assertEqual([(s["number"], s["query"], s["found"]) for s in of(events, "search")], [(1, QUESTION, 2)])
        self.assertEqual((events[-1][0], answer_of(events).strip()), ("done", fake_lmstudio.BEFORE_CALLS.strip() + fake_lmstudio.DEMO_ANSWER))
        self.assertEqual(self.chat_bodies()[1]["messages"][-1]["tool_call_id"], fake_lmstudio.DEMO_CALL_ID)

    def test_a_request_without_tools_is_played_its_script(self):
        self.fake.plays_tools = True
        _, events = self.ask_hosted()
        self.assertEqual(answer_of(events).strip(), "Răspuns hosted [1].")


if __name__ == "__main__":
    unittest.main()
