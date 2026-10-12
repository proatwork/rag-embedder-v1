# Commands

Run from the project folder. Sources: `NormativeIncendiu/` (2 PDFs, 1 DOCX).
Every command is run with `venvs/bin/python`. More background on each option is in `MANUAL.md`.

## Settings: this machine's addresses, ports and keys

They are in one file, `settings.env`, which is not committed. Make it from the pattern and change what differs:

```sh
cp settings.example.env settings.env && chmod 600 settings.env
```

| Name | Default | Meaning |
|---|---|---|
| `NORMS_EMBEDDING_SERVER` | `http://127.0.0.1:1234` | The server that runs the embedding model (LM Studio, or `llama-server`). The default of every `--server`. |
| `NORMS_CHAT_SERVER` | the embedding server | The server that runs the language model, for `ask.py` and the chatbot. |
| `NORMS_CHAT_MODEL` | `qwen/qwen3.8-27b` | The language model's id on the chat server. The default of `--chat-model`. |
| `NORMS_CHATBOT_PORT` | `8765` | The port of the chatbot's page. |
| `NORMS_MCP_PORT` | `8766` | The port of the MCP server. |
| `NORMS_MCP_KEY` | none | The key of the MCP server (step 9). |
| any name you choose | none | The key of a hosted chat API; `--chat-key-env` gives `ask.py` the name. |

A value is looked for in this order: the script's own option (`--server`, `--port`, ...), then the environment
(`export NORMS_EMBEDDING_SERVER=...`), then `settings.env`, then the default. Where the tables below say
"`settings.env`" as a default, they mean the value found that way. A shell can read the same file
(`set -a; . ./settings.env; set +a`), and so can systemd (`EnvironmentFile=`). With `NORMS_SETTINGS_FILE` another
file is read instead; set to nothing, none is. The embedding model itself (`text-embedding-bge-m3`) is not a setting:
the vectors are bound to it.

Steps 1–4 share three options:

| Option | Meaning |
|---|---|
| `--input-dir DIR` | **Required.** The files directly inside this folder are processed (subfolders are not searched). Hidden files and Word lock files (`~$...`) are ignored. |
| `--output-dir DIR` | **Required.** Created if missing. Each result is named after its source file. |
| `--skip-existing` | Skip a file whose result already exists and is not empty. Use it to resume a stopped run. A failed file leaves no partial result, so it is retried. |

A file that fails is reported and the rest still run; the script then exits with an error and lists the failed files.

## 1. PDF → JSON (+ images)

```sh
venvs/bin/python pdf_to_md.py --input-dir NormativeIncendiu --output-dir docs_processed/json --to json --images
```

~9 minutes for both PDFs (OCR). Needs the `tesseract` command with Romanian data (`tesseract --list-langs` shows `ron`).

| Option | Default | Meaning |
|---|---|---|
| `--to {md,json}` | `md` | Output format. `json` is Docling's full document (text, tables, page numbers and positions); `md` is only the text. One format per run. Use `json` for the next steps. |
| `--images` | off | With `--to json`: write the pictures as PNG files in `<name>_images/` beside the `.json`. The JSON refers to them by a path relative to its own folder (`<name>_images/image_000000_<hash>.png`). |
| `--page-images` | off | With `--images`: also keep an image of every page. Left out by default: large, and not a picture in the document. |
| `--ocr-engine` | `tesseract` | The OCR engine Docling uses, as Docling names it, for example `ocrmac` for Apple Vision. |
| `--ocr-lang` | `ron` | OCR language, as the engine names it (`ron` is Romanian for Tesseract). |
| `--ocr-mode` | `full_page` | Which regions are OCR'd. `full_page` OCRs the whole page and ignores any text layer, because the text layer of a scan cannot be trusted. The other values (`layout_regions`, `pdf_aware_layout_regions`, `default`) are Docling's own modes. |
| `--device {auto,cpu,mps}` | `auto` | Where Docling's models run. `mps` is the Mac's GPU. |
| `-- ...` | | Everything after `--` is passed to `docling convert` unchanged, for example `-- --page-range 1-4` to try a few pages. Do not pass `--to`, `--output`, `--output-file` or (with `--images`) `--image-export-mode` there: the script sets them. |

## 2. DOCX → JSON (+ images)

```sh
venvs/bin/python docx_to_md.py --input-dir NormativeIncendiu --output-dir docs_processed/json --to json --images
```

~2.5 minutes. Options: `--input-dir`, `--output-dir`, `--skip-existing`, `--to`, `--images`, `--page-images` and `-- ...`, all as in
step 1. There is no OCR (Docling reads the Word structure directly), so no `--ocr-*` or `--device`.

