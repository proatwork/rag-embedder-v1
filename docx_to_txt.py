"""Convert a folder of Word files to plain text, keeping all of the text (standard library only): one .txt per .docx.

Every .docx directly inside --input-dir becomes --output-dir/<name>.txt.

Usage (any Python 3.10+; the virtual environment's interpreter works too):
    venvs/bin/python docx_to_txt.py --input-dir docs/docx --output-dir OUT
    venvs/bin/python docx_to_txt.py --input-dir IN --output-dir OUT --skip-existing   # resume

Kept: body paragraphs in order; list labels ("a)", "1.1.", "(2)", "-") resolved from
numbering.xml, including numbering inherited from paragraph styles; tables, one row per
line with cells joined by " | "; Symbol-font characters (>=, <=, +-, ...) and Word equations
(OMML) as text.
Marked in the text and counted in the status line: embedded objects (old-style equations), as
"[object omitted]"; symbols from fonts not in SYMBOL_FONT_CHARS, as "[Font:code]".
Dropped and counted: images/drawings. Dropped silently: headers, footers, footnotes.
Not modelled: lvlRestart and full per-level redefinitions inside <w:lvlOverride>.
A failing file is reported and skipped; the exit status is non-zero if any failed.
"""

import argparse
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import NamedTuple

import pipeline_common
from pipeline_common import DOCX_SUFFIX, TEXT_SUFFIX

TEXT_ENCODING = "utf-8"
CELL_SEPARATOR = " | "
BULLET_LABEL = "-"
NO_BREAK_SPACE = " "
OBJECT_PLACEHOLDER = "[object omitted]"
BLANK_LINES = re.compile(r"\n{3,}")
WHITESPACE = re.compile(r"\s+")
LEVEL_REFERENCE = re.compile(r"%(\d)")

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
MARKUP_COMPAT_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
W, M, MC = f"{{{WORD_NS}}}", f"{{{MATH_NS}}}", f"{{{MARKUP_COMPAT_NS}}}"
ATTR_VAL = W + "val"
DOCUMENT_PART = "word/document.xml"
NUMBERING_PART = "word/numbering.xml"
STYLES_PART = "word/styles.xml"
DEFAULT_START = 1
DEFAULT_LEVEL = 0
NO_LIST_ID = "0"
MAX_STYLE_CHAIN = 50  # guards against a basedOn cycle
SYMBOL_CODE_MASK = 0xFF  # <w:sym w:char="F0B3"> is private-use 0xF000 + the Symbol font code 0xB3
SYMBOL_FONT_CHARS = {
    "Symbol": {
        0xB3: "≥", 0xA3: "≤", 0x3C: "<", 0x3E: ">", 0xB1: "±", 0xB0: "°", 0xB4: "×", 0xB9: "≠",
        0xBB: "≈", 0xAB: "↔", 0xAE: "→", 0xAC: "←", 0xB5: "µ", 0x70: "π", 0xD6: "√",
    }
}

ROMAN_NUMERALS = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
                  (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]
LETTER_COUNT = 26


def to_letters(n: int) -> str:
    """1 -> a, 26 -> z, 27 -> aa (Word's lowerLetter)."""
    letters = ""
    while n > 0:
        n, remainder = divmod(n - 1, LETTER_COUNT)
        letters = chr(ord("a") + remainder) + letters
    return letters


def to_roman(n: int) -> str:
    numeral = ""
    for value, symbol in ROMAN_NUMERALS:
        while n >= value:
            numeral += symbol
            n -= value
    return numeral


NUMBER_FORMATTERS = {
    "decimal": str,
    "lowerLetter": to_letters,
    "upperLetter": lambda n: to_letters(n).upper(),
    "lowerRoman": to_roman,
    "upperRoman": lambda n: to_roman(n).upper(),
}


def val_of(parent: ET.Element | None, tag: str) -> str | None:
    """The w:val of parent's child `tag` (e.g. pStyle, numId), or None."""
    child = parent.find(W + tag) if parent is not None else None
    return child.get(ATTR_VAL) if child is not None else None


class Level(NamedTuple):
    fmt: str
    template: str
    start: int
    style: str | None  # paragraph style this list level is linked to


