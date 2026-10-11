"""A stand-in for LM Studio's server, for the chatbot's tests and for trying the page without any model.

It lists models and answers POST /api/v1/chat with a scripted stream of events; it never runs a model. It also plays a
hosted OpenAI-style API under OPENAI_PREFIX: POST <OPENAI_PREFIX>/chat/completions streams scripted chunks, the same
ones for every request (openai_chunks) or others for each request in turn (openai_rounds), which is how a model that
calls a tool and then answers is played (tool_call_chunks, then openai_chunks).

Run it by hand:  venvs/bin/python chatbot/tests/fake_lmstudio.py [port]   (default 1235)
Run so, a request to the OpenAI-style API (chat server http://127.0.0.1:1235/api/coding/v3 on the page) that carries
tools is answered with one call of the first of them, for the question's own words, and then, when the request holds
what the call gave, with an answer: the page's search by the model can be tried without one.
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = 1235
POLL_SECONDS = 0.01  # how often serve_forever looks for a stop request, so that a test ends at once
MODEL_ID = "fake-model"

DEFAULT_MODELS = [
    {"id": "text-embedding-fake", "type": "embeddings", "state": "loaded", "loaded_context_length": 2048},
    {"id": "other-model", "type": "llm", "state": "not-loaded", "loaded_context_length": None},
    {"id": MODEL_ID, "type": "llm", "state": "loaded", "loaded_context_length": 8192},
]


def event(kind: str, **fields) -> tuple[str, dict]:
    return kind, {"type": kind, **fields}


def default_script(model: str = MODEL_ID, answer: str = "Pereții antifoc trebuie să reziste minimum o oră [1], conform normei [2].") -> list[tuple[str, dict]]:
    script = [
        event("chat.start", model_instance_id=model),
        event("prompt_processing.start"), event("prompt_processing.progress", progress=0.4),
        event("prompt_processing.progress", progress=1), event("prompt_processing.end"),
        event("reasoning.start"), event("reasoning.delta", content="Citesc"), event("reasoning.delta", content=" extrasele."), event("reasoning.end"),
        event("message.start"),
    ]
    script += [event("message.delta", content=word + " ") for word in answer.split(" ")]
    script += [
        event("message.end"),
        event("chat.end", result={"model_instance_id": model, "output": [], "stats": {
            "input_tokens": 900, "total_output_tokens": 30, "reasoning_output_tokens": 2, "tokens_per_second": 44.5,
            "time_to_first_token_seconds": 0.21,
        }}),
    ]
    return script


OPENAI_PREFIX = "/api/coding/v3"  # where the OpenAI-style API lives, as on a hosted service
OPENAI_MODEL = "resolved-model"  # the name the OpenAI-style API answers under
FINISH_STOP = "stop"
FINISH_TOOL_CALLS = "tool_calls"  # how a round that calls tools ends
BEFORE_CALLS = "\n\n"  # the content LM Studio sends between a model's thinking and its tool calls
ROLE_TOOL = "tool"  # a message of a request that holds what a tool call gave
DEMO_CALL_ID = "call-by-hand"  # of the one call made when the fake is run by hand
DEMO_ANSWER = "Răspuns scris după o căutare [1]."


def delta_chunk(model: str, delta: dict, finish: str | None = None) -> dict:
    """One chunk of an OpenAI-style stream that carries a piece of the reply, or its end (`finish`)."""
    return {"model": model, "choices": [{"delta": delta, **({"finish_reason": finish} if finish else {})}]}


def opening_chunks(model: str, reasoning: str) -> list[dict]:
    """How every OpenAI-style stream begins: the role, then the model's thinking, a word at a time."""
    return [delta_chunk(model, {"role": "assistant"})] + [delta_chunk(model, {"reasoning_content": piece}) for piece in reasoning.split(" ") if piece]


def closing_chunks(model: str, finish: str | None, usage: bool) -> list[dict]:
    """How an OpenAI-style stream ends: the finish marker, then the token counts (each only when asked for)."""
    chunks = [delta_chunk(model, {}, finish)] if finish else []
    if usage:
        chunks.append({"model": model, "choices": [], "usage": {"prompt_tokens": 700, "completion_tokens": 12, "completion_tokens_details": {"reasoning_tokens": 1}}})
    return chunks


