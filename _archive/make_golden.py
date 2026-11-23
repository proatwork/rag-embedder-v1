"""Writes worker/test/golden.ts: fixture chunks and what the Python search gives for them.

Run from the project root: venvs/bin/python <this file> worker/test/golden.ts
"""

import json
import sys
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path.cwd()))

import mcp_server  # noqa: E402
import search  # noqa: E402
import search_tool  # noqa: E402
from embed_chunks import Embedder  # noqa: E402
from search_tool import SearchSession  # noqa: E402

DOC_KEY = "doc"
CONFIG = {DOC_KEY: {}, "other": {}}
NORM = "T 1/2025"
OTHER_NORM = "T 2/2013"
TITLE = "Titlu"
OTHER_TITLE = "Normativ, Partea a II-a"


def figure(number: int, page, caption: str) -> dict:
    return {"id": f"t-1-2025/fig-{number}", "url": "", "page": page, "width": 100, "height": 50, "caption": caption}


def chunk(key: str, number: str | None, label: str | None, norm: str = NORM, piece: int = 0, **fields) -> dict:
    other = norm == OTHER_NORM
    text = f"{label or 'Text'}. Bucata {piece}."
    record = {
        "id": f"{norm}:{key}:{piece}", "kind": "article", "doc_key": "other" if other else DOC_KEY, "doc_code": norm,
        "doc_title": OTHER_TITLE if other else TITLE, "edition_year": 2013 if other else 2025, "status": "in_force",
        "amended_by": [], "repealed_on": None, "gazette": None, "heading_path": [], "label": label, "caption": None,
        "article": number, "articles": [number] if number else [], "pdf_pages": None, "text": text,
        "embed_text": f"{norm} > {label or 'Text'}\n{text}", "prev_id": None, "next_id": None, "parent_id": None,
        "flags": [], "images": [],
    }
    record.update(fields)
    return record


RECORDS = {
    "wall": chunk("wall", "2.3.1", "Art. 2.3.1", pdf_pages=[36, 36], gazette="M. Of. nr. 595 bis/2013"),
    "door": chunk("door", "2.3.2", "Art. 2.3.2", pdf_pages=[95, 96], status="unverified"),
    "point": chunk("point", "7.10", "7.10", caption="TABEL 7.1  Înălțimea\nmaximă", status="repealed", repealed_on="2026-08-25", pdf_pages=[40]),
    "annex": chunk("annex", "A.10.1.1.1", "A.10. 1.1.1", amended_by=["Ordinul 6026/2018", "Ordinul 1/2020"]),
    "both": chunk("both", "3.1", "Art. 3.1", status="repealed", repealed_on="2020-01-05", amended_by=["Legea 1/2019"]),
    "headings": chunk("headings", None, None, heading_path=["ANEXA 4 - ALCĂTUIRI", "ANEXA 4.1 - ASCENSOARE", "Ascensoare pentru intervenție", "Prevederi generale"]),
    "section": chunk("section", None, "7", heading_path=["SECŢIUNEA 3", "Ceva"]),
    "plain": chunk("plain", None, None, heading_path=["A", "B", "C"]),
    "long": chunk("long", None, "", heading_path=["x" * 200], caption="y" * 200),
    "bare": chunk("bare", None, None, heading_path=[], doc_title="", pdf_pages=[]),
    "old": chunk("old", "1.2", None, articles=["1.2", "1.3", "1.4"]),
    "unknown": chunk("unknown", "8.1", "8.1", status="unknown"),
    "pictured": chunk("pictured", "9.1", "Art. 9.1", images=[figure(n, 4 if n < 4 else 5, f"Figura  {n}\nPerete") for n in range(1, 8)]),
    "pictured_no_page": chunk("pictured_no_page", "9.2", "Art. 9.2", images=[figure(1, None, ""), figure(2, None, "z" * 200)]),
    "pictured_one": chunk("pictured_one", "9.3", "Art. 9.3", images=[figure(1, 4, "Figura 1")]),
    **{f"piece{piece}": chunk("pieces", "5.1", "Art. 5.1", piece=piece) for piece in range(4)},
    "elsewhere": chunk("elsewhere", "2.3.1", "Art. 2.3.1", norm=OTHER_NORM),
}
ORDER = list(RECORDS)  # the order of the norms, as the store gives it
VECTORS = {key: [1.0 if other == key else 0.0 for other in ORDER] for key in ORDER}  # every chunk a direction of its own
STEP = 0.125


