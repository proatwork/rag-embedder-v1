"""Answer a question from the norms: a language model on the server searches them (the tool of search_tool.py) and
writes the answer from what it finds, naming the norm and article behind every statement.

Usage (run with the virtual environment's interpreter):
    venvs/bin/python ask.py --chunks-dir docs_processed/chunks --vectors-dir docs_processed/vectors "Ce rezistență la foc are un perete antifoc?"
    venvs/bin/python ask.py --chunks-dir CHUNKS --vectors-dir VECTORS --top 8 --doc "P118-1/2025" "perete antifoc"
    venvs/bin/python ask.py --chunks-dir CHUNKS --vectors-dir VECTORS --show-prompt "perete antifoc"     # the language model is not asked
    venvs/bin/python ask.py --chunks-dir CHUNKS --vectors-dir VECTORS --chat-server http://HOST:1234 --chat-model ID "perete antifoc"
    venvs/bin/python ask.py --chunks-dir CHUNKS --vectors-dir VECTORS --instructions my-rules.md "perete antifoc"
    venvs/bin/python ask.py --chunks-dir CHUNKS --vectors-dir VECTORS --conversation chat.json "follow-up question"
    venvs/bin/python ask.py --chunks-dir CHUNKS --vectors-dir VECTORS --single-search "perete antifoc"   # one search, with the question's own words
    venvs/bin/python ask.py --chunks-dir CHUNKS --vectors-dir VECTORS --chat-server https://HOST/api/v3 --chat-path /chat/completions --chat-key-env MY_KEY --chat-model ID "perete antifoc"

The model is given the tool search_norms and decides what to search for, in its own words, up to MAX_SEARCHES times
for one question; --top is how many chunks a search gives it and --doc the one norm it may search. Each search is
printed as it is run. A model that answers without a search that found anything, or writes its call as text, is not
taken at its word: it is then given the chunks closest to the question, as with --single-search. That option is the way without the tool,
for a model or a server that takes none: the chunks closest to the question's own words are found once and handed over.

With --conversation FILE, the earlier questions and answers are sent to the language model before the question, so a
follow-up ("și la clădiri înalte?") is understood, and searched for in full. The file is a JSON array of user /
assistant messages holding the plain questions and the answers (not the excerpts, which would grow the prompt by
thousands of tokens a turn); the new pair is appended. Only the last MAX_HISTORY_MESSAGES messages are sent. With
--single-search the chunks are still found from the new question alone. To start fresh, remove the file, or give a new name.

The server (--server) must have the embedding model the vectors were made with loaded, for the question. The language
model (--chat-model) that writes the answer is loaded on the same server, or on the one named by --chat-server. The language model is told to use only the chunks it is
given and to say so when they do not hold the answer. Its rules are in ask-instructions.md (or the file named by
--instructions), where tokens such as {cell_separator}, {status_in_force} and {figures_label} stand for the words the
excerpts use (INSTRUCTION_TOKENS lists them); how it is to search is in ask-tool-instructions.md, which is put in front
of them. Printed: the answer, the searches, then the chunks it was written from, as search.py shows them, numbered as
the answer cites them. The answer is the model's own: check it against those chunks.

From Python, ask() does the same and returns an Answer; main() shows how the index it needs is loaded.
"""

import argparse
import json
import re
import sys
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import settings
from docx_to_txt import CELL_SEPARATOR
from embed_chunks import API_MODEL, Embedder, check_answered_by, endpoint, post_json
from json_to_chunks import ARTICLE_PREFIX, LINE_SEPARATOR, STATUS_IN_FORCE, STATUS_REPEALED, STATUS_UNVERIFIED, load_config
from pipeline_common import ConversionError
from search import (
    AMENDED_MARK, DEFAULT_TOP, FIGURES_LABEL, PAGE_LABEL, PARAGRAPH_PREFIX, POINT_PREFIX, STATUS_LABEL, STATUS_WORDS, Index,
    add_chunks_arguments, add_result_arguments, load_chunks, load_index, nearest, print_results, query_vectors,
)
from search_tool import (
    ARG_ARTICLE, ARG_COUNT, ARG_NORM, ARG_QUERY, EXCERPT_SEPARATOR, MAX_SEARCHES, TOOL_NAME, Search, SearchSession, excerpts,
)

