"""Cut a folder of Docling JSON documents into chunks for embedding: one .jsonl per .json, one chunk per line.

Every .json directly inside --input-dir (written by pdf_to_md.py or docx_to_md.py with --to json) becomes
--output-dir/<name>.jsonl. A chunk is one article or numbered paragraph of the norm; its chapter and section are kept
as a heading path and put in front of the text that gets embedded. Tables are chunks of their own.

Usage (run with the virtual environment's interpreter, the one Docling is installed in):
    venvs/bin/python json_to_chunks.py --input-dir docs_processed/md --output-dir OUT --docx-dir docs/docx
    venvs/bin/python json_to_chunks.py --input-dir IN --output-dir OUT --skip-existing     # resume
    venvs/bin/python json_to_chunks.py --input-dir IN --output-dir OUT --max-tokens 800    # larger chunks

Docling's own chunkers are not used: on these documents its heading labels are unreliable, so the structure is read
from the numbering in the text (CAPITOLUL, Art. 2.3.2.1, 3.7.4). Every document needs an entry in chunk-config.json.
--docx-dir names the folder with the original Word files: Docling drops the paragraph numbers Word generates, and
they are put back from the .docx of the same name. Sizes are counted with the tokenizer of the embedding model.
The pictures that pdf_to_md.py / docx_to_md.py wrote with --images are listed in the "images" of the chunk they belong to;
the text of the chunks is not changed by them.
A failing file is reported and skipped; the exit status is non-zero if any failed.
"""

import argparse
import difflib
import json
import os
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from itertools import chain
from pathlib import Path

import semchunk
from docling_core.types.doc import DocItemLabel, DoclingDocument, InlineGroup, ListItem, PictureItem, TableItem, TextItem

import docx_to_txt
import pipeline_common
from docx_to_txt import CELL_SEPARATOR
from pipeline_common import DOCX_SUFFIX, JSON_SUFFIX, JSONL_SUFFIX, UNSAFE_NAME_TAIL, ConversionError

TEXT_ENCODING = "utf-8"
CONFIG_FILE = Path(__file__).with_name("chunk-config.json")
CONFIG_KEYS = ("code", "title", "year", "status", "profile")  # required for every document
FIRST_BODY_PAGE_KEY = "first_body_page"  # optional: the PDF page the norm's text starts on
# What a norm's "status" may be. Only "in_force" says it is; "unverified" is for a norm nobody has checked.
STATUS_IN_FORCE = "in_force"
STATUS_UNVERIFIED = "unverified"
STATUS_REPEALED = "repealed"
STATUSES = (STATUS_IN_FORCE, STATUS_UNVERIFIED, STATUS_REPEALED)
# Optional, copied into every chunk with the status, for whoever cites it.
AMENDED_BY_KEY = "amended_by"  # the acts that changed the norm after this text: a list of names
REPEALED_ON_KEY = "repealed_on"  # the day a repealed norm stopped applying, as YYYY-MM-DD
GAZETTE_KEY = "gazette"  # where it was published, for example "M. Of. nr. 595 bis/2013"
EMBED_FIELD_KEY = "embed_field"  # optional, read by embed_chunks.py: which field of a chunk gets the vector
OPTIONAL_KEYS = (FIRST_BODY_PAGE_KEY, AMENDED_BY_KEY, REPEALED_ON_KEY, GAZETTE_KEY, EMBED_FIELD_KEY)
ISO_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")

# bge-m3 and multilingual-e5-large are both built on XLM-RoBERTa and share this tokenizer.
DEFAULT_TOKENIZER = "BAAI/bge-m3"
DEFAULT_MIN_TOKENS = 80
DEFAULT_MAX_TOKENS = 480  # leaves room under the 512 of the smaller model
MIN_BODY_SHARE = 4  # the text of a chunk gets at least max_tokens / this, however long its breadcrumb is

PROFILE_ART = "art"  # a chunk starts at "Art. N"
PROFILE_DECIMAL = "decimal"  # a chunk starts at a numbered paragraph, "3.7.4.2"
PROFILE_HEADINGS = "headings"  # no numbering: a chunk is the text under a heading
PROFILES = (PROFILE_ART, PROFILE_DECIMAL, PROFILE_HEADINGS)

KIND_ARTICLE = "article"
KIND_TABLE = "table"
KIND_FRAGMENTED = "fragmented"
FLAG_HAS_TABLE = "has_table"
FLAG_OVERSIZE = "oversize"
FLAG_BROKEN_CHARS = "broken_chars"
DATA_URI_PREFIX = "data:"  # a picture kept inside the JSON instead of in a file
REPLACEMENT_CHAR = "�"

BREADCRUMB_SEPARATOR = " > "
LINE_SEPARATOR = "\n"
RANGE_SEPARATOR = "–"
ARTICLE_PREFIX = "Art. "
ID_SEPARATOR = ":"
ID_REPEAT_MARK = "~"  # "art-12~2" is the second unit numbered 12
ID_ARTICLE = "art"
ID_SECTION = "sec"  # text that has no number of its own
ID_TABLE = "tab"
ID_FRAGMENT = "frag"

BLOCK_TEXT = "text"
BLOCK_HEADING = "heading"
BLOCK_TABLE = "table"

# Heading ranks where Docling gives no usable levels (the PDFs, where every heading is level 1): lower is higher up.
RANK_TOP = 1
RANK_PART = 10
RANK_SUBANNEX = 15
RANK_CHAPTER = 20
RANK_SECTION = 30
RANK_NUMBERED = 40  # plus the depth of the number: "3.6.1" is 43
RANK_UNNUMBERED = 90

