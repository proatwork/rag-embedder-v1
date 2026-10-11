"""A chat page for the norms: ask a question in the browser, watch the answer being written, see the chunks behind it.

Usage (run with the virtual environment's interpreter, from the project folder):
    venvs/bin/python chatbot/server.py                      # then open http://127.0.0.1:8765
    venvs/bin/python chatbot/server.py --port 9000 --chunks-dir CHUNKS --vectors-dir VECTORS

Every question goes through ask.py, in one of two ways, which the page chooses (its Search field):

- once: prepare() embeds the question on the embedding server, finds the closest chunks and builds the prompt with the
  rules of ask-instructions.md. The model is handed those chunks and writes the answer in one go.
- model: the model searches for itself. It is given the tool of search_tool.py (search_norms) and the rules of
  ask-tool-instructions.md, and is asked round after round: in a round it either calls the tool, in its own words or
  for an article by its number (up to search_tool.MAX_SEARCHES calls for one question), or writes the answer from what
  the calls gave it. The page is shown every round as it is written, every search as it was run, and the chunks found
  so far. Only an OpenAI-style chat server takes tools: with LM Studio's native API the question is searched once. A
  model that answers without searching, writes its call as text or will not stop searching is not taken at its word:
  the page is told to drop the answer and the sources it showed (the list of searches stays), and the question is answered the first way.

The answer is written by the chat model, streamed from the chat server's native API (LM Studio's POST /api/v1/chat), so
the page can show the model loading, the prompt being read, the model thinking and the answer as it arrives. An
OpenAI-style API (POST .../chat/completions, with an API key when it is a hosted one) can write it instead; it streams
too, but sends no prompt or loading progress. The servers, the API style, the key, the chat model, how many chunks a
search gives (top) and the norm (doc) are fields of the page; the chat model is picked from the list the chat server
gives, or typed when it gives none.

The page is three files beside this script: index.html, chatbot.css and chatbot.js. They are read at every request, so
an edit shows on reload. The page listens on this machine only, and only answers requests that come from its own page.
The chunks and vectors are loaded once, when the script starts.
"""

import argparse
import contextlib
import http.client
import ipaddress
import json
import mimetypes
import os
import select
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))  # ask.py and the modules it uses live in the project folder

import ask  # noqa: E402
import search_tool  # noqa: E402
from embed_chunks import DEFAULT_SERVER as DEFAULT_EMBEDDING_SERVER  # noqa: E402
from embed_chunks import OPENER, REDIRECT_CODES  # noqa: E402
from json_to_chunks import CONFIG_FILE, load_config  # noqa: E402
from pipeline_common import ConversionError  # noqa: E402
from search import (  # noqa: E402
    DEFAULT_TOP, IMAGE_FILE_KEY, Index, applies_as_written, breadcrumb, citation, load_chunks, load_index, pages_note, reference,
    status_words,
)

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_CHAT_SERVER = "http://192.168.0.157:1234"
PAGE_FILE = Path(__file__).with_name("index.html")
TEXT_ENCODING = ask.TEXT_ENCODING
MAX_TOP = 50
MAX_BODY_BYTES = 100_000
LIST_TIMEOUT = 10  # seconds, to list the models of a server
STREAM_TIMEOUT = 600  # seconds to wait for the next event of a stream: a big model reads its prompt for a while
ERROR_DETAIL_BYTES = 500  # of a chat server's error reply shown on the page
MAX_REPLY_BYTES = 1_000_000  # of a reply that is not a stream (a whole answer as JSON)
SNIFF_BYTES = 16  # how much of a reply is looked at to tell a stream from anything else
STREAM_STARTS = (b"data:", b"event:", b":")  # what a server-sent event stream begins with
MIN_SPEED_SECONDS = 0.5  # a speed is worked out only from a stream that took at least this long to arrive
MIN_SPEED_PIECES = 5
FETCH_SITE_OK = ("same-origin", "none")  # the Sec-Fetch-Site values of a request made by this page (or typed into the address bar)
FINISH_LENGTH = "length"  # an OpenAI-style stream that ran out of room
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
# The files the page loads beside itself, by the path they are asked at, with their content types; nothing else in this folder is served.
CSS_TYPE = "text/css; charset=utf-8"
SCRIPT_TYPE = "text/javascript; charset=utf-8"
PAGE_PARTS = {"/chatbot.css": (PAGE_FILE.with_name("chatbot.css"), CSS_TYPE), "/chatbot.js": (PAGE_FILE.with_name("chatbot.js"), SCRIPT_TYPE)}
NO_SNIFFING = {"X-Content-Type-Options": "nosniff"}  # a reply is what its content type says: an image or JSON is never run as a script

# The routes of this server, and the keys of their query strings.
ROUTE_PAGE = "/"
ROUTE_SETTINGS = "/api/settings"
ROUTE_MODELS = "/api/models"
ROUTE_IMAGE = "/image"
ROUTE_ASK = "/api/ask"
QUERY_SERVER = "server"
QUERY_API = "api"
QUERY_PATH = "p"
HTML_TYPE = "text/html; charset=utf-8"
JSON_TYPE = "application/json"
JSON_REPLY_TYPE = JSON_TYPE + "; charset=utf-8"
STREAM_MIME = "text/event-stream"
STREAM_TYPE = STREAM_MIME + "; charset=utf-8"
NO_CACHE = "no-store"
# The page may talk only to this server and load nothing from anywhere else, so that even a script that got in could not send a key out.
# Its script and styles are files of their own (PAGE_PARTS): nothing written into the page itself is run or applied.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
    "form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)

