# Manual of the archived scripts

These sections were moved here from the project's MANUAL.md when the local search, the chat and the Python MCP
server were archived; the search now runs on Cloudflare (`worker/`). The commands were written for the project
root: to run one now, run it from `_archive/` with `PYTHONPATH=..:.` so that it finds the scripts that stayed.

## `search.py`

Shows the chunks closest to a question, or the chunks of one article.

```sh
venvs/bin/python search.py --chunks-dir docs_processed/chunks --vectors-dir docs_processed/vectors "perete antifoc"
venvs/bin/python search.py --chunks-dir docs_processed/chunks --article 2.3.2.1.2
```

| Option | Default | Meaning |
|---|---|---|
| `--chunks-dir` | required | The folder `json_to_chunks.py` wrote. |
| `--vectors-dir` | required for a question | The folder `embed_chunks.py` wrote from those chunks. |
| `--top` | `5` | How many results to show. |
| `--doc` | all norms | Only this norm, by its code: `--doc "P118-1/2025"`. |
| `--article` | | List every chunk holding this article or paragraph number, tables included. The server is not asked. |
| `--config`, `--server` | | As for `embed_chunks.py`. |

- Each result shows its score (1 is identical text; unrelated text is not 0, compare scores only with each other),
  the chunk id, the kind and flags, the citation with the norm's status in words, the breadcrumb, and the start of the text.
- The question is embedded by the server, with the model named in the `.npz` files, so a search needs the server
  running with that model loaded. Its `ş ţ` become `ș ț`, as in the chunks. Letters typed without diacritics are
  left as typed.
- `... changed after its vectors were made: run embed_chunks.py again` means the chunk file was written again since
  it was embedded. `no vectors for ...` means the file was never embedded.
- The pictures of a result's chunk are listed under it as `image:` lines (the file, the PDF page, the start of the caption). The path
  is made from `--chunks-dir`, so it opens from where the script runs. `ask.py` shows them the same way.
- A number such as `4.1` exists in several norms; add `--doc` to `--article` to see one.
- `--article` asks the server nothing and works without it.

## `eval_retrieval.py`

Searches every question of a file and reports where the article that answers it came.

```sh
venvs/bin/python eval_retrieval.py --chunks-dir docs_processed/chunks --vectors-dir docs_processed/vectors --questions eval-questions.jsonl
venvs/bin/python eval_retrieval.py --chunks-dir docs_processed/chunks --questions eval-questions.jsonl --check-questions
```

| Option | Default | Meaning |
|---|---|---|
| `--questions` | required | The questions file (below). |
| `--strip-diacritics` | off | Also score every question typed without `ă â î ș ț`, in a second table. |
| `--check-questions` | off | Only check that every answer names a chunk that is embedded. No server, no `--vectors-dir`. |
| `--chunks-dir`, `--vectors-dir`, `--config`, `--server` | | As for `search.py`. |

### The questions file

One JSON object per line:

```json
{"question": "Ce rezistență la foc trebuie să aibă un perete antifoc?", "doc": "P118-1/2025", "articles": ["2.3.2.1.2"], "author": "me"}
{"question": "De la ce înălțime se consideră o clădire ca fiind înaltă?", "doc": "P118-1/2025", "ids": ["p118-1-2025:art-1.2.1:16"]}
```

| Key | Meaning |
|---|---|
| `question` | As you would type it. |
| `doc` | The norm's code, as in `chunk-config.json`. |
| `articles` | The article or paragraph numbers that answer it. A result counts when it is a chunk of that norm holding one of them (also as the second of two joined articles), or a table of that article. |
| `ids` | For text with no number, one table, or one piece of a long article. A chunk id without its last part (`p118-1-2025:sec-3-tab1`) stands for all its pieces; a whole chunk id (`...:art-1.2.1:16`) for that piece only. It must match exactly: `sec-1` does not count `sec-18`. |
| `author` | Optional. With more than one author in the file, the scores are also printed per author. |

- A question needs `articles` or `ids`; it may have both. Find them with `search.py --article N`.
- Use `ids` with a whole chunk id for a definition: all the definitions of P118-1 are article `1.2.1`, which is
  over 80 chunks, and `"articles": ["1.2.1"]` would count any of them. Piece numbers move when the chunker's
  settings change; `--check-questions` only notices an id that no longer exists.
- A question naming a norm, number or id that no embedded chunk has stops the run with the line number. It is a
  mistake in the file, not a miss.

### Reading the result

```
                         n   top 1   top 5  top 10     MRR
all                     30     60%     83%     90%    0.70
by norm:
  P118-1/2025           15     67%     87%     93%    0.75
```

(The numbers above only show the layout.)

- `top 5` is the share of questions whose answer is among the first five results; this is the one to watch, since
  five chunks is about what an answering step would be given. `MRR` is the mean of 1/rank, counting an answer
  below rank 10 as zero.
- Every question is searched in all ten norms, not only in its own.
- Below the tables, every question is listed with the rank of its answer. For one not found in the first 10, the
  three chunks that came first are listed.
- Keep the output of a run (`> eval-2026-10-10.txt`) to compare with the next one.

### The questions that are there

`eval-questions.jsonl` holds 30 questions written by Claude (`"author": "claude"`): 15 on P118-1/2025, 4 on
P118/2-2013, 3 on P118/3-2015, 2 on the parking norm and 1 on each of the other five. Six are typed without
diacritics, eight are answered in a table, and two can only be answered from a `broken_chars` table of `sp28_15`.

**Their score is an upper bound.** Each answer was found by reading the chunks, so the wording leans toward the
norm's, and they are not the questions asked at work. A bad score is real; a good one is not yet proof. Delete or
reword the ones you would never ask, and add your own with another `author`: the score per author then shows how
much the first set flatters the search.

