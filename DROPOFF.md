# Drop-off: move to Ubuntu, and the search as an MCP server for Claude

State as of **2026-10-12**, written on the Mac. Read this first, then `COMMANDS.md` (how every script is run) and
`MANUAL.md`. This file is "where are we, what is decided, what is next"; it does not repeat the two manuals.

**The two "Update" sections below are the current state**; the rest is the findings and the plan they grew from, kept
for the reasons it gives.

## Update: one settings file (branch `poc`)

- **Addresses, ports and keys are in `settings.env`**, read by `settings.py` (see *Settings* in `COMMANDS.md`). The
  file is not committed; `settings.example.env` is the committed pattern. No script holds an address of the local
  network any more: the default is `http://127.0.0.1:1234`. What this file says below about hardcoded addresses,
  about `--server` being needed on the box, and about the key in `~/.config/norms-mcp.key` is how it was before.
- **Every machine has its own `settings.env`**: copy the code with `--exclude settings.env`, or the box's server
  address and key are overwritten by the Mac's.
- **The MCP key** is `NORMS_MCP_KEY` in the box's `settings.env`, the same value as before (Claude's connector holds
  it). Do not make a new one unless the connector is to be added again.
- **This file still names the box, its address and the public name of the server.** Take it out, with the history,
  before the repository is made public.

## Update, later on 2026-10-12: the box is set up for search

Done from the Mac over SSH, after the rest of this file was written. Where the two disagree, this section is right.

- **The box is `g3`, `proatwork@192.168.0.241`, folder `/home/proatwork/ai/embedding-test`.** It is *not* the
  i7-3770S of finding 1: Ubuntu 22.04.5, i5-8300H (**has AVX2**), GTX 1050 Ti (4 GB), 15 GB RAM. `sudo` asks for a
  password, so apt installs are the user's.
- **Python 3.11 is required, not optional.** Six pins refuse 3.10 (PyPI's Requires-Python: `numpy`, `pandas`, `scipy`,
  `networkx`, `shapely`, `rpds-py`). The user installed `python3.11` (deadsnakes) and Tesseract with `ron`.
- **`venvs/` exists on the box, with only what search needs** (58 packages, no `torch`, no `docling`):
  `venvs/bin/python -m pip install -c requirements.txt numpy semchunk docling-core transformers`. The `-c` keeps the
  versions of `requirements.txt` and skips its Mac-only lines. PDF conversion (`pdf_to_md.py`) will not run until the
  rest is installed.
- **LM Studio runs on the box itself**, with `text-embedding-bge-m3`. After the reboot (below) its server was off;
  it was started with `~/.lmstudio/bin/lms server start` and now listens on **`127.0.0.1:1234` only** (before the
  reboot: `192.168.0.241:1234`). So on the box every script needs `--server http://127.0.0.1:1234`, and other
  machines cannot reach it. It does not start by itself after a reboot. The default in the code, `192.168.0.157`,
  did not answer on this day.
- **`search.py` works on the box, both ways** (run and read): `--article 2.3.2.1.2`, and a search by meaning.
  11 chunks embedded again on the box match their stored vectors (similarity 0.9992 or more), so the box's bge-m3
  and the vectors agree.
- **The GPU is not in use.** The first search crashed the model: `CUDA error: unspecified launch failure`, then
  `ggml_cuda_init: failed to initialize CUDA: unknown error`; LM Studio loaded it again on the CPU, where it works.
  The box had slept from 23:32 to 00:20; CUDA failing after a suspend until the driver is reloaded is a known
  pattern (a guess here, not proven). **The user rebooted, and the GPU is in use again**: its memory went from
  148 MiB to 824 MiB when the model loaded, and the search ran. A box that serves Claude must not sleep.
- **A shared tmux session `claude` is on the box** (the user watches with
  `ssh -t proatwork@192.168.0.241 tmux attach -t claude`). The user wants commands on the box run inside it, so that
  they can see them. Its shell has a `PROMPT_COMMAND` that releases the tmux lock `claude-lock`. It is gone after a
  reboot.
- **Not done on the box:** the full install (`torch`, `docling`), so `pdf_to_md.py` does not run there.

## Update: the MCP server is built (2026-10-12)