# The two kinds of chat server. LM Studio's native API (lmstudio.ai/docs/developer/rest) streams the model loading and the
# prompt being read as well; an OpenAI-style API (chat completions) is what hosted services offer.
API_LMSTUDIO = "lmstudio"
API_OPENAI = "openai"
APIS = (API_LMSTUDIO, API_OPENAI)
LIST_PATH = "/api/v0/models"  # lists the models with their type and state
V1_LIST_PATH = "/v1/models"  # the fallback, which has only ids
NATIVE_CHAT_PATH = "/api/v1/chat"
OPENAI_LIST_PATH = "/models"  # on the base address of an OpenAI-style API, which ends in its version (/v1, /v3)
OPENAI_CHAT_PATH = "/chat/completions"
KEY_HEADER = "X-Chat-Api-Key"  # how the page sends the key when it asks for a model list (a query string would be logged)
AUTHORIZATION = ask.AUTHORIZATION
BEARER = ask.BEARER
MAX_KEY_CHARS = 1000
MAX_PATH_CHARS = 200
REDACTED = ask.KEY_HIDDEN
TYPE_EMBEDDINGS = "embeddings"
EMBEDDING_NAME_MARK = "embed"  # an id with this in it is taken for an embedding model when the server does not say
STATE_LOADED = "loaded"
STORE_CHAT = False  # do not keep the conversation on the chat server
MAX_CALL_CHARS = 10_000  # of the arguments of one tool call gathered from a stream; what comes after is dropped, and the call is then refused as not JSON
MAX_HISTORY_CHARS = 60_000  # of the earlier exchange a request may carry; the page sends far less
# LM Studio's own API takes one input text, not messages: the earlier exchange is written into it under these lines.
HISTORY_HEADING = "Earlier exchange (for context; the excerpts below are the only source):"
HISTORY_LABELS = {ask.ROLE_USER: "Question", ask.ROLE_ASSISTANT: "Answer"}
HISTORY_END = "Now:"
# How the chunks for a question are found (the request's "search"): the model searches for them itself, with the tool of
# search_tool.py, or they are searched for once, with the question's own words. Only an OpenAI-style server takes tools.
SEARCH_MODEL = "model"
SEARCH_ONCE = "once"
SEARCH_MODES = (SEARCH_MODEL, SEARCH_ONCE)
SSE_DATA = "data:"
# Its streaming events, and the fields they use.
LMS_CHAT_START = "chat.start"
LMS_CHAT_END = "chat.end"
LMS_MODEL_LOAD = "model_load."
LMS_MODEL_LOAD_END = "model_load.end"
LMS_PROMPT_PROGRESS = "prompt_processing.progress"
LMS_REASONING = "reasoning.delta"
LMS_MESSAGE = "message.delta"
LMS_ERROR = "error"
KEY_TYPE = "type"
KEY_CONTENT = "content"
KEY_PROGRESS = "progress"
KEY_INSTANCE = "model_instance_id"
KEY_RESULT = "result"
KEY_STATS = "stats"
KEY_INDEX = "index"  # of a piece of a tool call in an OpenAI-style stream: which of the round's calls it belongs to
KEY_CALLS = ask.API_TOOL_CALLS  # of a chat.end's result: the tool calls the model made, as an OpenAI-style message holds them
STATS_APPROXIMATE = "approximate"  # the token counts were estimated from the pieces received: the server sent no usage
STATS_CUT = "cut"  # the model ran out of room: the answer is not whole
STATS_INCOMPLETE = "incomplete"  # the stream ended with no finish marker: the answer may not be whole

# What the page is sent: one server-sent event per item, named as below.
EVENT_STATUS = "status"
EVENT_SOURCES = "sources"
EVENT_SEARCH = "search"  # one search the model made, as it was run
EVENT_FALLBACK = "fallback"  # the model did not search as it was to: what was shown so far is void, the chunks are found for it
EVENT_MODEL_LOAD = "model_load"
EVENT_PROMPT = "prompt"
EVENT_REASONING = "reasoning"
EVENT_MESSAGE = "message"
EVENT_NOTICE = "notice"
EVENT_DONE = "done"
EVENT_ERROR = "error"
STATUS_SEARCHING = "searching"
STATUS_WAITING = "waiting"

BAD_ADDRESS = "{address!r} is not a server address like " + DEFAULT_CHAT_SERVER
API_STYLE_MESSAGE = f"the API style must be one of: {', '.join(APIS)}"
SEARCH_MODE_MESSAGE = f"search must be one of: {', '.join(SEARCH_MODES)}"
NO_TOOLS_HINT = " (if this model or server takes no tools, set Search to once)"  # after a chat server's refusal of a request because of its tools
WRONG_BASE = "Unexpected endpoint"  # what LM Studio answers to an OpenAI-style request that is not under /v1
WRONG_BASE_HINT = " (LM Studio's OpenAI-style API is under /v1: end the chat server's address with /v1)"


class UpstreamError(Exception):
    """A server this one talks to cannot be used; the message says why, for the page."""


class ClientGone(Exception):
    """The browser closed the connection (the stop button, or a closed tab)."""


class ChatFailed(Exception):
    """The chat server reported an error in a round with a model that searches; the page was told as it came."""


@dataclass
class AppState:
    """What the page needs, loaded once."""
    index: Index
    config: dict
    instructions: str
    docs: list[str]  # the norms' codes
    image_root: Path  # images are served from inside this folder only
    embedding_server: str  # the defaults of the page's fields
    chat_server: str
    tool_instructions: str = ""  # what a model that searches for itself is told (ask.tool_instructions)


@dataclass
class Rounds:
    """What is kept of the rounds with the chat model for one question, from one to the next: a model that searches for
    itself is asked round after round, and once more when it did not search and is handed the chunks."""
    asked: int = 0  # how many rounds were streamed
    stats: dict = field(default_factory=dict)  # of the last one


# ---- the servers ----------------------------------------------------------------------------------------------------

def normalize_server(address: str) -> str:
    """The address of a server without a trailing slash: http(s)://host[:port], and a path when the API lives under one
    (https://host/api/coding/v3). Raises UpstreamError when it is anything else."""
    address = (address or "").strip().rstrip("/")
    try:
        parts = urllib.parse.urlsplit(address)
        parts.port  # noqa: B018 - raises ValueError for a port that is not a number
    except ValueError as error:
        raise UpstreamError(BAD_ADDRESS.format(address=address)) from error
    if (
        parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment
        or any(character.isspace() for character in address)
    ):
        raise UpstreamError(BAD_ADDRESS.format(address=address))
    return address