WHITESPACE = re.compile(r"\s+")
NOT_WORD = re.compile(r"[\W_]+")
SLUG_BREAK = re.compile(r"[^a-z0-9]+")
TOC_LINE = re.compile(r"(\t|\.{3,}|…)\s*\d+\s*$")  # a contents entry: a tab or dot leader, then the page number
TOC_TITLE = "cuprins"
ARTICLE = re.compile(r"Art\.\s*(\d+(?:\.\d+)*)\.*(?=[\s(]|$)")  # "Art. 6.2.2.2.." occurs
ANNEX_PARAGRAPH = re.compile(r"A\.(\d+)\.\s*(\d+(?:\.\d+)+)\.?(?=\s|$)")  # "A.10. 3.10.95." in annex 10 of P118-1
ANNEX_NUMBER = "A.{}.{}"
DECIMAL = re.compile(r"(\d+(?:\.\d+)+)\.?(?=\s)|(\d+)\.(?=\s)")
OCR_DECIMAL = re.compile(r"\d+(?:[.,]\d+)+[.,]?(?=\s)")  # "3,7.3.6": a comma read where a dot is printed
PARAGRAPH = re.compile(r"\(\d+\)(?=\s|$)")
LEADING_LABEL = re.compile(
    r"(Art\.\s*\d+(?:\.\d+)*\.?|A\.\d+\.\s*\d+(?:\.\d+)*\.?|\(\d+\)|\d+(?:\.\d+)*\.|[a-z]\)|\d+\)|[-–—•])(?=\s|$)"
)
CAPTION = re.compile(r"(Tabel|TABEL)(ul|UL)?\s*(nr\.?\s*)?[\dIVX]")
KEYWORD_HEADINGS = [  # the first pattern that matches gives the rank
    (re.compile(r"ANEXA\s+(nr\.?\s*)?\d+\.\d"), RANK_SUBANNEX),
    (re.compile(r"(ANEXA|PARTEA|Partea)\b"), RANK_PART),
    (re.compile(r"(CAPITOLUL\b|CAP\.)"), RANK_CHAPTER),
    (re.compile(r"SEC[ȚT]IUNEA\b"), RANK_SECTION),
]
TOP_ANNEX = re.compile(r"ANEXA\s+\d+(?!\.\d)")  # P118-1 files "ANEXA 1" one level under the last chapter
# A heading that is only its keyword and ordinal ("SECȚIUNEA a 2-a", "CAPITOLUL 3"): its title is the next heading.
BARE_HEADING = re.compile(r"(CAPITOLUL|CAP\.|SEC[ȚT]IUNEA|PARTEA|Partea)(\s+(a\s+)?[IVXl\d]+(\s*-\s*a)?)?\.?", re.IGNORECASE)
ORDINAL_FRAGMENT = re.compile(r"(a\s+)?[IVXl\d]+\s*-\s*a", re.IGNORECASE)  # "a 2-a", read apart from its "SECȚIUNEA"
NOT_A_HEADING = re.compile(r"(Figura|Fig\.|Tabel|TABEL|NOT[AĂ]|Not[aă]|unde|[A-Za-z]\)|\d)")
MAX_ABSORBED_HEADINGS = 2
TITLE_MAX_CHARS = 120  # a numbered line this short that is not a sentence is a title, not a paragraph
HEADING_MIN_CHARS = 4
HEADING_MAX_CHARS = 150
NOT_TITLE_END = ".;:,"
LEAD_IN_END = ":"  # a line ending so introduces the list after it and stays with its first item

LONG_TEXT_CHARS = 200  # body text, as opposed to a contents entry or a title-page line
TOC_WINDOW = 60  # blocks after a long text that must be free of contents entries for the body to have started
BACK_GAP = 5  # other blocks tolerated between two entries of a closing list of tables or figures
BACK_MIN_ENTRIES = 10
FRAGMENT_MAX_CHARS = 12  # a figure or rotated table read word by word comes out as a run of items this short
FRAGMENT_MIN_RUN = 5
MIN_TABLE_ROWS = 2  # a table with fewer rows of text is page layout
SEQUENCE_REPORT_MAX = 20


@dataclass(frozen=True)
class Picture:
    """A picture of the document that is a file beside the JSON (pdf_to_md.py / docx_to_md.py with --images)."""
    ref: str  # Docling's name for it: "#/pictures/12"
    uri: str  # the file, relative to the folder of the JSON
    page: int | None  # the PDF page; Word files have none
    width: int
    height: int
    caption: str


@dataclass
class Block:
    """One item of the document in reading order: a line of text, a heading or a table."""
    kind: str
    text: str = ""
    pages: tuple[int, ...] = ()
    level: int = 0  # Docling's level, for a heading
    from_list: bool = False
    in_picture: bool = False
    toc_like: bool = False
    rows: list[list[str]] = field(default_factory=list)  # for a table
    header_rows: int = 0
    caption: str = ""
    cell_words: int = 0  # each cell counted once, however many rows or columns it spans
    is_index: bool = False  # a table Docling recognised as the table of contents
    pictures: list[Picture] = field(default_factory=list)  # the pictures that follow this block in the document

    @property
    def chars(self) -> int:
        if self.kind == BLOCK_TABLE:
            return visible_chars(self.caption) + sum(row_chars(row) for row in self.rows)
        return visible_chars(self.text)


@dataclass
class Mark:
    """What a line is in the structure: a heading (with a rank), or the start of an article or numbered paragraph."""
    heading: bool
    rank: int = 0
    number: str = ""  # "2.3.2.1"
    label: str = ""  # the number as the breadcrumb shows it: "Art. 2.3.2.1"


@dataclass
class Unit:
    """An article or numbered paragraph with everything up to the next one; or the text right under a heading."""
    path: tuple[str, ...]
    blocks: list[Block]
    numbers: list[str] = field(default_factory=list)  # several once small neighbours are merged
    labels: list[str] = field(default_factory=list)


@dataclass
class Line:
    text: str
    pages: tuple[int, ...]
    chars: int  # visible characters of the source, to prove nothing was lost on the way to the chunks
    pictures: tuple[Picture, ...] = ()


@dataclass
class Chunk:
    kind: str
    key: str  # the part of the id after the document: "art-2.3.2.1"
    piece: int
    path: tuple[str, ...]
    numbers: list[str]
    label: str
    text: str
    pages: tuple[int, ...]
    chars: int
    flags: list[str] = field(default_factory=list)
    caption: str = ""  # of a table
    parent: "Chunk | None" = None
    id: str = ""
    pictures: list[Picture] = field(default_factory=list)


def clean(text: str) -> str:
    return WHITESPACE.sub(" ", pipeline_common.normalize_romanian(text)).strip()


def visible_chars(text: str) -> int:
    return len(WHITESPACE.sub("", text))


def word_count(text: str) -> int:
    return len(NOT_WORD.sub(" ", text).split())


def row_chars(row: list[str]) -> int:
    return sum(visible_chars(cell) for cell in row)


def render_row(row: list[str]) -> str:
    return CELL_SEPARATOR.join(row)


def unique(*groups: Iterable[Picture]) -> list[Picture]:
    """The pictures of all the groups without repeats, in order."""
    return list(dict.fromkeys(chain.from_iterable(groups)))


def page_span(pages) -> tuple[int, ...]:
    pages = sorted(set(pages))
    return (pages[0], pages[-1]) if pages else ()


