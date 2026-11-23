# Manual: from scans and Word files to text and chunks

Five scripts turn the source documents in `docs/` into Markdown, JSON or plain text, and a sixth cuts the JSON into
chunks for embedding. One more embeds the chunks, and `upload_to_cloudflare.py` puts them on Cloudflare. Each one
works on a whole folder.

| Script | Reads | Writes |
|---|---|---|
| `split_pages.py` | PDFs where each page shows two book pages | PDFs with one book page per page |
| `pdf_to_md.py` | PDFs with one book page per page | one `.md` per PDF, or one `.json` with `--to json` (Docling + Tesseract OCR) |
| `pdf_to_md_vision.py` | the same | one `.md` per PDF, or one `.json` with `--to json` (Docling + Apple Vision OCR) |
| `docx_to_md.py` | `.docx` files | one `.md` per file, or one `.json` with `--to json` (Docling) |
| `docx_to_txt.py` | `.docx` files | one `.txt` per file (own code, nothing dropped) |
| `json_to_chunks.py` | the `.json` files of the three Docling scripts | one `.jsonl` per file, one chunk per line |
| `embed_chunks.py` | the `.jsonl` chunk files | one `.npz` per file, one vector per chunk |

The search runs on Cloudflare, as an MCP server in `worker/`: see *The MCP Worker* at the end. The local search
(`search.py`, `eval_retrieval.py`), the chat (`ask.py`, `chatbot/`) and the Python MCP server are in `_archive/`,
with their part of this manual (`_archive/MANUAL.md`).

`split_pages.py` and `pdf_to_md_vision.py` are not used for the current documents and now sit in `OLD/` (copy them back
to this folder to use them: they import `pipeline_common.py` and `pdf_to_md.py`). `pipeline_common.py` holds the code they share. The previous tools are in `OLD/` and are not used by these scripts.

## Before you start

- Run every script with the virtual environment's Python: `venvs/bin/python <script> ...`. That environment has
  Docling, pypdfium2, Pillow and numpy. `docx_to_txt.py` alone also runs with any Python 3.10 or newer.
- Do not use `venvs/bin/docling` or `source venvs/bin/activate`: the environment was moved after it was created
  and those two still point at its old location. The scripts do not need them.
- `pdf_to_md.py` needs the `tesseract` command with Romanian language data (`tesseract --list-langs` must list
  `ron`). On this Mac it is installed through Homebrew.
- `pdf_to_md_vision.py` runs only on macOS and needs the `ocrmac` package, which is in the virtual environment.
- `json_to_chunks.py` needs `transformers` and `semchunk`, both in the virtual environment, and downloads a
  tokenizer from Hugging Face the first time it runs (see its section).
- `embed_chunks.py` does not run the embedding model itself. It asks a
  server for the vectors: LM Studio at the address `settings.env` gives (`NORMS_EMBEDDING_SERVER`; see *Settings* in
  `COMMANDS.md`), with the model `text-embedding-bge-m3` loaded.
  When that server is off, or the model is not loaded, it cannot embed anything. No model weights are
  downloaded to this machine. `embed_chunks.py` still needs `transformers` and the tokenizer `json_to_chunks.py`
  downloaded, to count texts that are too long.

## Options every script has

| Option | Meaning |
|---|---|
| `--input-dir DIR` | Required. The files directly inside this folder are converted (subfolders are not searched). |
| `--output-dir DIR` | Required. Created if missing. Each result is named after its source file. |
| `--skip-existing` | Skip files whose result already exists and is not empty. Use it to resume a stopped run. |

Hidden files and Word lock files (`~$...`) are ignored. A file that fails is reported and the rest still run; the
script then ends with a non-zero exit status and lists the failed files. A failed file leaves no partial result
behind, so `--skip-existing` retries it.

## The usual order

```sh
venvs/bin/python split_pages.py --input-dir docs/double_paged     --output-dir docs_processed/split
venvs/bin/python pdf_to_md.py   --input-dir docs_processed/split  --output-dir docs_processed/md
venvs/bin/python pdf_to_md.py   --input-dir docs/single_paged     --output-dir docs_processed/md
venvs/bin/python docx_to_md.py  --input-dir docs/docx             --output-dir docs_processed/md
venvs/bin/python docx_to_txt.py --input-dir docs/docx             --output-dir docs_processed/txt
venvs/bin/python json_to_chunks.py --input-dir docs_processed/md  --output-dir docs_processed/chunks --docx-dir docs/docx
venvs/bin/python embed_chunks.py   --input-dir docs_processed/chunks --output-dir docs_processed/vectors
```