def is_private_host(hostname: str) -> bool:
    """Whether `hostname` is this machine or an address of a private network, where plain http is no risk to a key."""
    if hostname == "localhost" or hostname.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def require_safe_for_key(server: str) -> None:
    """Raises UpstreamError when an API key would travel in the clear to a server that is not on this machine or its network."""
    parts = urllib.parse.urlsplit(server)
    if parts.scheme == "http" and not is_private_host(parts.hostname or ""):
        raise UpstreamError(f"will not send an API key over plain http to {parts.hostname}: use its https address")


def checked_key(text: str | None) -> str | None:
    """The API key in `text` without surrounding blanks; None when there is none. Raises ValueError when it is not a key."""
    key = (text or "").strip()
    if not key:
        return None
    if len(key) > MAX_KEY_CHARS or not key.isascii() or not key.isprintable() or " " in key:
        raise ValueError("the API key has characters a key cannot have")
    return key


def redact(text: str, api_key: str | None) -> str:
    """`text` with the API key, if it appears in it, hidden: a message for the page must never carry the key."""
    return text.replace(api_key, REDACTED) if api_key else text


def key_headers(api_key: str | None) -> dict[str, str]:
    """The header that carries the API key to a hosted server (none without a key)."""
    return {AUTHORIZATION: BEARER + api_key} if api_key else {}


def http_error_text(url: str, error: urllib.error.HTTPError) -> str:
    """What to tell the page about an HTTP error: a redirect names where it pointed (it is not followed, for the key's sake)."""
    if error.code in REDIRECT_CODES:
        return f"{url} redirected to {error.headers.get('Location')} (not followed: use that address)"
    detail = error.read(ERROR_DETAIL_BYTES).decode(TEXT_ENCODING, "replace").strip()
    return f"{url} answered {error.code}" + (f": {detail}" if detail else "")


def get_json(url: str, api_key: str | None = None) -> dict:
    try:
        with OPENER.open(urllib.request.Request(url, headers=key_headers(api_key)), timeout=LIST_TIMEOUT) as reply:
            return json.load(reply)
    except urllib.error.HTTPError as error:
        raise UpstreamError(http_error_text(url, error) if error.code in REDIRECT_CODES else f"{url} answered {error.code}") from error
    except ValueError as error:  # the server answered, but not with JSON: usually a wrong address
        raise UpstreamError(f"{url} did not answer with a list of models (is the chat server's address the API's base, like .../api/coding/v3?)") from error
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as error:
        raise UpstreamError(f"cannot reach {url}: {getattr(error, 'reason', error)}") from error


def model_ids(listed) -> list[dict]:
    """The models of a list as the server sent it, whatever is in it: only entries with a text id are kept."""
    return [model for model in listed if isinstance(model, dict) and isinstance(model.get("id"), str)]


def list_chat_models(server: str, api: str = API_LMSTUDIO, api_key: str | None = None) -> list[dict]:
    """The models of `server` that can write an answer (embedding models left out), the loaded ones first. Each is
    {id, state, context}; `state` and `context` are None when the server does not say. An OpenAI-style server has only
    the ids, and some (a hosted plan with one alias) give no list at all: then this raises, and the page lets you type.
    The key is used for an OpenAI-style server only."""
    server = normalize_server(server)
    if api == API_OPENAI:
        if api_key:
            require_safe_for_key(server)
        try:
            listed = get_json(server + OPENAI_LIST_PATH, api_key)["data"]
            return [{"id": model["id"], "state": None, "context": None} for model in model_ids(listed) if EMBEDDING_NAME_MARK not in model["id"].lower()]
        except (KeyError, TypeError) as error:
            raise UpstreamError(f"{server} did not give a list of models") from error
    try:
        models = [
            {"id": model["id"], "state": model.get("state"), "context": model.get("loaded_context_length")}
            for model in model_ids(get_json(server + LIST_PATH)["data"]) if model.get("type") != TYPE_EMBEDDINGS
        ]
    except (UpstreamError, KeyError, TypeError):
        try:
            listed = get_json(server + V1_LIST_PATH)["data"]
            models = [{"id": model["id"], "state": None, "context": None} for model in model_ids(listed) if EMBEDDING_NAME_MARK not in model["id"].lower()]
        except (KeyError, TypeError) as error:
            raise UpstreamError(f"{server} did not give a list of models") from error
    return sorted(models, key=lambda model: model["state"] != STATE_LOADED)


def sse_events(reply) -> Iterator[dict]:
    """The JSON objects of a server-sent event stream, as they arrive; `[DONE]`, lines that are not JSON and JSON that is
    not an object are skipped."""
    for raw in reply:
        line = raw.decode(TEXT_ENCODING, "replace").strip()
        if line.startswith(SSE_DATA):
            try:
                event = json.loads(line[len(SSE_DATA):])
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event