def openai_chunks(
    answer: str = "Răspuns hosted [1].", model: str = OPENAI_MODEL, reasoning: str = "gândesc", usage: bool = True, finish: str | None = FINISH_STOP,
) -> list[dict]:
    """The chunks of an OpenAI-style stream (what a hosted chat/completions endpoint sends)."""
    answered = [delta_chunk(model, {"content": word + " "}) for word in answer.split(" ")]
    return opening_chunks(model, reasoning) + answered + closing_chunks(model, finish, usage)


def tool_call_chunks(calls: list[tuple[str, str, dict | str]], model: str = OPENAI_MODEL, reasoning: str = "caut", usage: bool = True) -> list[dict]:
    """The chunks of an OpenAI-style stream in which the model calls tools instead of answering, as LM Studio sends
    them: after the thinking, each call's id and name with empty arguments, then its arguments in two pieces, all
    under the call's index; then the finish marker of a round of calls. `calls` are (id, name, arguments), the
    arguments an object, or the text to send as it is (JSON, or what a model wrote in its place)."""
    chunks = opening_chunks(model, reasoning) + [delta_chunk(model, {"content": BEFORE_CALLS})]
    for index, (call_id, name, arguments) in enumerate(calls):
        written = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
        half = len(written) // 2
        chunks.append(delta_chunk(model, {"tool_calls": [{"index": index, "id": call_id, "type": "function", "function": {"name": name, "arguments": ""}}]}))
        chunks += [delta_chunk(model, {"tool_calls": [{"index": index, "type": "function", "function": {"arguments": piece}}]}) for piece in (written[:half], written[half:])]
    return chunks + closing_chunks(model, FINISH_TOOL_CALLS, usage)


def by_hand_chunks(body: dict) -> list[dict]:
    """What a request with tools is answered when the fake is run by hand: a call of the first tool for the words of
    the last message, or, once the request holds what a call gave, an answer."""
    messages = body.get("messages") or []
    if any(message.get("role") == ROLE_TOOL for message in messages):
        return openai_chunks(DEMO_ANSWER)
    return tool_call_chunks([(DEMO_CALL_ID, body["tools"][0]["function"]["name"], {"query": messages[-1]["content"] if messages else ""})])