The folder names after `--output-dir` are examples; no script assumes any folder. `json_to_chunks.py` reads `.json`
files, so the three Docling lines above must be run with `--to json` for it to have input.

## `split_pages.py`

Cuts each double page in two and writes the left half, then the right half. Source page `k` becomes output pages
`2k-1` and `2k`. A page that is taller than wide is kept whole.

| Option | Default | Meaning |
|---|---|---|
| `--divider` | `auto` | Where each page is cut. `auto` finds the gutter on every page. A number from 0.05 to 0.95 cuts every page at that fraction of its width (`0.5` is the middle). |
| `--dpi` | `300` | Resolution of the output pages. |
| `--white-point` | off | A gray level from 1 to 255. Everything that light or lighter becomes white, to remove print showing through thin paper. |
| `--page-scale` | `1` | Declares the output pages this many times their real size (same pixels), which gives Docling's OCR more pixels per letter. Up to 10. |

- The status line shows where the pages were cut, for example `cut at 0.412 to 0.518`. A value far from the
  others is worth a look.
- The output pages are images; a text layer in the source is not carried over.
- Most scans in `docs/double_paged/` are 150 DPI. At the default 300 the six results take about 300 MB against
  31 MB of sources. With `--dpi 150` the two files measured came out at about a third of their 300 DPI size; a
  few pages hold finer black-and-white images, which lose detail at 150. The cut is found the same way whatever
  `--dpi` is.
- A whole file is held in memory until it is written: about 500 MB for the 122 double pages of `28_13` at 300 DPI.
- Show-through: on thin paper the text of the other side is visible as faint mirrored print, and the OCR reads it
  as stray quotes and punctuation. `--white-point 170` washed it out on `28_4` (show-through there is gray 170 to
  230, real ink below 60). Lower the number if faint text remains, but with care: a low value also thins the fine
  strokes of the real text. It applies to the whole run, and to pages kept whole as well, so a folder of single-page
  PDFs can be cleaned the same way before `pdf_to_md.py`.
- Page scale: Docling renders every page for OCR at 216 DPI of its declared size, and its command line has no
  setting for that. On the small print of these book scans that is too coarse. With `--page-scale 2`, on 12 pages
  of `28_4`, Apple Vision went from 68 wrong `ä`/`ã` and 18 `î` to 4 and 129; Tesseract changed little. The
  conversion took 28% longer with Vision and 45% longer with Tesseract.
- There is no per-page override. If `auto` misplaces a cut, the only other choice is one fixed `--divider` for
  the whole run.

## `pdf_to_md.py`

Runs Docling once per PDF and writes `<name>.md`, or `<name>.json` with `--to json`.

| Option | Default | Meaning |
|---|---|---|
| `--to` | `md` | Output format: `md` or `json`. One format per run. |
| `--ocr-engine` | `tesseract` | The OCR engine Docling uses. Docling's own choice on a Mac would be Apple Vision (`ocrmac`); this script always names the engine. |
| `--ocr-lang` | `ron` | Language, as the engine names it. |
| `--ocr-mode` | `full_page` | `full_page` OCRs the whole page and ignores any text layer. |
| `--device` | `auto` | `auto`, `cpu` or `mps`. |
| `--images` | off | With `--to json`: write the pictures as PNG files in `<name>_images/` beside the `.json`, which refers to them by a relative path (`<name>_images/image_000000_<hash>.png`). `docx_to_md.py` and `pdf_to_md_vision.py` have it too. |
| `--page-images` | off | With `--images`: also keep the image of every page. Left out by default. |
| `-- ...` | | Everything after `--` is passed to `docling convert` unchanged. Not `--image-export-mode` with `--images`. |

```sh
venvs/bin/python pdf_to_md.py --input-dir docs_processed/split --output-dir /tmp/try -- --page-range 1-4   # try a few pages first
venvs/bin/python pdf_to_md.py --input-dir docs/single_paged --output-dir docs_processed/json --to json   # JSON instead of Markdown
```

