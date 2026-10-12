"""The search as an MCP server, so that Claude (claude.ai, Claude Desktop, the phone app, Claude Code) can search the norms.

Usage (run with the virtual environment's interpreter):
    export NORMS_MCP_KEY="$(cat ~/.config/norms-mcp.key)"  # the key every request must carry (see below)
    venvs/bin/python mcp_server.py --server http://127.0.0.1:1234
    venvs/bin/python mcp_server.py --port 9000 --chunks-dir CHUNKS --vectors-dir VECTORS

One tool, search_norms, as search_tool.py describes it: a search by meaning (`query`) or an article fetched by its
number (`article`). Every call stands alone: its excerpts are numbered from 1 and there is no limit of calls, because
an MCP client says nothing of where one question ends. The rules for searching and citing (mcp-instructions.md) are
part of the tool's description, which every client shows its model.

The server speaks MCP's Streamable HTTP at /mcp and listens on this machine only (127.0.0.1): a reverse proxy in front
of it holds the certificate and passes the requests on. A call is answered as plain JSON; a client may also keep a GET
open for messages from the server (none are ever sent), which is why the proxy must not buffer the replies.

A request without `Authorization: Bearer <key>` is answered 401. The key is read from the environment (NORMS_MCP_KEY),
never from the command line, where every user of the machine could read it; without a key the server does not start.
Make the key once and keep it, in a file only its owner can read and outside this folder: a new key means adding the
server to Claude again.
    (umask 077; mkdir -p ~/.config && openssl rand -hex 16 > ~/.config/norms-mcp.key)
The key is the only gate: the Host and Origin headers of a request are not checked, since a page of another site
cannot send the key, and so the server answers under whatever name the proxy is reached.

A search by meaning needs the embedding server (--server) with the model the vectors were made with; when it cannot be
used the call is answered with an error, and fetching by number still works.
"""

import argparse
import functools
import hmac
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

import anyio
import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

import ask
from embed_chunks import add_server_argument
from json_to_chunks import CONFIG_FILE, load_config
from pipeline_common import ConversionError
from search import DEFAULT_TOP, Index, load_chunks, load_index
from search_tool import (
    ARG_ARTICLE, EXCERPT_SEPARATOR, MAX_COUNT, SCHEMA_DESCRIPTION, SCHEMA_FUNCTION, SCHEMA_NAME, SCHEMA_PARAMETERS, TOOL_NAME, SearchSession,
)

PROJECT_DIR = Path(__file__).resolve().parent
RULES_FILE = PROJECT_DIR / "mcp-instructions.md"  # how to search and cite, put into the tool's description
SERVER_NAME = "norms"
SERVER_TITLE = "Romanian fire-safety norms"
SERVER_VERSION = "1.0"
TOOL_TITLE = "Search the fire-safety norms"
SERVER_INSTRUCTIONS = f"Call {TOOL_NAME} for any question about the Romanian fire-safety norms; its description says how to search and how to cite."
HOST = "127.0.0.1"  # this machine only: the proxy is the way in
DEFAULT_PORT = 8766
MCP_PATH = "/mcp"
KEY_VARIABLE = "NORMS_MCP_KEY"
MIN_KEY_CHARS = 32  # a shorter key could be guessed
AUTH_SCHEME = ask.BEARER.strip().lower()  # the scheme of the Authorization header, which may be written in any case
UNAUTHORIZED = 401
NO_KEY_REPLY = f"This server needs its key: send the header '{ask.AUTHORIZATION}: {ask.BEARER}<key>'."
CHALLENGE = {"WWW-Authenticate": ask.BEARER.strip()}
# The kinds of connection a web server hands an application (ASGI scopes).
SCOPE_TYPE = "type"
SCOPE_LIFESPAN = "lifespan"  # the server starting and stopping: no request, so nothing to carry a key
SCOPE_HTTP = "http"
WEBSOCKET_CLOSE = "websocket.close"
POLICY_VIOLATION = 1008  # the WebSocket close code for a connection that is not allowed
TEXT_CONTENT = "text"  # the kind of content a result holds
# What a client is told when a search fails; the reason itself, which may name the embedding server's address, is
# written to this server's own output.
SEARCH_UNAVAILABLE = (
    "The search by meaning cannot be used right now: the server that embeds the question did not answer. "
    f"Fetching an article or point by its number (`{ARG_ARTICLE}`) still works."
)
SEARCH_FAILED = "The search failed on the server, for a reason written to its own output. Nothing was searched."


@dataclass
class SearchState:
    """What every call needs, loaded once."""
    index: Index
    config: dict
    embedding_server: str
    count: int  # excerpts a call gets when it does not say
    rules: str  # how to search and cite (RULES_FILE)


def new_session(state: SearchState) -> SearchSession:
    """The session of one call: no limit of calls, and nothing shows the reader the pictures of an excerpt."""
    return SearchSession(state.index, state.config, state.embedding_server, state.count, max_searches=None, figures_shown=False)


def tool(state: SearchState) -> types.Tool:
    """The tool as an MCP client is told of it: the schema search_tool.py gives a chat model, and the rules for searching
    and citing after the description."""
    function = new_session(state).schema()[SCHEMA_FUNCTION]
    return types.Tool(
        name=function[SCHEMA_NAME], title=TOOL_TITLE, description=function[SCHEMA_DESCRIPTION] + EXCERPT_SEPARATOR + state.rules,
        input_schema=function[SCHEMA_PARAMETERS],
        annotations=types.ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False),
    )


