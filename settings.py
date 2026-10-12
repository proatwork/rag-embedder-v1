"""The one place the scripts take this machine's addresses, ports and keys from.

Every setting has a name (NORMS_...). Its value is looked for in this order:
    1. the environment (export NORMS_EMBEDDING_SERVER=...), which wins;
    2. the file settings.env beside this script, one NAME=value on a line (copy settings.example.env);
    3. the default written here, which suits one machine that runs everything itself.
A script's own option on the command line (--server, --port, ...) still wins over all three: these are its defaults.

settings.env is this machine's own. It is not committed, and it is where an address of the local network or a key
belongs: keep it readable by its owner only (chmod 600 settings.env). A shell can read the same file
(`set -a; . ./settings.env; set +a`), and so can systemd (EnvironmentFile=).

Another file is read instead when NORMS_SETTINGS_FILE names one; an empty NORMS_SETTINGS_FILE means no file at all
(the tests use that, so that they do not depend on the machine they run on). The values of the file are never put into
the environment: they are looked up when a script asks for them.
"""

import os
from collections.abc import Mapping
from pathlib import Path

SETTINGS_FILE = Path(__file__).with_name("settings.env")
EXAMPLE_FILE = Path(__file__).with_name("settings.example.env")  # the committed pattern, with every name
TEXT_ENCODING = "utf-8"
# The names of the settings.
FILE_VARIABLE = "NORMS_SETTINGS_FILE"  # only ever read from the environment
EMBEDDING_SERVER = "NORMS_EMBEDDING_SERVER"
CHAT_SERVER = "NORMS_CHAT_SERVER"
CHAT_MODEL = "NORMS_CHAT_MODEL"
CHATBOT_PORT = "NORMS_CHATBOT_PORT"
MCP_PORT = "NORMS_MCP_PORT"
MCP_KEY = "NORMS_MCP_KEY"
# Their defaults. The embedding model has no setting: the vectors were made with one model, and embed_chunks.py checks
# what it is given against that model's own numbers.
LOCAL_SERVER = "http://127.0.0.1:1234"  # LM Studio on this machine; also the example an error message shows of an address
DEFAULT_CHAT_MODEL = "qwen/qwen3.8-27b"  # the language model's id on the chat server
DEFAULT_CHATBOT_PORT = 8765
DEFAULT_MCP_PORT = 8766
# How the file is written.
COMMENT_MARK = "#"
ASSIGNMENT = "="
EXPORT_PREFIX = "export "  # allowed in front of a name, so that a shell can read the file too
QUOTES = "\"'"
MAX_PORT = 65535


def settings_file(environment: Mapping[str, str] | None = None) -> Path | None:
    """The file the settings are read from: SETTINGS_FILE, or the one FILE_VARIABLE names in `environment` (the
    process's own when none is given); None when FILE_VARIABLE is empty, which means no file."""
    environment = os.environ if environment is None else environment
    if FILE_VARIABLE not in environment:
        return SETTINGS_FILE
    named = environment[FILE_VARIABLE].strip()
    return Path(named) if named else None


def read_file(path: Path) -> dict[str, str]:
    """The settings a file holds, by name; none when there is no such file. A line is `NAME=value`; a line that starts
    with "#" is a comment; quotes around a value are dropped. Stops when the file cannot be read or a line is neither."""
    if not path.is_file():
        return {}
    try:
        lines = path.read_text(encoding=TEXT_ENCODING).splitlines()
    except (OSError, ValueError) as error:
        raise SystemExit(f"cannot read the settings file {path}: {error}")
    values: dict[str, str] = {}
    for number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line or line.startswith(COMMENT_MARK):
            continue
        name, assignment, value = line.removeprefix(EXPORT_PREFIX).partition(ASSIGNMENT)
        name, value = name.strip(), value.strip()
        if not assignment or not name.isidentifier():
            raise SystemExit(f"{path}, line {number}: not NAME=value (a comment starts with {COMMENT_MARK})")
        if len(value) > 1 and value[0] == value[-1] and value[0] in QUOTES:
            value = value[1:-1]
        values[name] = value
    return values


def everything(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    """Every value that is set, by name: those of the settings file, and over them those of `environment` (the
    process's own when none is given)."""
    environment = os.environ if environment is None else environment
    path = settings_file(environment)
    return {**(read_file(path) if path else {}), **environment}


def setting(name: str, environment: Mapping[str, str] | None = None) -> str:
    """The value of `name` from the environment or else the settings file, without the space around it; empty when it is
    set in neither, or set to nothing."""
    return everything(environment).get(name, "").strip()


def port(name: str, default: int, environment: Mapping[str, str] | None = None) -> int:
    """The port `name` is set to, or `default`. Stops when it is set to anything but a port number."""
    value = setting(name, environment)
    if not value:
        return default
    if not value.isascii() or not value.isdigit() or not 0 < int(value) <= MAX_PORT:
        raise SystemExit(f"{name} is set to {value!r}, which is not a port (a whole number from 1 to {MAX_PORT})")
    return int(value)


def embedding_server(environment: Mapping[str, str] | None = None) -> str:
    """The address of the server that runs the embedding model."""
    return setting(EMBEDDING_SERVER, environment) or LOCAL_SERVER


def chat_server(environment: Mapping[str, str] | None = None) -> str:
    """The address of the server that runs the language model: the embedding server's when none is set."""
    return setting(CHAT_SERVER, environment) or embedding_server(environment)


def chat_model(environment: Mapping[str, str] | None = None) -> str:
    """The language model's id on the chat server."""
    return setting(CHAT_MODEL, environment) or DEFAULT_CHAT_MODEL


def chatbot_port(environment: Mapping[str, str] | None = None) -> int:
    return port(CHATBOT_PORT, DEFAULT_CHATBOT_PORT, environment)


def mcp_port(environment: Mapping[str, str] | None = None) -> int:
    return port(MCP_PORT, DEFAULT_MCP_PORT, environment)