DEFAULT_CHAT_MODEL = settings.chat_model()  # the language model's id on the server, from settings.env
CHAT_PATH = "/v1/chat/completions"  # of the OpenAI API
KEY_HIDDEN = "***"
AUTHORIZATION = "Authorization"  # a hosted API wants the key in this header, as "Bearer <key>"
BEARER = "Bearer "
CHAT_TIMEOUT = 600  # seconds; the whole answer is waited for, and a large model takes its time
TEMPERATURE = 0.2  # low, to stay with the text; not 0, which Qwen3's model card says can make it repeat itself
# The fields of a chat request and of its reply.
API_MESSAGES = "messages"
API_TEMPERATURE = "temperature"
API_ROLE = "role"
API_CONTENT = "content"
API_CHOICES = "choices"
API_MESSAGE = "message"
API_FINISH = "finish_reason"
API_TOOLS = "tools"  # of a request: the tools the model may call
API_TOOL_CALLS = "tool_calls"  # of a reply, and of the assistant message that repeats it
API_TOOL_CALL_ID = "tool_call_id"  # of the message that answers a call
API_ID = "id"
API_TYPE = "type"
API_FUNCTION = "function"
API_NAME = "name"
API_ARGUMENTS = "arguments"
TYPE_FUNCTION = "function"
ROLE_SYSTEM = "system"
ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"
ROLE_TOOL = "tool"  # a message that carries what a tool call gave
CALL_ID = "call_{number}"  # for a call the server gave no id
NO_ARGUMENTS = "{}"  # sent back in place of arguments that are not a JSON object
HISTORY_ROLES = (ROLE_USER, ROLE_ASSISTANT)  # what an earlier exchange may hold
MAX_HISTORY_MESSAGES = 12  # of the earlier exchange, the newest ones (6 questions with their answers); older ones are not sent
FINISH_LENGTH = "length"  # the model ran out of room before it was done
# What a model that thinks before it answers may put in front of the answer; without an end, it never got to the answer.
THINKING = re.compile(r"\A\s*<think>.*?(?:</think>|\Z)", re.DOTALL)
# A tool call written into the answer as text, which a server sends when it did not recognise the call as one: in the
# model's own mark-up, or as a bare JSON object that opens with a field of a call or with any of the tool's parameters.
# It may stand in a code fence.
WRITTEN_CALL = re.compile(
    r"<\|?tool_call\|?>|\[TOOL_CALLS\]|<function=|\A\s*(?:```\w*\s*)?\{\s*\"(?:" + "|".join((API_NAME, API_ARGUMENTS, ARG_QUERY, ARG_ARTICLE, ARG_NORM, ARG_COUNT)) + r")\"",
)
# What a server says when it refuses a request because of its tools: only then is the single search worth advising.
TOOLS_REFUSED = re.compile(r"\b(?:tools?|tool_choice|functions?|function calling)\b", re.IGNORECASE)