def text_of(value) -> str:
    """The text of a piece of a message: a string, or a list of parts like {"type": "text", "text": "..."}; else none."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(part.get("text", "") if isinstance(part, dict) and isinstance(part.get("text", ""), str) else "" for part in value)
    return ""


def error_text(value) -> str:
    """The message of an error as a server reported it: an object with a message, or just text."""
    return str(value.get("message") or value) if isinstance(value, dict) else str(value)


def whole_number(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def add_call_piece(calls: dict[int, list[dict]], piece, place: int) -> None:
    """Add one piece of a tool call, as an OpenAI-style stream sends it (delta.tool_calls[]), to the `calls` gathered so
    far, which are kept by the index the server gives each call; a piece without one (a reply that is not streamed has
    none) belongs at `place`, where it stood in its list. A piece goes on with the newest call of its index, unless it
    carries another id than that call has: then it begins a new call (a server that gives every call the same index,
    or none). The id and the name are those of the first piece that has them; the arguments are the pieces of JSON
    text put together (up to MAX_CALL_CHARS), or the object a server sent in their place. A call is gathered in the
    shape an OpenAI-style message holds it. Anything that is not a piece is skipped."""
    if not isinstance(piece, dict):
        return
    function = piece.get(ask.API_FUNCTION) if isinstance(piece.get(ask.API_FUNCTION), dict) else {}
    index = whole_number(piece.get(KEY_INDEX))
    begun = calls.setdefault(place if index is None else index, [])
    given_id = piece.get(ask.API_ID) if isinstance(piece.get(ask.API_ID), str) else None
    if not begun or (given_id and begun[-1].get(ask.API_ID) not in (None, given_id)):
        begun.append({ask.API_TYPE: ask.TYPE_FUNCTION, ask.API_FUNCTION: {ask.API_ARGUMENTS: ""}})
    call = begun[-1]
    gathered = call[ask.API_FUNCTION]
    for holder, source, name in ((call, piece, ask.API_ID), (gathered, function, ask.API_NAME)):
        if not holder.get(name) and isinstance(source.get(name), str):
            holder[name] = source[name]
    arguments = function.get(ask.API_ARGUMENTS)
    if isinstance(arguments, dict):
        gathered[ask.API_ARGUMENTS] = arguments
    elif isinstance(arguments, str) and isinstance(gathered[ask.API_ARGUMENTS], str) and len(gathered[ask.API_ARGUMENTS]) <= MAX_CALL_CHARS:
        gathered[ask.API_ARGUMENTS] += arguments


def openai_events(chunks: Iterator[dict], started: float | None = None) -> Iterator[dict]:
    """The chunks of an OpenAI-style stream (choices[].delta) as the events of LM Studio's native stream, so that one
    relay serves both: chat.start, reasoning.delta, message.delta, chat.end with stats. `started` is when the request was
    sent. The speed and the time to the first token are measured here (the speed only from a stream that took a while to
    arrive, as a buffered one says nothing); the token counts are the server's usage when it sends one, else the number of
    pieces received. The stats also say whether the model ran out of room (finish_reason length) or the stream ended with
    no finish marker. The tool calls the model made, which arrive in pieces (see add_call_piece), are given whole beside
    the stats of chat.end (KEY_CALLS), in the order of their indexes. Whatever odd shape a chunk has, it is skipped, not fatal."""
    started = time.monotonic() if started is None else started
    first = last = None
    pieces = reasoning_pieces = 0
    usage: dict = {}
    calls: dict[int, list[dict]] = {}  # the tool calls gathered so far, by their index
    finish = None
    began = errored = False
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        problem = chunk.get(LMS_ERROR) or (chunk.get("message") if chunk.get("object") == LMS_ERROR else None)
        if problem and not chunk.get("choices"):
            errored = True
            yield {KEY_TYPE: LMS_ERROR, LMS_ERROR: {"message": error_text(problem)}}
            continue
        if not began:
            began = True
            yield {KEY_TYPE: LMS_CHAT_START, KEY_INSTANCE: chunk["model"] if isinstance(chunk.get("model"), str) else ""}
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            finish = choice.get("finish_reason") or finish
            reasoning, content = text_of(delta.get("reasoning_content") or delta.get("reasoning")), text_of(delta.get(KEY_CONTENT))
            if reasoning or content:
                first = first if first is not None else time.monotonic()
                last = time.monotonic()
            if reasoning:
                reasoning_pieces += 1
                yield {KEY_TYPE: LMS_REASONING, KEY_CONTENT: reasoning}
            if content:
                pieces += 1
                yield {KEY_TYPE: LMS_MESSAGE, KEY_CONTENT: content}
            call_pieces = delta.get(ask.API_TOOL_CALLS)
            for place, piece in enumerate(call_pieces if isinstance(call_pieces, list) else []):
                add_call_piece(calls, piece, place)
    if not began and not errored:
        raise UpstreamError("the chat server sent an empty stream: check the chat server's address and the chat path")
    counted = whole_number(usage.get("completion_tokens"))
    output = counted if counted is not None else pieces + reasoning_pieces
    details = usage.get("completion_tokens_details")
    reasoning_tokens = whole_number(details.get("reasoning_tokens")) if isinstance(details, dict) else None
    elapsed = last - first if first is not None else 0
    stats = {
        "input_tokens": whole_number(usage.get("prompt_tokens")), "total_output_tokens": output,
        "reasoning_output_tokens": reasoning_tokens if reasoning_tokens is not None else reasoning_pieces,
        "tokens_per_second": output / elapsed if elapsed >= MIN_SPEED_SECONDS and pieces + reasoning_pieces >= MIN_SPEED_PIECES else None,
        "time_to_first_token_seconds": first - started if first is not None else None,
        STATS_APPROXIMATE: counted is None, STATS_CUT: finish == FINISH_LENGTH, STATS_INCOMPLETE: finish is None,
    }
    result = {KEY_STATS: {name: value for name, value in stats.items() if value is not None}, KEY_CALLS: [call for index in sorted(calls) for call in calls[index]]}
    yield {KEY_TYPE: LMS_CHAT_END, KEY_RESULT: result}


def looks_like_stream(reply) -> bool:
    """Whether a reply is a server-sent event stream: it says so, or (some servers send no type, or text/plain) it begins like one."""
    if reply.headers.get_content_type() == STREAM_MIME:
        return True
    try:
        return reply.peek(SNIFF_BYTES).lstrip().startswith(STREAM_STARTS)
    except (AttributeError, OSError, ValueError):
        return False


def openai_reply_events(reply, url: str, started: float) -> Iterator[dict]:
    """The events of an OpenAI-style reply. A server that streams sends server-sent events; one that ignores `stream` sends
    the whole answer as JSON, which is shown at once, with the tool calls it holds; anything else (a web page from a
    wrong address, an error) raises."""
    if looks_like_stream(reply):
        yield from openai_events(sse_events(reply), started)
        return
    text = reply.read(MAX_REPLY_BYTES).decode(TEXT_ENCODING, "replace").strip()
    try:
        data = json.loads(text)
    except ValueError as error:
        raise UpstreamError(f"{url} did not answer with a stream: {text[:ERROR_DETAIL_BYTES]!r}") from error
    choices = data.get("choices") if isinstance(data, dict) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
        delta = {
            KEY_CONTENT: message.get(KEY_CONTENT), "reasoning_content": message.get("reasoning_content"),
            ask.API_TOOL_CALLS: message.get(ask.API_TOOL_CALLS),
        }
        chunk = {"model": data.get("model"), "choices": [{"delta": delta, "finish_reason": choices[0].get("finish_reason")}], "usage": data.get("usage")}
        yield from openai_events(iter([chunk]), started)
        return
    detail = (data.get(LMS_ERROR) or data) if isinstance(data, dict) else data
    raise UpstreamError(f"{url} answered: {error_text(detail)[:ERROR_DETAIL_BYTES]}")


def stream_chat(
    chat_server: str, model: str, messages: list[dict], api: str = API_LMSTUDIO, path: str | None = None, api_key: str | None = None,
    tools: list[dict] | None = None,
) -> Iterator[dict]:
    """The events of the chat server's reply to the prompt `messages` (a system message, the earlier exchange and the
    user message, as ask.prepare gives them), as they arrive, in the shape of LM Studio's stream (lmstudio.ai/docs/developer/rest/streaming-events). `path`
    replaces the API's usual endpoint. The key is sent to an OpenAI-style server only, and never over plain http to a
    server that is not on this network. `tools` are the tools the model may call (see search_tool.tool_schema), which
    only an OpenAI-style server is given; the calls it makes come with chat.end (see openai_events). A redirect is not
    followed. Raises UpstreamError when the server cannot be used. Closing the generator closes the connection, which
    stops the model."""
    if api == API_OPENAI:
        url_path = path or OPENAI_CHAT_PATH
        body = {"model": model, "messages": messages, "stream": True, "temperature": ask.TEMPERATURE, **({ask.API_TOOLS: tools} if tools else {})}
    else:
        url_path = path or NATIVE_CHAT_PATH
        body = {"model": model, "system_prompt": messages[0][ask.API_CONTENT], "input": folded_input(messages[1:]), "stream": True, "temperature": ask.TEMPERATURE, "store": STORE_CHAT}
        api_key = None
    url = normalize_server(chat_server) + url_path
    if api_key:
        require_safe_for_key(url)
    try:
        request = urllib.request.Request(url, json.dumps(body).encode(TEXT_ENCODING), {"Content-Type": JSON_TYPE, **key_headers(api_key)})
    except (ValueError, UnicodeError) as error:
        raise UpstreamError(f"{url} is not a usable address") from error
    started = time.monotonic()
    try:
        with OPENER.open(request, timeout=STREAM_TIMEOUT) as reply:
            yield from (openai_reply_events(reply, url, started) if api == API_OPENAI else sse_events(reply))
    except urllib.error.HTTPError as error:
        raise UpstreamError(http_error_text(url, error)) from error
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as error:
        raise UpstreamError(f"cannot reach {chat_server}: {getattr(error, 'reason', error)}") from error


def folded_input(messages: list[dict]) -> str:
    """The earlier exchange and the user message as the one input text LM Studio's own API takes: the user message alone
    when there is no earlier exchange, else the exchange under HISTORY_HEADING, labelled, and the user message after
    HISTORY_END."""
    *history, last = messages
    if not history:
        return last[ask.API_CONTENT]
    turns = [f"{HISTORY_LABELS[message[ask.API_ROLE]]}: {message[ask.API_CONTENT]}" for message in history]
    return ask.EXCERPT_SEPARATOR.join([HISTORY_HEADING, *turns, HISTORY_END + ask.LINE_SEPARATOR + last[ask.API_CONTENT]])


def advised(message: str, with_tools: bool = False) -> str:
    """A chat server's refusal with what to do about it, when that is known: an address that lacks LM Studio's /v1, or
    (for a request sent `with_tools`) a server or a model that takes no tools."""
    if WRONG_BASE in message:
        return message + WRONG_BASE_HINT
    return message + (NO_TOOLS_HINT if with_tools and ask.TOOLS_REFUSED.search(message) else "")


def answered_by(instance, model: str) -> bool:
    """Whether the instance the server names is the model asked for (a second loaded copy is named `model:2`)."""
    instance = instance if isinstance(instance, str) else ""
    return instance == model or instance.startswith(model + ":")


def end_result(event: dict) -> dict:
    """What a chat.end event holds (its stats, and the tool calls of an OpenAI-style stream); empty when it holds nothing."""
    result = event.get(KEY_RESULT)
    return result if isinstance(result, dict) else {}


def stats_of(result: dict) -> dict:
    """The stats in what a chat.end event holds (see end_result); empty when it has none."""
    stats = result.get(KEY_STATS)
    return stats if isinstance(stats, dict) else {}


# ---- what the page is sent -------------------------------------------------------------------------------------------

def image_url(file: str, root: Path) -> str | None:
    """The address the page asks an image from, or None when the file is not inside `root`."""
    relative = os.path.relpath(file, root)
    return None if relative.startswith("..") else ROUTE_IMAGE + "?" + urllib.parse.urlencode({QUERY_PATH: relative})


def first_searches(searches: list[search_tool.Search]) -> dict[int, int]:
    """For every chunk the `searches` of one question gave, by its number: the number of the first search that gave it."""
    first: dict[int, int] = {}
    for search in searches:
        for number in search.numbers:
            first.setdefault(number, search.number)
    return first


def sources_payload(sources: list[tuple[float | None, dict]], root: Path, searches: list[search_tool.Search] | None = None) -> list[dict]:
    """The chunks the model was given, numbered as its answer cites them. Each comes with its short name and its PDF
    pages for the row, its full citation, and its status in words when the norm does not simply apply (empty when it does).
    A chunk fetched by its article number has no score (null). With `searches`, the calls a model that searches for
    itself made, each chunk also says which of them gave it first ("search")."""
    first = first_searches(searches) if searches is not None else None
    payload = []
    for number, (score, record) in enumerate(sources, start=1):
        images = [
            {"url": url, "caption": image["caption"], "page": image["page"]}
            for image in record.get("images", [])
            if image.get(IMAGE_FILE_KEY) and (url := image_url(image[IMAGE_FILE_KEY], root))
        ]
        payload.append({
            "number": number, "score": None if score is None else round(score, 3), "id": record["id"], "doc_code": record["doc_code"],
            "year": record["edition_year"], "kind": record["kind"], "reference": reference(record),
            "pages": pages_note(record.get("pdf_pages")), "citation": citation(record),
            "status_note": "" if applies_as_written(record) else status_words(record),
            "breadcrumb": breadcrumb(record), "text": record["text"], "images": images,
            **({} if first is None else {"search": first.get(number)}),
        })
    return payload


def served_image(root: Path, relative: str) -> Path | None:
    """The image file `relative` names, or None unless it is an image inside `root`."""
    try:
        path = (root / relative).resolve()
    except (ValueError, OSError):  # an embedded null byte, or a loop of links
        return None
    if not relative or root.resolve() not in path.parents or path.suffix.lower() not in IMAGE_SUFFIXES or not path.is_file():
        return None
    return path


def validated_request(body: dict, state: AppState) -> dict:
    """The fields of an ask request, checked. Raises ValueError with the message to show."""
    question = str(body.get("question", "")).strip()
    model = str(body.get("model", "")).strip()
    if not question:
        raise ValueError("the question is empty")
    if not model:
        raise ValueError("pick a chat model first")
    try:
        top = int(body.get("top", DEFAULT_TOP))
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("top must be a whole number") from error
    if not 1 <= top <= MAX_TOP:
        raise ValueError(f"top must be between 1 and {MAX_TOP}")
    doc = str(body.get("doc") or "").strip() or None
    if doc is not None and doc not in state.docs:
        raise ValueError(f"no norm with code {doc!r}; known: {', '.join(state.docs)}")
    try:
        embedding_server = normalize_server(body.get("embedding_server") or state.embedding_server)
        chat_server = normalize_server(body.get("chat_server") or state.chat_server)
    except UpstreamError as error:
        raise ValueError(str(error)) from error
    api = str(body.get("chat_api") or API_LMSTUDIO)
    if api not in APIS:
        raise ValueError(API_STYLE_MESSAGE)
    path = str(body.get("chat_path") or "").strip() or None
    if path is not None and (not path.startswith("/") or not path.isascii() or len(path) > MAX_PATH_CHARS or any(c.isspace() or c in "?#" for c in path)):
        raise ValueError("the chat path must start with / and have only plain characters: no spaces, ? or #")
    api_key = checked_key(body.get("chat_api_key")) if api == API_OPENAI else None  # LM Studio's own API takes no key: never send one to it
    search = str(body.get("search") or SEARCH_ONCE)
    if search not in SEARCH_MODES:
        raise ValueError(SEARCH_MODE_MESSAGE)
    try:
        history = ask.checked_history(body.get("history") or [], "history")
    except ConversionError as error:
        raise ValueError(str(error)) from error
    if len(history) > ask.MAX_HISTORY_MESSAGES or sum(len(message[ask.API_CONTENT]) for message in history) > MAX_HISTORY_CHARS:
        raise ValueError(f"history: at most {ask.MAX_HISTORY_MESSAGES} messages and {MAX_HISTORY_CHARS} characters")
    return {
        "question": question, "model": model, "top": top, "doc": doc, "embedding_server": embedding_server, "chat_server": chat_server,
        "chat_api": api, "chat_path": path, "chat_api_key": api_key, "history": history, "search": search,
    }


# ---- the web server ---------------------------------------------------------------------------------------------------

def make_handler(state: AppState) -> type[BaseHTTPRequestHandler]:
    """The request handler class for `state`."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # one line per request is noise on a page that polls
            return

        # -- helpers
        def own_origins(self) -> set[str]:
            port = self.server.server_address[1]
            return {f"http://{HOST}:{port}", f"http://localhost:{port}"}

        def request_ok(self, posting: bool) -> bool:
            """Only this page may use this server. The Host must be this machine's own name (a name another site
            controls could point here: DNS rebinding); an Origin, when the browser sends one, must be this page; and a POST
            must be JSON, which a page on another site cannot send without a preflight this server never answers."""
            if self.headers.get("Host", "") not in {origin.split("//")[1] for origin in self.own_origins()}:
                return False
            origin, site = self.headers.get("Origin"), self.headers.get("Sec-Fetch-Site")
            if origin is not None and origin not in self.own_origins():
                return False
            if site is not None and site not in FETCH_SITE_OK:  # a browser says when a request comes from another site, even a plain <img>
                return False
            return not posting or self.headers.get("Content-Type", "").split(";")[0].strip().lower() == JSON_TYPE

        def client_gone(self) -> bool:
            """Whether the browser has closed the connection (a closed socket reads as end of file)."""
            try:
                readable, _, _ = select.select([self.connection], [], [], 0)
                return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
            except OSError:
                return True

        def send_bytes(self, status: HTTPStatus, content_type: str, data: bytes, headers: dict[str, str] | None = None) -> None:
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            for name, value in NO_SNIFFING.items():
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", NO_CACHE)
            self.end_headers()
            self.wfile.write(data)

        def send_json(self, status: HTTPStatus, payload: dict | list) -> None:
            self.send_bytes(status, JSON_REPLY_TYPE, json.dumps(payload, ensure_ascii=False).encode(TEXT_ENCODING))

        def send_event(self, name: str, data: dict) -> None:
            message = f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode(TEXT_ENCODING)
            try:
                self.wfile.write(message)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError) as error:
                raise ClientGone from error

        # -- routes
        def do_GET(self) -> None:
            try:
                self.route_get()
            except Exception as error:  # a bug answers with a message, not a dropped connection
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{type(error).__name__}: {error}"})

        def route_get(self) -> None:
            if not self.request_ok(posting=False):
                return self.send_json(HTTPStatus.FORBIDDEN, {"error": "not a request from this page"})
            route = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(route.query)
            if route.path == ROUTE_PAGE:
                return self.send_bytes(HTTPStatus.OK, HTML_TYPE, PAGE_FILE.read_bytes(), {"Content-Security-Policy": CONTENT_SECURITY_POLICY})
            if route.path == ROUTE_SETTINGS:
                return self.send_json(HTTPStatus.OK, {
                    "embedding_server": state.embedding_server, "chat_server": state.chat_server, "top": DEFAULT_TOP,
                    "max_top": MAX_TOP, "docs": state.docs,
                })
            if route.path == ROUTE_MODELS:
                api = query.get(QUERY_API, [API_LMSTUDIO])[0]
                if api not in APIS:
                    return self.send_json(HTTPStatus.BAD_REQUEST, {"error": API_STYLE_MESSAGE})
                api_key = None
                try:
                    api_key = checked_key(self.headers.get(KEY_HEADER)) if api == API_OPENAI else None
                    return self.send_json(HTTPStatus.OK, list_chat_models(query.get(QUERY_SERVER, [state.chat_server])[0], api, api_key))
                except (UpstreamError, ValueError) as error:
                    return self.send_json(HTTPStatus.BAD_GATEWAY, {"error": redact(str(error), api_key)})
            if route.path == ROUTE_IMAGE:
                path = served_image(state.image_root, query.get(QUERY_PATH, [""])[0])
                if path is None:
                    return self.send_json(HTTPStatus.NOT_FOUND, {"error": "no such image"})
                return self.send_bytes(HTTPStatus.OK, mimetypes.guess_type(path.name)[0] or "application/octet-stream", path.read_bytes())
            if route.path in PAGE_PARTS:
                part, content_type = PAGE_PARTS[route.path]
                return self.send_bytes(HTTPStatus.OK, content_type, part.read_bytes())
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            if not self.request_ok(posting=True):
                return self.send_json(HTTPStatus.FORBIDDEN, {"error": "not a request from this page"})
            if urllib.parse.urlsplit(self.path).path != ROUTE_ASK:
                return self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY_BYTES:
                    raise ValueError("the request is empty or too large")
                request = validated_request(json.loads(self.rfile.read(length)), state)
            except (ValueError, TypeError, AttributeError) as error:
                return self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            self.send_response(HTTPStatus.OK)
            for name, value in NO_SNIFFING.items():
                self.send_header(name, value)
            self.send_header("Content-Type", STREAM_TYPE)
            self.send_header("Cache-Control", NO_CACHE)
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                self.answer(request)
            except ClientGone:
                pass
            except Exception as error:  # a bug must show on the page, not end the stream with no word
                try:
                    self.send_event(EVENT_ERROR, {"message": redact(f"{type(error).__name__}: {error}", request["chat_api_key"])})
                except ClientGone:
                    pass

        def answer(self, request: dict) -> None:
            """Answer one question on the stream to the page. The model searches for itself when the request asks for
            that and the chat server takes tools (an OpenAI-style one); otherwise, and when the model did not search as
            it was to, the question is searched once and the model is handed what was found. Every failure is sent to
            the page as an error event."""
            rounds = Rounds()
            if request["search"] == SEARCH_MODEL and request["chat_api"] == API_OPENAI:
                if self.answer_searching(request, rounds):
                    return
                self.send_event(EVENT_FALLBACK, {"message": ask.FELL_BACK_NOTE})
            self.answer_once(request, rounds)

        def answer_searching(self, request: dict, rounds: Rounds) -> bool:
            """Let the model search for itself, round after round (see ask.answer_with_tools): every round is relayed as
            it is written, every search is told as it was run, with the chunks found so far, and one `done` ends the
            answer, with the stats of the last round (`rounds` keeps them). Returns False when the model cannot be taken
            at its word and is to be handed the chunks instead: what the page was shown is then void. Returns True when
            the question is dealt with, which an error sent to the page does too."""
            api_key = request["chat_api_key"]
            self.send_event(EVENT_STATUS, {"state": STATUS_WAITING})  # nothing is searched before the model is asked
            try:
                session = search_tool.SearchSession(state.index, state.config, request["embedding_server"], request["top"], request["doc"])
                reply = ask.answer_with_tools(
                    session, ask.tool_messages(request["question"], state.tool_instructions, request["history"]),
                    lambda messages, tools: self.ask_round(request, messages, tools, rounds), lambda search: self.searched(session, search),
                )
                if reply is None:
                    return False
            except ChatFailed:  # the page has the error; the stream ends as it does when the question is searched once: with the stats
                pass
            except ConversionError as error:  # the embedding server cannot be used
                self.send_event(EVENT_ERROR, {"message": str(error)})
                return True
            except UpstreamError as error:
                self.send_event(EVENT_ERROR, {"message": advised(redact(str(error), api_key), with_tools=True)})
                return True
            self.send_event(EVENT_DONE, {KEY_STATS: rounds.stats})
            return True

        def ask_round(self, request: dict, messages: list[dict], tools: list[dict] | None, rounds: Rounds) -> ask.Reply:
            """One round with a model that searches for itself: its reply to `messages` is relayed to the page as it is
            written, and given back as its text without its thinking, the calls of `tools` it made, and whether it was
            cut short. The round's stats are kept in `rounds` and not sent: the page gets one `done`, after the last
            round. Raises ClientGone when the browser left before the round (the stop button ends the rounds),
            ChatFailed when the chat server reported an error in its stream, and UpstreamError when it cannot be used."""
            if self.client_gone():  # stopped between two rounds: do not ask the model again
                raise ClientGone
            api_key = request["chat_api_key"]
            pieces: list[str] = []
            result: dict = {}
            failed = False
            with contextlib.closing(stream_chat(request["chat_server"], request["model"], messages, request["chat_api"], request["chat_path"], api_key, tools)) as events:
                for event in events:
                    kind = event.get(KEY_TYPE)
                    if kind == LMS_CHAT_END:
                        result = end_result(event)
                        break
                    self.relay(event, request["model"], api_key, not rounds.asked)
                    if kind == LMS_MESSAGE:
                        pieces.append(text_of(event.get(KEY_CONTENT)))
                    failed = failed or kind == LMS_ERROR
            rounds.asked += 1
            rounds.stats = stats_of(result)
            if failed:
                raise ChatFailed
            calls = [ask.ToolCall(call.id, call.name, redact(call.arguments, api_key)) for call in ask.tool_calls_of(result)]  # a server may echo the key into a call
            return ask.Reply(ask.THINKING.sub("", "".join(pieces)).strip(), calls, bool(rounds.stats.get(STATS_CUT)))

        def searched(self, session: search_tool.SearchSession, search: search_tool.Search) -> None:
            """Tell the page of one search the model made, as it was run; and, when it found anything, of all the
            chunks found so far, numbered as the model is given them, each with the search that gave it first."""
            self.send_event(EVENT_SEARCH, {
                "number": search.number, "query": search.query, "article": search.article, "norm": search.norm,
                "found": len(search.numbers), "new": search.new, "problem": search.problem,
            })
            if search.numbers:
                self.send_event(EVENT_SOURCES, {"sources": sources_payload(session.sources, state.image_root, session.searches)})

        def answer_once(self, request: dict, rounds: Rounds) -> None:
            """Search once, with the question's own words, then relay the chat server's stream. Every failure is sent to
            the page as an error event. `rounds` says whether the model was asked before for this question (it was to
            search, and did not)."""
            self.send_event(EVENT_STATUS, {"state": STATUS_SEARCHING})
            try:
                sources, messages = ask.prepare(
                    request["question"], state.index, state.config, request["embedding_server"], request["top"], request["doc"], state.instructions,
                    history=request["history"],
                )
            except (ConversionError, SystemExit) as error:  # the embedding server cannot be used; the search stops the program at a norm no chunk has
                return self.send_event(EVENT_ERROR, {"message": str(error.code if isinstance(error, SystemExit) else error)})
            self.send_event(EVENT_SOURCES, {"sources": sources_payload(sources, state.image_root)})
            self.send_event(EVENT_STATUS, {"state": STATUS_WAITING})
            if self.client_gone():  # stopped during the search: do not start the model
                raise ClientGone
            api_key = request["chat_api_key"]
            try:
                with contextlib.closing(stream_chat(request["chat_server"], request["model"], messages, request["chat_api"], request["chat_path"], api_key)) as events:
                    for event in events:
                        self.relay(event, request["model"], api_key, not rounds.asked)
                        if event.get(KEY_TYPE) == LMS_CHAT_END:
                            break
            except UpstreamError as error:
                self.send_event(EVENT_ERROR, {"message": advised(redact(str(error), api_key))})

        def relay(self, event: dict, model: str, api_key: str | None = None, first: bool = True) -> None:
            """Send the page what one event of the chat server means. A hosted API (one that wants a key) may answer under
            the name an alias resolves to, so then the model that answered is not compared; and a message that carries
            the key, which a server may echo in an error, is shown without it. `first` is whether this is the first
            round with the model for a question: the page is told that another model answers once, not at every round."""
            kind = event.get(KEY_TYPE, "")
            if kind == LMS_CHAT_START and first and not api_key and not answered_by(event.get(KEY_INSTANCE), model):
                self.send_event(EVENT_NOTICE, {"message": f"answered by {event.get(KEY_INSTANCE)}, not {model}"})
            elif kind.startswith(LMS_MODEL_LOAD) and kind != LMS_MODEL_LOAD_END:
                self.send_event(EVENT_MODEL_LOAD, {KEY_PROGRESS: event.get(KEY_PROGRESS, 0)})
            elif kind == LMS_PROMPT_PROGRESS:
                self.send_event(EVENT_PROMPT, {KEY_PROGRESS: event.get(KEY_PROGRESS, 0)})
            elif kind == LMS_REASONING:
                self.send_event(EVENT_REASONING, {KEY_CONTENT: event.get(KEY_CONTENT, "")})
            elif kind == LMS_MESSAGE:
                self.send_event(EVENT_MESSAGE, {KEY_CONTENT: event.get(KEY_CONTENT, "")})
            elif kind == LMS_ERROR:
                self.send_event(EVENT_ERROR, {"message": redact(error_text(event.get(LMS_ERROR)) if event.get(LMS_ERROR) else "the chat server reported an error", api_key)})
            elif kind == LMS_CHAT_END:
                self.send_event(EVENT_DONE, {KEY_STATS: stats_of(end_result(event))})

    return Handler