class Sizer:
    """Counts tokens as the embedding model will, and cuts a text that cannot be split at any structure."""

    def __init__(self, tokenizer_name: str, min_tokens: int, max_tokens: int):
        from transformers import AutoTokenizer  # slow to import, and only needed once the arguments are valid

        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)
        self.special_tokens = self.tokenizer.num_special_tokens_to_add()
        self.min_tokens = min_tokens
        self.max_tokens = max_tokens
        self._counts: dict[str, int] = {}

    def count(self, text: str) -> int:
        if text not in self._counts:
            self._counts[text] = len(self.tokenizer.encode(text, add_special_tokens=False, verbose=False))
        return self._counts[text]

    def count_lines(self, lines: list[Line]) -> int:
        return sum(self.count(line.text) for line in lines) + len(lines)  # one token per line break at most

    def budget(self, breadcrumb: str) -> int:
        """Tokens left for the text of a chunk whose embedded text starts with `breadcrumb`."""
        left = self.max_tokens - self.special_tokens - self.count(breadcrumb) - 1
        return max(left, self.max_tokens // MIN_BODY_SHARE)

    def cut(self, text: str, budget: int) -> list[str]:
        """Cut one text that has no structure left to split at. Only white space may go missing between the parts."""
        parts = semchunk.chunkerify(self.count, budget)(text)
        if visible_chars("".join(parts)) != visible_chars(text):
            raise ConversionError("a text lost characters while being cut to size")
        return parts


# ---- reading the Docling document -----------------------------------------------------------------------------------

def item_text(item: TextItem) -> str:
    """The text of an item, with the label of a list item ("a)", "1.1.4.") put back in front: Docling keeps it apart."""
    text = item.text.strip()
    marker = (item.marker or "").strip() if isinstance(item, ListItem) else ""
    return f"{marker} {text}" if marker and not text.startswith(marker) else text


def table_block(item: TableItem, doc: DoclingDocument, in_picture: bool) -> Block:
    rows, header_rows, in_header = [], 0, True
    for row in item.data.grid:
        # A cell merged across columns is in the grid once per column; its text is written in the first only.
        cells = [clean(cell.text) if cell.start_col_offset_idx == column else "" for column, cell in enumerate(row)]
        if not any(cells):
            continue
        in_header = in_header and all(cell.column_header for cell in row if cell.text.strip())
        header_rows += in_header
        rows.append(cells)
    return Block(
        BLOCK_TABLE, pages=tuple(prov.page_no for prov in item.prov), rows=rows, header_rows=header_rows,
        caption=clean(item.caption_text(doc)), cell_words=sum(word_count(cell.text) for cell in item.data.table_cells),
        in_picture=in_picture, is_index=item.label == DocItemLabel.DOCUMENT_INDEX,
    )


def picture_uri(item: PictureItem) -> str | None:
    """Where the picture's file is, relative to the JSON; None for a picture with no file (kept as data in the JSON)."""
    uri = str(item.image.uri) if item.image is not None and item.image.uri is not None else ""
    return uri if uri and not uri.startswith(DATA_URI_PREFIX) else None


def picture_of(item: PictureItem, uri: str, doc: DoclingDocument) -> Picture:
    return Picture(
        item.self_ref, uri, item.prov[0].page_no if item.prov else None,
        int(item.image.size.width), int(item.image.size.height), clean(item.caption_text(doc)),
    )


def flatten(doc: DoclingDocument) -> list[Block]:
    """The body of the document as a flat list in reading order. Three things in Docling's tree are put right:
    the text under a table repeats its cells and is left out; the pieces of an inline group ("Art. 1.1.1. (1)" and
    its sentence) are joined into one line; a caption that belongs to a table goes with the table.
    A picture is not a block: it is attached to the block before it (the text that introduces it; build_units moves
    the pictures of a heading on to the text under it); one before the first block goes with the first; a picture in a
    table cell goes with the table. Text found inside a picture is not an anchor."""
    table_captions = {ref.cref for table in doc.tables for ref in table.captions}
    blocks: list[Block] = []
    table_level = picture_level = inline_level = None  # the tree depth of the table, picture or inline group we are in
    inline: list[Block] = []
    anchor: Block | None = None  # the last block that is not inside a picture: pictures attach to it
    parked: list[Picture] = []  # pictures before the first block of the document
    table_block_now: Block | None = None  # the table we are inside, to hold the pictures in its cells

    def add_block(block: Block, to: list[Block]) -> None:
        nonlocal anchor
        to.append(block)
        if not block.in_picture:
            anchor = block
            block.pictures += parked
            parked.clear()

    def close_inline() -> None:
        nonlocal anchor
        if inline:
            first = inline[0]
            first.text = clean(" ".join(part.text for part in inline))
            first.toc_like = inline[-1].toc_like
            first.pictures = [picture for part in inline for picture in part.pictures]
            blocks.append(first)
            if any(anchor is part for part in inline):
                anchor = first
            inline.clear()


    def place_picture(item: PictureItem, holder: Block | None) -> None:
        uri = picture_uri(item)
        if uri is None:
            return
        picture = picture_of(item, uri, doc)
        if holder is not None:
            holder.pictures.append(picture)
        elif anchor is None:
            parked.append(picture)
        else:
            anchor.pictures.append(picture)

    for item, level in doc.iterate_items(with_groups=True, traverse_pictures=True):
        if table_level is not None and level > table_level:
            if isinstance(item, PictureItem):
                place_picture(item, table_block_now)
            continue
        table_level = None
        if picture_level is not None and level <= picture_level:
            picture_level = None
        if inline_level is not None and level <= inline_level:
            close_inline()
            inline_level = None
        if isinstance(item, TableItem):
            table_level = level
            table_block_now = table_block(item, doc, picture_level is not None)
            add_block(table_block_now, blocks)
        elif isinstance(item, PictureItem):
            picture_level = level
            place_picture(item, None)
        elif isinstance(item, InlineGroup):
            inline_level = level
        elif isinstance(item, TextItem) and item.self_ref not in table_captions and item.text.strip():
            is_heading = item.label in (DocItemLabel.SECTION_HEADER, DocItemLabel.TITLE)
            raw = item_text(item)
            block = Block(
                BLOCK_HEADING if is_heading else BLOCK_TEXT, clean(raw), tuple(prov.page_no for prov in item.prov),
                level=getattr(item, "level", RANK_TOP) if is_heading else 0, from_list=isinstance(item, ListItem),
                in_picture=picture_level is not None, toc_like=bool(TOC_LINE.search(raw)),
            )
            if not block.text:
                continue
            add_block(block, inline if inline_level is not None else blocks)
    close_inline()
    return blocks


# ---- Word's own paragraph labels ------------------------------------------------------------------------------------

def leading_label(text: str) -> str:
    match = LEADING_LABEL.match(text)
    return match.group(1) if match else ""


def strip_label(text: str) -> str:
    return text[len(leading_label(text)):].lstrip()


def match_key(text: str) -> str:
    """The letters and digits of a line without its label: what the JSON and the Word file must agree on."""
    return NOT_WORD.sub("", strip_label(text)).casefold()


def restore_labels(blocks: list[Block], docx: Path) -> int:
    """Put back the labels Word generates ("(2)", "4.3.1.2.") and Docling dropped or renumbered. Each line of the
    JSON is lined up with the same line of docx_to_txt's reading of the Word file, which has the labels as Word shows
    them; only the label is taken over, the text stays the JSON's. Returns the number of lines changed."""
    own = [line for line in map(clean, docx_to_txt.convert(docx).text.splitlines()) if match_key(line)]
    targets = [block for block in blocks if block.kind != BLOCK_TABLE and match_key(block.text)]
    matcher = difflib.SequenceMatcher(None, [match_key(b.text) for b in targets], [match_key(l) for l in own], autojunk=False)
    restored = 0
    for match in matcher.get_matching_blocks():
        for offset in range(match.size):
            block, label = targets[match.a + offset], leading_label(own[match.b + offset])
            if label and leading_label(block.text) != label:
                block.text = f"{label} {strip_label(block.text)}"
                restored += 1
    return restored


# ---- front and back matter ------------------------------------------------------------------------------------------

def is_toc_marker(block: Block) -> bool:
    return block.is_index or block.toc_like or block.text.casefold() == TOC_TITLE


def is_long_text(block: Block) -> bool:
    return block.kind == BLOCK_TEXT and not block.toc_like and len(block.text) >= LONG_TEXT_CHARS


def body_start(blocks: list[Block], first_body_page: int | None) -> int:
    """The index of the first block of the norm's own text: after the title page and the table of contents. The cut
    is by position, never by the look of a single line: real lines end in a number too. `first_body_page`, from the
    config file, settles it for a PDF whose contents entries cannot be told from headings."""
    if first_body_page:
        return next((i for i, block in enumerate(blocks) if block.pages and block.pages[0] >= first_body_page), 0)
    start, last_marker = 0, None
    for index in range(start, len(blocks)):
        if is_toc_marker(blocks[index]):
            last_marker = index
        elif is_long_text(blocks[index]) and not any(map(is_toc_marker, blocks[index + 1:index + 1 + TOC_WINDOW])):
            break
    if last_marker is None:
        return start
    for index in range(last_marker + 1, len(blocks)):  # what follows the contents: stray marks, then the first heading
        block = blocks[index]
        if block.kind == BLOCK_HEADING or is_long_text(block) or DECIMAL.match(block.text) or ARTICLE.match(block.text):
            return index
    return len(blocks)


def back_matter(blocks: list[Block], start: int) -> tuple[int, int]:
    """(first, one past last) of a closing list of tables or figures, whose lines look like contents entries; an
    empty range when the document has none."""
    entries = [index for index in range(start, len(blocks)) if blocks[index].toc_like]
    if not entries or len(blocks) - 1 - entries[-1] > BACK_GAP:
        return len(blocks), len(blocks)
    first = last = entries[-1]
    count = 1
    for index in reversed(entries[:-1]):
        if first - index > BACK_GAP:
            break
        first, count = index, count + 1
    if count < BACK_MIN_ENTRIES:
        return len(blocks), len(blocks)
    while first > start and blocks[first - 1].kind == BLOCK_HEADING:  # "Listă tabele:"
        first -= 1
    return first, last + 1


# ---- structure ------------------------------------------------------------------------------------------------------

def title_like(text: str) -> bool:
    return len(text) <= TITLE_MAX_CHARS and text[-1] not in NOT_TITLE_END


def keyword_rank(block: Block) -> int | None:
    for pattern, rank in KEYWORD_HEADINGS:
        if pattern.match(block.text):
            return rank if block.kind == BLOCK_HEADING or title_like(block.text) else None
    return None


def decimal_number(block: Block) -> str:
    """The number a line starts with ("3.7.4" or "3"), or "". Where Docling calls the line a heading, a comma the OCR
    read for a dot is accepted."""
    text = block.text
    if block.kind == BLOCK_HEADING and OCR_DECIMAL.match(text):
        end = OCR_DECIMAL.match(text).end()
        text = text[:end].replace(",", ".") + text[end:]
    match = DECIMAL.match(text)
    return (match.group(1) or match.group(2)) if match else ""


def plausible_heading(text: str) -> bool:
    """Whether a line Docling labeled a heading reads like one. On the scans it labels "unde:" and "431." too."""
    return (
        HEADING_MIN_CHARS <= len(text) <= HEADING_MAX_CHARS and text[0].isupper() and text[-1] not in NOT_TITLE_END
        and not NOT_A_HEADING.match(text)
    )


def classify(block: Block, profile: str, trusted: bool) -> Mark | None:
    """What `block` is in the structure, or None for ordinary text. `trusted` says Docling's heading levels are real
    (a Word file with styles); otherwise headings are told by keyword and numbering."""
    if block.kind == BLOCK_TABLE or block.in_picture:
        return None
    text = block.text
    if trusted and block.kind == BLOCK_HEADING:
        return Mark(True, RANK_TOP if TOP_ANNEX.match(text) else block.level)
    if profile == PROFILE_ART:
        article, annex = ARTICLE.match(text), ANNEX_PARAGRAPH.match(text)
        if article:
            return Mark(False, number=article.group(1), label=ARTICLE_PREFIX + article.group(1))
        if annex:
            return Mark(False, number=ANNEX_NUMBER.format(*annex.groups()), label=annex.group(0).rstrip("."))
    number = decimal_number(block) if profile == PROFILE_DECIMAL else ""
    depth = number.count(".") + 1
    if trusted:
        return Mark(False, number=number, label=number) if depth > 1 else None
    rank = keyword_rank(block)
    if rank is not None:
        return Mark(True, rank)
    if depth > 1:
        return Mark(title_like(text), RANK_NUMBERED + depth, number, number)
    if number:  # "3. Cerinţe" is a chapter; "3. se verifică ..." is an item of a numbered list
        if block.kind == BLOCK_HEADING or (title_like(text) and not block.from_list and strip_label(text)[:1].isupper()):
            return Mark(True, RANK_NUMBERED + depth, number, number)
    if block.kind == BLOCK_HEADING and plausible_heading(text):
        return Mark(True, RANK_UNNUMBERED)
    return None


def merge_split_headings(blocks: list[Block], profile: str, trusted: bool) -> list[Block]:
    """Join a heading that holds only a keyword and ordinal with the title that follows it as a separate heading:
    "SECȚIUNEA" + "a 2-a", "CAPITOLUL 3" + "ORGANIZAREA ...", and in a Word file "SECŢIUNEA I" + "1.1. Obiect"."""
    merged: list[Block] = []
    absorbed = 0
    for block in blocks:
        previous = merged[-1] if merged else None
        if previous is not None and BLOCK_TABLE not in (previous.kind, block.kind) and not block.in_picture:
            bare = BARE_HEADING.fullmatch(previous.text) is not None
            if trusted:
                joins = bare and previous.kind == block.kind == BLOCK_HEADING and block.level == previous.level
            elif keyword_rank(previous) is None or absorbed == MAX_ABSORBED_HEADINGS:
                joins = False
            elif ORDINAL_FRAGMENT.fullmatch(block.text):
                joins = True
            else:
                mark = classify(block, profile, trusted)
                own_structure = mark is not None and (not mark.heading or mark.rank != RANK_UNNUMBERED)
                joins = bare and block.kind == BLOCK_HEADING and not own_structure
            if joins:
                previous.text = f"{previous.text} {block.text}"
                previous.pages += block.pages
                previous.pictures += block.pictures
                absorbed += 1
                continue
        absorbed = 0
        merged.append(block)
    return merged


def build_units(blocks: list[Block], profile: str, trusted: bool) -> tuple[list[Unit], int]:
    """Cut the body into units and give each the headings above it. Returns the units and the visible characters of
    the headings that went into the paths (a heading with nothing under it becomes a unit itself, so it is not lost).
    The pictures of a line that became a heading go to the next block that is not one and not text inside a picture; if
    another heading comes first, they stay with that text (the figure's own scraps)."""
    units: list[Unit] = []
    stack: list[tuple[Mark, Block]] = []  # the headings we are under, outermost first
    current: Unit | None = None
    empty_heading = False  # the innermost heading has nothing under it yet
    heading_chars = 0
    pending: list[Picture] = []  # pictures of headings, for the next block under them
    last_scrap: Block | None = None  # the last text inside a picture since the pending pictures began waiting

    def settle_with_scrap() -> None:
        """No text came to take the pending pictures before the next heading: they belong to the figure just read."""
        if pending and last_scrap is not None:
            last_scrap.pictures = unique(last_scrap.pictures, pending)
            pending.clear()

    def path() -> tuple[str, ...]:
        return tuple(block.text for _, block in stack)

    def close_empty_heading(next_rank: int) -> None:
        nonlocal heading_chars
        if empty_heading and next_rank <= stack[-1][0].rank:
            mark, block = stack[-1]
            units.append(Unit(path()[:-1], [block], [mark.number] if mark.number else [], [mark.label] if mark.label else []))
            heading_chars -= block.chars
            block.pictures, pending[:] = unique(pending, block.pictures), []  # the pictures of the headings above too

    for block in blocks:
        mark = classify(block, profile, trusted)
        if mark is not None and mark.heading:
            settle_with_scrap()
            close_empty_heading(mark.rank)
            while stack and stack[-1][0].rank >= mark.rank:
                stack.pop()
            stack.append((mark, block))
            heading_chars += block.chars
            pending.extend(block.pictures)
            current, empty_heading = None, True
        elif mark is not None:
            if block.in_picture:
                last_scrap = block
            else:
                block.pictures, pending[:] = unique(pending, block.pictures), []
                last_scrap = None
            current = Unit(path(), [block], [mark.number], [mark.label])
            units.append(current)
            empty_heading = False
        else:
            if current is None:  # text right under a heading: it carries the heading's number, if it has one
                mark = stack[-1][0] if stack else Mark(True)
                current = Unit(path(), [], [mark.number] if mark.number else [], [mark.label] if mark.label else [])
                units.append(current)
            if block.in_picture:
                last_scrap = block
            else:
                block.pictures, pending[:] = unique(pending, block.pictures), []
                last_scrap = None
            current.blocks.append(block)
            empty_heading = False
    settle_with_scrap()
    close_empty_heading(RANK_TOP - 1)
    return units, heading_chars


def number_parts(number: str) -> tuple[str, tuple[int, ...]]:
    """("A.10", (3, 10, 95)) for "A.10.3.10.95": the series a number belongs to and its place in it."""
    parts = number.split(".")
    head = 0 if parts[0].isdigit() else ANNEX_NUMBER.count(".")
    return ".".join(parts[:head]), tuple(int(part) for part in parts[head:])


def follows(before: tuple[int, ...], after: tuple[int, ...]) -> bool:
    """Whether `after` can be the number right after `before`: 3.1.3 is followed by 3.1.4, 3.2, 4 or 3.1.3.1."""
    if after[:len(before)] == before and len(after) > len(before):
        return True
    return any(after[:depth] == before[:depth] and after[depth] == before[depth] + 1 for depth in range(min(len(before), len(after))))


def sequence_problems(units: list[Unit]) -> list[str]:
    """Numbers that do not follow the one before them: a missing article, or a number the OCR misread."""
    problems, last = [], {}
    for unit in units:
        for number in unit.numbers:
            series, place = number_parts(number)
            before = last.get(series)
            if before is not None and place and before[1] and not follows(before[1], place):
                problems.append(f"{before[0]} is followed by {number}")
            last[series] = (number, place)
    return problems


# ---- chunks ---------------------------------------------------------------------------------------------------------

def merge_small_units(units: list[Unit], sizer: Sizer, code: str) -> list[Unit]:
    """Join a numbered unit under the minimum size with the next one under the same heading, while the pair fits."""
    def text_tokens(unit: Unit) -> int:
        return sum(sizer.count(block.text) + 1 for block in unit.blocks if block.kind != BLOCK_TABLE)

    merged: list[Unit] = []
    for unit in units:
        previous = merged[-1] if merged else None
        if (
            previous is not None and previous.numbers and unit.numbers and previous.path == unit.path
            and text_tokens(previous) < sizer.min_tokens
            and text_tokens(previous) + text_tokens(unit) <= sizer.budget(breadcrumb(code, previous.path, unit_label(previous)))
        ):
            previous.blocks += unit.blocks
            previous.numbers += unit.numbers
            previous.labels += unit.labels
        else:
            merged.append(unit)
    return merged


def unit_label(unit: Unit) -> str:
    if len(unit.labels) > 1:
        return f"{unit.labels[0]}{RANGE_SEPARATOR}{unit.numbers[-1]}"
    return unit.labels[0] if unit.labels else ""


def breadcrumb(code: str, path: tuple[str, ...], label: str, caption: str = "") -> str:
    return BREADCRUMB_SEPARATOR.join(part for part in (code, *path, label, caption) if part)


def split_lines(lines: list[Line], budget: int, sizer: Sizer) -> list[list[Line]]:
    """Cut the lines of one unit into pieces of at most `budget` tokens: between paragraphs "(1)", "(2)" where there
    are any, else between lines (never right after a lead-in ending in ":"), else inside a line."""
    if sizer.count_lines(lines) <= budget:
        return [lines]
    starts = [i for i in range(1, len(lines)) if PARAGRAPH.match(lines[i].text)]
    if not starts:
        starts = [i for i in range(1, len(lines)) if not lines[i - 1].text.endswith(LEAD_IN_END)]
    if not starts:  # one line, or a lead-in and its item: nothing left to split at
        pieces = []
        for line in lines:
            parts = sizer.cut(line.text, budget) if sizer.count(line.text) > budget else [line.text]
            pieces += [[Line(part, line.pages, 0 if index else line.chars, () if index else line.pictures)] for index, part in enumerate(parts)]
        return pieces
    groups = [lines[a:b] for a, b in zip([0, *starts], [*starts, len(lines)])]
    pieces: list[list[Line]] = []
    current: list[Line] = []
    for group in groups:
        if current and sizer.count_lines(current + group) <= budget:
            current = current + group
            continue
        if current:
            pieces.append(current)
        *done, current = split_lines(group, budget, sizer)
        pieces += done
    pieces.append(current)
    return pieces


def table_pieces(table: Block, caption: Line | None, budget: int, sizer: Sizer) -> list[tuple[str, int]]:
    """(text, visible source characters) of each chunk of a table: one line per row, cut between rows, with the
    caption and header rows on top of every piece. Header rows too long to repeat are treated as ordinary rows."""
    title = caption.text if caption else table.caption
    title_chars = caption.chars if caption else visible_chars(table.caption)
    header, body = table.rows[:table.header_rows], table.rows[table.header_rows:]
    head = [line for line in (title, *map(render_row, header)) if line]
    if not body or sizer.count(LINE_SEPARATOR.join(head)) > budget // 2:
        head, header, body = ([title] if title else []), [], table.rows
    head_chars = title_chars + sum(map(row_chars, header))
    row_budget = max(budget - sizer.count(LINE_SEPARATOR.join(head)) - 1, budget // MIN_BODY_SHARE)
    groups: list[tuple[list[str], int]] = []  # (lines, visible characters) of each piece, without its head
    for row in body:
        text = render_row(row)
        if sizer.count(text) > row_budget:  # one row larger than a chunk: cut inside it
            groups += [([part], 0 if index else row_chars(row)) for index, part in enumerate(sizer.cut(text, row_budget))]
        elif groups and sizer.count(LINE_SEPARATOR.join([*groups[-1][0], text])) <= row_budget:
            groups[-1] = ([*groups[-1][0], text], groups[-1][1] + row_chars(row))
        else:
            groups.append(([text], row_chars(row)))
    if not groups:
        groups = [([], 0)]
    return [(LINE_SEPARATOR.join([*head, *lines]), chars + (head_chars if index == 0 else 0)) for index, (lines, chars) in enumerate(groups)]


def unit_chunks(unit: Unit, section: int, code: str, sizer: Sizer, scanned: bool) -> list[Chunk]:
    """The chunks of one unit: its text in one or more pieces, then each of its tables, then what a figure or rotated
    table left behind as scraps of text. `section` numbers the units that have no number of their own."""
    label = unit_label(unit)
    key = f"{ID_ARTICLE}-{unit.numbers[0]}" if unit.numbers else f"{ID_SECTION}-{section}"
    lines: list[Line] = []
    tables: list[tuple[Block, Line | None]] = []
    scraps: list[list[Block]] = []
    run: list[Block] = []  # short items in a row; a long enough run is a figure or table read word by word
    carried: list[Picture] = []  # the pictures of a block that gave no line; the next line takes them

    def add_line(text: str, pages: tuple[int, ...], chars: int, pictures: list[Picture]) -> None:
        lines.append(Line(text, pages, chars, (*carried, *pictures)))
        carried.clear()

    def close_run() -> None:
        if len(run) >= FRAGMENT_MIN_RUN:
            scraps.append(list(run))
        else:
            for block in run:
                add_line(block.text, block.pages, block.chars, block.pictures)
        run.clear()

    for index, block in enumerate(unit.blocks):
        before = unit.blocks[index - 1] if index else None
        if block.kind != BLOCK_TABLE and block.in_picture:  # the text Docling found inside a picture
            close_run()
            if before is not None and before.in_picture and before.kind != BLOCK_TABLE:
                scraps[-1].append(block)
            else:
                scraps.append([block])
        elif block.kind == BLOCK_TABLE:
            close_run()
            if sum(1 for row in block.rows if any(row)) < MIN_TABLE_ROWS:
                carried.extend(block.pictures)
                for row in block.rows:
                    add_line(render_row(row), block.pages, row_chars(row), [])
                if block.caption:
                    add_line(block.caption, block.pages, visible_chars(block.caption), [])
            else:
                takes_line = bool(lines) and not block.caption and CAPTION.match(lines[-1].text) is not None
                tables.append((block, lines.pop() if takes_line else None))
        elif scanned and index and len(block.text) <= FRAGMENT_MAX_CHARS and not LEADING_LABEL.match(block.text):
            run.append(block)
        else:
            close_run()
            add_line(block.text, block.pages, block.chars, block.pictures)
    close_run()

    chunks: list[Chunk] = []
    crumb = breadcrumb(code, unit.path, label)
    if lines:
        for piece, part in enumerate(split_lines(lines, sizer.budget(crumb), sizer)):
            chunks.append(Chunk(
                KIND_ARTICLE, key, piece, unit.path, unit.numbers, label, LINE_SEPARATOR.join(line.text for line in part),
                page_span(page for line in part for page in line.pages), sum(line.chars for line in part),
                [FLAG_HAS_TABLE] if tables else [], pictures=unique(*(line.pictures for line in part)),
            ))
    parent = chunks[0] if chunks else None
    for number, (table, caption) in enumerate(tables, start=1):
        title = caption.text if caption else table.caption
        table_crumb = breadcrumb(code, unit.path, label, title)
        for piece, (text, chars) in enumerate(table_pieces(table, caption, sizer.budget(table_crumb), sizer)):
            pages = page_span(table.pages + (caption.pages if caption else ()))
            table_pictures = unique(caption.pictures if caption else (), table.pictures) if piece == 0 else []
            chunks.append(Chunk(
                KIND_TABLE, f"{key}-{ID_TABLE}{number}", piece, unit.path, unit.numbers, label, text, pages, chars,
                caption=title, parent=parent, pictures=table_pictures,
            ))
    # A fragmented chunk is never embedded, so the pictures it holds are also kept where a search can find them.
    home = parent or next((chunk for chunk in chunks if chunk.kind == KIND_TABLE), None)
    for number, scrap in enumerate(scraps, start=1):
        text = " ".join(block.text for block in scrap)
        pages = page_span(page for block in scrap for page in block.pages)
        chars = sum(block.chars for block in scrap)
        scrap_pictures = unique(*(block.pictures for block in scrap))
        chunks.append(Chunk(
            KIND_FRAGMENTED, f"{key}-{ID_FRAGMENT}{number}", 0, unit.path, unit.numbers, label, text, pages, chars,
            parent=parent, pictures=scrap_pictures,
        ))
        if home is not None:
            home.pictures = unique(home.pictures, scrap_pictures)
    if carried and chunks:  # the pictures of a table with no text, and nothing after it in the unit
        chunks[0].pictures = unique(chunks[0].pictures, carried)
    return chunks


def image_record(picture: Picture, image_folder: Path, out_folder: Path) -> dict:
    """A picture as a chunk refers to it: the file's path from the folder the chunks are written to, and what is known
    of it. The path is made with os.path.relpath so that the letters of the folder name stay as Docling wrote them."""
    return {
        "path": Path(os.path.relpath(image_folder / picture.uri, out_folder)).as_posix(), "ref": picture.ref, "page": picture.page,
        "width": picture.width, "height": picture.height, "caption": picture.caption,
    }


def norm_fields(entry: dict) -> dict:
    """What every chunk carries of its norm's config entry: the name, the title, the edition and the status with what
    goes with it. search.py compares them with the config, so that a change there cannot go unnoticed in old chunks."""
    return {
        "doc_code": entry["code"], "doc_title": entry["title"], "edition_year": entry["year"], "status": entry["status"],
        AMENDED_BY_KEY: list(entry.get(AMENDED_BY_KEY) or []), REPEALED_ON_KEY: entry.get(REPEALED_ON_KEY), GAZETTE_KEY: entry.get(GAZETTE_KEY),
    }


def chunk_record(
    chunk: Chunk, entry: dict, doc_key: str, sizer: Sizer, previous: Chunk | None, following: Chunk | None,
    image_folder: Path, out_folder: Path,
) -> dict:
    """The JSON object written for one chunk."""
    crumb = breadcrumb(entry["code"], chunk.path, chunk.label, chunk.caption)
    embed_text = f"{crumb}{LINE_SEPARATOR}{chunk.text}"
    flags = list(chunk.flags)
    if sizer.count(embed_text) + sizer.special_tokens > sizer.max_tokens:
        flags.append(FLAG_OVERSIZE)
    if REPLACEMENT_CHAR in chunk.text:
        flags.append(FLAG_BROKEN_CHARS)
    return {
        "id": chunk.id, "kind": chunk.kind, "doc_key": doc_key, **norm_fields(entry), "heading_path": list(chunk.path),
        "label": chunk.label, "caption": chunk.caption,  # as in the breadcrumb: "Art. 2.3.1", "7.10–7.11", a table's title
        "article": chunk.numbers[0] if chunk.numbers else None, "articles": chunk.numbers,
        "pdf_pages": list(chunk.pages) or None, "text": chunk.text, "embed_text": embed_text,
        "prev_id": previous.id if previous else None, "next_id": following.id if following else None,
        "parent_id": chunk.parent.id if chunk.parent else None, "flags": flags,
        "images": [image_record(picture, image_folder, out_folder) for picture in chunk.pictures],
    }


def assign_ids(chunks: list[Chunk], code: str) -> None:
    """Give every chunk its id: document, unit, piece. A number that occurs twice (an OCR misread, or a real repeat
    in the norm) gets a mark so that ids stay unique."""
    slug = SLUG_BREAK.sub("-", code.lower()).strip("-")
    seen: Counter[str] = Counter()
    name = ""
    for chunk in chunks:
        if chunk.piece == 0:  # the pieces of one text or table follow each other
            seen[chunk.key] += 1
            name = chunk.key if seen[chunk.key] == 1 else f"{chunk.key}{ID_REPEAT_MARK}{seen[chunk.key]}"
        chunk.id = ID_SEPARATOR.join((slug, name, str(chunk.piece)))


# ---- one document ---------------------------------------------------------------------------------------------------

def picture_note(doc: DoclingDocument, blocks: list[Block], body: list[Block], chunks: list[Chunk], missing_files: set[str]) -> str:
    """Where every picture of the document went, for the status line: no picture goes unaccounted for."""
    refs = lambda group: {picture.ref for block in group for picture in block.pictures}  # noqa: E731
    with_file = {item.self_ref for item in doc.pictures if picture_uri(item) is not None}
    embedded = {picture.ref for chunk in chunks if chunk.kind != KIND_FRAGMENTED for picture in chunk.pictures}
    scraps_only = {picture.ref for chunk in chunks for picture in chunk.pictures} - embedded
    cut = refs(blocks) - refs(body)
    lost = with_file - missing_files - embedded - scraps_only - cut
    return (
        f"{len(doc.pictures)} pictures: {len(embedded)} in embedded chunks, {len(scraps_only)} only in fragmented chunks, "
        f"{len(cut)} cut with the contents, {len(lost)} not placed, {len(missing_files)} file missing, "
        f"{len(doc.pictures) - len(with_file)} without a file"
    )


def convert(source: Path, out_path: Path, config: dict, docx_files: dict[str, Path], sizer: Sizer) -> str:
    """Write the chunks of `source` to `out_path`; the returned note counts them and says what was cut or flagged."""
    doc_key = pipeline_common.nfc(source.stem)
    entry = config.get(doc_key)
    if entry is None:
        raise ConversionError(f"no entry for {doc_key!r} in the config file")
    doc = DoclingDocument.load_from_json(source)
    blocks = flatten(doc)
    missing_files = {
        picture.ref for block in blocks for picture in block.pictures if not (source.parent / picture.uri).is_file()
    }
    for block in blocks:  # a picture whose file is gone is not referred to
        block.pictures = [picture for picture in block.pictures if picture.ref not in missing_files]
    notes = []
    docx = docx_files.get(doc_key.casefold())
    if docx is not None:
        restored = restore_labels(blocks, docx)
        own_words = word_count(docx_to_txt.convert(docx).text)
        json_words = sum(word_count(block.text) + word_count(block.caption) + block.cell_words for block in blocks)
        notes.append(f"{restored} labels restored from {docx.name}; words in the Word file {own_words}, in the JSON {json_words}")

    start = body_start(blocks, entry.get(FIRST_BODY_PAGE_KEY))
    back_first, back_end = back_matter(blocks, start)
    body = [block for index, block in enumerate(blocks) if index >= start and not back_first <= index < back_end and not block.is_index]
    cut_chars = sum(block.chars for block in blocks) - sum(block.chars for block in body)
    if not body:
        raise ConversionError("no text left after the table of contents")

    profile = entry["profile"]
    trusted = len({block.level for block in body if block.kind == BLOCK_HEADING}) > 1
    scanned = any(block.pages for block in body)
    unmerged = body
    body = merge_split_headings(body, profile, trusted)
    units, heading_chars = build_units(body, profile, trusted)
    problems = sequence_problems(units)
    units = merge_small_units(units, sizer, entry["code"])

    chunks: list[Chunk] = []
    section = 0
    for unit in units:
        section += not unit.numbers
        chunks += unit_chunks(unit, section, entry["code"], sizer, scanned)
    assign_ids(chunks, entry["code"])

    body_chars, chunk_chars = sum(block.chars for block in body), sum(chunk.chars for chunk in chunks)
    if body_chars != heading_chars + chunk_chars:
        raise ConversionError(f"text lost or doubled: body {body_chars} characters, headings {heading_chars} + chunks {chunk_chars}")

    records = [
        chunk_record(
            chunk, entry, doc_key, sizer, chunks[index - 1] if index else None,
            chunks[index + 1] if index + 1 < len(chunks) else None, source.parent, out_path.parent,
        )
        for index, chunk in enumerate(chunks)
    ]
    out_path.write_text("".join(json.dumps(record, ensure_ascii=False) + LINE_SEPARATOR for record in records), encoding=TEXT_ENCODING)

    for problem in problems[:SEQUENCE_REPORT_MAX]:
        print(f"   sequence: {problem}", flush=True)
    kinds = Counter(record["kind"] for record in records)
    flags = Counter(flag for record in records for flag in record["flags"] if flag != FLAG_HAS_TABLE)
    notes = [
        f"{len(records)} chunks: " + ", ".join(f"{count} {kind}" for kind, count in kinds.items()),
        f"body {body_chars} characters = headings {heading_chars} + chunks {chunk_chars}; contents and lists cut: {cut_chars}",
        f"{len(problems)} numbers out of sequence", *(f"{count} {flag}" for flag, count in flags.items()),
        picture_note(doc, blocks, unmerged, chunks, missing_files), *notes,
    ]
    return "; ".join(notes)


def one_line(value) -> bool:
    """Whether `value` is a text with something in it and on one line."""
    return isinstance(value, str) and bool(value.strip()) and LINE_SEPARATOR not in value and "\r" not in value


def entry_problem(entry) -> str:
    """What is wrong with a config entry, as the end of a sentence about it; empty when nothing is."""
    if not isinstance(entry, dict):
        return "is not an object with keys"
    missing = [key for key in CONFIG_KEYS if key not in entry]
    if missing:
        return f"lacks {', '.join(missing)}"
    unknown = [key for key in entry if key not in CONFIG_KEYS + OPTIONAL_KEYS]
    if unknown:
        return f"has {', '.join(unknown)}, which no script reads; known: {', '.join(CONFIG_KEYS + OPTIONAL_KEYS)}"
    if entry["profile"] not in PROFILES:
        return f"has profile {entry['profile']!r}; known: {', '.join(PROFILES)}"
    if not (one_line(entry["code"]) and one_line(entry["title"])):
        return "needs a code and a title that are one line of text each"
    if not isinstance(entry["year"], int) or isinstance(entry["year"], bool):
        return f"has year {entry['year']!r}, which is not a whole number"
    return status_problem(entry)


def status_problem(entry: dict) -> str:
    """What is wrong with the status of a config entry and with what goes with it, as the end of a sentence about the
    entry; empty when nothing is."""
    status, amended_by, repealed_on, gazette = (entry.get(key) for key in ("status", AMENDED_BY_KEY, REPEALED_ON_KEY, GAZETTE_KEY))
    if status not in STATUSES:
        return f"has status {status!r}; known: {', '.join(STATUSES)}"
    if amended_by is not None and not (isinstance(amended_by, list) and all(one_line(act) for act in amended_by)):
        return f"has {AMENDED_BY_KEY} that is not a list of names, one line each"
    if gazette is not None and not one_line(gazette):
        return f"has a {GAZETTE_KEY} that is not one line of text"
    if (status == STATUS_REPEALED) != (repealed_on is not None):
        return f"needs {REPEALED_ON_KEY} when, and only when, its status is {STATUS_REPEALED!r}"
    if repealed_on is not None:
        try:
            if not (isinstance(repealed_on, str) and ISO_DAY.fullmatch(repealed_on)):
                raise ValueError
            date.fromisoformat(repealed_on)
        except ValueError:
            return f"has {REPEALED_ON_KEY} {repealed_on!r}, which is not a day written as YYYY-MM-DD"
    return ""


def load_config(path: Path) -> dict:
    """The per-document settings, keyed by file name without suffix. Stops when the file or an entry is not usable."""
    try:
        config = json.loads(path.read_text(encoding=TEXT_ENCODING))
    except (OSError, ValueError) as error:
        raise SystemExit(f"cannot read the config file {path}: {error}")
    if not isinstance(config, dict):
        raise SystemExit(f"{path.name}: not an object with one entry per document")
    for name, entry in config.items():
        problem = entry_problem(entry)
        if problem:
            raise SystemExit(f"{path.name}: {name!r} {problem}")
    return {pipeline_common.nfc(name): entry for name, entry in config.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    pipeline_common.add_dir_arguments(parser)
    parser.add_argument("--docx-dir", type=Path, help="folder with the original .docx files, to restore Word's paragraph labels")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="per-document settings (default: %(default)s)")
    parser.add_argument("--tokenizer", default=DEFAULT_TOKENIZER, help="Hugging Face name of the embedding model's tokenizer (default: %(default)s)")
    parser.add_argument("--min-tokens", type=int, default=DEFAULT_MIN_TOKENS, help="a smaller article is joined with the next (default: %(default)s)")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="a larger chunk is split; the breadcrumb counts (default: %(default)s)")
    args = parser.parse_args()
    if not 0 <= args.min_tokens < args.max_tokens:
        parser.error("--min-tokens must be smaller than --max-tokens")

    config = load_config(args.config)
    files = pipeline_common.collect_files(args.input_dir, JSON_SUFFIX)
    docx_files = {}
    if args.docx_dir is not None:
        for docx in pipeline_common.collect_files(args.docx_dir, DOCX_SUFFIX):
            docx_files[pipeline_common.nfc(docx.stem.rstrip(UNSAFE_NAME_TAIL)).casefold()] = docx
    sizer = Sizer(args.tokenizer, args.min_tokens, args.max_tokens)
    pipeline_common.run_batch(
        files, args.output_dir, JSONL_SUFFIX,
        lambda source, out_path: convert(source, out_path, config, docx_files, sizer), args.skip_existing,
    )


if __name__ == "__main__":
    main()