INSTRUCTIONS_FILE = Path(__file__).with_name("ask-instructions.md")  # the rules the language model is given
TOOL_INSTRUCTIONS_FILE = Path(__file__).with_name("ask-tool-instructions.md")  # how it is to search, put in front of the rules
TEXT_ENCODING = "utf-8"
# Tokens the instructions file may use; each is replaced by its value, and nothing else in the file is touched. They are
# the words the excerpts use (see search.py), so that the rules and the excerpts cannot come to say different things.
INSTRUCTION_TOKENS = {
    "{cell_separator}": CELL_SEPARATOR, "{status_label}": STATUS_LABEL, "{status_in_force}": STATUS_WORDS[STATUS_IN_FORCE],
    "{status_unverified}": STATUS_WORDS[STATUS_UNVERIFIED], "{status_repealed}": STATUS_WORDS[STATUS_REPEALED],
    "{amended_mark}": AMENDED_MARK, "{figures_label}": FIGURES_LABEL, "{article_prefix}": ARTICLE_PREFIX.strip(),
    "{point_prefix}": POINT_PREFIX.strip(), "{paragraph_prefix}": PARAGRAPH_PREFIX.strip(), "{page_label}": PAGE_LABEL,
}
# Tokens the tool instructions file may use besides those: the tool's own name and parameters, and its limit of calls.
TOOL_TOKENS = {
    "{tool_name}": TOOL_NAME, "{max_searches}": str(MAX_SEARCHES), "{arg_query}": ARG_QUERY, "{arg_norm}": ARG_NORM,
    "{arg_article}": ARG_ARTICLE,
}
SEARCH_TOKENS = {**INSTRUCTION_TOKENS, **TOOL_TOKENS}  # all a file may use that tells a model how to search
REQUEST_TEMPLATE = "Excerpts from the norms:{separator}{excerpts}{separator}Question: {question}"
CUT_NOTE = "The answer above is cut short: the model ran out of room. Load it on the server with a larger context length."
NO_ROOM = ": it ran out of room before it got to the answer; load it on the server with a larger context length"
NO_TOOLS_HINT = " (if this model or server takes no tools, use the single search instead)"
FELL_BACK_NOTE = (
    "The model's own searching was not used (it answered without a search that found anything, or wrote a call as text): "
    "it was given the chunks closest to the question instead."
)
SEARCH_LINE = "search {number}: {what}{norm}: {outcome}"
SEARCH_FOR_QUERY = '"{query}"'
SEARCH_FOR_ARTICLE = "number {article}"
SEARCH_IN_NORM = " in {norm}"
SEARCH_FOUND = "{found} chunks, {new} new"
SEARCH_NOT_RUN = "not run: {problem}"
PROMPT_PART = "--- {role} ---"
PROMPT_TOOLS = "tools"  # the part of a shown prompt that holds the tool the model is given


@dataclass
class Answer:
    """What the language model wrote, and what it wrote it from."""
    text: str
    sources: list[tuple[float | None, dict]]  # the chunks the model was given with their scores (none for one fetched by its number), numbered from 1 in this order
    model: str
    cut: bool  # the model ran out of room: `text` is not the whole answer
    searches: list[Search] = field(default_factory=list)  # the searches the model made, in order; none when it was handed the chunks
    fell_back: bool = False  # the model was to search and did not: it was handed the chunks closest to the question


@dataclass
class ToolCall:
    """A call of a tool, as a model made it."""
    id: str
    name: str
    arguments: str  # the JSON text the model wrote


@dataclass
class Reply:
    """What a model sent back to one request: its text without its thinking, and the tool calls it made."""
    text: str
    calls: list[ToolCall]
    cut: bool = False  # the model ran out of room


def load_instructions(path: Path = INSTRUCTIONS_FILE, tokens: dict[str, str] = INSTRUCTION_TOKENS) -> str:
    """The rules for the language model, read from `path`, with each of the `tokens` the file uses replaced by its
    value. Raises ConversionError when the file cannot be read or is empty."""
    try:
        text = path.read_text(encoding=TEXT_ENCODING).strip()
    except OSError as error:
        raise ConversionError(f"cannot read the instructions file {path}: {error}") from error
    if not text:
        raise ConversionError(f"the instructions file {path} is empty")
    for token, value in tokens.items():
        text = text.replace(token, value)
    return text


def tool_instructions(rules: str, path: Path = TOOL_INSTRUCTIONS_FILE) -> str:
    """What a model that searches for itself is told: how to search (the file at `path`, which may use TOOL_TOKENS as
    well as INSTRUCTION_TOKENS), then the `rules` for reading the excerpts and writing the answer. Raises as
    load_instructions does."""
    return load_instructions(path, SEARCH_TOKENS) + EXCERPT_SEPARATOR + rules