Steps 1 and 2 write `<name>.json` and a folder `<name>_images/`. `<name>` is the source file name without its extension
(trailing dots and spaces dropped). Without `--images` the pictures of a Word file stay in the JSON as base64 text.

## 3. JSON → chunks

```sh
venvs/bin/python json_to_chunks.py --input-dir docs_processed/json --output-dir docs_processed/chunks --docx-dir NormativeIncendiu
```

Cuts each JSON into chunks (one article or numbered paragraph each, tables on their own) and writes one `.jsonl` per JSON, one
chunk per line. Takes about 10 seconds. The first run downloads the tokenizer (needs a network connection).

| Option | Default | Meaning |
|---|---|---|
| `--docx-dir DIR` | none | Folder with the original `.docx` files. For a JSON that has a `.docx` of the same name there, the paragraph labels Word generates (`(2)`, `a)`, `4.3.1.2.`) are put back; Docling drops or renumbers them. Without it, Word files are chunked without those labels and often cut in the wrong places. |
| `--config FILE` | `chunk-config.json` | Per-document settings (see below). |
| `--tokenizer NAME` | `BAAI/bge-m3` | Hugging Face name of the embedding model's tokenizer. Chunk sizes are counted with it. Use the Hugging Face name, not the id the server gives the model. |
| `--min-tokens N` | `80` | A numbered article smaller than this is joined with the next one under the same heading, as long as the pair fits. |
| `--max-tokens N` | `480` | A larger chunk is split. The heading path in front of the text counts. 480 leaves room under the 512 limit of the smaller models. |

`--skip-existing` only checks whether the `.jsonl` exists. After changing an option or the config file, run without it, or into a new folder.

**`chunk-config.json`** has one entry per document, keyed by the file name without its extension (`Normativ_P118_1_2015`,
`Normativ_P118_2_2013`, `Normativ_P118_3_2015`; the three are there). A renamed file needs its key renamed. A JSON with no entry fails with
`no entry for ... in the config file`.

| Entry key | Meaning |
|---|---|
| `code` | The norm's short name (`P118-1/2025`). Starts every heading path and, in lower case, every chunk id (`p118-1-2025:art-2.3.2.1:0`). It is what `doc` in the eval questions and `--doc` refer to. |
| `title`, `year` | Copied into every chunk (`doc_title`, `edition_year`) and used in its citation. |
| `status` | Whether the norm applies: `in_force`, `unverified` (nobody has checked) or `repealed`. Copied into every chunk; the model is told it in words under each citation (`Statut: în vigoare`), and the chatbot marks a source whose norm is not simply in force. Any other value stops the script. |
| `repealed_on` | Needed when, and only when, `status` is `repealed`: the day it stopped applying, as `2026-08-25`. The model is told "abrogat din 25.08.2026". |
| `amended_by` | Optional: the acts that changed the norm after this text, as a list (`["Ordinul 6026/2018"]`). The model is told the text is the original, changed later by them. |
| `gazette` | Optional: where the norm was published (`"M. Of. nr. 595 bis/2013"`); it is added to every citation. |
| `profile` | How the document numbers its text: `art` (`Art. 12`), `decimal` (`3.7.4.2`) or `headings` (no numbering, one chunk per heading). |
| `first_body_page` | Optional, PDFs only: the page where the real text starts. Everything before it (cover, contents) is dropped. |
| `embed_field` | Optional, read by the embedding step: embed the chunk's `text` instead of `embed_text` (the heading path plus the text). |

After a change to `code`, `title`, `year`, `status`, `repealed_on`, `amended_by` or `gazette`, run this step and step 4 again, then restart the chatbot. The texts do not change, so step 4 embeds nothing (`0 embedded, N reused`) and takes seconds. If you forget, `search.py`, `ask.py` and the chatbot stop and say so (`... was chunked before the config file changed (status): run json_to_chunks.py again`): a chunk is cited from what it carries, so an old status can never reach an answer. A key the scripts do not know (a misspelt `amendedBy`) stops the script too.