class FakeLMStudio(ThreadingHTTPServer):
    """Settings are attributes: models, openai_models (None: /v1/models is not served), script, delay (seconds between
    events), chat_status (an HTTP error code for /api/v1/chat), last_chat_body (what was posted); those of the
    OpenAI-style API are explained where they are set."""

    def __init__(self, port: int = 0):
        super().__init__(("127.0.0.1", port), _Handler)
        self.models = list(DEFAULT_MODELS)
        self.openai_models = None
        self.script = default_script()
        self.delay = 0.0
        self.chat_status = None
        self.last_chat_body = None
        self.events_sent = 0  # how many events of the script (or chunks of the OpenAI-style stream) were written, to see a stream being cut
        self.openai_chunks = openai_chunks()  # what POST <OPENAI_PREFIX>/chat/completions streams
        # When set, what it streams instead, one entry for each request in turn (the last one again for any request after
        # it): the chunks of a round, or (HTTP status, message) for a request that is refused with that error.
        self.openai_rounds = None
        self.openai_asked = 0  # how many chat requests the OpenAI-style API answered or refused by its script
        self.plays_tools = False  # run by hand: a request with tools gets a call, then an answer (by_hand_chunks)
        self.required_key = None  # when set, the OpenAI-style paths want "Authorization: Bearer <this>"
        self.openai_model_ids = ["ark-code-latest", "text-embedding-x"]  # GET <OPENAI_PREFIX>/models; None: no such path
        self.seen = []  # (path, Authorization header, body) of every request
        self.openai_raw = None  # (content type, body) sent instead of a stream by POST <OPENAI_PREFIX>/chat/completions
        self.models_raw = None  # (content type, body) sent instead of the model list by GET <OPENAI_PREFIX>/models
        self.stream_type = "text/event-stream"  # the content type of the OpenAI-style stream ("" sends none)
        self.redirect_to = None  # when set, every OpenAI-style path answers 307 with this Location

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def start(self) -> "FakeLMStudio":
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": POLL_SECONDS}, daemon=True).start()
        return self


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def send_json(self, status: int, payload) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_raw(self, status: int, content_type: str, body: str) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def keyed(self) -> bool:
        """Whether a request may use an OpenAI-style path; answers 401 when it may not (or redirects, when scripted to)."""
        self.server.seen.append((self.path, self.headers.get("Authorization"), None))
        if self.server.redirect_to:
            self.send_response(307)
            self.send_header("Location", self.server.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        if self.server.required_key and self.headers.get("Authorization") != f"Bearer {self.server.required_key}":
            self.send_json(401, {"error": {"message": "invalid api key"}})
            return False
        return True

    def do_GET(self):
        if self.path == OPENAI_PREFIX + "/models" and self.server.openai_model_ids is not None:
            if self.keyed():
                if self.server.models_raw:
                    self.send_raw(200, *self.server.models_raw)
                else:
                    self.send_json(200, {"data": [{"id": name} for name in self.server.openai_model_ids]})
            return
        if self.path == "/api/v0/models" and self.server.models is not None:
            return self.send_json(200, {"data": self.server.models})
        if self.path == "/v1/models" and self.server.openai_models is not None:
            return self.send_json(200, {"data": [{"id": name} for name in self.server.openai_models]})
        self.send_json(404, {"error": "Unexpected endpoint or method."})

    def do_POST(self):
        if self.path == OPENAI_PREFIX + "/chat/completions":
            return self.openai_chat()
        if self.path != "/api/v1/chat":
            self.server.seen.append((self.path, self.headers.get("Authorization"), None))
            return self.send_json(404, {"error": "Unexpected endpoint or method."})
        self.server.events_sent = 0
        self.server.last_chat_body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append((self.path, self.headers.get("Authorization"), self.server.last_chat_body))
        if self.server.chat_status:
            return self.send_json(self.server.chat_status, {"error": {"type": "invalid_request", "message": "scripted failure"}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for kind, data in self.server.script:
                self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode())
                self.wfile.flush()
                self.server.events_sent += 1
                time.sleep(self.server.delay)
        except (BrokenPipeError, ConnectionResetError):
            pass


    def round_chunks(self, body: dict) -> list | tuple:
        """What this chat request of the OpenAI-style API is answered: the chunks to stream, or (HTTP status, message)."""
        rounds, asked = self.server.openai_rounds, self.server.openai_asked
        self.server.openai_asked += 1
        if self.server.plays_tools and body.get("tools"):
            return by_hand_chunks(body)
        return rounds[min(asked, len(rounds) - 1)] if rounds else self.server.openai_chunks

    def openai_chat(self):
        self.server.last_chat_body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self.keyed():
            return
        self.server.seen[-1] = (self.path, self.headers.get("Authorization"), self.server.last_chat_body)
        if self.server.openai_raw:
            return self.send_raw(200, *self.server.openai_raw)
        chunks = self.round_chunks(self.server.last_chat_body)
        if isinstance(chunks, tuple):
            status, message = chunks
            return self.send_json(status, {"error": {"message": message}})
        self.send_response(200)
        if self.server.stream_type:
            self.send_header("Content-Type", self.server.stream_type)
        self.end_headers()
        self.server.events_sent = 0
        try:
            for chunk in chunks:
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
                self.server.events_sent += 1
                time.sleep(self.server.delay)
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    fake = FakeLMStudio(int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT)
    fake.delay = 0.05
    fake.plays_tools = True
    print(f"fake LM Studio on {fake.url} (model {MODEL_ID}); no model runs. Ctrl+C stops it", flush=True)
    try:
        fake.serve_forever()
    except KeyboardInterrupt:
        pass