def framed(instructions: str, history: list[dict] | None, request: str) -> list[dict]:
    """A request to the language model as messages: the rules, then the newest MAX_HISTORY_MESSAGES of the earlier
    exchange `history` (user / assistant messages, without the system message), then `request` as the user's."""
    messages = [{API_ROLE: ROLE_SYSTEM, API_CONTENT: instructions}]
    messages.extend(history[-MAX_HISTORY_MESSAGES:] if history else [])
    messages.append({API_ROLE: ROLE_USER, API_CONTENT: request})
    return messages


def build_messages(question: str, sources: list[tuple[float, dict]], config: dict, instructions: str, history: list[dict] | None = None) -> list[dict]:
    """What the language model is sent when it is handed the chunks: the rules (`instructions`), then the earlier exchange
    (see framed), then the chunks and the question. A chunk is given as it was embedded, under its citation and status
    (see search_tool.excerpt); the embedded text itself does not change."""
    numbered = list(enumerate((record for _, record in sources), start=1))
    request = REQUEST_TEMPLATE.format(separator=EXCERPT_SEPARATOR, excerpts=EXCERPT_SEPARATOR.join(excerpts(numbered, config)), question=question)
    return framed(instructions, history, request)


def tool_messages(question: str, instructions: str, history: list[dict] | None = None) -> list[dict]:
    """What the language model is sent when it searches for itself: `instructions` (see tool_instructions), then the
    earlier exchange (see framed), then the question as it was asked."""
    return framed(instructions, history, question)


def prepare(
    question: str, index: Index, config: dict, server: str, top: int, doc: str | None, instructions: str,
    history: list[dict] | None = None,
) -> tuple[list[tuple[float, dict]], list[dict]]:
    """The chunks closest to a question, and what the language model is sent about them. Asks the server for the
    question's vector, and raises ConversionError when it cannot. Stops the program at a `doc` no chunk has."""
    sources = nearest(index, query_vectors(Embedder(index.model, server), [question])[0], top, doc)
    return sources, build_messages(question, sources, config, instructions, history)


def api_key_from(variable: str | None) -> str | None:
    """The API key kept under the name `variable` in the environment or in settings.env (it is never given on the
    command line, which shells remember); None when no name is given. Raises ConversionError when it is empty or not set."""
    if variable is None:
        return None
    key = settings.setting(variable)
    if not key:
        raise ConversionError(f"{variable} is not set or is empty: write the API key under that name into {settings.SETTINGS_FILE.name}, or export it")
    if not key.isascii() or not key.isprintable() or any(character.isspace() for character in key):
        raise ConversionError(f"{variable} holds something that cannot be a key (spaces, line breaks or odd characters)")
    return key


def tool_calls_of(message: dict) -> list[ToolCall]:
    """The tool calls of a model's message, whatever else is in the list. A call without an id is given one that no
    other call of the message has; arguments sent as an object are turned into the JSON text other servers send."""
    entries = [
        entry for entry in message.get(API_TOOL_CALLS) or []
        if isinstance(entry, dict) and isinstance(entry.get(API_FUNCTION), dict) and isinstance(entry[API_FUNCTION].get(API_NAME), str)
    ]
    taken = {entry[API_ID] for entry in entries if isinstance(entry.get(API_ID), str)}
    spare = (made for number in itertools.count(1) if (made := CALL_ID.format(number=number)) not in taken)
    calls = []
    for entry in entries:
        arguments = entry[API_FUNCTION].get(API_ARGUMENTS)
        calls.append(ToolCall(
            entry[API_ID] if isinstance(entry.get(API_ID), str) and entry[API_ID] else next(spare),
            entry[API_FUNCTION][API_NAME], arguments if isinstance(arguments, str) else json.dumps(arguments or {}, ensure_ascii=False),
        ))
    return calls