def make_server(state: AppState, host: str = HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(state))


def load_state(args: argparse.Namespace) -> AppState:
    config = load_config(args.config)
    instructions = ask.load_instructions(args.instructions)
    chunks_dir = args.chunks_dir.resolve()  # absolute, so that the image paths are too
    index = load_index(load_chunks(chunks_dir), args.vectors_dir, config)
    docs = sorted({record["doc_code"] for record in index.records})
    return AppState(
        index, config, instructions, docs, (args.images_root or chunks_dir.parent).resolve(), args.server, args.chat_server,
        ask.tool_instructions(instructions),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chunks-dir", type=Path, default=PROJECT_DIR / "docs_processed" / "chunks", help="folder with the .jsonl files of json_to_chunks.py (default: %(default)s)")
    parser.add_argument("--vectors-dir", type=Path, default=PROJECT_DIR / "docs_processed" / "vectors", help="folder with the .npz files of embed_chunks.py (default: %(default)s)")
    parser.add_argument("--images-root", type=Path, help="images are served from inside this folder (default: the parent of --chunks-dir)")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="per-document settings (default: %(default)s)")
    parser.add_argument("--instructions", type=Path, default=ask.INSTRUCTIONS_FILE, help="file with the rules for the language model (default: %(default)s)")
    parser.add_argument("--server", default=DEFAULT_EMBEDDING_SERVER, help="the page's default embedding server (default: %(default)s)")
    parser.add_argument("--chat-server", default=DEFAULT_CHAT_SERVER, help="the page's default chat server (default: %(default)s)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="default: %(default)s")
    args = parser.parse_args()
    try:
        state = load_state(args)
    except ConversionError as error:
        sys.exit(str(error))
    server = make_server(state, HOST, args.port)
    print(f"{len(state.index.records)} chunks of {len(state.docs)} norms loaded. Open http://{HOST}:{args.port}  (Ctrl+C stops it)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
