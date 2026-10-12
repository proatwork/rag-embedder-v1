"""Tests of the chat server options of ask.py: the endpoint path and the API key of a hosted API.

A stand-in OpenAI-style server on this machine plays the hosted API; no real server and no model is used.
Run from the project folder: venvs/bin/python -m unittest discover -s tests -v
"""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import ask
import settings
from pipeline_common import ConversionError

KEY = "secret-key-123"
VARIABLE = "TEST_CHAT_KEY"
MESSAGES = [{"role": "system", "content": "rules"}, {"role": "user", "content": "question"}]


class StandIn(ThreadingHTTPServer):
    """Answers POST /chat/completions like a hosted API: it wants a Bearer key, and names the model an alias resolved to."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.seen = []  # (path, Authorization header, body)
        self.moved_to = ""  # where /api/coding/v3/moved sends the client

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/api/coding/v3"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append((self.path, self.headers.get("Authorization"), body))
        if self.path == "/api/coding/v3/echo":  # a server that repeats the key it was given in its error
            status, reply = 400, {"error": {"message": f"bad key {self.headers.get('Authorization')}"}}
        elif self.path == "/api/coding/v3/moved":
            status, reply = 307, {}
        elif self.path != "/api/coding/v3/chat/completions":
            status, reply = 404, {"error": {"message": "no such path"}}
        elif self.headers.get("Authorization") != f"Bearer {KEY}":
            status, reply = 401, {"error": {"message": "invalid api key"}}
        else:
            status, reply = 200, {"model": "resolved-model-name", "choices": [{"message": {"content": "Răspuns [1]"}, "finish_reason": "stop"}]}
        data = json.dumps(reply).encode()
        self.send_response(status)
        if status == 307:
            self.send_header("Location", self.server.moved_to)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ChatOptionsTest(unittest.TestCase):
    def setUp(self):
        self.api = StandIn()
        threading.Thread(target=self.api.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(self.api.server_close)
        self.addCleanup(self.api.shutdown)

    def test_the_path_and_the_key_are_used_and_the_resolved_model_name_is_accepted(self):
        text, cut = ask.complete(self.api.url, "my-alias", MESSAGES, "/chat/completions", KEY)
        self.assertEqual((text, cut), ("Răspuns [1]", False))
        path, authorization, body = self.api.seen[0]
        self.assertEqual((path, authorization), ("/api/coding/v3/chat/completions", f"Bearer {KEY}"))
        self.assertEqual((body["model"], body["messages"]), ("my-alias", MESSAGES))

    def test_the_default_path_is_the_openai_one_and_this_server_has_no_such_path(self):
        with self.assertRaisesRegex(ConversionError, "404"):
            ask.complete(self.api.url, "m", MESSAGES, api_key=KEY)
        self.assertEqual(self.api.seen[0][0], "/api/coding/v3/v1/chat/completions")

    def test_a_wrong_key_is_refused_with_the_servers_reason(self):
        with self.assertRaisesRegex(ConversionError, "401.*invalid api key"):
            ask.complete(self.api.url, "m", MESSAGES, "/chat/completions", "wrong")

    def test_without_a_key_nothing_is_sent_and_the_model_is_compared(self):
        with self.assertRaisesRegex(ConversionError, "401"):
            ask.complete(self.api.url, "m", MESSAGES, "/chat/completions")
        self.assertIsNone(self.api.seen[0][1])

    def test_the_key_never_appears_in_an_error_message(self):
        with self.assertRaises(ConversionError) as caught:
            ask.complete(self.api.url, "m", MESSAGES, "/nope", KEY)
        self.assertNotIn(KEY, str(caught.exception))

    def test_a_key_the_server_echoes_in_its_error_is_hidden(self):
        with self.assertRaises(ConversionError) as caught:
            ask.complete(self.api.url, "m", MESSAGES, "/echo", KEY)
        self.assertIn("bad key", str(caught.exception))
        self.assertNotIn(KEY, str(caught.exception))

    def test_a_redirect_is_not_followed_so_the_key_goes_nowhere_else(self):
        other = StandIn()
        threading.Thread(target=other.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        self.api.moved_to = other.url + "/chat/completions"
        with self.assertRaisesRegex(ConversionError, "redirected to .*not followed"):
            ask.complete(self.api.url, "m", MESSAGES, "/moved", KEY)
        self.assertEqual(other.seen, [])  # nothing ever reached the other server


class ApiKeyTest(unittest.TestCase):
    def setUp(self):  # no settings file, unless a test names one: this machine's own may hold a key under any name
        patcher = mock.patch.dict(os.environ, {settings.FILE_VARIABLE: ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_variable_means_no_key(self):
        self.assertIsNone(ask.api_key_from(None))

    def test_the_key_is_read_from_the_environment_and_trimmed(self):
        with mock.patch.dict(os.environ, {VARIABLE: f"  {KEY}\n"}):
            self.assertEqual(ask.api_key_from(VARIABLE), KEY)

    def test_the_key_is_read_from_the_settings_file_when_the_environment_has_none(self):
        with TemporaryDirectory() as folder:
            file = Path(folder) / "settings.env"
            file.write_text(f"{VARIABLE}={KEY}\n", encoding=settings.TEXT_ENCODING)
            with mock.patch.dict(os.environ, {settings.FILE_VARIABLE: str(file)}):
                os.environ.pop(VARIABLE, None)
                self.assertEqual(ask.api_key_from(VARIABLE), KEY)
                with mock.patch.dict(os.environ, {VARIABLE: KEY + "-of-the-environment"}):
                    self.assertEqual(ask.api_key_from(VARIABLE), KEY + "-of-the-environment")

    def test_a_key_with_spaces_or_line_breaks_is_refused(self):
        for bad in ("two words", "line\nbreak", "tab\tinside", "clé"):
            with mock.patch.dict(os.environ, {VARIABLE: bad}), self.assertRaisesRegex(ConversionError, VARIABLE):
                ask.api_key_from(VARIABLE)

    def test_an_unset_or_empty_variable_is_an_error_that_names_it(self):
        with mock.patch.dict(os.environ, {VARIABLE: " "}), self.assertRaisesRegex(ConversionError, VARIABLE):
            ask.api_key_from(VARIABLE)
        with mock.patch.dict(os.environ, clear=False):
            os.environ.pop(VARIABLE, None)
            with self.assertRaisesRegex(ConversionError, VARIABLE):
                ask.api_key_from(VARIABLE)


if __name__ == "__main__":
    unittest.main()