- **`mcp_server.py` exists and works**, with `mcp-instructions.md` (its rules, in the tool's description) and
  `tests/test_mcp_server.py`. How to run it, the proxy's part and the steps in Claude are in `COMMANDS.md`, section 9.
- **What changed besides:** `search_tool.SearchSession` takes `max_searches=None` (no limit, a header that does not
  count) and `figures_shown`; `search.figures_note` takes `shown`. The defaults keep `ask.py` and the chatbot as they
  were. `requirements.txt` has the 14 new pins of `mcp` 2.3.0.
- **Run and read, on the Mac and on the box:** 278 tests in `tests/`, 144 in `chatbot/tests/`, all passing. On the box
  the server was started against the real chunks and the box's LM Studio and called by the `mcp` package's own
  client over HTTP: 401 without the key; with it, the tool listed with its three norms, an article fetched by number,
  a search by meaning answered. That test server was stopped; nothing listens on 8766 now.
- **How it was written against `mcp` 2.3.0** (read in the installed source, not from memory of 1.x): the low-level
  `mcp.server.lowlevel.Server` with `on_list_tools` / `on_call_tool`, its `streamable_http_app(json_response=True,
  stateless_http=True)`, and a small ASGI class of our own (`KeyGate`) in front for the key. The package's own OAuth
  machinery is not used.
- **A decision to know about: the key is the only gate.** The package's check of the `Host` and `Origin` headers is
  switched off, so there is no `--public-host` option (the plan below had one). Reason: nothing passes `KeyGate`
  without the key, so the check kept nobody out; and it would have refused every request of Claude if Anthropic's
  servers send an `Origin` header, which could not be found out from here.
- **A fresh review subagent went over it**; its one must-fix (a failure inside a search other than the embedding
  server's reached the client with its own text) and the smaller points are fixed, each with a test.
- **It is live and connected to claude.ai (2026-10-12).** The user put a Cloudflare tunnel in front (`cloudflared` on
  the box, a token-based tunnel set up in the dashboard): `https://ailinca.proatwork.ro/mcp` goes to
  `http://127.0.0.1:8766`. The connector was added in Claude with "No sign-in" and the request header
  `Authorization: Bearer <key>`, and it connected. The first try failed ("Couldn't connect to the server") because
  the header was not there yet: the server's output then shows Claude's OAuth lookups and `POST /mcp`, all `401`.
  Request headers are a beta of Claude's that not every account has; without them the fallback would be the key in
  the address (`/mcp/<key>`), which was designed but **not built**, as it was not needed.
- **Not done, and whose it is:**
  - The domain, the certificate and the reverse proxy: **the user's**. Until then nothing can be tried from claude.ai.
  - Starting the server for real on the box, with the user's own key (kept in `~/.config/norms-mcp.key`): after the proxy.
  - Nothing starts by itself after a reboot: neither LM Studio's server nor `mcp_server.py` (no systemd unit).
  - The changes are **not committed**: the repository (`origin` is `github.com:proatwork/rag-embedder-v1`, data
    folders ignored) has one commit, from before this work. The box has no `git`; its copy was made with `rsync`.

## What the user wants

1. Move this folder from the Mac to a separate Ubuntu machine.
2. Wrap the search (`search_tool.py`) in an MCP server and use it from **every Claude**: claude.ai in a browser, Claude
   Desktop, the phone app, Claude Code. The user handles **SSL and the domain** themselves.

## Where we are

| Item | State |
|---|---|
| Why `llmster` does not run on the Ubuntu box | **Found** (CPU has no AVX2, see below) |
| What breaks when the folder moves to Ubuntu | **Found**, listed below. Nothing fixed |
| Five small changes that make the setup portable | **Proposed, not approved, not done** |
| MCP server for Claude | **Built and tested** (see the update below); the public address is the user's part |

## Finding 1: `llmster` on the Ubuntu box

- The box (`proatworkdv7`, Ubuntu 24.04.1) has an **Intel i7-3770S**: AVX, but **no AVX2**, on bare metal. `lms` dies
  with `Illegal instruction (core dumped)`. No setting fixes that; LM Studio's headless daemon will not run there.
- The way out is `llama.cpp` built on the box (it compiles for the CPU it finds). Its `llama-server` speaks the
  OpenAI-style API the scripts use (`/v1/embeddings`, `/v1/chat/completions`, `/v1/models`).
- **Not run, from memory:**
  ```sh
  sudo apt install -y build-essential cmake git libcurl4-openssl-dev
  git clone https://github.com/ggml-org/llama.cpp && cd llama.cpp && cmake -B build && cmake --build build -j --config Release
  ./build/bin/llama-server -m /path/to/bge-m3.gguf --embedding --alias text-embedding-bge-m3 --host 0.0.0.0 --port 1234
  ```
  `--alias` gives the id `embed_chunks.py` expects (`DEFAULT_MODEL`), so its "answered with model X, not Y" check
  passes. One process serves one model: with `--embedding` it serves embeddings only.
- The chatbot's *LM Studio (native)* API style does not exist on `llama-server`; use *OpenAI-style* with `/v1`.
- A chat model on that CPU would be a few tokens a second at best. Embeddings are slow but usable.
- **Unknown:** whether the "separate Ubuntu machine" of the move is this same box. Ask.

## Finding 2: what breaks when the folder moves to Ubuntu

The code itself is portable: no absolute Mac paths, no symlinks, no odd file names. The environment is not.

- **Do not copy `venvs/`** (1.4 GB of Mac binaries). Build a new one on the box.
- **`requirements.txt` does not install on Linux as it is.** Six lines are Mac-only: `ocrmac` and five `pyobjc-*`.
  On the box the packages were installed with `pip install -c requirements.txt <names>`, which skips them.
- **`torch` on Linux pulls several GB of NVIDIA libraries by default.** Without an NVIDIA card, install with
  `--extra-index-url https://download.pytorch.org/whl/cpu`.
- **Python:** the pins were made on 3.11; Ubuntu 24.04 ships 3.12. Not tested on 3.12.
- **Tesseract:** `sudo apt install tesseract-ocr tesseract-ocr-ron` (`pdf_to_md.py` needs the `ron` language).
- **`pdf_to_md.py --device`** offers `auto`, `cpu`, `mps` only (`pdf_to_md.py:32`); `mps` is a Mac thing.
- **The chatbot answers on `127.0.0.1` only** and rejects any other Host (`chatbot/server.py:65`, `own_origins`).
  From another machine: `ssh -L 8765:127.0.0.1:8765 user@box`, then open `http://127.0.0.1:8765`.
- **The model server address is hardcoded** as `http://192.168.0.157:1234` in `embed_chunks.py:38` and
  `chatbot/server.py:67`. `--server` overrides it.
- **The vectors copy as they are** (`docs_processed/`, 97 MB). Questions must still be embedded by bge-m3.
- **The tokenizer `BAAI/bge-m3` downloads from Hugging Face on first run** (the box needs internet once).
- **The folder is not a git repo.** The copy is the only history; keep the Mac folder until the box is proven.
- **If the box is the i7-3770S:** run `venvs/bin/python -c "import numpy, torch, search_tool"` first. It is not known
  whether these builds run without AVX2; it would be the same crash as `llmster`.

Copy command (not run; `OLD/` is 1.1 GB, drop its exclude to take it along):

```sh
rsync -av --exclude venvs --exclude OLD --exclude __pycache__ --exclude .DS_Store --exclude .claude --exclude settings.env /Users/proatwork/htdocs/proatwork/ai-dev/embedding-test/ user@box:embedding-test/
```

## Proposed, not approved: five changes for portability

1. `requirements.txt`: mark the six Mac-only lines with `; sys_platform == "darwin"`, so one file serves both.
2. Settle on one Python (get 3.11 on Ubuntu, or test the pins on 3.12). **The user has not chosen.**
3. Read the model server address from one environment variable, today's value as the fallback (two files).
4. Add `cuda` to `DEVICES` in `pdf_to_md.py`.
5. Make the folder a git repo; ignore `venvs/`, `__pycache__`, `.DS_Store`, `OLD/`.

Left alone on purpose: the chatbot's `127.0.0.1` (the SSH tunnel does it), and Docker (overkill here).

## The MCP plan

### Checked facts

Read on Anthropic's help page on 2026-10-12
(<https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp>):

- A custom connector is reached **from Anthropic's servers, not from the user's machine**, in every client: claude.ai,
  Claude Desktop, Cowork, the mobile apps. So the server needs a public HTTPS address; a LAN address does not work.
- The connector dialog has **Request headers**: fixed credentials "such as API keys that Claude sends on every
  request", for a server that uses "an API key, bearer token, or other fixed credential instead of OAuth". So a fixed
  key is enough; **no OAuth**. In the dialog: Authentication "No sign in", and the key under Request headers.
- A connector cannot be edited: to change the URL or key it is removed and added again.
- Not checked: the exact Claude Code command. From memory:
  `claude mcp add --transport http norms https://DOMAIN/mcp --header "Authorization: Bearer KEY"`.

### Design

- `mcp_server.py` in this folder: a small HTTP server (MCP "Streamable HTTP"), listening on `127.0.0.1`. The user's
  proxy terminates TLS and forwards `/mcp` to it.
- The key is read from an **environment variable**, never from the command line. No key: refuse to start. Compare
  with `hmac.compare_digest`, answer 401 otherwise, never log the key.
- One tool, `search_norms`, with the arguments it has today (`query`, `norm`, `article`, `count`).
- **Each call stands alone.** No numbers running on from one search to the next, no limit of 5 for a question: MCP
  has no notion of "one question". Claude cites by norm and article (`P118/2-2013, pct. 7.10`), not by `[n]`. This is
  the one change in behaviour from the chatbot, and the user was told.

### Build list

1. **`mcp` is 2.3.0, a new major version** (it splits off `mcp-types` and uses `httpx2`). Do not write against the 1.x
   `FastMCP` API from memory: install it and read its server and Streamable-HTTP API first.
   `venvs/bin/python -m pip install --dry-run mcp` on the Mac said it adds 14 packages and upgrades none:
   `mcp mcp-types httpx2 httpcore2 starlette uvicorn sse-starlette python-multipart PyJWT cryptography cffi pycparser
   opentelemetry-api truststore`.
2. **Take the input schema from `search_tool.tool_schema()`** (`search_tool.py:105`), not from a decorated function's
   signature: the list of norms and the help texts stay in one place. That probably means the low-level server API,
   with an explicit input schema.
3. **One `SearchSession` per call** (`search_tool.py:128`). As it is, `RESULT_HEADER` (`search_tool.py:54`) would print
   "Search 1 of at most 5" on every call, and `LAST_SEARCH` would follow a limit that is not there: the session needs
   a way to run with no limit and a header without the count.
4. **Do not send `ask-tool-instructions.md` unchanged**: it says the numbers run on and that there are 5 searches,
   both false here. Write a text for MCP. Put the rules that matter (search in Romanian, cite norm and point, read
   the status line of each excerpt) in the **tool description**: it is not known that claude.ai shows a server's own
   instructions to the model.
5. **`ConversionError`** (the embedding server is down) becomes a tool error with a readable message, not a crash.
6. **Run the embedding call in a worker thread**: `Embedder` uses blocking `urllib`, and clients share one event loop.
7. **Behind the proxy:** check whether the package validates the `Host` header. If it does, the public domain must be
   allowed (an option of the script), or every request through the proxy is rejected. The proxy must not buffer the
   replies (nginx: `proxy_buffering off`). Both belong in the manual.
8. **`requirements.txt` is a full `pip freeze`**: add all the new pins, not `mcp` alone.
9. **Tests** with `unittest`, beside the others, the embedding call replaced as in `tests/test_search_tool.py`: 401
   without the key and with a wrong one; the tool list carries the norms; a search by meaning and a fetch by number
   return excerpts; a failing embedding server gives a tool error. Then both existing suites (240 and 144 tests, see
   `COMMANDS.md`, "Tests").
10. **Docs:** a section in `COMMANDS.md` (the chatbot is section 8 there): how to start it, the key, what the proxy
    must do, the steps in Claude's connector dialog, the Claude Code command.

### Open problems the MCP server does not solve

- **A search by meaning needs bge-m3 reachable from the box.** Today that is LM Studio at `192.168.0.157:1234`. If
  that machine is not kept on, `llama-server` on the box is the replacement (finding 1). Fetching an article by its
  number works without any model.
- **Nothing here can be tested against claude.ai** without the public address. Test locally with a client of the
  `mcp` package; the first real connection is the user's.
- Anyone who holds the key can search. The norms are public documents; the cost of a leaked key is load on the box.
- Excerpts go out as text. The pictures of a chunk are not sent (MCP can carry images; not planned now).

## How the user likes to work

From `~/.claude/_bootstrap` on the Mac (that folder and the Mac's memory are **not** on the box):

- Blunt, verdict first, no filler. Say plainly what was not verified. Never "this works" unless it was run.
- **Short replies in simple terms.** Long blocks of text are hard for the user to follow.
- **On a question or a bug report: explain, then wait for a go-ahead** before editing or running anything. A direct
  request to build something is a go-ahead.
- DRY, no magic values (constants), import what exists instead of rewriting it.
- After a non-trivial change: run the tests, then have a **fresh review subagent** check requirements, integration
  points and error paths; fix what it finds; then report.
- Offer tests at the start of a feature, and a manual update at a milestone.
- Never mount disks or images, never use `hdiutil`.

## Don'ts

- Do not copy or "repair" `venvs/` from the Mac; build it new.
- Do not open the chatbot or the MCP server to the network by binding to `0.0.0.0`: the proxy or an SSH tunnel is
  the way in.
- Do not put the key in a file that is copied or committed, or on a command line.
- Do not re-embed the chunks because the machine changed: the vectors are valid as long as the model is bge-m3.