def chat_reply(
    server: str, model: str, messages: list[dict], path: str = CHAT_PATH, api_key: str | None = None, tools: list[dict] | None = None,
) -> Reply:
    """What the language model sends back to `messages`: its text without its thinking, the calls it made of the
    `tools` it was given, and whether it was cut short. Raises when the server cannot be used, when another model
    answered, and when there is neither an answer nor a call. With an `api_key` the server is a hosted API: the key is
    sent, and the model that answered is not compared, because such an API may answer under the name of the model an
    alias resolves to (LM Studio is the server that answers an unknown id with another model)."""
    url = endpoint(server, path)
    headers = {AUTHORIZATION: BEARER + api_key} if api_key else None
    body = {API_MODEL: model, API_MESSAGES: messages, API_TEMPERATURE: TEMPERATURE, **({API_TOOLS: tools} if tools else {})}
    try:
        reply = post_json(url, body, CHAT_TIMEOUT, headers)
    except ConversionError as error:
        message = str(error) + (NO_TOOLS_HINT if tools and TOOLS_REFUSED.search(str(error)) else "")
        if api_key and api_key in message:  # a server may echo the key in its error
            message = message.replace(api_key, KEY_HIDDEN)
        raise ConversionError(message) from None
    if not api_key:
        check_answered_by(reply, model, url)
    try:
        choice = reply[API_CHOICES][0]
        text = THINKING.sub("", choice[API_MESSAGE].get(API_CONTENT) or "").strip()  # a message that only makes calls may come without content
        calls = tool_calls_of(choice[API_MESSAGE])
        cut = choice.get(API_FINISH) == FINISH_LENGTH
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        raise ConversionError(f"{url} did not answer with a message: {type(error).__name__}: {error}") from error
    if not text and not calls:
        raise ConversionError(f"{model} gave no answer" + (NO_ROOM if cut else ""))
    return Reply(text, calls, cut)


def complete(server: str, model: str, messages: list[dict], path: str = CHAT_PATH, api_key: str | None = None) -> tuple[str, bool]:
    """The language model's answer to `messages`, without its thinking, and whether it was cut short (see chat_reply,
    which this is without tools)."""
    reply = chat_reply(server, model, messages, path, api_key)
    return reply.text, reply.cut


def sendable_arguments(arguments: str) -> str:
    """The arguments of a call as they are sent back to the model: as it wrote them when they are a JSON object, else
    an empty object (a server may read them, and refuse the whole request for arguments that are not JSON; the model is
    told what was wrong with them in the call's result)."""
    try:
        return arguments if isinstance(json.loads(arguments), dict) else NO_ARGUMENTS
    except (ValueError, RecursionError):
        return NO_ARGUMENTS


def calls_message(reply: Reply) -> dict:
    """A model's message that made tool calls, as it is sent back to it before the results of those calls."""
    calls = [
        {API_ID: call.id, API_TYPE: TYPE_FUNCTION, API_FUNCTION: {API_NAME: call.name, API_ARGUMENTS: sendable_arguments(call.arguments)}}
        for call in reply.calls
    ]
    return {API_ROLE: ROLE_ASSISTANT, API_CONTENT: reply.text, API_TOOL_CALLS: calls}


def answer_with_tools(
    session: SearchSession, messages: list[dict], ask_model: Callable[[list[dict], list[dict] | None], Reply],
    searched: Callable[[Search], None] | None = None,
) -> Reply | None:
    """Let the model search until it answers. `messages` is what it is sent first (see tool_messages); its calls and
    what they gave are added to it here. `ask_model(messages, tools)` asks the model once and gives its Reply; `tools`
    is None when it is to answer and may search no more. `searched` is called after each call was run, with the call
    (see search_tool.Search). Every call is answered by `session`, which numbers the chunks and counts the calls. Of
    the calls of one reply, those up to the limit are run and one more is told that the limit is reached; the rest are
    dropped. A model that was told so is asked once more without the tool, to answer from what it has.
    Returns the reply that holds the answer; None when the model cannot be taken at its word: no search of its gave
    it anything (it made none, or none that could be run), or it wrote a call as text, or it called a tool it was not
    given. The caller then hands it the chunks itself.
    Raises what `ask_model` raises, and ConversionError when the embedding server cannot be used."""
    tools: list[dict] | None = [session.schema()]
    while True:
        reply = ask_model(messages, tools)
        if not reply.calls:
            return reply if session.sources and not WRITTEN_CALL.search(reply.text) else None
        if tools is None:
            return None
        reply.calls = reply.calls[:session.max_searches + 1 - len(session.searches)]
        messages.append(calls_message(reply))
        for call in reply.calls:
            result = session.reply(call.arguments, call.name)
            messages.append({API_ROLE: ROLE_TOOL, API_TOOL_CALL_ID: call.id, API_CONTENT: result})
            if searched:
                searched(session.searches[-1])
        if session.refused:
            tools = None