def towards(*keys: str) -> list[float]:
    vector = [0.0] * len(ORDER)
    for place, key in enumerate(keys):
        vector[ORDER.index(key)] = 1 - place * STEP
    return vector


ABOUT_WALLS = "pereți antifoc"
ABOUT_DOORS = "uși rezistente la foc"  # comma below
DOORS_AS_TYPED = "uşi rezistente la foc"  # cedilla
NOTHING_CLOSE = "nimic"
QUESTIONS = {ABOUT_WALLS: towards("wall", "door", "elsewhere"), ABOUT_DOORS: towards("door", "point", "wall"), NOTHING_CLOSE: [0.0] * len(ORDER)}

INDEX = search.Index([RECORDS[key] for key in ORDER], np.array([VECTORS[key] for key in ORDER], dtype=np.float32), "model")

CALLS = [
    {"query": ABOUT_WALLS, "count": 3}, {"query": ABOUT_WALLS}, {"query": f"  {ABOUT_WALLS} "}, {"query": DOORS_AS_TYPED, "count": 2},
    {"query": ABOUT_WALLS, "norm": " t 2/2013 "}, {"query": ABOUT_WALLS, "norm": OTHER_NORM, "count": 20}, {"query": ABOUT_WALLS, "norm": "", "count": 1},
    {"query": ABOUT_WALLS, "norm": "   ", "count": 1}, {"query": ABOUT_WALLS, "norm": None, "count": 1}, {"query": ABOUT_WALLS, "count": None},
    {"query": ABOUT_WALLS, "count": ""}, {"query": ABOUT_WALLS, "count": " 2 "}, {"query": ABOUT_WALLS, "count": "+2"}, {"query": ABOUT_WALLS, "count": "2_0"},
    {"query": ABOUT_WALLS, "count": "02"}, {"query": ABOUT_WALLS, "count": 2.0}, {"query": ABOUT_WALLS, "count": 20},
    {"article": "Art. 2.3.1."}, {"article": "pct. 7.10"}, {"article": "a.10.1.1.1"}, {"article": "2.3.1", "norm": OTHER_NORM},
    {"article": "5.1", "count": 2}, {"article": "5.1", "count": "4"}, {"query": ABOUT_DOORS, "article": "7.10"}, {"article": "9.1"},
    {"article": "9.2"}, {"article": "3.1"}, {"article": "8.1"}, {"article": "1.3"}, {"article": "2.3.1", "norm": "t 1/2025"},
    # what cannot be run
    {}, {"query": "  ", "article": ""}, {"query": None, "article": None}, {"query": 7}, {"norm": NORM, "count": 3},
    {"query": ABOUT_WALLS, "norm": "X 9"}, {"article": "2.3.1", "norm": "X 9"}, {"article": "99.9"}, {"article": "2.3"},
    {"article": "7.10", "norm": OTHER_NORM}, {"article": "anexa"}, {"article": "99.9", "norm": "t 1/2025"},
    *({"query": ABOUT_WALLS, "count": count} for count in (0, -1, 21, "21", "many", "2.5", 2.5, True, False, [2], {}, "  ", "0x10", "1e3", "2 0")),
    {"article": "7.10", "count": 0}, {"query": ABOUT_WALLS, "norm": "X 9", "count": 0}, {"query": ABOUT_WALLS, "article": "7.10", "count": 0},
    *({"query": ABOUT_WALLS, "norm": norm} for norm in ([NORM], 5, 2.5, {"code": NORM}, True, "O'Brien", 'a"b', "a'b\"c", "back\\slash", "tab\there", "ș​x")),
    {"query": NOTHING_CLOSE, "norm": "X 9"},
]