class Numbering:
    """Resolves list labels the way Word does: from the paragraph or its style, with one
    counter per list definition (abstractNum) and level, and startOverride restarts."""

    def __init__(self, numbering_xml: bytes | None, styles_xml: bytes | None):
        self.levels: dict[str, dict[int, Level]] = {}  # abstractNumId -> ilvl -> Level
        self.abstract_of: dict[str, str] = {}  # numId -> abstractNumId
        self.start_overrides: dict[str, dict[int, int]] = {}  # numId -> ilvl -> start
        self.styles: dict[str, tuple[str | None, str | None, str | None]] = {}  # styleId -> basedOn, numId, ilvl
        self.counters: dict[str, dict[int, int]] = {}  # abstractNumId -> ilvl -> current number
        self.started: set[str] = set()  # numIds whose startOverrides have been applied
        if numbering_xml:
            self._parse_numbering(ET.fromstring(numbering_xml))
        if styles_xml:
            self._parse_styles(ET.fromstring(styles_xml))

    def _parse_numbering(self, root: ET.Element) -> None:
        for abstract in root.iter(W + "abstractNum"):
            levels = {}
            for lvl in abstract.findall(W + "lvl"):
                start = val_of(lvl, "start")
                levels[int(lvl.get(W + "ilvl"))] = Level(
                    val_of(lvl, "numFmt") or "decimal",
                    val_of(lvl, "lvlText") or "",
                    int(start) if start is not None else DEFAULT_START,
                    val_of(lvl, "pStyle"),
                )
            self.levels[abstract.get(W + "abstractNumId")] = levels
        for num in root.iter(W + "num"):
            num_id = num.get(W + "numId")
            self.abstract_of[num_id] = val_of(num, "abstractNumId")
            for override in num.findall(W + "lvlOverride"):
                start = val_of(override, "startOverride")
                if start is not None:
                    self.start_overrides.setdefault(num_id, {})[int(override.get(W + "ilvl"))] = int(start)

    def _parse_styles(self, root: ET.Element) -> None:
        for style in root.iter(W + "style"):
            num_pr = style.find(f"{W}pPr/{W}numPr")
            self.styles[style.get(W + "styleId")] = (
                val_of(style, "basedOn"), val_of(num_pr, "numId"), val_of(num_pr, "ilvl"),
            )

    def _from_style(self, style_id: str | None) -> tuple[str | None, int | None, list[str]]:
        """numId and ilvl a paragraph style gives (own or inherited), plus the style chain."""
        num_id = level = None
        chain: list[str] = []
        while style_id in self.styles and len(chain) < MAX_STYLE_CHAIN:
            chain.append(style_id)
            based_on, style_num, style_level = self.styles[style_id]
            num_id = num_id if num_id is not None else style_num
            level = level if level is not None else (int(style_level) if style_level is not None else None)
            style_id = based_on
        return num_id, level, chain

    def resolve(self, p: ET.Element) -> tuple[str, int] | None:
        """(numId, ilvl) of a numbered paragraph, or None."""
        p_pr = p.find(W + "pPr")
        if p_pr is None:
            return None
        num_id, level, chain = self._from_style(val_of(p_pr, "pStyle"))
        direct = p_pr.find(W + "numPr")
        if direct is not None:
            num_id = val_of(direct, "numId") or num_id
            direct_level = val_of(direct, "ilvl")
            level = int(direct_level) if direct_level is not None else level
        if num_id is None or num_id == NO_LIST_ID:
            return None
        if level is None:  # the list level may be linked to the style from numbering.xml
            levels = self.levels.get(self.abstract_of.get(num_id, ""), {})
            level = next((ilvl for ilvl, lvl in levels.items() if lvl.style in chain), DEFAULT_LEVEL)
        return num_id, level

    def label(self, num_id: str, level: int) -> str:
        abstract = self.abstract_of.get(num_id, "")
        levels = self.levels.get(abstract, {})
        if level not in levels:
            return ""
        counters = self.counters.setdefault(abstract, {})
        if num_id not in self.started:
            self.started.add(num_id)
            for ilvl, start in self.start_overrides.get(num_id, {}).items():
                counters[ilvl] = start - 1
        counters[level] = counters.get(level, levels[level].start - 1) + 1
        for deeper in [lvl for lvl in counters if lvl > level]:
            del counters[deeper]  # a new item at this level restarts the deeper levels
        for skipped in range(level):  # like Word: levels skipped on the way down count as already started
            counters.setdefault(skipped, levels[skipped].start if skipped in levels else DEFAULT_START)

        fmt, template, _, _ = levels[level]
        if fmt == "bullet":
            return BULLET_LABEL
        if fmt == "none":
            return ""

        def substitute(match: re.Match) -> str:
            ref = int(match.group(1)) - 1
            ref_level = levels.get(ref, levels[level])
            return NUMBER_FORMATTERS.get(ref_level.fmt, str)(counters.get(ref, ref_level.start))

        return LEVEL_REFERENCE.sub(substitute, template)


