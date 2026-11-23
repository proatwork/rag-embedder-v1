"""Tests of the chatbot server, against a fake LM Studio. No model is ever run and no real server is used.

Run from the project folder: venvs/bin/python -m unittest discover -s chatbot/tests -v
"""

import json
import re
import socket
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]  # fake_lmstudio.py, server.py (which adds the project folder itself)

import fake_lmstudio  # noqa: E402
import numpy as np  # noqa: E402
import server  # noqa: E402
from fake_lmstudio import MODEL_ID, FakeLMStudio, event  # noqa: E402
from search import IMAGE_FILE_KEY, Index  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
SYSTEM_TEXT = "RULES"
USER_TEXT = "Excerpts... Question: x"
MESSAGES = [{server.ask.API_ROLE: "system", server.ask.API_CONTENT: SYSTEM_TEXT}, {server.ask.API_ROLE: "user", server.ask.API_CONTENT: USER_TEXT}]


def record(images=(), **fields):
    return {
        "id": "p118-1-2025:art-1:0", "doc_code": "P118-1/2025", "doc_title": "Normativ", "edition_year": 2025, "status": "in_force", "kind": "article",
        "label": "Art. 1", "caption": "", "articles": ["1"], "pdf_pages": [7, 8], "heading_path": [],
        "embed_text": "P118-1/2025 > Art. 1\nArt. 1. Text.", "text": "Art. 1. Text.", "images": list(images), **fields,
    }