- `--to json` writes Docling's full document: the text of every heading, paragraph, list item and table cell,
  each with its page number and its position on the page, in reading order. Markdown keeps only the text. Read
  it back in Python with `DoclingDocument.load_from_json(path)` (from `docling_core.types.doc`). The JSON of a
  PDF holds no image data unless `--images` is used (below).
- In the `.json` file, letters outside ASCII are written as escapes (`ă` is `\u0103`). Any JSON reader gives the
  letter back; a plain search of the file for `ă` finds nothing. Docling's command line has no setting for that.
- Each run writes one format. For both, run the script twice, which runs the OCR twice. The `.md` and `.json` of
  one source can share an output folder, and `--skip-existing` looks only at the format asked for.
- Set the format with the script's own `--to`, before `--`. After `--`, do not pass `--to`, `--output` or
  `--output-file`: the script sets the format and the output file itself, and a second `--to` makes Docling stop
  with an error. The same holds for `docx_to_md.py`.
- Feed it PDFs with one book page per page. Docling reads across a double page and mixes the two pages' lines.
- Images are written as placeholders, not as embedded data, unless `--images` is used (below).
- Speed on the first sample was about 1.2 seconds per page.

## `pdf_to_md_vision.py`

The same script with Apple Vision reading the text instead of Tesseract. Docling still finds the headings, lists
and tables. Its options are those of `pdf_to_md.py`, `--to json` included; only two defaults differ:
`--ocr-engine ocrmac` and `--ocr-lang ro-RO`.

```sh
venvs/bin/python split_pages.py      --input-dir docs/double_paged --output-dir OUT/split --white-point 170 --page-scale 2
venvs/bin/python pdf_to_md_vision.py --input-dir OUT/split --output-dir OUT/md_vision
```

- Split the book scans with `--page-scale 2` first (see above). At the normal page size Vision misreads the
  Romanian letters on them: `ă` comes out as `ä` or `ã` and most `î` are lost.
- The two PDFs in `docs/single_paged/` have larger print; two pages of `sp28_16` read without that problem at
  their normal size.
- Compared with Tesseract on the same 12 pages of `28_4` (page scale 2): Vision writes `ș` and `ț` in one
  consistent form, Tesseract mixes the two forms of each (`ș`/`ş`, `ț`/`ţ`) about half and half; Vision took
  12 seconds, Tesseract 22.
- Vision's output is not frozen: the same page can read slightly differently on another day. Keep the results
  you have accepted.

## `docx_to_md.py`

Runs Docling once per Word file and writes `<name>.md`, or `<name>.json` with `--to json`. It has no OCR options;
`-- ...` passes arguments to `docling convert` as above.

```sh
venvs/bin/python docx_to_md.py --input-dir docs/docx --output-dir docs_processed/json --to json
```

The JSON of a Word file differs from that of a PDF in two ways:

- It has no page numbers and no positions: a Word file has no fixed pages. It holds the text items, tables and
  their order.
- The pictures of the Word file stay in it, as base64 text. The placeholder setting that keeps them out of the
  Markdown does not remove them from the JSON. The files are large: 3.6 MB for the parking norm (2.5 MB of it
  pictures) and 50 MB for the fire safety norm (13 MB of it pictures).
  With `--images` they are written as PNG files instead: the fire safety norm (2025) came out as a 36 MB `.json`
  plus 11 MB of PNG in `<name>_images/`.

Check the result before relying on it. Docling renumbers lists itself, and on the parking norm it logged
"The list item will be ignored". An earlier comparison on that file found a whole section missing and none of
Word's own paragraph labels (`(1)`, `a)`). The large norm takes about two minutes.

## `docx_to_txt.py`

Reads the Word file directly and writes `<name>.txt`.

- Kept: every paragraph in order; list labels as Word shows them (`a)`, `1.1.`, `(2)`, `-`); tables, one row per
  line with cells joined by ` | `; symbol-font characters (≥ ≤ ± ...); Word equations as text.
- Marked in the text: old-style embedded equations as `[object omitted]`; unknown symbol-font characters as
  `[Font:code]`.