class Converter:
    def __init__(self, numbering: Numbering):
        self.numbering = numbering
        self.unmapped_symbols = 0
        self.objects = 0

    def _symbol(self, node: ET.Element) -> str:
        font, code = node.get(W + "font", ""), node.get(W + "char", "")
        mapped = SYMBOL_FONT_CHARS.get(font, {}).get(int(code, 16) & SYMBOL_CODE_MASK) if code else None
        if mapped is None:
            self.unmapped_symbols += 1
            return f"[{font}:{code}]"
        return mapped

    def paragraph_text(self, p: ET.Element) -> str:
        parts = []
        for node in p.iter():
            if node.tag in (W + "t", M + "t"):
                parts.append(node.text or "")
            elif node.tag == W + "tab":
                parts.append("\t")
            elif node.tag in (W + "br", W + "cr"):
                parts.append("\n")
            elif node.tag == W + "noBreakHyphen":
                parts.append("-")
            elif node.tag == W + "sym":
                parts.append(self._symbol(node))
            elif node.tag == W + "object":
                self.objects += 1
                parts.append(OBJECT_PLACEHOLDER)
        return "".join(parts).replace(NO_BREAK_SPACE, " ")

    def paragraph(self, p: ET.Element) -> str:
        text = self.paragraph_text(p).strip()
        numbered = self.numbering.resolve(p)
        if numbered is None:
            return text
        label = self.numbering.label(*numbered)  # empty numbered paragraphs still consume a number in Word
        return f"{label} {text}" if label and text else text

    def table(self, tbl: ET.Element) -> list[str]:
        rows = []
        for tr in tbl.findall(W + "tr"):
            cells = []
            for tc in tr.findall(W + "tc"):
                cell = " ".join(t for t in (self.paragraph(p) for p in tc.iter(W + "p")) if t)
                cells.append(WHITESPACE.sub(" ", cell))
            if any(cells):
                rows.append(CELL_SEPARATOR.join(cells))
        return rows

    def blocks(self, parent: ET.Element, lines: list[str]) -> None:
        for child in parent:
            if child.tag == W + "p":
                text = self.paragraph(child)
                if text:
                    lines.append(text)
            elif child.tag == W + "tbl":
                rows = self.table(child)
                if rows:
                    lines.extend(["", *rows, ""])
            elif child.tag == W + "sdt":
                content = child.find(W + "sdtContent")
                if content is not None:
                    self.blocks(content, lines)


class Conversion(NamedTuple):
    text: str
    images: int
    objects: int
    unmapped_symbols: int


def count_images(document: ET.Element) -> int:
    """Drawings plus legacy pictures; a legacy picture inside mc:Fallback duplicates a drawing."""
    fallback_pictures = {id(pic) for fallback in document.iter(MC + "Fallback") for pic in fallback.iter(W + "pict")}
    pictures = sum(1 for pic in document.iter(W + "pict") if id(pic) not in fallback_pictures)
    return sum(1 for _ in document.iter(W + "drawing")) + pictures


def convert(docx_path: Path) -> Conversion:
    with zipfile.ZipFile(docx_path) as archive:
        names = set(archive.namelist())
        document = ET.fromstring(archive.read(DOCUMENT_PART))
        numbering = Numbering(
            archive.read(NUMBERING_PART) if NUMBERING_PART in names else None,
            archive.read(STYLES_PART) if STYLES_PART in names else None,
        )
    converter = Converter(numbering)
    lines: list[str] = []
    converter.blocks(document.find(W + "body"), lines)
    text = BLANK_LINES.sub("\n\n", "\n".join(lines)).strip() + "\n"
    return Conversion(text, count_images(document), converter.objects, converter.unmapped_symbols)


def convert_file(docx: Path, out_path: Path) -> str:
    """Write the text of `docx` to `out_path`; the returned note says how much there is and what was left out."""
    result = convert(docx)
    if not result.text.strip():
        raise pipeline_common.ConversionError("no text in the document")
    out_path.write_text(result.text, encoding=TEXT_ENCODING)
    return (f"{len(result.text.splitlines())} lines; omitted: {result.images} images, {result.objects} objects; "
            f"{result.unmapped_symbols} unmapped symbols")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    pipeline_common.add_dir_arguments(parser)
    args = parser.parse_args()

    files = pipeline_common.collect_files(args.input_dir, DOCX_SUFFIX)
    pipeline_common.run_batch(files, args.output_dir, TEXT_SUFFIX, convert_file, args.skip_existing)


if __name__ == "__main__":
    main()