**What a chunk holds:** `id`, `kind` (`article`, `table` or `fragmented`), `doc_code`, `doc_title`, `edition_year`, `status` with
`repealed_on`, `amended_by` and `gazette` (all from the config), `heading_path`, `label` and `caption` (how it is named in a citation: `Art. 2.3.1`
or `7.10–7.11`, and a table's title), `article`/`articles`, `pdf_pages`,
`text`, `embed_text` (heading path + text, what gets embedded), `prev_id`/`next_id`, `parent_id`, `flags`, and `images`: the
pictures that belong to it, each with `path` (relative to `docs_processed/chunks/`, e.g.
`../json/<name>_images/image_000150_<hash>.png`), `ref`, `page`, `width`, `height`, `caption`. The status line shows where every picture went.
Convert with `--images` first (steps 1 and 2); without it the `images` lists are empty. `fragmented` chunks (text read out of a picture) get no vector.

## 4. Embed chunks

```sh
venvs/bin/python embed_chunks.py --input-dir docs_processed/chunks --output-dir docs_processed/vectors
```

Turns every chunk into a vector with `bge-m3` and writes one `.npz` per chunk file. About 1.5 minutes the first time; a second
run only embeds the chunks whose text changed (`0 embedded, N reused`).

| Option | Default | Meaning |
|---|---|---|
| `--server URL` | `settings.env` | The server that runs the embedding model. Must speak the OpenAI embeddings API, as LM Studio does. Give the whole address, `http://` and port included. |
| `--model ID` | `text-embedding-bge-m3` | The model's id on that server, as LM Studio lists it. Under another id the model-card check is skipped, and vectors made under one id are not reused under another. |
| `--config FILE` | `chunk-config.json` | Checked as in step 3, and read for `embed_field`. |

Before anything is written the script checks that the model reproduces the example on its model card, and stops if not. After
re-chunking, run it again **without** `--skip-existing`: with it, a file whose `.npz` exists is not looked at.

## 5. Search

```sh
venvs/bin/python search.py --chunks-dir docs_processed/chunks --vectors-dir docs_processed/vectors "perete antifoc"
venvs/bin/python search.py --chunks-dir docs_processed/chunks --vectors-dir docs_processed/vectors --doc "P118-1/2025" --top 10 "perete antifoc"
venvs/bin/python search.py --chunks-dir docs_processed/chunks --article 2.3.2.1.2   # by article number, no server needed
```

| Option | Default | Meaning |
|---|---|---|
| `query` | | The question to search for (put it last, in quotes). Give this or `--article`, not both. |
| `--chunks-dir DIR` | | **Required.** The folder `json_to_chunks.py` wrote. |
| `--vectors-dir DIR` | | Required for a question. The folder `embed_chunks.py` wrote from those chunks. Not needed with `--article`. |
| `--top N` | `5` | How many results to show. |
| `--doc CODE` | all norms | Only this norm, by its code. Also useful with `--article`: a number such as `4.1` exists in several norms. |
| `--article NUMBER` | | List every chunk holding this article or paragraph number, tables included, instead of searching. The server is not asked. |
| `--server URL` | `settings.env` | The embedding server, which embeds the question. |
| `--config FILE` | `chunk-config.json` | As in step 4. |

Each result shows its score (1 is identical text; compare scores only with each other), the chunk id, the chunk kind and flags,
its citation with the norm's status in words (see step 7), the heading path and the start of the text. The pictures of a chunk follow as `image:` lines, with a path
that opens from this folder. If the chunks were written again after they were embedded, the script stops and says to run step 4 again.

## 6. Eval the retrieval

```sh
venvs/bin/python eval_retrieval.py --chunks-dir docs_processed/chunks --vectors-dir docs_processed/vectors --questions eval-questions-3docs.jsonl
venvs/bin/python eval_retrieval.py --chunks-dir docs_processed/chunks --questions eval-questions-3docs.jsonl --check-questions   # no server
```

Searches every question and reports where the article that answers it came. Redirect the output (`> eval-2026-10-11.txt`) to compare runs.

| Option | Default | Meaning |
|---|---|---|
| `--questions FILE` | | **Required.** The questions file (below). |
| `--chunks-dir DIR` | | **Required.** As in step 5. |
| `--vectors-dir DIR` | | Required unless `--check-questions`. |
| `--strip-diacritics` | off | Also score every question typed without `ă â î ș ț`, in a second table. |
| `--check-questions` | off | Only check that every answer names an embedded chunk. No server and no `--vectors-dir`. |
| `--server URL`, `--config FILE` | as in step 5 | |

**The questions file** has one JSON object per line:

```json
{"question": "Ce rezistență la foc trebuie să aibă un perete antifoc?", "doc": "P118-1/2025", "articles": ["2.3.2.1.2"], "author": "claude"}
{"question": "Ce înseamnă …?", "doc": "P118-1/2025", "ids": ["p118-1-2025:art-1.2.1:43"], "author": "claude2"}
```

| Key | Meaning |
|---|---|
| `question` | As you would type it. |
| `doc` | The norm's `code` from `chunk-config.json`. |
| `articles` | The article numbers that answer it. A result counts if it is a chunk of that norm holding one of them (or a table of that article). |
| `ids` | For text with no number, one table, or one piece of a long article. A chunk id without its last part (`p118-1-2025:art-6.2-tab1`) stands for all its pieces; a whole id (`...:art-1.2.1:43`) for that piece only. Needs `articles` or `ids`, or both. |
| `author` | Optional. With more than one author the scores are also printed per author. |

`eval-questions-3docs.jsonl` has 322 questions on the three norms: the first 22 (`author` `claude`, cut from `eval-questions.jsonl`) and 300 more
(`author` `claude2`). The first 22 are the harder set. 114 questions name a chunk by id, so they only fit the chunks of a clean Word
conversion (2,680 chunks for P118-1/2025); `--check-questions` tells you if an id is missing. The full `eval-questions.jsonl` names norms
that are not embedded and stops the run.

**Reading the result:** `top 5` is the share of questions whose answer is among the first five results (the number to watch);
`MRR` is the mean of 1/rank, with an answer below rank 10 counted as zero. The questions were written from the chunks, so the score is
an upper bound.

## 7. Ask the LLM (answers written from your chunks)

```sh
venvs/bin/python ask.py --chunks-dir docs_processed/chunks --vectors-dir docs_processed/vectors --chat-server http://127.0.0.1:1234 --chat-model qwen/qwen3.8-27b "Ce rezistență la foc are un perete antifoc?"
```

The LLM at `--chat-server` is given one tool, `search_norms`, and searches the chunks for itself: it writes the query in its
own words, may search again (up to 5 times for one question), and may fetch an article by its number. Each search is embedded on
`--server` and printed as it is run (`search 1: "perete antifoc rezistență la foc": 10 chunks, 10 new`). The LLM then writes the
answer from what the searches returned only. It cites the norm, the article and the excerpt number, and says so when the chunks do
not answer. After the answer come the searches and the chunks it was written from, with their `image:` lines. The answer is the
model's own: check it against those chunks.

- **Why the model searches:** a follow-up such as "și la clădiri înalte?" holds no words to search for. The model turns it into
  `"clădiri înalte: rezistența la foc a pereților antifoc"`, and a question that two norms answer is searched in each.
- **The tool** (`search_tool.py`): `search_norms(query, norm, article, count)`. `query` searches by meaning; `article` fetches
  an article or point by its number (`"2.3.2.1.2"`, `"7.10"`, `"A.10.1.1.1"`) and needs no vector; `norm` limits a search to one
  norm; `count` is how many chunks come back (default `--top`, at most 20 either way). The excerpts are numbered across all searches of one
  question, and a chunk found again keeps its number, so `[4]` in the answer is one chunk.
- **When the model does not search:** if it answers without a search that found anything, or writes its call as text, its text
  is thrown away and it is handed the chunks closest to the question's own words, as with `--single-search`. The output says so.
  This costs one more round with the model (also for a plain "Mulțumesc!").
- **The limit:** the fifth search's result says it was the last. A call after it is told so and not run, and the model is then
  asked once more without the tool, so it answers from what it has. Of many calls in one reply, only those up to the limit are run.
- **A wrong call** (an unknown norm, a number no chunk has, no query) is answered with the reason, and the model goes on; it
  counts as one of the five.
- **`--single-search`** is the way without the tool, for a model or a server that takes none: one search with the question's own
  words, the chunks pasted into the prompt. The server needs an OpenAI-style `tools` field for the other way (LM Studio's
  `/v1/chat/completions` has it; the loaded model must support tool use, as Qwen3 does).

| Option | Default | Meaning |
|---|---|---|
| `question` | | The question to answer (last, in quotes). |
| `--chunks-dir DIR` | | **Required.** As in step 5. |
| `--vectors-dir DIR` | | **Required.** |
| `--chat-server URL` | `settings.env`, else `--server` | The server that runs the LLM. Leave it out if one server runs both models. |
| `--chat-model ID` | `settings.env` | The LLM's id on that server, as `<chat-server>/v1/models` lists it. The script stops if another model answers. |
| `--server URL` | `settings.env` | The server with the embedding model (`text-embedding-bge-m3`) that embeds the question. |
| `--top N` | `5` | How many chunks a search gives the LLM when it does not ask for a number itself (it may ask for up to 20). With `--single-search`: how many chunks it is handed. More chunks give more context and need more context length loaded. |
| `--doc CODE` | all norms | Only chunks of this norm: the LLM can search no other. |
| `--single-search` | off | Do not let the LLM search: find the chunks closest to the question's own words once and hand them over. |
| `--chat-path PATH` | `/v1/chat/completions` | The chat endpoint's path on the chat server. LM Studio uses the default; many hosted OpenAI-style APIs use `/chat/completions`. |
| `--chat-key-env VARIABLE` | none | The **name** the chat server's API key is kept under, in `settings.env` or the environment. It is sent as `Authorization: Bearer <key>`. The key itself is never an option, because shells remember what you type. With a key, the model that answers is not compared with `--chat-model` (a hosted API may answer under the name an alias resolves to). |
| `--instructions FILE` | `ask-instructions.md` | The rules the LLM is given (see below). |
| `--show-prompt` | off | Print what the LLM would be sent first and ask nobody: the rules, the question and the tool (with `--single-search`: the rules, the chunks and the question; the embedding server is then still used). |
| `--conversation FILE` | none | Keep a conversation in this JSON file: the earlier questions and answers in it are sent to the LLM before the question, so a follow-up ("și la clădiri înalte?") is understood and searched for in full, and the new pair is appended. The file holds the plain questions and the answers as user / assistant messages (not the excerpts, which would grow the prompt by thousands of tokens a turn); only the newest 12 messages are sent. With `--single-search` the chunks are still found from the new question alone, so a follow-up that names nothing may find the wrong chunks. To start a new conversation, remove the file or give a new name. |
| `--config FILE` | `chunk-config.json` | As in step 4. |

**The LLM's rules** are in `ask-instructions.md`, a plain text file that `ask.py` reads on every run (the chatbot reads it once, when it starts: restart it after an edit). Edit it to change how the model answers
(language, tone, what to refuse, how to cite); to try a variant, copy it and pass the copy with `--instructions`. It is sent as the
system message, so the whole file counts against the model's context. A missing or empty file stops the script with a message.
`--show-prompt` prints exactly what would be sent.

**How the model is to search** is in a second file, `ask-tool-instructions.md`, which is put in front of the rules when the model
has the tool (search first, write the query in full, search each norm on its own, never answer from memory). It may use the
tokens below and `{tool_name}`, `{max_searches}`, `{arg_query}`, `{arg_norm}` and `{arg_article}`. With `--single-search` it is not sent.

**What the model is given of each chunk** (`excerpt()` in `search_tool.py`), by a search and when the chunks are handed over, for example:

```
[2] P118/2-2013, pct. 7.10 — Normativ privind securitatea la incendiu a construcțiilor, Partea a II-a - Instalații de stingere (ed. 2013, PDF p. 34)
Statut: în vigoare · Figuri: 1 (PDF p. 35), afișate utilizatorului în sursele de sub răspuns; legende: "Figura 7.2. Schemă de determinare a claselor de pericol la incendiu pentru depoz…"
<the chunk, as it was embedded>
```

- **The citation** (`citation()` in `search.py`), which the model is told to cite from, adding a paragraph (`alin. (2)`) or point number only
  when the chunk's own text shows it, and never inventing one: the norm's code; the article (`Art. 2.3.1`) or point (`pct. 7.10–7.11`); for text
  without such a number, the heading it is under, with the nearest annex, chapter or numbered title above it; a table's title; the norm's title; the edition; the gazette if the config names one; the PDF page.
  The page is the page of the PDF file, not always the number printed on it. P118-1/2025 was converted from a Word file, so its chunks have no page.
- **The status** in words: `în vigoare`, `neverificat` or `abrogat din <day>`, then `text original, modificat ulterior prin: …` when the
  config names amending acts.
- **The figures**, when the chunk has pictures: how many, on which PDF pages, and the start of the captions they have (cut at 80
  characters, the first three). The model is not given any picture. The rules tell it to point to that excerpt ("see the figure in excerpt [3]"), not to
  describe what a picture shows beyond its caption, and not to say there are no images when an excerpt lists some. The pictures
  themselves are shown under the answer by the chatbot page (in its *Sources*), and as `image:` lines by `ask.py`.

These words are Romanian because the model quotes them in Romanian answers; they are constants at the top of `search.py`, and
everything else (the config, the chunk fields, the code) stays English. The rules file refers to them through tokens, replaced when it is
read, so the rules and the excerpts cannot come to say different things: `{cell_separator}` (` | `), `{status_label}` (`Statut`),
`{status_in_force}`, `{status_unverified}`, `{status_repealed}`, `{amended_mark}` (`modificat ulterior`), `{figures_label}` (`Figuri`),
`{article_prefix}` (`Art.`), `{point_prefix}` (`pct.`), `{paragraph_prefix}` (`alin.`) and `{page_label}` (`PDF p.`).
Any other braces are left as they are. (`{status_current}` and `{images_label}` of earlier versions are gone: a rules file of your own
that still uses them needs the new ones.) None of this changes the chunk's text, so the embeddings are not affected.

**A hosted LLM instead of LM Studio** (here BytePlus ModelArk, Coding Plan). The embeddings stay on LM Studio (`--server`); only the answer is written elsewhere.

```sh
# first write the key into settings.env, on a line of its own:  ARK_API_KEY=<your key>
venvs/bin/python ask.py --chunks-dir docs_processed/chunks --vectors-dir docs_processed/vectors --chat-server https://ark.ap-southeast.bytepluses.com/api/coding/v3 --chat-path /chat/completions --chat-key-env ARK_API_KEY --chat-model ark-code-latest "Ce rezistență la foc are un perete antifoc?"
```

- The Coding Plan's OpenAI-style base URL is `.../api/coding/v3` and its model name is `ark-code-latest` (BytePlus docs). Do not use `.../api/v3`: the docs say that
  one is billed separately and does not use the plan's quota. The plan's key is not the same as a ModelArk platform key.
- The path `/chat/completions` after that base URL is the usual OpenAI one; it has **not been tried** against BytePlus from here.
- **What leaves your machine:** your question and the chunks it found (text from the norms) go to BytePlus. Nothing else does.
- **Terms:** the Coding Plan is sold for AI coding tools. The docs I could read do not say whether calls from your own script are allowed; check BytePlus's terms
  before using it this way.
- The chatbot (step 8) can use it: choose *API style* OpenAI-style and enter the key there.

If the answer ends early, the model ran out of room: load it with a larger context length. The two servers can be different machines.
The vectors must come from the same embedding model that embeds the question (step 4 checks this).

## 8. Chatbot (a web page that does step 7 for you)

```sh
venvs/bin/python chatbot/server.py
```

Then open `http://127.0.0.1:8765`. A sidebar on the left lists saved conversations (a `New chat` button starts one, and stops an answer that is still being written; an empty chat offers three example questions, set in `EMPTY_EXAMPLES` at the top of `chatbot.js`); every Q&A pair is saved to localStorage as it completes, so the history survives a reload. Click a conversation to restore it, or ✕ to delete it. The rest of the page works as in step 7 above: the model searches the chunks for itself (with the OpenAI style; see *Search* below) or is handed the closest ones, and the answer is streamed from the chat server (you see the model think and write as it comes, then its speed; with the LM Studio style also the model loading and the prompt being read). Above the answer the page lists the searches the model made, as it wrote them (`1. "clădiri înalte: rezistența la foc a pereților antifoc" · P118-1/2025 · 10 results`); text the model wrote before a search is a draft and is cleared. Under each answer, "Sources" opens the numbered chunks the model was given by all its searches: each row names the chunk as it is cited (`P118/2-2013, pct. 7.10`), its PDF pages, its score (`by number` for an article that was fetched, not searched for) and, when there was more than one search, the search that found it first, with a red mark when its norm is not simply in force (unverified, repealed, or changed by later acts); opened, it shows the full citation, the heading path, the text and the pictures; a source with pictures carries a badge saying how many, and a click on a picture opens it in a viewer over the page (a click beside it, or Esc, closes it). A `[1]` in the answer opens source 1.

**The page's files** are `chatbot/index.html` (markup), `chatbot/chatbot.css` (the look: colours, sizes and the dark theme are the variables at the top) and `chatbot/chatbot.js`. They are read at every request, so an edit shows on reload; only a change to `server.py` needs a restart. The page allows no inline script or style, no web font and no `data:` image, so keep styles in the `.css` file. It follows the system's light or dark theme, and on a narrow screen the conversations open over the chat. The design before the 2026-10-11 restyle is kept in `OLD/chatbot-design-before/`.

**Fields on the page** (kept in the browser's localStorage between visits): *Embedding server*, *Chat server* (its model list is read from the server when you leave the
field, or with the ↻ button; embedding models are left out), *API style*, *Chat path*, *API key*, *Chat model* (pick from the list or type an id), *Chunks per search*, *Norm*
(all norms, or one code) and *Search*. Within a conversation, the newest 6 questions with their answers are sent to the model before the excerpts, as with `--conversation` in step 7 (failed answers are left out); `+ New chat` starts with none. With the LM Studio style the exchange is written into the input text under an "Earlier exchange" heading, because that API takes one text, not messages; with the OpenAI style it goes as messages.

- **Search** *The model searches*: the model gets the tool of step 7 and searches for itself, up to 5 times a question; a follow-up
  is searched in full. It needs the OpenAI style, because LM Studio's own API takes no tools. For the model in LM Studio, set
  *API style* OpenAI-style and *Chat server* `http://127.0.0.1:1234/v1` (with `/v1`: without it LM Studio answers
  "Unexpected endpoint", and the page says to add it). If the model does not search (it answers without a search that found
  anything, or writes its call as text), the page says so, clears the answer and the sources, and the question is searched once
  instead. With this style LM Studio sends no token counts, so the numbers under an answer are estimates (`~`).
- **Search** *Once*: the way before, and the only one with the LM Studio style: one search with the question's own words, the
  chunks pasted into the prompt. Use it for a model or a server that takes no tools.
- **API style** *LM Studio (native)*: LM Studio's `/api/v1/chat`; the page shows the model loading and the prompt being read. No key is needed. The model cannot search for itself with it.
- **API style** *OpenAI-style*: a hosted service's `.../chat/completions`, streamed; there is no loading or prompt progress, and the token counts are the
  server's `usage` when it sends one, otherwise the number of pieces received (shown with `~`). For BytePlus ModelArk's Coding Plan: *Chat server*
  `https://ark.ap-southeast.bytepluses.com/api/coding/v3`, *API style* OpenAI-style, *Chat model* `ark-code-latest`, and your key (see the notes in step 7 on what is untested and on the plan's terms).
- **Chat path** is added after the *Chat server* address and replaces the API's usual endpoint (`/api/v1/chat` or `/chat/completions`); leave it empty normally. The line under the fields shows the
  exact address the answers are asked from. A hosted API's version belongs in the *Chat server* (`https://ark.ap-southeast.bytepluses.com/api/coding/v3`), not in the path. If the server answers with a web page,
  an empty stream or a JSON error instead of a stream, the page shows what it said, so a wrong address is easy to spot.
- **API key** is stored in this browser's localStorage, which any script running on `http://127.0.0.1:<port>` can read: the page loads no outside scripts (a content-security policy
  also stops it talking to any other address), but do not use this on a shared machine or user account. *Show* reveals it and *Forget* deletes it. The page sends it to its own
  server (this machine), which passes it to the chat server as `Authorization: Bearer <key>`. It is used only with *OpenAI-style* (the field is greyed out for LM Studio, which takes
  no key), never goes to the embedding server, is not written to any log, and is cut out of error messages. It is never sent over plain `http://` to a server outside your network
  (`localhost` and private addresses such as `192.168.x.x` are fine), and a redirect from the chat server is not followed, so the key cannot be passed on to another host. With a key,
  the model that answers is not compared with the one you chose (a hosted alias may answer under another name).
- **Cut answers:** if the model ran out of room (`finish_reason: length`), or the stream ended without a finish marker, the page says the answer may be incomplete. Do not rely on a cut answer.
- Requests the browser marks as coming from another site are refused, as are all other addresses than this page's own.
- The model list of an OpenAI-style server is read from `<chat server>/models` with the key; a service that gives no list just means you type the model's id.

| Option | Default | Meaning |
|---|---|---|
| `--chunks-dir DIR` | `docs_processed/chunks` | The folder `json_to_chunks.py` wrote. |
| `--vectors-dir DIR` | `docs_processed/vectors` | The folder `embed_chunks.py` wrote. |
| `--images-root DIR` | parent of `--chunks-dir` | Pictures are shown only from inside this folder. |
| `--instructions FILE` | `ask-instructions.md` | The rules for the model, as in step 7. |
| `--config FILE` | `chunk-config.json` | As in step 4. |
| `--server URL` | `settings.env` | The page's default embedding server. |
| `--chat-server URL` | `settings.env` | The page's default chat server (the embedding server's, unless set). |
| `--port N` | `settings.env` (`8765`) | The page listens on this machine only (`127.0.0.1`). |

