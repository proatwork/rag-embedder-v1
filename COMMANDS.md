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
| `NORMS_CHAT_SERVER` | the embedding server | The server that runs the language model, for `ask.py` and the chatbot. Archived scripts only (`_archive/COMMANDS.md`). |
| `NORMS_CHAT_MODEL` | `qwen/qwen3.8-27b` | The language model's id on the chat server. The default of `--chat-model`. Archived scripts only (`_archive/COMMANDS.md`). |
| `NORMS_CHATBOT_PORT` | `8765` | The port of the chatbot's page. Archived scripts only (`_archive/COMMANDS.md`). |
| `NORMS_MCP_PORT` | `8766` | The port of the Python MCP server. Archived scripts only (`_archive/COMMANDS.md`). |
| `NORMS_MCP_KEY` | none | The key of the Python MCP server. The Worker's key is its secret `MCP_KEY` (MANUAL.md, *The MCP Worker*); `upload_to_cloudflare.py` does not read this one. |
| any name you choose | none | The key of a hosted chat API; `--chat-key-env` gives `ask.py` the name. Archived scripts only (`_archive/COMMANDS.md`). |

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
| `code` | The norm's short name (`P118-1/2025`). Starts every heading path and, in lower case, every chunk id (`p118-1-2025:art-2.3.2.1:0`). It is what the Worker's `norm` argument names. |
| `title`, `year` | Copied into every chunk (`doc_title`, `edition_year`) and used in its citation. |
| `status` | Whether the norm applies: `in_force`, `unverified` (nobody has checked) or `repealed`. Copied into every chunk; the model is told it in words under each citation (`Statut: în vigoare`). Any other value stops the script. |
| `repealed_on` | Needed when, and only when, `status` is `repealed`: the day it stopped applying, as `2026-08-25`. The model is told "abrogat din 25.08.2026". |
| `amended_by` | Optional: the acts that changed the norm after this text, as a list (`["Ordinul 6026/2018"]`). The model is told the text is the original, changed later by them. |
| `gazette` | Optional: where the norm was published (`"M. Of. nr. 595 bis/2013"`); it is added to every citation. |
| `profile` | How the document numbers its text: `art` (`Art. 12`), `decimal` (`3.7.4.2`) or `headings` (no numbering, one chunk per heading). |
| `first_body_page` | Optional, PDFs only: the page where the real text starts. Everything before it (cover, contents) is dropped. |
| `embed_field` | Optional, read by the embedding step: embed the chunk's `text` instead of `embed_text` (the heading path plus the text). |

After a change to `code`, `title`, `year`, `status`, `repealed_on`, `amended_by` or `gazette`, run this step and step 4 again, then `upload_to_cloudflare.py`. The texts do not change, so step 4 embeds nothing (`0 embedded, N reused`) and takes seconds. A chunk is cited from what it carries: until the upload, the Worker cites the old status. A key the scripts do not know (a misspelt `amendedBy`) stops the script too.

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

## Tests

```sh
venvs/bin/python -m unittest discover -s tests -v
```

The settings file (`tests/test_settings.py`: how it is read, what wins over what, the defaults, that no script names a
private address), the picture references in the chunks (`tests/test_chunk_images.py`) and the upload to Cloudflare
(`tests/test_upload_to_cloudflare.py`). No server and no download needed. The Worker's own tests: `npm test` in
`worker/`. The tests of the archived scripts are in `_archive/` (see `_archive/COMMANDS.md`).