class ServerCase(unittest.TestCase):
    """A chatbot server on a free port with a fake LM Studio as its chat server."""

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.root = Path(self.folder.name).resolve()
        (self.root / "pics").mkdir()
        (self.root / "pics" / "a.png").write_bytes(PNG)
        (self.root / "pics" / "notes.txt").write_text("not an image")
        self.outside = TemporaryDirectory()
        (Path(self.outside.name) / "secret.png").write_bytes(PNG)
        self.fake = FakeLMStudio().start()
        self.state = server.AppState(
            Index([record()], np.zeros((1, 2), dtype=np.float32), "m"), {}, "RULES", ["P118-1/2025", "P118/2-2013"], self.root,
            "http://embedding.invalid:1", self.fake.url,
        )
        self.web = server.make_server(self.state, "127.0.0.1", 0)
        threading.Thread(target=self.web.serve_forever, kwargs={"poll_interval": fake_lmstudio.POLL_SECONDS}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.web.server_address[1]}"
        picture = {"path": "x", "caption": "Figura 1", "page": 4, IMAGE_FILE_KEY: str(self.root / "pics" / "a.png")}
        self.sources = [(0.91, record([picture]))]
        self.patcher = mock.patch.object(server.ask, "prepare", autospec=True, return_value=(self.sources, MESSAGES))
        self.prepare = self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def tearDown(self):
        self.web.shutdown(); self.web.server_close(); self.fake.shutdown(); self.fake.server_close()
        self.folder.cleanup(); self.outside.cleanup()

    def get(self, path, headers=None):
        request = urllib.request.Request(self.base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                return reply.status, reply.headers, reply.read()
        except urllib.error.HTTPError as error:
            return error.code, error.headers, error.read()

    def post(self, body, headers=None):
        request = urllib.request.Request(self.base + "/api/ask", body, headers or {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as reply:
                return reply.status, reply.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def ask(self, **fields):
        """POST /api/ask; returns the HTTP status and the list of (event name, data)."""
        body = {"question": "Ce rezistență?", "model": MODEL_ID, "top": 5, "doc": "", "chat_server": self.fake.url, "embedding_server": "http://embedding.invalid:1"}
        body.update(fields)
        request = urllib.request.Request(self.base + "/api/ask", json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as reply:
                text = reply.read().decode()
                status = reply.status
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())
        events = []
        for block in text.split("\n\n"):
            if block.strip():
                name = next(line[6:].strip() for line in block.split("\n") if line.startswith("event:"))
                data = next(line[5:].strip() for line in block.split("\n") if line.startswith("data:"))
                events.append((name, json.loads(data)))
        return status, events


class ServersTest(unittest.TestCase):
    def test_normalize_server(self):
        self.assertEqual(server.normalize_server(" http://10.0.0.1:1234/ "), "http://10.0.0.1:1234")
        self.assertEqual(server.normalize_server("https://ark.example/api/coding/v3/"), "https://ark.example/api/coding/v3")
        for bad in ("", "10.0.0.1:1234", "ftp://x:1", "http://x:1/v1?a=1", "http://x:1/v1#a", "javascript:alert(1)"):
            with self.assertRaises(server.UpstreamError, msg=bad):
                server.normalize_server(bad)

    def test_models_without_embedding_models_loaded_first(self):
        fake = FakeLMStudio().start()
        self.addCleanup(fake.server_close); self.addCleanup(fake.shutdown)
        models = server.list_chat_models(fake.url)
        self.assertEqual([m["id"] for m in models], [MODEL_ID, "other-model"])
        self.assertEqual(models[0], {"id": MODEL_ID, "state": "loaded", "context": 8192})

    def test_models_fall_back_to_the_openai_list(self):
        fake = FakeLMStudio().start()
        self.addCleanup(fake.server_close); self.addCleanup(fake.shutdown)
        fake.models = None
        fake.openai_models = ["text-embedding-x", "qwen-4b"]
        self.assertEqual(server.list_chat_models(fake.url), [{"id": "qwen-4b", "state": None, "context": None}])

    def test_unreachable_server_is_an_upstream_error(self):
        with self.assertRaises(server.UpstreamError):
            server.list_chat_models("http://127.0.0.1:1")

    def test_stream_chat_sends_the_documented_fields_and_yields_events(self):
        fake = FakeLMStudio().start()
        self.addCleanup(fake.server_close); self.addCleanup(fake.shutdown)
        events = list(server.stream_chat(fake.url, MODEL_ID, [{"role": "system", "content": "SYS"}, {"role": "user", "content": "USER"}]))
        self.assertEqual(events[0]["type"], "chat.start")
        self.assertEqual(events[-1]["type"], "chat.end")
        body = fake.last_chat_body
        self.assertEqual((body["model"], body["system_prompt"], body["input"], body["stream"], body["store"]), (MODEL_ID, "SYS", "USER", True, False))
        self.assertEqual(body["temperature"], server.ask.TEMPERATURE)

    def test_stream_chat_reports_an_http_error_with_its_message(self):
        fake = FakeLMStudio().start()
        self.addCleanup(fake.server_close); self.addCleanup(fake.shutdown)
        fake.chat_status = 400
        with self.assertRaisesRegex(server.UpstreamError, "400.*scripted failure"):
            list(server.stream_chat(fake.url, MODEL_ID, MESSAGES))

    def test_answered_by(self):
        self.assertTrue(server.answered_by("qwen", "qwen"))
        self.assertTrue(server.answered_by("qwen:2", "qwen"))
        self.assertFalse(server.answered_by("qwen2", "qwen"))
        self.assertFalse(server.answered_by("other", "qwen"))


class RoutesTest(ServerCase):
    def test_page_and_settings(self):
        status, headers, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Norms Chat", body)
        status, _, body = self.get("/api/settings")
        settings = json.loads(body)
        self.assertEqual((status, settings["docs"], settings["chat_server"]), (200, ["P118-1/2025", "P118/2-2013"], self.fake.url))

    def test_models_route(self):
        status, _, body = self.get("/api/models?server=" + self.fake.url)
        self.assertEqual((status, [m["id"] for m in json.loads(body)]), (200, [MODEL_ID, "other-model"]))
        status, _, body = self.get("/api/models?server=http://127.0.0.1:1")
        self.assertEqual(status, 502)
        self.assertIn("error", json.loads(body))

    def test_image_is_served_from_inside_the_root_only(self):
        self.assertEqual(self.get("/image?p=pics/a.png")[0], 200)
        self.assertEqual(self.get("/image?p=pics/a.png")[2], PNG)
        for bad in ("pics/notes.txt", "pics/missing.png", "../" + Path(self.outside.name).name + "/secret.png", "../../../../etc/passwd", "", "pics"):
            self.assertEqual(self.get("/image?p=" + bad)[0], 404, bad)

    def test_a_foreign_host_header_is_refused(self):
        self.assertEqual(self.get("/", {"Host": "evil.example:80"})[0], 403)
        self.assertEqual(self.get("/", {"Host": f"localhost:{self.web.server_address[1]}"})[0], 200)

    def test_unknown_route(self):
        self.assertEqual(self.get("/nope")[0], 404)


class AskTest(ServerCase):
    def test_a_full_answer_is_relayed_in_order(self):
        status, events = self.ask()
        self.assertEqual(status, 200)
        names = [name for name, _ in events]
        self.assertEqual(names[:2], ["status", "sources"])
        self.assertEqual(names[2], "status")
        self.assertEqual(names[-1], "done")
        self.assertIn("prompt", names)
        self.assertEqual("".join(d["content"] for n, d in events if n == "message").strip(), "Pereții antifoc trebuie să reziste minimum o oră [1], conform normei [2].")
        self.assertEqual("".join(d["content"] for n, d in events if n == "reasoning"), "Citesc extrasele.")
        self.assertEqual([d["progress"] for n, d in events if n == "prompt"], [0.4, 1])
        self.assertEqual(events[-1][1]["stats"]["tokens_per_second"], 44.5)

    def test_sources_carry_the_chunk_and_an_image_url(self):
        _, events = self.ask()
        source = events[1][1]["sources"][0]
        self.assertEqual((source["number"], source["score"], source["doc_code"], source["text"]), (1, 0.91, "P118-1/2025", "Art. 1. Text."))
        self.assertEqual(source["images"][0]["caption"], "Figura 1")
        self.assertEqual(self.get(source["images"][0]["url"])[2], PNG)

    def test_a_source_carries_its_citation_and_no_remark_when_its_norm_simply_applies(self):
        _, events = self.ask()
        source = events[1][1]["sources"][0]
        self.assertEqual((source["reference"], source["pages"]), ("P118-1/2025, Art. 1", "PDF p. 7–8"))
        self.assertEqual(source["citation"], "P118-1/2025, Art. 1 — Normativ (ed. 2025, PDF p. 7–8)")
        self.assertEqual(source["status_note"], "")
        self.assertNotIn("status", source)  # the page is given words, never the config's value

    def test_a_source_of_a_norm_that_does_not_simply_apply_says_so(self):
        for fields, note in (
            ({"status": "unverified"}, "neverificat"), ({"status": "repealed", "repealed_on": "2026-08-25"}, "abrogat din 25.08.2026"),
            ({"amended_by": ["Ordinul 1"]}, "în vigoare; text original, modificat ulterior prin: Ordinul 1"),
        ):
            payload = server.sources_payload([(0.5, record(**fields))], self.root)
            self.assertEqual(payload[0]["status_note"], note, fields)

    def test_a_source_without_pages_or_a_place_has_empty_ones(self):
        payload = server.sources_payload([(0.5, record(label="", articles=[], pdf_pages=None))], self.root)
        self.assertEqual((payload[0]["reference"], payload[0]["pages"]), ("P118-1/2025", ""))

    def test_an_image_outside_the_root_is_left_out(self):
        picture = {"path": "x", "caption": "", "page": None, IMAGE_FILE_KEY: str(Path(self.outside.name) / "secret.png")}
        payload = server.sources_payload([(0.5, record([picture]))], self.root)
        self.assertEqual(payload[0]["images"], [])

    def test_prepare_gets_the_fields_of_the_request(self):
        self.ask(top=8, doc="P118/2-2013", question="  perete  ")
        args = self.prepare.call_args.args
        self.assertEqual((args[0], args[3], args[4], args[5], args[6]), ("perete", "http://embedding.invalid:1", 8, "P118/2-2013", "RULES"))

    def test_the_prompt_is_sent_to_the_chat_server_as_system_prompt_and_input(self):
        self.ask()
        self.assertEqual((self.fake.last_chat_body["system_prompt"], self.fake.last_chat_body["input"]), (SYSTEM_TEXT, USER_TEXT))

    def test_a_bad_request_is_a_400_with_the_reason(self):
        for fields, reason in (
            ({"question": "  "}, "empty"), ({"model": ""}, "chat model"), ({"top": 0}, "between"), ({"top": "x"}, "whole number"),
            ({"top": 51}, "between"), ({"doc": "NOPE"}, "no norm"), ({"chat_server": "banana"}, "server address"),
        ):
            status, payload = self.ask(**fields)
            self.assertEqual(status, 400, fields)
            self.assertIn(reason, payload["error"])

    def test_the_embedding_server_failing_is_an_error_event(self):
        self.prepare.side_effect = SystemExit("no answer from http://embedding.invalid:1")
        status, events = self.ask()
        self.assertEqual((status, [n for n, _ in events]), (200, ["status", "error"]))
        self.assertIn("no answer from", events[-1][1]["message"])

    def test_the_conversion_error_of_prepare_is_an_error_event(self):
        self.prepare.side_effect = server.ConversionError("a chunk file changed after its vectors were made")
        _, events = self.ask()
        self.assertIn("changed after", events[-1][1]["message"])

    def test_an_unreachable_chat_server_is_an_error_event_after_the_sources(self):
        _, events = self.ask(chat_server="http://127.0.0.1:1")
        self.assertEqual([n for n, _ in events], ["status", "sources", "status", "error"])
        self.assertIn("cannot reach", events[-1][1]["message"])

    def test_another_model_answering_is_noted_but_not_blocked(self):
        self.fake.script = fake_lmstudio.default_script(model="some-other-model")
        _, events = self.ask()
        self.assertIn(("notice", {"message": "answered by some-other-model, not fake-model"}), events)
        self.assertEqual(events[-1][0], "done")
        self.assertIn("message", [n for n, _ in events])

    def test_a_second_copy_of_the_model_is_accepted_without_a_notice(self):
        self.fake.script = fake_lmstudio.default_script(model=MODEL_ID + ":2")
        _, events = self.ask()
        self.assertEqual(events[-1][0], "done")
        self.assertNotIn("notice", [n for n, _ in events])

    def test_the_chat_servers_error_event_is_relayed_and_the_stream_still_ends(self):
        script = fake_lmstudio.default_script()
        script.insert(1, event("error", error={"type": "model_not_found", "message": "model 'x' not found"}))
        self.fake.script = script
        _, events = self.ask()
        self.assertIn(("error", {"message": "model 'x' not found"}), events)
        self.assertEqual(events[-1][0], "done")

    def test_model_loading_progress_is_relayed(self):
        script = fake_lmstudio.default_script()
        script[1:1] = [event("model_load.start", model_instance_id=MODEL_ID), event("model_load.progress", model_instance_id=MODEL_ID, progress=0.5),
                       event("model_load.end", model_instance_id=MODEL_ID, load_time_seconds=3)]
        self.fake.script = script
        _, events = self.ask()
        self.assertEqual([d["progress"] for n, d in events if n == "model_load"], [0, 0.5])

    def test_a_stream_with_no_answer_still_ends_with_done(self):
        self.fake.script = [event("chat.start", model_instance_id=MODEL_ID), event("reasoning.delta", content="hmm"),
                            event("chat.end", result={"stats": {"total_output_tokens": 5}})]
        _, events = self.ask()
        self.assertEqual([n for n, _ in events][-2:], ["reasoning", "done"])
        self.assertNotIn("message", [n for n, _ in events])


class HardeningTest(ServerCase):
    def test_a_foreign_origin_is_refused_on_get_and_post(self):
        self.assertEqual(self.get("/", {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.get("/api/models?server=" + self.fake.url, {"Origin": "https://evil.example"})[0], 403)
        body = json.dumps({"question": "x", "model": MODEL_ID}).encode()
        status, _ = self.post(body, {"Content-Type": "application/json", "Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        own = f"http://127.0.0.1:{self.web.server_address[1]}"
        self.assertEqual(self.get("/", {"Origin": own})[0], 200)

    def test_a_post_must_be_json(self):
        body = json.dumps({"question": "x", "model": MODEL_ID}).encode()
        for content_type in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data"):
            self.assertEqual(self.post(body, {"Content-Type": content_type})[0], 403, content_type)
        self.assertEqual(self.post(body, {"Content-Type": "application/json; charset=utf-8"})[0], 200)
        self.prepare.assert_called_once()  # only the JSON one searched

    def test_odd_requests_get_an_answer_not_a_dropped_connection(self):
        self.assertEqual(self.get("/image?p=a%00.png")[0], 404)
        self.assertEqual(self.get("/image")[0], 404)
        for address in ("http://[", "http://127.0.0.1:abc", "http://a b", "http://h:1#x", "http://h:1?x=1", "ftp://h", "//h"):
            status, _, body = self.get("/api/models?server=" + urllib.parse.quote(address))
            self.assertEqual(status, 502, address)
            self.assertIn("error", json.loads(body))
        for payload in (b"[]", b"1", b"null", b"{", b"\xff\xfe", b'{"question": "x", "model": "m", "top": 1e999}', b'{"question": "x", "model": "m", "top": null}'):
            status, reply = self.post(payload)
            self.assertEqual(status, 400, payload)
            self.assertIn("error", json.loads(reply))
        self.assertEqual(self.post(b"")[0], 400)
        self.assertEqual(self.post(b"x" * (server.MAX_BODY_BYTES + 1))[0], 400)

    def test_the_search_stopping_the_program_is_caught_on_the_real_path(self):
        self.patcher.stop()  # the real ask.prepare: it embeds the question on the server named in the request
        status, events = self.ask(embedding_server="http://127.0.0.1:1")
        self.assertEqual((status, [n for n, _ in events]), (200, ["status", "error"]))
        self.assertIn("127.0.0.1:1", events[-1][1]["message"])
        self.patcher.start()

    def test_stopping_during_the_search_does_not_start_the_model(self):
        self.prepare.side_effect = lambda *args: (time.sleep(0.6), (self.sources, MESSAGES))[1]
        body = json.dumps({"question": "x", "model": MODEL_ID, "top": 5, "chat_server": self.fake.url}).encode()
        with socket.create_connection(("127.0.0.1", self.web.server_address[1]), timeout=10) as client:
            client.sendall(b"POST /api/ask HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (self.web.server_address[1], len(body), body))
            received = b""
            while b"searching" not in received:
                received += client.recv(4096)
        time.sleep(1.5)
        self.assertIsNone(self.fake.last_chat_body)

    def test_stopping_during_the_answer_stops_the_stream(self):
        self.fake.script = [event("chat.start", model_instance_id=MODEL_ID)] + [event("message.delta", content="x")] * 200 + [event("chat.end", result={})]
        self.fake.delay = 0.02
        body = json.dumps({"question": "x", "model": MODEL_ID, "top": 5, "chat_server": self.fake.url}).encode()
        with socket.create_connection(("127.0.0.1", self.web.server_address[1]), timeout=10) as client:
            client.sendall(b"POST /api/ask HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n%s" % (self.web.server_address[1], len(body), body))
            received = b""
            while b"message" not in received:
                received += client.recv(4096)
        time.sleep(1.0)
        sent = self.fake.events_sent
        time.sleep(0.5)
        self.assertEqual(self.fake.events_sent, sent)  # nothing more is written
        self.assertLess(sent, 100)


KEY = "sk-test-KEY-123"
PREFIX = fake_lmstudio.OPENAI_PREFIX


class HostedCase(ServerCase):
    """A chatbot whose chat server is a hosted OpenAI-style API that wants a key."""

    def setUp(self):
        super().setUp()
        self.fake.required_key = KEY
        self.hosted = self.fake.url + PREFIX

    def ask_hosted(self, key=KEY, **fields):
        return self.ask(chat_server=self.hosted, chat_api="openai", chat_api_key=key, model="ark-code-latest", **fields)


class ApiKeyTest(HostedCase):
    """A hosted OpenAI-style API with a key: the stream, the model list, the errors, and the key never shown."""

    def test_the_openai_stream_becomes_the_same_events(self):
        events = list(server.stream_chat(self.hosted, "ark-code-latest", MESSAGES, "openai", None, KEY))
        types = [e["type"] for e in events]
        self.assertEqual((types[0], types[-1]), ("chat.start", "chat.end"))
        self.assertEqual(events[0]["model_instance_id"], "resolved-model")
        self.assertEqual("".join(e["content"] for e in events if e["type"] == "message.delta").strip(), "Răspuns hosted [1].")
        self.assertEqual("".join(e["content"] for e in events if e["type"] == "reasoning.delta"), "gândesc")
        stats = events[-1]["result"]["stats"]
        self.assertEqual((stats["input_tokens"], stats["total_output_tokens"], stats["reasoning_output_tokens"], stats["approximate"]), (700, 12, 1, False))
        path, authorization, body = self.fake.seen[-1]
        self.assertEqual((path, authorization), (PREFIX + "/chat/completions", f"Bearer {KEY}"))
        self.assertEqual((body["model"], body["messages"], body["stream"]), ("ark-code-latest", MESSAGES, True))
        self.assertNotIn("system_prompt", body)

    def test_without_usage_the_counts_are_marked_approximate(self):
        self.fake.openai_chunks = fake_lmstudio.openai_chunks(usage=False)
        stats = list(server.stream_chat(self.hosted, "m", MESSAGES, "openai", None, KEY))[-1]["result"]["stats"]
        self.assertTrue(stats["approximate"])
        self.assertEqual(stats["total_output_tokens"], 3 + 1)  # the pieces of the answer and of the thinking

    def test_a_full_answer_through_the_page_route_with_a_key(self):
        status, events = self.ask_hosted()
        self.assertEqual(status, 200)
        names = [n for n, _ in events]
        self.assertEqual(names[:3], ["status", "sources", "status"])
        self.assertEqual(names[-1], "done")
        self.assertNotIn("prompt", names)  # a hosted API reports no prompt processing
        self.assertNotIn("notice", names)  # the model it names (resolved-model) is not compared when a key is used
        self.assertEqual("".join(d["content"] for n, d in events if n == "message").strip(), "Răspuns hosted [1].")
        self.assertEqual(events[-1][1]["stats"]["total_output_tokens"], 12)

    def test_the_key_goes_to_the_chat_server_only_and_is_never_shown(self):
        _, events = self.ask_hosted()
        self.assertNotIn(KEY, json.dumps(events))
        self.assertEqual(self.prepare.call_args.args[3], "http://embedding.invalid:1")  # the embedding server got no key
        self.assertNotIn(KEY.encode(), self.get("/api/settings")[2])
        self.assertNotIn(KEY.encode(), self.get("/")[2])

    def test_a_wrong_key_is_an_error_event_without_the_key(self):
        _, events = self.ask_hosted(key="sk-WRONG-999")
        self.assertEqual([n for n, _ in events][-1], "error")
        self.assertIn("401", events[-1][1]["message"])
        self.assertIn("invalid api key", events[-1][1]["message"])
        self.assertNotIn("sk-WRONG-999", json.dumps(events))

    def test_a_key_the_server_echoes_in_its_error_is_hidden(self):
        self.fake.openai_chunks = [{"error": {"message": f"bad key {KEY}"}}]
        _, events = self.ask_hosted()
        errors = [data["message"] for name, data in events if name == "error"]
        self.assertEqual(errors, ["bad key ***"])
        self.assertNotIn(KEY, json.dumps(events))
        self.assertEqual(server.redact(f"cannot reach with {KEY}", KEY), "cannot reach with ***")
        self.assertEqual(server.redact("no key", None), "no key")

    def test_without_a_key_another_model_answering_is_still_noted(self):
        self.fake.required_key = None
        _, events = self.ask(chat_server=self.hosted, chat_api="openai", model="ark-code-latest")
        self.assertIn(("notice", {"message": "answered by resolved-model, not ark-code-latest"}), events)

    def test_a_custom_chat_path_is_used(self):
        _, events = self.ask_hosted(chat_path="/elsewhere")
        self.assertEqual(self.fake.seen[-1][0], PREFIX + "/elsewhere")
        self.assertIn("404", events[-1][1]["message"])

    def test_models_of_an_openai_style_server_are_listed_with_the_key(self):
        status, _, body = self.get("/api/models?server=" + urllib.parse.quote(self.hosted) + "&api=openai", {server.KEY_HEADER: KEY})
        self.assertEqual((status, json.loads(body)), (200, [{"id": "ark-code-latest", "state": None, "context": None}]))  # the embedding model is left out
        self.assertEqual(self.fake.seen[-1], (PREFIX + "/models", f"Bearer {KEY}", None))

    def test_models_with_a_wrong_or_missing_key_are_an_error_that_does_not_show_the_key(self):
        for headers in ({server.KEY_HEADER: "sk-WRONG-999"}, {}):
            status, _, body = self.get("/api/models?server=" + urllib.parse.quote(self.hosted) + "&api=openai", headers)
            self.assertEqual(status, 502)
            self.assertIn("401", json.loads(body)["error"])
            self.assertNotIn(b"sk-WRONG-999", body)

    def test_a_server_with_no_model_list_is_an_error_the_page_answers_by_letting_you_type(self):
        self.fake.openai_model_ids = None
        status, _, body = self.get("/api/models?server=" + urllib.parse.quote(self.hosted) + "&api=openai", {server.KEY_HEADER: KEY})
        self.assertEqual(status, 502)
        self.assertIn("error", json.loads(body))

    def test_the_new_fields_are_checked(self):
        for fields, reason in (
            ({"chat_api": "soap"}, "API style"), ({"chat_path": "chat"}, "chat path"), ({"chat_path": "/é"}, "chat path"), ({"chat_path": "/a b"}, "chat path"),
            ({"chat_path": "/a?x=1"}, "chat path"), ({"chat_path": "/" + "a" * 300}, "chat path"),
            ({"chat_api_key": "bad key"}, "characters"), ({"chat_api_key": "line\nbreak"}, "characters"), ({"chat_api_key": "k" * 2000}, "characters"),
            ({"chat_api_key": "clé-ünicode"}, "characters"),
        ):
            status, payload = self.ask(**{"chat_api": "openai", **fields})
            self.assertEqual(status, 400, fields)
            self.assertIn(reason, payload["error"])
        status, _, body = self.get("/api/models?server=" + urllib.parse.quote(self.hosted) + "&api=nope")
        self.assertEqual(status, 400)


class WrongAddressTest(HostedCase):
    """What a wrong address looks like: the server answers 200 with something that is not a stream. It must say so."""

    def test_a_web_page_instead_of_a_stream_is_an_error_that_shows_the_start_of_it(self):
        self.fake.openai_raw = ("text/html", "<html><body>Console login</body></html>")
        _, events = self.ask_hosted()
        self.assertEqual([n for n, _ in events][-1], "error")
        self.assertIn("did not answer with a stream", events[-1][1]["message"])
        self.assertIn("Console login", events[-1][1]["message"])
        self.assertNotIn("done", [n for n, _ in events])

    def test_a_json_error_with_status_200_is_shown(self):
        self.fake.openai_raw = ("application/json", json.dumps({"error": {"message": "unknown model deepseek-x"}}))
        _, events = self.ask_hosted()
        self.assertIn("unknown model deepseek-x", events[-1][1]["message"])

    def test_a_server_that_ignores_stream_true_is_shown_its_whole_answer(self):
        reply = {"model": "resolved", "choices": [{"message": {"content": "Răspuns întreg [1]", "reasoning_content": "hm"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 4}}
        self.fake.openai_raw = ("application/json", json.dumps(reply))
        _, events = self.ask_hosted()
        self.assertEqual("".join(d["content"] for n, d in events if n == "message"), "Răspuns întreg [1]")
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(events[-1][1]["stats"]["total_output_tokens"], 4)

    def test_an_empty_stream_is_an_error_not_a_blank_answer(self):
        self.fake.openai_chunks = []
        _, events = self.ask_hosted()
        self.assertEqual(events[-1][0], "error")
        self.assertIn("empty stream", events[-1][1]["message"])

    def test_a_model_list_that_is_not_json_says_so(self):
        self.fake.models_raw = ("text/html", "<html>hello</html>")
        status, _, body = self.get("/api/models?server=" + urllib.parse.quote(self.hosted) + "&api=openai", {server.KEY_HEADER: KEY})
        self.assertEqual(status, 502)
        self.assertIn("did not answer with a list of models", json.loads(body)["error"])


class ReviewFixesTest(HostedCase):
    """What the review of the key handling found: redirects, the key's reach, cross-site fetches, cut answers, odd shapes."""

    def other_server(self):
        other = FakeLMStudio().start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        return other

    def test_a_redirect_is_not_followed_in_the_stream_the_model_list_or_with_a_key(self):
        other = self.other_server()
        self.fake.redirect_to = other.url + PREFIX + "/chat/completions"
        _, events = self.ask_hosted()
        self.assertEqual(events[-1][0], "error")
        self.assertIn("redirected to", events[-1][1]["message"])
        self.assertIn("not followed", events[-1][1]["message"])
        status, _, body = self.get("/api/models?server=" + urllib.parse.quote(self.hosted) + "&api=openai", {server.KEY_HEADER: KEY})
        self.assertEqual(status, 502)
        self.assertIn("redirected to", json.loads(body)["error"])
        self.assertEqual(other.seen, [])  # the key reached nobody else

    def test_the_key_is_never_sent_to_lm_studios_own_api(self):
        _, events = self.ask(chat_server=self.fake.url, chat_api="lmstudio", chat_api_key=KEY)
        self.assertEqual(events[-1][0], "done")
        self.assertIsNone(self.fake.seen[-1][1])  # no Authorization header on /api/v1/chat
        self.assertEqual(self.fake.seen[-1][0], "/api/v1/chat")

    def test_a_key_is_not_sent_in_the_clear_to_a_server_outside_the_network(self):
        _, events = self.ask(chat_server="http://example.com/api/v3", chat_api="openai", chat_api_key=KEY, model="m")
        self.assertEqual(events[-1][0], "error")
        self.assertIn("plain http to example.com", events[-1][1]["message"])
        self.assertNotIn(KEY, json.dumps(events))
        status, _, body = self.get("/api/models?server=http://example.com/api/v3&api=openai", {server.KEY_HEADER: KEY})
        self.assertEqual(status, 502)
        self.assertIn("plain http", json.loads(body)["error"])

    def test_which_hosts_count_as_this_network(self):
        for host in ("localhost", "127.0.0.1", "192.168.0.120", "10.1.2.3", "172.16.0.5", "169.254.1.1", "::1", "printer.local"):
            self.assertTrue(server.is_private_host(host), host)
        for host in ("example.com", "8.8.8.8", "ark.ap-southeast.bytepluses.com", "172.32.0.1", "localhost.evil.com"):
            self.assertFalse(server.is_private_host(host), host)

    def test_a_key_in_the_models_header_is_checked_too(self):
        for bad in ("two words", "clé", "k" * 2000):
            status, _, body = self.get("/api/models?server=" + urllib.parse.quote(self.hosted) + "&api=openai", {server.KEY_HEADER: bad})
            self.assertEqual(status, 502, bad[:10])
            self.assertIn("characters", json.loads(body)["error"])

    def test_a_request_the_browser_marks_as_cross_site_is_refused(self):
        for site, expected in (("cross-site", 403), ("same-site", 403), ("same-origin", 200), ("none", 200)):
            self.assertEqual(self.get("/api/settings", {"Sec-Fetch-Site": site})[0], expected, site)
        body = json.dumps({"question": "x", "model": MODEL_ID}).encode()
        status, _ = self.post(body, {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_an_answer_that_ran_out_of_room_is_flagged_as_cut(self):
        self.fake.openai_chunks = fake_lmstudio.openai_chunks(finish="length")
        _, events = self.ask_hosted()
        self.assertEqual((events[-1][0], events[-1][1]["stats"]["cut"], events[-1][1]["stats"]["incomplete"]), ("done", True, False))

    def test_a_stream_that_ends_with_no_finish_marker_is_flagged_incomplete(self):
        self.fake.openai_chunks = fake_lmstudio.openai_chunks(finish=None)
        _, events = self.ask_hosted()
        self.assertEqual((events[-1][1]["stats"]["cut"], events[-1][1]["stats"]["incomplete"]), (False, True))

    def test_a_whole_answer_is_neither_cut_nor_incomplete_and_has_no_speed_from_a_fast_stream(self):
        _, events = self.ask_hosted()
        stats = events[-1][1]["stats"]
        self.assertEqual((stats["cut"], stats["incomplete"]), (False, False))
        self.assertNotIn("tokens_per_second", stats)  # the whole stream arrived at once: a speed from it would mean nothing

    def test_odd_chunks_are_skipped_and_the_answer_survives(self):
        self.fake.openai_chunks = [
            42, "text", None, {"model": None, "choices": [None, "x", {"delta": "str"}, {"delta": {"content": ["a", {"type": "text", "text": "Bună"}, {"text": 5}]}}]},
            {"choices": [{"delta": {"content": " ziua"}, "finish_reason": "stop"}], "usage": {"completion_tokens": None, "prompt_tokens": "x", "completion_tokens_details": 3}},
        ]
        _, events = self.ask_hosted()
        self.assertEqual("".join(d["content"] for n, d in events if n == "message"), "Bună ziua")
        self.assertEqual(events[-1][0], "done")
        self.assertTrue(events[-1][1]["stats"]["approximate"])

    def test_error_shapes_other_servers_use_are_shown(self):
        for chunks, shown in (
            ([{"object": "error", "message": "top level error"}], "top level error"),
            ([{"error": "plain text error"}], "plain text error"),
            ([{"error": {"message": {"nested": "x"}}}], "nested"),
        ):
            self.fake.openai_chunks = chunks
            _, events = self.ask_hosted()
            errors = [d["message"] for n, d in events if n == "error"]
            self.assertTrue(errors and shown in errors[0], (chunks, errors))
            self.assertEqual(events[-1][0], "done")

    def test_a_stream_sent_as_text_plain_or_with_no_type_is_still_a_stream(self):
        for stream_type in ("text/plain", "application/octet-stream", ""):
            self.fake.stream_type = stream_type
            _, events = self.ask_hosted()
            self.assertEqual("".join(d["content"] for n, d in events if n == "message").strip(), "Răspuns hosted [1].", stream_type)

    def test_odd_model_lists_give_an_error_not_a_dropped_connection(self):
        for raw, expected_status in (
            ('{"data": [1, null, {"id": 5}, {"id": "ok"}, "x"]}', 200), ('{"data": null}', 502), ('{"data": 5}', 502), ('[]', 502), ('{"nope": 1}', 502),
        ):
            self.fake.models_raw = ("application/json", raw)
            status, _, body = self.get("/api/models?server=" + urllib.parse.quote(self.hosted) + "&api=openai", {server.KEY_HEADER: KEY})
            self.assertEqual(status, expected_status, raw)
            if status == 200:
                self.assertEqual([m["id"] for m in json.loads(body)], ["ok"])
        self.fake.models = [1, None, {"id": 5}, {"id": "fine", "type": "llm", "state": "loaded"}, {"id": "vec", "type": "embeddings"}]
        status, _, body = self.get("/api/models?server=" + urllib.parse.quote(self.fake.url))
        self.assertEqual((status, [m["id"] for m in json.loads(body)]), (200, ["fine"]))
        self.fake.models = None  # no list on either path
        self.assertEqual(self.get("/api/models?server=" + urllib.parse.quote(self.fake.url))[0], 502)

    def test_the_answering_models_name_may_be_missing_or_odd(self):
        self.fake.openai_chunks = [{"model": 5, "choices": [{"delta": {"content": "ok"}}]}]
        _, events = self.ask(chat_server=self.hosted, chat_api="openai", model="ark-code-latest", chat_api_key=None)
        self.fake.required_key = None
        _, events = self.ask(chat_server=self.hosted, chat_api="openai", model="ark-code-latest")
        self.assertEqual(events[-1][0], "done")
        self.assertIn("notice", [n for n, _ in events])

    def test_the_page_comes_with_a_policy_that_lets_it_talk_to_this_server_only(self):
        _, headers, _ = self.get("/")
        policy = headers["Content-Security-Policy"]
        self.assertIn("connect-src 'self'", policy)
        self.assertIn("default-src 'none'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        for part in ("script-src 'self'", "style-src 'self'"):
            self.assertIn(part + ";", policy)
        self.assertNotIn("unsafe-inline", policy)


EXCHANGE = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1 [2]"}]
WITH_HISTORY = [MESSAGES[0], *EXCHANGE, MESSAGES[1]]


class ConversationTest(HostedCase):
    """The earlier exchange of a conversation goes with the question: to ask.prepare, then to the chat server."""

    def test_the_history_of_the_request_goes_to_prepare(self):
        self.ask(history=EXCHANGE)
        self.assertEqual(self.prepare.call_args.kwargs["history"], EXCHANGE)

    def test_no_history_is_an_empty_exchange(self):
        self.ask()
        self.assertEqual(self.prepare.call_args.kwargs["history"], [])

    def test_lm_studio_gets_the_exchange_folded_into_the_input(self):
        self.prepare.return_value = (self.sources, WITH_HISTORY)
        self.ask(history=EXCHANGE)
        body = self.fake.last_chat_body
        self.assertEqual(body["system_prompt"], SYSTEM_TEXT)
        self.assertEqual(
            body["input"],
            server.HISTORY_HEADING + "\n\nQuestion: q1\n\nAnswer: a1 [2]\n\n" + server.HISTORY_END + "\n" + USER_TEXT,
        )
        self.assertNotIn("messages", body)

    def test_an_openai_style_server_gets_the_exchange_as_messages(self):
        self.prepare.return_value = (self.sources, WITH_HISTORY)
        self.ask(chat_server=self.hosted, chat_api="openai", model="ark-code-latest", history=EXCHANGE)
        self.assertEqual(self.fake.last_chat_body["messages"], WITH_HISTORY)

    def test_folded_input_without_an_exchange_is_the_user_message_alone(self):
        self.assertEqual(server.folded_input(MESSAGES[1:]), USER_TEXT)

    def test_a_bad_history_is_a_400_that_says_so(self):
        for history in ("q", [{"role": "system", "content": "x"}], [{"role": "user"}], [{"content": "x", "role": "bot"}], [1]):
            status, payload = self.ask(history=history)
            self.assertEqual(status, 400, history)
            self.assertIn("history", payload["error"])

    def test_too_long_a_history_is_a_400(self):
        status, payload = self.ask(history=EXCHANGE * (server.ask.MAX_HISTORY_MESSAGES // 2 + 1))
        self.assertEqual((status, "history" in payload["error"]), (400, True))
        status, payload = self.ask(history=[{"role": "user", "content": "x" * (server.MAX_HISTORY_CHARS + 1)}])
        self.assertEqual((status, "history" in payload["error"]), (400, True))


class PagePartsTest(ServerCase):
    """The page is index.html with its styles and its script in files of their own, and the three agree."""

    def setUp(self):
        super().setUp()
        self.page = server.PAGE_FILE.read_text(encoding="utf-8")
        self.script = server.PAGE_PARTS["/chatbot.js"][0].read_text(encoding="utf-8")
        self.styles = server.PAGE_PARTS["/chatbot.css"][0].read_text(encoding="utf-8")

    def test_the_stylesheet_and_the_script_are_served_with_their_types(self):
        for path, content_type in (("/chatbot.css", server.CSS_TYPE), ("/chatbot.js", server.SCRIPT_TYPE)):
            status, headers, body = self.get(path)
            self.assertEqual((status, headers["Content-Type"], headers["Cache-Control"]), (200, content_type, server.NO_CACHE), path)
            self.assertEqual(body, server.PAGE_PARTS[path][0].read_bytes(), path)
        self.assertTrue(self.script.startswith('"use strict";'))

    def test_nothing_else_of_the_folder_is_served(self):
        for path in ("/server.py", "/index.html", "/nope.js", "/tests/fake_lmstudio.py", "/tests/chatbot.js", "/../ask.py", "/chatbot.js/", "/CHATBOT.JS", "/chatbot.js%00"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_a_request_from_another_site_gets_no_part(self):
        for path in server.PAGE_PARTS:
            for headers in ({"Sec-Fetch-Site": "cross-site"}, {"Origin": "http://evil.example"}, {"Host": "evil.example"}):
                self.assertEqual(self.get(path, headers)[0], 403, (path, headers))

    def test_no_reply_may_be_sniffed_into_another_type(self):
        image = "/image?" + urllib.parse.urlencode({"p": "pics/a.png"})
        for path in ("/", "/chatbot.js", "/chatbot.css", "/api/settings", image, "/nope"):
            self.assertEqual(self.get(path)[1]["X-Content-Type-Options"], "nosniff", path)
        request = urllib.request.Request(self.base + "/api/ask", json.dumps({"question": "x", "model": MODEL_ID, "chat_server": self.fake.url}).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as stream:
            self.assertEqual(stream.headers["X-Content-Type-Options"], "nosniff")
            stream.read()

    def test_the_page_loads_both_and_holds_no_script_or_style_of_its_own(self):
        page = self.get("/")[2].decode()
        self.assertEqual(page, self.page)
        self.assertIn('<link rel="stylesheet" href="chatbot.css">', page)
        self.assertEqual(re.findall(r"<script\b[^>]*>", page), ['<script src="chatbot.js">'])
        self.assertEqual(page.count("</script>"), 1)
        self.assertRegex(page, r'<script src="chatbot\.js"></script>\s*</body>\s*</html>\s*\Z')  # last, so that every element exists when it runs
        for inline in (r"<style", r"\sstyle\s*=", r"\son\w+\s*=", r"javascript:", r"use strict"):
            self.assertIsNone(re.search(inline, page, re.IGNORECASE), inline)

    def test_every_part_the_server_names_is_a_file_the_page_asks_for(self):
        for path, (file, _) in server.PAGE_PARTS.items():
            self.assertTrue(file.is_file(), path)
            self.assertIn('"' + path.lstrip("/") + '"', self.page)

    def test_the_script_does_not_write_html_inline_styles_or_code(self):
        for banned in (r"innerHTML", r"outerHTML", r"insertAdjacentHTML", r"document\.write", r"setAttribute\(\s*[\"'](style|on)", r"cssText", r"\beval\(", r"new Function", r"set(Timeout|Interval)\(\s*[\"'`]"):
            self.assertIsNone(re.search(banned, self.script), banned)

    def test_every_id_the_script_uses_is_in_the_page_once(self):
        ids = re.findall(r'\bid="([^"]+)"', self.page)
        self.assertEqual(sorted(ids), sorted(set(ids)), "an id is used twice")
        used = set(re.findall(r'\$\("([^"]+)"\)', self.script)) | set(re.findall(r'getElementById\("([^"]+)"\)', self.script))
        used |= {name for names in re.findall(r"for \(const id of \[([^\]]+)\]\)", self.script) for name in re.findall(r'"([^"]+)"', names)}
        used |= set(re.findall(r'"([^"]+)"', re.search(r"const FIELDS = \[([^\]]+)\]", self.script).group(1)))
        self.assertGreater(len(used), 20)
        self.assertEqual(sorted(used - set(ids)), [])

    def test_both_new_chat_buttons_stop_a_running_answer(self):
        handler = re.search(r'for \(const id of \["new-chat", "clear"\]\) \{.*?\n\}', self.script, re.DOTALL).group(0)
        self.assertLess(handler.index("controller.abort()"), handler.index("newConversation()"))
        self.assertNotIn('$("new-chat").addEventListener("click", newConversation)', self.script)

    def test_the_buttons_that_show_only_an_icon_have_a_name(self):
        for button in re.findall(r"<button\b[^>]*>", self.page):
            if 'class="ghost' in button or "round" in button:
                self.assertIn("aria-label=", button, button)
        self.assertEqual(len(re.findall(r"<svg\b", self.page)), len(re.findall(r'<svg\b[^>]*aria-hidden="true"', self.page)))

    def test_the_height_the_question_box_grows_to_is_the_same_in_both_files(self):
        height = re.search(r"const MAX_INPUT_PX = (\d+);", self.script).group(1)
        self.assertIn(f"max-height: {height}px", self.styles)

    def test_every_colour_has_a_dark_value(self):
        blocks = re.findall(r":root \{(.*?)\n\s*\}", self.styles, re.DOTALL)
        light, dark = (set(re.findall(r"(--[\w-]+):\s*(?:#|rgba)", block)) for block in blocks[:2])
        self.assertGreater(len(light), 12)
        self.assertEqual(sorted(light - dark), [])

    def test_every_class_of_the_page_and_the_script_has_a_style(self):
        classes = {name for value in re.findall(r'\bclass="([^"]+)"', self.page) for name in value.split()}
        classes |= {name for value in re.findall(r'\bel\("\w+", "([^"]+)"', self.script) for name in value.split()}
        classes |= {name for value in re.findall(r'className = "([^"]+)"', self.script) for name in value.split()}
        classes |= set(re.findall(r'classList\.\w+\("([^"]+)"\)', self.script))
        self.assertGreater(len(classes), 30)
        styled = set(re.findall(r"\.([a-zA-Z][\w-]*)", re.sub(r"/\*.*?\*/", "", self.styles, flags=re.DOTALL)))
        self.assertEqual(sorted(classes - styled), [])

    def test_the_stylesheet_is_whole_and_loads_nothing_from_elsewhere(self):
        self.assertEqual(self.styles.count("{"), self.styles.count("}"))
        for banned in (r"url\(", r"@import", r"@font-face", r"expression\("):  # the policy allows no font and no data: image
            self.assertIsNone(re.search(banned, self.styles), banned)
        self.assertIn("[hidden] { display: none !important; }", self.styles)  # the script hides the Stop button, the bar and the viewer with the attribute
        for scheme in ("prefers-color-scheme: dark", "prefers-reduced-motion: reduce"):
            self.assertIn(scheme, self.styles)

    def test_the_script_shows_what_the_server_says_of_a_source_and_knows_no_status_itself(self):
        for field in ("reference", "pages", "citation", "status_note"):
            self.assertIn("s." + field, self.script, field)
        for value in ("current", "unknown", *server.ask.STATUS_WORDS, *server.ask.STATUS_WORDS.values()):
            self.assertNotIn('"' + value + '"', self.script, value)

    def test_the_width_at_which_the_sidebar_lies_over_the_chat_is_the_same_in_both_files(self):
        narrow = re.search(r'const NARROW_SCREEN = "([^"]+)"', self.script).group(1)
        self.assertIn("@media " + narrow, self.styles)

if __name__ == "__main__":
    unittest.main()