- Dropped: images, headers, footers, footnotes. The status line counts the images and objects left out.
- The output is plain text: no Markdown headings or tables.

## `json_to_chunks.py`

Cuts each Docling `.json` into chunks and writes `<name>.jsonl`, one chunk per line. A chunk is one article or
numbered paragraph of the norm, with its chapter and section kept as a heading path. Tables are chunks of their own.

```sh
venvs/bin/python json_to_chunks.py --input-dir docs_processed/md --output-dir docs_processed/chunks --docx-dir docs/docx
```

| Option | Default | Meaning |
|---|---|---|
| `--docx-dir` | none | Folder with the original `.docx` files. For a `.json` that has a `.docx` of the same name there, the paragraph labels Word generates (`(2)`, `4.3.1.2.`) are put back; Docling drops or renumbers them. Leave it out and the Word files are chunked without those labels. |
| `--config` | `chunk-config.json` beside the script | The per-document settings (below). |
| `--tokenizer` | `BAAI/bge-m3` | Hugging Face name of the embedding model's tokenizer. Sizes are counted with it. |
| `--min-tokens` | `80` | A numbered article whose text is smaller is joined with the next one under the same heading, as long as the pair fits. |
| `--max-tokens` | `480` | A larger chunk is split. The breadcrumb in front of the text counts. |

- The whole run takes about 12 seconds for the ten documents; nothing is OCR'd again.
- The first run downloads the tokenizer (a few megabytes) and needs a network connection. After that it is read
  from `~/.cache/huggingface`.
- The default tokenizer fits both models considered so far: `bge-m3` and `multilingual-e5-large` are built on
  XLM-RoBERTa and share it. 480 leaves room under the 512-token limit of the smaller one. For another model, pass
  its name with `--tokenizer` and set `--max-tokens` to its limit. The name is the Hugging Face one (`BAAI/bge-m3`),
  not the id the server gives the model.
- `--skip-existing` looks only at whether the `.jsonl` exists. After changing an option or the config file, run
  without it, or into a new folder.
- Docling's own chunkers are not used. On these documents its heading labels are unreliable (in the scans every
  heading is level 1, and lines like `unde:` are labeled headings), so the structure is read from the numbering in
  the text.

### The config file

Every `.json` needs an entry in `chunk-config.json`, under its file name without `.json`. A file with no entry
fails with `no entry for ... in the config file`; the others still run.

```json
"28_13_NP_073_2002": {
  "code": "NP 073-02",
  "title": "Norme de prevenire și stingere a incendiilor ...",
  "year": 2002,
  "status": "unverified",
  "profile": "art",
  "first_body_page": 10
}
```

| Key | Meaning |
|---|---|
| `code` | The norm's indicative. It starts every breadcrumb and, in lower case, every chunk id (`np-073-02`). |
| `title`, `year`, `status` | Copied into every chunk as `doc_title`, `edition_year` and `status`. `status` is `in_force`, `unverified` (nobody has checked) or `repealed`; a repealed norm needs `repealed_on` (`2026-08-25`). Optional beside them: `amended_by` (a list of the acts that changed the norm later) and `gazette` (where it was published). All of it goes into the citation and the status the answering step gives the model: see COMMANDS.md, step 7. |
| `profile` | How the document numbers its text: see the table below. |
| `first_body_page` | Optional, PDFs only. The PDF page the norm's own text starts on; everything before it (title page, table of contents) is left out. Without it the script finds the end of the contents itself, which works for the Word files but not for a scan whose contents entries look like headings. |
| `embed_field` | Optional. Read by `embed_chunks.py`, not by this script: the chunk field a document is embedded from, `embed_text` (the default) or `text`. |

| `profile` | A chunk starts at | Used for |
|---|---|---|
| `art` | `Art. 12` or `Art. 2.3.2.1`; in annex 10 of P118-1 also `A.10. 3.10.95.` | P118-1/2025, `28_13` |
| `decimal` | a numbered paragraph, `3.7.4.2`. A short numbered line that is not a sentence is taken as a title and goes into the heading path. | the parking norm, `28_1`, `28_4`, `28_6`, `28_12`, `sp28_15`, `sp28_16` |
| `headings` | each heading | `28_3`, which has no numbering |

To add a document: convert it with `--to json`, add its entry, and for a PDF open it to read off the page the text
starts on.