def ask(
    question: str, index: Index, config: dict, server: str,
    chat_model: str = DEFAULT_CHAT_MODEL, top: int = DEFAULT_TOP, doc: str | None = None, chat_server: str | None = None,
    instructions: str | None = None, chat_path: str = CHAT_PATH, chat_api_key: str | None = None, history: list[dict] | None = None,
    single_search: bool = False, searched: Callable[[Search], None] | None = None,
) -> Answer:
    """Answer `question` from `index`: the language model searches it for itself (see answer_with_tools), getting `top`
    chunks a search and, with `doc`, searching that norm only; `searched` is called after each of its searches. With
    `single_search`, or when the model does not search, it is handed the `top` chunks closest to the question instead.
    `index` and `config` come from search.load_index and json_to_chunks.load_config; `instructions` is the rules text
    (default: the instructions file); `chat_path` and `chat_api_key` are for a hosted API; `server` runs the embedding
    model and, without `chat_server`, the language model too; `history` is the earlier exchange (see framed).
    Raises ConversionError when the language model or the embedding server cannot be used, or there is no answer."""
    rules = instructions or load_instructions()
    chat = chat_server or server
    if not single_search:
        session = SearchSession(index, config, server, top, doc)
        reply = answer_with_tools(
            session, tool_messages(question, tool_instructions(rules), history),
            lambda messages, tools: chat_reply(chat, chat_model, messages, chat_path, chat_api_key, tools), searched,
        )
        if reply is not None:
            return Answer(reply.text, session.sources, chat_model, reply.cut, session.searches)
    sources, messages = prepare(question, index, config, server, top, doc, rules, history)
    text, cut = complete(chat, chat_model, messages, chat_path, chat_api_key)
    return Answer(text, sources, chat_model, cut, fell_back=not single_search)


def search_line(search: Search) -> str:
    """One search of the model on one line: what it looked for and how much it found, or why it was not run."""
    what = SEARCH_FOR_ARTICLE.format(article=search.article) if search.article else SEARCH_FOR_QUERY.format(query=search.query)
    outcome = SEARCH_NOT_RUN.format(problem=search.problem) if search.problem else SEARCH_FOUND.format(found=len(search.numbers), new=search.new)
    return SEARCH_LINE.format(number=search.number, what=what, norm=SEARCH_IN_NORM.format(norm=search.norm) if search.norm else "", outcome=outcome)


def checked_history(messages, source: str) -> list[dict]:
    """`messages` as an earlier exchange: a list of {"role": user | assistant, "content": text}. Raises ConversionError,
    naming `source`, when it is anything else."""
    if not isinstance(messages, list):
        raise ConversionError(f"{source}: the exchange must be a JSON array of messages")
    for message in messages:
        if not isinstance(message, dict) or message.get(API_ROLE) not in HISTORY_ROLES or not isinstance(message.get(API_CONTENT), str):
            raise ConversionError(f"{source}: every message must have a role ({' or '.join(HISTORY_ROLES)}) and a text content")
    return [{API_ROLE: message[API_ROLE], API_CONTENT: message[API_CONTENT]} for message in messages]


def load_conversation(path: Path) -> list[dict]:
    """The exchange saved at `path` (see checked_history), or an empty list when the file is new."""
    try:
        messages = json.loads(path.read_text(encoding=TEXT_ENCODING))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise ConversionError(f"cannot read the conversation file {path}: {error}") from error
    return checked_history(messages, f"conversation file {path}")