The chat server must be LM Studio 0.4 or newer (its native `/api/v1/chat`). The chunks, the vectors and the two instructions files (`ask-instructions.md`, `ask-tool-instructions.md`) are read once when the script starts: restart it
after re-chunking, re-embedding or editing the rules. If an answer is empty, the model used up its room thinking: load it with a larger context length.
To try the page without any model, run `venvs/bin/python chatbot/tests/fake_lmstudio.py` (a fake chat server on `http://127.0.0.1:1235` that plays a scripted
answer; its OpenAI-style API is at `http://127.0.0.1:1235/api/coding/v3`, where it plays one search and then an answer when the model is to search) and put that address in the *Chat server* field.

## 9. MCP server (the search as a tool for Claude)

`mcp_server.py` gives Claude one tool, `search_norms`: the search of step 5 and the lookup by number, with the arguments
the chatbot's model has (`query`, `norm`, `article`, `count`). Claude writes the searches and the answer; this server
only returns excerpts. It works in every Claude: claude.ai, Claude Desktop, the phone app and Claude Code.

Make the key **once** and keep it: print one, and write it into `settings.env` as `NORMS_MCP_KEY=<the key>`.

```sh
openssl rand -hex 16
```

Then start the server; the key, the port and the embedding server come from `settings.env`:

```sh
venvs/bin/python mcp_server.py
```

It answers at `http://127.0.0.1:8766/mcp`, on this machine only. Two things must be in place around it:

- **The key.** Every HTTP request must carry `Authorization: Bearer <key>`; anything else is answered `401`. The key is
  `NORMS_MCP_KEY` of `settings.env` or of the environment (at least 32 plain characters), never an option on the
  command line, and the server does not start without it. Whoever has the key can search. A new key means restarting the server and
  adding the connector again in Claude, which is why it is made once and kept. The key is the only gate: the server
  does not check under which name (`Host`) or from which page (`Origin`) a request comes, so it needs no option for
  the proxy's name.
- **A reverse proxy with the certificate.** Claude connects from Anthropic's servers, so the server needs a public
  `https://` address. The proxy forwards `https://norms.example.com/mcp` (no slash at the end) to
  `http://127.0.0.1:8766/mcp`, passes the `Authorization` header on unchanged, and does not buffer the replies. For
  nginx: `proxy_buffering off;` and `proxy_set_header X-Forwarded-Proto $scheme;` (without the second, a request to
  `/mcp/` is redirected to an `http://` address).

| Option | Default | Meaning |
|---|---|---|
| `--server URL` | `settings.env` | The embedding server, as in step 5. |
| `--port N` | `settings.env` (`8766`) | The server listens on this machine only (`127.0.0.1`). |
| `--top N` | `5` | Excerpts a call gets when it does not say (1–20). |
| `--chunks-dir DIR` | `docs_processed/chunks` | The chunk files of step 3. |
| `--vectors-dir DIR` | `docs_processed/vectors` | The vector files of step 4. |
| `--config FILE` | `chunk-config.json` | As in step 4. |
| `--rules FILE` | `mcp-instructions.md` | How to search and cite; put into the tool's description, which Claude reads. |