def answer(state: SearchState, name: str, arguments: dict) -> tuple[str, bool]:
    """What one call of the tool `name` is answered with, and whether it is an error: a call that could not be run (an
    unknown tool, wrong arguments, an unknown norm or number) is answered with the reason, for the model to go on from.
    Raises ConversionError when the embedding server cannot be used."""
    session = new_session(state)
    text = session.reply(arguments, name)
    return text, bool(session.searches[-1].problem)


def text_result(text: str, is_error: bool = False) -> types.CallToolResult:
    """The result of a call: `text`, and whether the call failed."""
    return types.CallToolResult(content=[types.TextContent(type=TEXT_CONTENT, text=text)], is_error=is_error)


def build_server(state: SearchState) -> Server:
    """The MCP server with its one tool."""
    described = tool(state)

    async def list_tools(context, params) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[described])

    async def call_tool(context, params: types.CallToolRequestParams) -> types.CallToolResult:
        try:  # in a thread: the embedding server is asked with a call that blocks, and the clients share one event loop
            text, is_error = await anyio.to_thread.run_sync(functools.partial(answer, state, params.name, params.arguments or {}))
        except ConversionError as error:
            print(f"{TOOL_NAME}: {error}", file=sys.stderr, flush=True)
            return text_result(SEARCH_UNAVAILABLE, is_error=True)
        except Exception:  # noqa: BLE001 - whatever went wrong, the client is told only that it did: the mcp package would send it the error's own text
            traceback.print_exc()
            return text_result(SEARCH_FAILED, is_error=True)
        return text_result(text, is_error)

    return Server(SERVER_NAME, version=SERVER_VERSION, title=SERVER_TITLE, instructions=SERVER_INSTRUCTIONS, on_list_tools=list_tools, on_call_tool=call_tool)


class KeyGate:
    """Passes on only what carries the key (`Authorization: Bearer <key>`): an HTTP request without it is answered 401,
    any other connection without it is closed. The key is never written anywhere."""

    def __init__(self, app: ASGIApp, key: str):
        self.app = app
        self.key = key.encode()

    def carries_key(self, scope: Scope) -> bool:
        scheme, _, given = Headers(scope=scope).get(ask.AUTHORIZATION, "").partition(" ")
        return scheme.lower() == AUTH_SCHEME and hmac.compare_digest(given.strip().encode(), self.key)  # in constant time

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope[SCOPE_TYPE] == SCOPE_LIFESPAN or self.carries_key(scope):
            await self.app(scope, receive, send)
        elif scope[SCOPE_TYPE] == SCOPE_HTTP:
            await PlainTextResponse(NO_KEY_REPLY, status_code=UNAUTHORIZED, headers=CHALLENGE)(scope, receive, send)
        else:
            await send({SCOPE_TYPE: WEBSOCKET_CLOSE, "code": POLICY_VIOLATION})


def http_app(state: SearchState, key: str) -> ASGIApp:
    """The server as a web application: MCP at MCP_PATH, behind the key. Every call is answered on its own, as JSON, and
    no session is kept between two. The mcp package's own check of the Host and Origin headers is off: it is there to
    keep the pages of other sites out of a server without a key, and here nothing passes KeyGate without the key."""
    app = build_server(state).streamable_http_app(
        streamable_http_path=MCP_PATH, json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return KeyGate(app, key)


def read_key(environment: dict[str, str]) -> str:
    """The key from the environment. Raises ConversionError when there is none, one short enough to be guessed, or one
    with characters a header cannot carry as they are (it could never match)."""
    key = environment.get(KEY_VARIABLE, "").strip()
    if len(key) < MIN_KEY_CHARS or not key.isascii():
        raise ConversionError(
            f"set {KEY_VARIABLE} to a key of at least {MIN_KEY_CHARS} plain (ASCII) characters before starting; make one with: "
            f"openssl rand -hex {MIN_KEY_CHARS // 2}"
        )
    return key


def load_rules(path: Path = RULES_FILE) -> str:
    """The rules for searching and citing, read from `path` as ask.py reads its own. Raises as ask.load_instructions does."""
    return ask.load_instructions(path, ask.SEARCH_TOKENS)


def load_state(args: argparse.Namespace) -> SearchState:
    config = load_config(args.config)
    index = load_index(load_chunks(args.chunks_dir.resolve()), args.vectors_dir, config)
    return SearchState(index, config, args.server, args.top, load_rules(args.rules))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chunks-dir", type=Path, default=PROJECT_DIR / "docs_processed" / "chunks", help="folder with the .jsonl files of json_to_chunks.py (default: %(default)s)")
    parser.add_argument("--vectors-dir", type=Path, default=PROJECT_DIR / "docs_processed" / "vectors", help="folder with the .npz files of embed_chunks.py (default: %(default)s)")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="per-document settings (default: %(default)s)")
    parser.add_argument("--rules", type=Path, default=RULES_FILE, help="file with the rules for searching and citing, put into the tool's description (default: %(default)s)")
    add_server_argument(parser)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, choices=range(1, MAX_COUNT + 1), metavar=f"1-{MAX_COUNT}", help="excerpts a call gets when it does not say (default: %(default)s)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="default: %(default)s")
    args = parser.parse_args()
    try:
        key = read_key(os.environ)
        state = load_state(args)
    except ConversionError as error:
        sys.exit(str(error))
    names = len({record["doc_code"] for record in state.index.records})
    print(f"{len(state.index.records)} chunks of {names} norms loaded. MCP at http://{HOST}:{args.port}{MCP_PATH}  (Ctrl+C stops it)", flush=True)
    uvicorn.run(http_app(state, key), host=HOST, port=args.port)


if __name__ == "__main__":
    main()