def remembered(question: str, answer: str) -> list[dict]:
    """What a conversation keeps of one turn: the plain question and the answer, without the excerpts."""
    return [{API_ROLE: ROLE_USER, API_CONTENT: question}, {API_ROLE: ROLE_ASSISTANT, API_CONTENT: answer}]


def save_conversation(path: Path, messages: list[dict]) -> None:
    """Write the exchange back, creating the file when it is new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(messages, indent=2, ensure_ascii=False) + "\n", encoding=TEXT_ENCODING)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_chunks_arguments(parser)
    parser.add_argument("question", help="the question to answer")
    add_result_arguments(parser, "give the language model")
    parser.add_argument("--chat-model", default=DEFAULT_CHAT_MODEL, help="the language model's id on the server (default: %(default)s)")
    parser.add_argument("--chat-server", default=settings.setting(settings.CHAT_SERVER) or None, help=f"address of the server that runs the language model (http://host:port); default: {settings.CHAT_SERVER} of settings.env, else --server")
    parser.add_argument("--chat-path", default=CHAT_PATH, help="path of the chat endpoint on the chat server (default: %(default)s); a hosted OpenAI-style API often uses /chat/completions")
    parser.add_argument("--chat-key-env", metavar="VARIABLE", help="the name the chat server's API key is kept under, in settings.env or the environment; it is sent as a Bearer token (never give the key itself here)")
    parser.add_argument("--instructions", type=Path, default=INSTRUCTIONS_FILE, help="file with the rules for the language model (default: %(default)s)")
    parser.add_argument("--show-prompt", action="store_true", help="print what the language model would be sent, and do not ask it")
    parser.add_argument("--single-search", action="store_true", help="do not let the model search: hand it the chunks closest to the question's own words")
    parser.add_argument("--conversation", type=Path, help="save and continue a conversation (a JSON file of the exchange)")
    args = parser.parse_args()
    if args.vectors_dir is None:
        parser.error("an answer needs --vectors-dir")

    config = load_config(args.config)
    try:
        instructions = load_instructions(args.instructions)
        chat_api_key = api_key_from(args.chat_key_env)
    except ConversionError as error:
        sys.exit(str(error))
    try:
        history = load_conversation(args.conversation) if args.conversation else None
    except ConversionError as error:
        sys.exit(str(error))
    index = load_index(load_chunks(args.chunks_dir), args.vectors_dir, config)

    if args.show_prompt:
        try:
            if args.single_search:
                _, messages = prepare(args.question, index, config, args.server, args.top, args.doc, instructions, history)
                parts = []
            else:
                session = SearchSession(index, config, args.server, args.top, args.doc)
                messages = tool_messages(args.question, tool_instructions(instructions), history)
                parts = [(PROMPT_TOOLS, json.dumps(session.schema(), indent=2, ensure_ascii=False))]
        except ConversionError as error:
            sys.exit(str(error))
        parts = [(message[API_ROLE], message[API_CONTENT]) for message in messages] + parts
        print(EXCERPT_SEPARATOR.join(PROMPT_PART.format(role=role) + LINE_SEPARATOR + text for role, text in parts))
        return

    try:
        answer = ask(
            args.question, index, config, args.server, args.chat_model, args.top, args.doc, args.chat_server, instructions,
            args.chat_path, chat_api_key, history, args.single_search, lambda search: print(search_line(search), file=sys.stderr, flush=True),
        )
    except ConversionError as error:
        sys.exit(str(error))

    if args.conversation:
        save_conversation(args.conversation, (history or []) + remembered(args.question, answer.text))

    print(answer.text, end=EXCERPT_SEPARATOR)
    if answer.cut:
        print(CUT_NOTE, end=EXCERPT_SEPARATOR)
    if answer.fell_back:
        print(FELL_BACK_NOTE, end=EXCERPT_SEPARATOR)
    if answer.searches:
        print(LINE_SEPARATOR.join(search_line(search) for search in answer.searches), end=EXCERPT_SEPARATOR)
    print(f"Written by {answer.model} from:", end=EXCERPT_SEPARATOR)
    print_results(answer.sources)


if __name__ == "__main__":
    main()