**Add it to Claude** (claude.ai; Claude Desktop and the phone app take it from the same account): *Customize → Connectors
→ + Add → Add custom connector*. Name it, give `https://norms.example.com/mcp`, choose **No sign in** under
*Authentication*, and under *Request headers* add `Authorization` with the value `Bearer <key>`. A connector cannot be
edited: to change the address or the key, remove it and add it again.

**Add it to Claude Code:**

```sh
(set -a; . ./settings.env; set +a; claude mcp add --transport http norms https://norms.example.com/mcp --header "Authorization: Bearer $NORMS_MCP_KEY")
```

How it differs from the chatbot:

- **Every call stands alone.** Its excerpts are numbered from 1 and there is no limit of 5 searches, because Claude does
  not say where one question ends. Claude cites by norm and article (`P118/2-2013, pct. 7.10`), not by `[n]`.
- **No pictures.** An excerpt with figures says so and says they are not shown (`Figuri: 2, neafișate utilizatorului`).
- **A search by meaning needs the embedding server.** When it does not answer, the call is answered with an error that
  says so, and the reason (with that server's address) is printed by `mcp_server.py` itself. A lookup by `article`
  works without it. Whatever else fails inside a search is answered the same way: Claude is told only that it failed.

The chunks, the vectors and `mcp-instructions.md` are read once when the script starts: restart it after re-chunking,
re-embedding or editing the rules. The server keeps nothing between two requests.

## Tests

```sh
venvs/bin/python -m unittest discover -s tests -v
```

305 tests: the settings file (`tests/test_settings.py`: how it is read, what wins over what, the defaults, that no script names a private address), the picture references (`tests/test_chunk_images.py`), the citation and the status of a norm (`tests/test_citation.py`), the instructions file (`tests/test_ask_instructions.py`), the conversation (`tests/test_ask_conversation.py`), the chat path and API key options (`tests/test_ask_chat_options.py`), the search tool (`tests/test_search_tool.py`: numbering across searches, wrong calls, the limit and a session without one, the lookup by number, pictures said to be shown or not), the model searching through `ask.py` (`tests/test_ask_tools.py`: the rounds, the fallback to a single search, the search rules file, the command line) and the MCP server (`tests/test_mcp_server.py`: the tool as an MCP client is told of it, calls in the same process and over HTTP on a free port of this machine in both protocol versions, the key, failures inside a search, the command line). `discover -s tests` looks for tests in the `tests` folder and
`-v` lists each one. No server and no download needed, and this machine's own `settings.env` is not read by the tests that depend on a setting.

```sh
venvs/bin/python -m unittest discover -s chatbot/tests -v
```

144 tests of the chatbot, against a fake chat server. `chatbot/tests/test_tool_search.py` covers the model searching (the events of several rounds, calls that arrive in pieces, the fallback, the limit, errors, the key never shown) and `chatbot/tests/test_page_search.py` the page's *Search* setting and list of searches (checks of the page's source only: nothing runs it in a browser). `chatbot/tests/test_chatbot.py` covers the rest: the model list, the streamed events, error cases, the image route, requests from other sites, malformed requests, the Stop button, the OpenAI-style stream with an API key, the conversation sent with a question, and the page's own files (that every id the script uses is in `index.html` and every class has a style in `chatbot.css`, and that nothing inline is left).
