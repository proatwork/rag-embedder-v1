# Future features: the model searches for itself

Goal: work the way a tool-based norms service does. Today the pipeline searches once, with the user's own words, pastes the
top chunks into the prompt and lets the model write. Instead, the model gets a **search tool** and decides when and what to
search; every result comes back with a full citation, a status, a figure note and the reading rules. The retrieval
(bge-m3 vectors, chunks, `chunk-config.json`) stays as it is; what changes is who calls it and what it returns.

The steps are in build order. Each is usable on its own; 1 and 2 give most of the value.

## 1. A search result the model can cite from — done (2026-10-11)

Every excerpt now starts with a full citation (norm, article or point or headings, title, edition, gazette, PDF page), then a line
with the norm's status in words and its figures. `citation()`, `status_words()` and `figures_note()` in `search.py` are used by
`search.py`, `ask.py` and the chatbot; step 2 returns the same. The config's `status` is `in_force`, `unverified` or `repealed`, with
`repealed_on`, `amended_by` and `gazette` beside it. See COMMANDS.md, steps 3 and 7.

Language: what the model may quote is Romanian (the citation, the status words, the figure note: constants at the top of
`search.py`); field names, config keys and tool parameters are English.

Still open here:
- P118-1/2025 has no page numbers (it was converted from a Word file; its PDF would give them), the pages are those of the PDF file
  and not the printed ones, and no norm has `amended_by` or `gazette` filled in.
- A citation is only as good as the chunker's heading path. Where the chunker missed an annex heading or took a line of text for one,
  the citation names the wrong place, though the page stays right. Found in a review on 2026-10-11, mostly in P118/2-2013: the annexes 2,
  3, 12, 18, 23, 29 and 30 have no heading of their own, so their text is cited under the heading before (for example the stack-storage
  tables of ANEXA NR. 2 under "58. STAS 6054-77 …", an item of the list of references in ANEXA NR. 1); 13 chunks have a point's text
  as heading ("7.167. (1) Configurații …"); 8 chunks share a number with another chunk (ids with `~2`). In P118-1/2025, 95 chunks without
  a number stand under headings with no annex or chapter in them. Fixing this means changing `json_to_chunks.py`, which changes heading
  paths and so the embedded text: do it together with a re-embed and an eval run, not on the side.
- 11 of the 53 picture captions are body text that the converter took for a caption.

## 2. The search as a tool the model calls — 2a done (2026-10-11), 2b open

**2a, done:** the model is given one tool, `search_norms(query, norm, article, count)` (`search_tool.py`), and searches for itself,
up to 5 times for one question: `ask.py` (the default; `--single-search` is the old way) and the chatbot with the OpenAI style
(*Search*: the model searches / once). A follow-up like "și la clădiri înalte?" is searched as "clădiri înalte: rezistența la foc a
pereților antifoc". The excerpts are numbered across the searches of one question. A model that does not search, or writes its
call as text, is handed the chunks of a single search instead; one that calls past the limit is asked once more without the tool.
See COMMANDS.md, steps 7 and 8.

Still open here:
- The result is the plain numbered excerpts under one line that says what was searched. The fixed envelope (`how_to_read`,
  `in_short`, `results`) and the reading rules inside each result are step 3.
- LM Studio's native `/api/v1/chat` has no tool calls in its documented request: the chatbot's "LM Studio style" keeps the single
  search. `tool_choice: "required"` is not honoured by LM Studio with Qwen (tried 2026-10-11: it answered "Mulțumesc!" without a
  call), so a model that does not search is caught after the fact, not prevented.
- A reply to small talk ("Mulțumesc!") costs two rounds: the model answers without a search, which is not accepted, and is then
  handed chunks it does not need.
- Every wrong call (an unknown norm, a number no chunk has) uses up one of the five searches.
- An article in more than 20 pieces cannot be fetched whole by its number (P118-1/2025 art. 1.2.1 has 96): the model is told to
  search for the part it needs.
- A call written as text is recognised by a few known shapes (`<tool_call>`, a bare JSON call, `[TOOL_CALLS]`); another shape, after
  a search that found something, would pass as an answer.
- The page's handling of the events is tested by reading its source, and by a Node script kept outside the project
  (it runs the real `chatbot.js` against a stand-in page). Moving it into `chatbot/tests` would make Node a test dependency.
- With the OpenAI style LM Studio sends no token usage in a stream unless asked (`stream_options.include_usage`), so the page
  estimates the counts.

**2b. An MCP server** (`mcp_server.py`, stdio) exposing the same `search_norms`, for Claude Desktop, Claude Code and any other MCP
client. It loads the chunks and vectors once and asks the embedding server for the query's vector, as the chatbot does; one
`SearchSession` per call (an MCP client keeps no "question"), so the numbering starts at 1 each time unless the client says
otherwise. ~120 lines with the `mcp` package (one new dependency), a test through the stdio protocol.

## 3. Rules that travel with the data

Move the reading rules from the system prompt into the result envelope (step 2) and keep in the system prompt only what is
about the answer's form (language, length, citing `[n]`). Add the rules the service states and we do not yet:
- cite verbatim; what the text does not give (a threshold, a value) is said to be not given, never filled in;
- a value that looks wrong is not corrected and not called a scanning error: "așa scrie în …", then what the other acts say;
- say what came from which source when more than one norm answers;
- an abrogated act in the past tense with the date; `neverificat` is not "in force".

`ask-instructions.md` stays the file for the answer's form; a second file, `ask-result-rules.md`, is the reading-rules text.

## 4. A second source: the law

The service answers from two shelves: the technical norms and the law (today the Code 169/2026, with "before / after" cards
per article). To do the same: a second document set (`LegiConstructii/`) converted and chunked with its own profile (`art` with
"Articolul N" headings), its own config entries (`fel: lege`), and the tool result split into `lege` and `normative` as the
service does. The retrieval is unchanged; the work is the chunking profile for the law's layout and the two-shelf envelope.
Left for when the law texts are available.

## 5. Reranker and hybrid search (kept from the old plan, lower priority)

With the model writing its own queries, retrieval misses drop; measure first. If needed:
- **reranker** `BAAI/bge-reranker-v2-m3` over the top 30 (about 80 lines, `rerank.py`, ~1–3 s a query on this Mac; LM Studio
  has no rerank endpoint, checked 2026-10-11);
- **hybrid** BM25 + vectors with reciprocal rank fusion, diacritics folded, a Romanian stemmer (about 100–150 lines).
Run `eval_retrieval.py` before and after; the eval set is `eval-questions-3docs.jsonl` (322 questions).

## Order

1 and 2a are done. Next: 3 rules in the result → the links between norms (where one norm relaxes a limit another sets) →
2b MCP server → measure with the 322 eval questions → 4 the law (depends on the texts) → 5 if the eval says so.