### What a chunk holds

One JSON object per line. Letters outside ASCII are written as themselves, so a plain search of the file for `ă`
works.

| Field | Content |
|---|---|
| `id` | `p118-1-2025:art-2.3.2.1:0`: document, unit, piece. The piece counts up when an article was split. Text with no number of its own is `sec-N`; a table adds `-tab1`, scraps add `-frag1`. A number that occurs twice in a document gets `~2`. |
| `kind` | `article`, `table` or `fragmented` (below). |
| `doc_key`, `doc_code`, `doc_title`, `edition_year`, `status`, `amended_by`, `repealed_on`, `gazette` | The source file name, and the entry from the config file. |
| `label`, `caption` | How the chunk is named in the breadcrumb and in a citation: `Art. 2.3.1` or `7.10–7.11` (empty for text without a number), and a table's title. |
| `heading_path` | The headings above the chunk, outermost first. |
| `article`, `articles` | The article or paragraph number, and all of them when small neighbours were joined. `null` and empty for text that has no number. |
| `pdf_pages` | `[first, last]` PDF page. `null` for the Word files, which have no pages. |
| `text` | The text of the chunk. |
| `embed_text` | What to embed: the breadcrumb (`P118-1/2025 > CAPITOLUL 2 > ... > Art. 2.3.2.1`), a line break, then `text`. |
| `prev_id`, `next_id` | The neighbours in reading order. Chunks do not overlap; use these to fetch context. |
| `parent_id` | For a table or scraps: the first chunk of the article they belong to. |
| `flags` | `has_table`: the article has a table chunk. `oversize`: still over `--max-tokens`. `broken_chars`: the text contains `�`. |
| `images` | The pictures that belong to this chunk, from `--images` conversions: `path` (the PNG, relative to the folder the chunks were written to), `ref` (Docling's `#/pictures/N`), `page` (`null` for Word), `width`, `height`, `caption`. Empty when there are none. |

- Pictures are attached, not inserted: `text` and `embed_text` do not change. A picture goes with the text just before
  it (the paragraph that introduces it); after a heading, with the text under that heading (not with the figure's own
  scraps, and not past the next heading); inside a table cell, with the table (its first piece). The scraps of a
  figure are in a `fragmented` chunk, which is never embedded, so their pictures are also listed on the first article
  chunk of the unit, or else its first table chunk. A picture with neither stays only on the `fragmented` chunk and
  cannot be found by a search (one in P118/2-2013: the frost-depth map of annex 32). A JSON converted without
  `--images` has no picture files and gives empty `images`. The status line says where every picture went (`... in
  embedded chunks, ... only in fragmented chunks, ... cut with the contents, ... not placed, ... file missing, ... without a
  file`) and covers every picture of the document; `not placed` should be 0, and a picture in a layer Docling does not
  read as body would show there. A picture kept inside the JSON as data (one in the fire safety norm) has no file and is
  not listed.
- A table chunk has one line per row, cells joined by ` | `, with the caption and the header rows on top. A table
  too large for one chunk is cut between rows and every piece repeats the caption and header rows. A table with
  fewer than two rows of text is page layout and stays in the article's text.
- A `fragmented` chunk is what was read from inside a picture, or a run of five or more items of at most 12
  characters in a scan: a figure or a rotated table that came out word by word. Leave these out when embedding;
  they are kept so that nothing disappears unseen.
- The table of contents, and a closing list of tables or figures, are cut by position and are in no chunk.

### Reading the status line

```
ok in 6s (2680 chunks: 2077 article, 603 table; body 1685026 characters = headings 20221 + chunks 1664805;
contents and lists cut: 71485; 0 numbers out of sequence; 739 labels restored from ....docx; words in the Word
file 316574, in the JSON 317716)
```

- `body ... = headings ... + chunks ...` counts visible characters. The script checks this sum itself: if any text
  was lost or doubled on the way to the chunks, the file fails with `text lost or doubled` and nothing is written.
- `contents and lists cut` is the size of what was left out in front and at the end. A value far from the size of
  the document's table of contents means the cut is in the wrong place.
- `numbers out of sequence` counts article numbers that do not follow the one before. The first 20 are listed above
  the status line, for example `sequence: 499 is followed by 580`. In the scans most are OCR misreads of a digit;
  a real gap means an article start was not recognised and its text sits in the chunk before it. The numbers are
  reported, not repaired.
- With `--docx-dir`: the number of labels put back, and the word count of the Word file against that of the JSON.
  The two should be within 1%; a JSON far above holds text twice.
- `oversize` and `broken_chars` count the chunks carrying those flags.

### State of the results

- P118-1/2025 is checked: all 816 article numbers of the Word file are found, each once; no chunk is over 480
  tokens; 1,373 of the 1,378 `(N)` paragraph labels are present. The five missing are on lines whose text differs
  between the Word file and the JSON (several hold an equation object), so they could not be lined up.
- The parking norm comes out wrong and should not be used yet. Docling's JSON has its headings out of place and
  one misnumbered, and a numbered list in article 1.2 is taken for chapter headings, so most breadcrumbs are wrong.
- The eight PDFs run and pass the character check, but their chunks have not been read through. `28_3` is mostly
  figures: 77 of its 119 chunks are `fragmented`.
- `sp28_15`: 154 chunks carry `broken_chars`. The PDF has a text layer with a broken font and Docling fills the
  table cells from it, so the letters `ă ș ț î â` in those tables are lost at the source. The chunker cannot
  restore them; the file would have to be converted again from page images.

## `embed_chunks.py`

Turns every chunk into a vector with the model `bge-m3`, which the server runs, and writes `<name>.npz` per chunk
file.

```sh
venvs/bin/python embed_chunks.py --input-dir docs_processed/chunks --output-dir docs_processed/vectors
```

| Option | Default | Meaning |
|---|---|---|
| `--config` | `chunk-config.json` beside the script | Read for `embed_field` (below). |
| `--server` | `settings.env`, else `http://127.0.0.1:1234` | Address of the server that runs the model. It must speak the OpenAI embeddings API, as LM Studio does. |
| `--model` | `text-embedding-bge-m3` | The model's id on the server, as LM Studio lists it. |

- The first line of a run says whether the model reproduces the example on its model card
  (`text-embedding-bge-m3 reproduces its model card: [[0.6254, 0.3475], [0.3487, 0.6786]] (on http://127.0.0.1:1234)`;
  the card says `[[0.6265, 0.3477], [0.3499, 0.678]]` and 0.01 either way is accepted). If it does not, the run stops before
  anything is written: the vectors would be wrong in a way no later step shows. The small gap is there because the
  server runs a compressed copy of the model (`Q8_0`).
- `no answer from ...` means the server is off, asleep or at another address; the script waits up to a minute
  before saying so. Pass `--server` with the whole address, `http://` and port included, if it changed.
- `answered with model 'X', not 'Y'` means the model asked for is not loaded. LM Studio answers an id it does not
  know with whatever embedding model is loaded, so the script checks which model answered and refuses the vectors
  of another one. Load the model, or pass the id LM Studio shows with `--model`: a second loaded copy is listed as
  `text-embedding-bge-m3:2`. Under an id other than `text-embedding-bge-m3` the model-card check is skipped (the
  first line says so), and vectors made under one id are not reused under another.
- What is embedded is each chunk's `embed_text`, the breadcrumb and the text. A document whose entry in
  `chunk-config.json` has `"embed_field": "text"` is embedded from `text` alone. The parking norm is set so, because
  its breadcrumbs are wrong.
- Chunks of kind `fragmented` get no vector. The status line counts them as `left out`.
- A second run embeds only the chunks whose text changed; the status line says `0 embedded, 179 reused`. So after
  running `json_to_chunks.py` again, run this script again **without** `--skip-existing`: with it, a file whose
  `.npz` exists is not looked at.
- `over 512 tokens` in the status line counts texts longer than the chunker is meant to leave them. They are still
  embedded whole, as long as they fit what the server was loaded with (2,048 tokens). With the default chunk size
  there are none. The count is made on this machine with the chunker's tokenizer, because the server does not
  report how many tokens it read.
- An `.npz` holds the chunk ids, the vectors (1,024 numbers each, length 1), a hash of the text each vector was
  made from, and the model's id. Read it with `numpy.load(path)`.
- The ten documents should take about two minutes: 64 chunks were measured at 1.15 seconds. The whole run has not
  been timed.
- For another model, load it on the server, pass its id with `--model`, and add its model-card example to
  `REFERENCE` in the script, or its vectors go unchecked. Chunk again with that model's `--tokenizer` first. The
  texts are sent as they are: a model that needs a prefix such as `query: ` (`multilingual-e5-large`) is not
  supported as the script stands.

## The MCP Worker (`worker/`)

The search as an MCP server on Cloudflare, so that it answers when this Mac and the Ubuntu box are off. It does what
`mcp_server.py` (now in `_archive/`) did: one tool, `search_norms`, by meaning (`query`) or by number (`article`), with the same
excerpts, citations, status words and rules (`worker/mcp-instructions.md`, read at build time; it is the whole prompt). OCR and chunking stay on
the Mac.

| Part | Holds |
|---|---|
| Worker `norms-mcp` | MCP at `/mcp`, behind the bearer key `MCP_KEY` |
| Workers AI | embeds the question with `@cf/qwen/qwen3-embedding-0.6b` (1024 numbers), as a query with an instruction |
| Vectorize index `norms` | one vector per chunk, its id the chunk id, its metadata `norm` the norm's code |
| D1 database `norms` | the norms, chunks, article numbers and figures (`worker/migrations/0001_init.sql`) |
| R2 (later) | the PNGs; `FIGURES_BASE_URL` in `wrangler.jsonc` is their public address |

**State:** the code and its tests are done, and so is `upload_to_cloudflare.py`, which loads the chunks, vectors and
PNGs (see *Uploading*). Nothing exists on Cloudflare yet and nothing was uploaded: until it is, the Worker has
nothing to search.

### Before you start

- Node.js and npm (this Mac has them through nvm). Install the packages once, from `worker/`: `npm install`.
- A Cloudflare account, and `npx wrangler login` once.
- The chunks must have short codes in `chunk-config.json` (`P118/2-2013`, not the norm's title): the chunk ids are
  made from the code, and Vectorize takes ids of at most 64 bytes. D1 refuses longer ones. Chunk again after
  changing a code.

### Commands (from `worker/`)

| Command | Does |
|---|---|
| `npm test` | All tests, in the Workers runtime with a local D1 (`wrangler.test.jsonc`). Workers AI and Vectorize have no local version: the tests pass stand-ins. |
| `npm run typecheck` | TypeScript, no output files |
| `npm run format` | Prettier on the folder (`worker-configuration.d.ts` is left alone) |
| `npm run types` | Writes `worker-configuration.d.ts` from `wrangler.jsonc`. Run it after every change to `wrangler.jsonc`; never edit that file by hand. |
| `npm run dev` | The Worker on this machine. Workers AI and Vectorize are still Cloudflare's (needs the login); the key comes from `.dev.vars`. |
| `npm run db:migrate:local` / `db:migrate:remote` | Applies `migrations/` to the local or the real D1 |
| `npm run deploy` | Publishes the Worker |

Tests and `wrangler types` start a local runtime that listens on a port. Inside Claude Code's sandbox that is refused
(`listen EPERM`) unless `sandbox.network.allowLocalBinding` is on.

`test/golden.ts` holds what the Python search gave for fixture chunks; the tests check the Worker gives the same.
The Python search is archived, so the file is now fixed: when the Worker's output changes on purpose, change the
tests that compare with it. `_archive/make_golden.py` wrote it; it is kept for reference and no longer runs as it is
(the archived `mcp_server.py` looks for the prompt next to itself).

### Setting up Cloudflare (once, not done yet)

```sh
npx wrangler d1 create norms                       # put the id it prints into wrangler.jsonc, database_id
npm run db:migrate:remote
npx wrangler vectorize create norms --dimensions=1024 --metric=cosine
npx wrangler vectorize create-metadata-index norms --propertyName=norm --type=string   # before any vector is added
openssl rand -hex 32                               # the key; keep it
npx wrangler secret put MCP_KEY                    # paste the key when asked
npm run deploy
```

- The metadata index must exist before the first upload: vectors added earlier cannot be filtered by norm.
- The key is at least 32 characters; without a usable key the Worker answers every request with 500.
- For `npm run dev`, put `MCP_KEY=<key>` in `worker/.dev.vars` (not committed).

### Uploading

`upload_to_cloudflare.py` (project folder) loads every `.jsonl` of a chunks folder into D1 and Vectorize, and with
`--bucket` its PNGs into R2. A norm loaded before is replaced; what the new chunks no longer hold is deleted.

```sh
venvs/bin/python upload_to_cloudflare.py --chunks-dir docs_processed/chunks --dry-run    # checks, writes the files, sends nothing
venvs/bin/python upload_to_cloudflare.py --chunks-dir docs_processed/chunks [--bucket NAME] [--cache-dir DIR] [--config FILE]
venvs/bin/python upload_to_cloudflare.py --remove P118-II [--bucket NAME] [--yes]
```

- Settings: `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` in `settings.env` (or the environment). The token
  needs D1 and Vectorize edit, Workers AI, and R2 edit (only with `--bucket`). Wrangler gets them in its environment, never on its command line.
  `--dry-run` needs neither.
- Wrangler runs as `npx wrangler` from `worker/` (run `npm install` there first), so it reads `wrangler.jsonc`.
- Order: every norm is checked first (a config entry, no two with one code, chunk ids of at most 64 bytes, every
  PNG present) and embedded by Workers AI (`@cf/qwen/qwen3-embedding-0.6b`, as passages), so a failure there leaves
  Cloudflare untouched. Then per norm: read what D1 holds of it; stop if another norm in D1 has its code; upsert the
  vectors; put the PNGs whose key and hash D1 does not have yet; delete the vectors of chunks that are gone; replace
  the norm in D1 in one file (one transaction). In this order a run that failed half way can be run again. A norm
  that fails there is reported and the others go on; the run ends with the list of those that failed.
- The vectors are cached in `docs_processed/cloudflare_cache` (`--cache-dir`), one `.npz` per norm: a run after a
  re-chunk embeds only the texts that changed.
- `--dry-run` writes each norm's `.sql` and `.ndjson` (cached vectors, `null` for those it would embed) to a
  temporary folder it prints. No network, no wrangler.
- `--remove DOC_KEY` (the chunk file's name) deletes the norm's vectors, its PNGs with `--bucket`, then its rows (the
  schema cascades). It asks first unless `--yes`.
- Without `--bucket` no PNG is sent; the figures still go into D1, with an empty hash, so the first later run with
  `--bucket` sends all of them.
- Chunks made before the short codes have ids over 64 bytes: the script refuses them. Chunk again first.
- The first real run needs everything in *Setting up Cloudflare* above: the D1 id in `wrangler.jsonc` (still the
  zero placeholder), the migration applied, and the Vectorize index with its `norm` metadata index. `--bucket` also
  needs the bucket, which that list does not make yet: `npx wrangler r2 bucket create <name>`.

### Connecting Claude

Claude Code:

```sh
claude mcp add --transport http norms https://norms-mcp.<your-subdomain>.workers.dev/mcp --header "Authorization: Bearer <key>"
```

- Clients of MCP's 2026 protocol get JSON replies; 2025 clients (Claude Code and Desktop today) get a short event
  stream, which they read the same way.
- Not known yet: whether claude.ai's custom connectors accept a fixed `Authorization` header. If they do not, the
  Worker needs OAuth (Cloudflare Access or an MCP server portal) for claude.ai and the phone app.
- On a custom domain the handler does not check the Host header (only the key gates it) and refuses browser
  Origins other than localhost: see the comment at the top of `src/index.ts`.

### Where the code is

| File | Holds |
|---|---|
| `src/index.ts` | the entry: key check, then the MCP handler (`agents` `createMcpHandler`, stateless) |
| `src/auth.ts` | the bearer key check |
| `src/store.ts` | D1 queries, the Vectorize search, the Workers AI embedding |
| `src/search.ts`, `src/tool.ts`, `src/rules.ts`, `src/server.ts` | the port of the archived `search.py`, `search_tool.py` and the tool's description |
| `src/types.ts` | what the data layer and the search logic exchange |

Where it still differs from the archived `mcp_server.py`: a call to an unknown tool gets the MCP library's own error, not
`NO_SUCH_TOOL`; the figures are still said to be not shown (links to them come with R2).