ARTICLE_NUMBERS = [
    "Art. 2.3.1.", "art 2.3.1", "pct. 7.10", "  7.10 ", "2.3.2.1.2", "articolul 5", "pct. 7.10 alin. (2)", "2. 3. 1", "A.10. 1.1.1",
    "A.10.1.1.1", "a.10.1.1.1", "pct. A. 10. 1.1.1", "P118/2-2013, pct. 7.10", "7.10 din P118/2-2013", "art. 7.10 alin. (2) lit. 3",
    "anexa. 7.10", "norma.7.10", "ordonanța. 7.10", "legea.A.7.10", "  anexa  ", "", "1.2 și 3.4", "A.1", "ţa.5.6", "x 2. 3",
]
PAGES = [None, [], [None, 0], [4], [4, 4], [9, None, 4], [10, 9, 100]]


def main() -> None:
    out = Path(sys.argv[1])
    per_record = {
        key: {
            "citation": search.citation(record), "reference": search.reference(record), "locator": search.locator(record),
            "status": search.status_words(record), "figures_shown": search.figures_note(record, True),
            "figures_not_shown": search.figures_note(record, False),
            "excerpt": search_tool.excerpt(1, record, record["embed_text"], figures_shown=False),
        }
        for key, record in RECORDS.items()
    }
    calls = []
    with mock.patch.object(Embedder, "encode", autospec=True, side_effect=lambda embedder, texts: np.array([QUESTIONS[text] for text in texts], dtype=np.float32)) as encode:
        for arguments in CALLS:
            state = mcp_server.SearchState(INDEX, CONFIG, "http://embedding.invalid", search.DEFAULT_TOP, "Rules.")
            encode.reset_mock()
            text, is_error = mcp_server.answer(state, search_tool.TOOL_NAME, arguments)
            embedded = [text for call in encode.call_args_list for text in call.args[1]]
            calls.append({"arguments": arguments, "text": text, "isError": is_error, "embedded": embedded})
    state = mcp_server.SearchState(INDEX, CONFIG, "http://embedding.invalid", search.DEFAULT_TOP, mcp_server.load_rules())
    tool = mcp_server.tool(state)
    golden = {
        "order": ORDER,
        "records": RECORDS,
        "vectors": VECTORS,
        "questions": QUESTIONS,
        "perRecord": per_record,
        "articleNumbers": [[text, search.article_number(text)] for text in ARTICLE_NUMBERS],
        "pages": [[pages, search.pages_note(pages)] for pages in PAGES],
        "calls": calls,
        "rules": mcp_server.load_rules(),
        "tool": {"name": tool.name, "title": tool.title, "description": tool.description, "inputSchema": tool.input_schema},
        "server": {"name": mcp_server.SERVER_NAME, "title": mcp_server.SERVER_TITLE, "version": mcp_server.SERVER_VERSION, "instructions": mcp_server.SERVER_INSTRUCTIONS},
        "searchUnavailable": mcp_server.SEARCH_UNAVAILABLE,
        "searchFailed": mcp_server.SEARCH_FAILED,
    }
    data = json.dumps(golden, ensure_ascii=False, indent=1)
    out.write_text(
        "// What the Python search (search.py, search_tool.py, mcp_server.py) gives for the fixture chunks here: the\n"
        "// expected strings of the tests, made once by running the Python functions on these same records. Data only.\n"
        "import type { ChunkRecord } from \"../src/types\";\n\n"
        f"export const GOLDEN = {data};\n\n"
        "export const RECORDS: Record<string, ChunkRecord> = GOLDEN.records;\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
