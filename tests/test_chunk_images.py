"""Tests of the picture references in the chunks (json_to_chunks.py) and their display (search.py).

Run from the project folder with the virtual environment's interpreter:
    venvs/bin/python -m unittest discover -s tests -v
No server and no tokenizer download are needed: sizes are counted in words.
"""

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

from docling_core.types.doc import DocItemLabel, DoclingDocument, ImageRef, Size
from docling_core.types.doc.document import TableCell, TableData

import json_to_chunks as jc
import search
from json_to_chunks import (
    BLOCK_HEADING, BLOCK_TABLE, BLOCK_TEXT, KIND_ARTICLE, KIND_FRAGMENTED, KIND_TABLE, Block, Picture, Sizer, Unit,
)

IMAGES_DIR = "doc_images"
CODE = "T 1"
BUDGET_WORDS = 40


class WordSizer(Sizer):
    """A Sizer that counts words, so that no tokenizer is needed."""

    def __init__(self, max_tokens: int = BUDGET_WORDS + 5, min_tokens: int = 3):
        self.special_tokens, self.min_tokens, self.max_tokens, self._counts = 0, min_tokens, max_tokens, {}

    def count(self, text: str) -> int:
        return len(text.split())


def picture(number: int, page: int | None = None, caption: str = "") -> Picture:
    return Picture(f"#/pictures/{number}", f"{IMAGES_DIR}/image_{number}.png", page, 100, 50, caption)


def image_ref(number: int) -> ImageRef:
    return ImageRef(mimetype="image/png", dpi=72, size=Size(width=100, height=50), uri=Path(f"{IMAGES_DIR}/image_{number}.png"))


def text_block(text: str, *pictures: Picture, in_picture: bool = False) -> Block:
    block = Block(BLOCK_TEXT, text, pages=(1,), in_picture=in_picture)
    block.pictures = list(pictures)
    return block


def table_block(*pictures: Picture, caption: str = "", rows: int = 3) -> Block:
    block = Block(BLOCK_TABLE, pages=(1,), rows=[[f"a{i}", f"b{i}"] for i in range(rows)], caption=caption)
    block.pictures = list(pictures)
    return block


def refs(chunk: jc.Chunk) -> list[str]:
    return [picture.ref for picture in chunk.pictures]


class FlattenTest(unittest.TestCase):
    def test_picture_goes_with_the_text_before_it(self):
        doc = DoclingDocument(name="t")
        doc.add_heading("1. Titlu", level=1)
        doc.add_text(DocItemLabel.TEXT, "1.1. Despre figura de mai jos.")
        doc.add_picture(image=image_ref(0))
        doc.add_text(DocItemLabel.TEXT, "1.2. Alt articol.")
        blocks = jc.flatten(doc)
        self.assertEqual([block.pictures for block in blocks], [[], [Picture("#/pictures/0", f"{IMAGES_DIR}/image_0.png", None, 100, 50, "")], []])  # the ref counts from 0 in each document

    def test_picture_after_a_heading_is_on_the_heading_for_build_units_to_move(self):
        doc = DoclingDocument(name="t")
        doc.add_text(DocItemLabel.TEXT, "Text din secțiunea veche.")
        doc.add_heading("2. Secțiune nouă", level=1)
        doc.add_picture(image=image_ref(1))
        doc.add_text(DocItemLabel.TEXT, "Text din secțiunea nouă.")
        by_text = {block.text: [p.uri for p in block.pictures] for block in jc.flatten(doc)}
        self.assertEqual(by_text, {"Text din secțiunea veche.": [], "2. Secțiune nouă": [f"{IMAGES_DIR}/image_1.png"], "Text din secțiunea nouă.": []})

    def test_picture_before_the_first_block_goes_with_the_first_block(self):
        doc = DoclingDocument(name="t")
        doc.add_picture(image=image_ref(0))
        doc.add_heading("1. Titlu", level=1)
        self.assertEqual([[p.uri for p in block.pictures] for block in jc.flatten(doc)], [[f"{IMAGES_DIR}/image_0.png"]])

    def test_picture_in_a_table_cell_goes_with_the_table(self):
        doc = DoclingDocument(name="t")
        doc.add_text(DocItemLabel.TEXT, "Text înainte de tabel.")
        cells = [TableCell(text=f"c{i}", start_row_offset_idx=i, end_row_offset_idx=i + 1, start_col_offset_idx=0, end_col_offset_idx=1) for i in range(2)]
        table = doc.add_table(data=TableData(num_rows=2, num_cols=1, table_cells=cells))
        doc.add_picture(image=image_ref(2), parent=table)
        blocks = jc.flatten(doc)
        self.assertEqual([[p.uri for p in block.pictures] for block in blocks], [[], [f"{IMAGES_DIR}/image_2.png"]])

    def test_picture_with_no_file_is_not_attached(self):
        doc = DoclingDocument(name="t")
        doc.add_text(DocItemLabel.TEXT, "Text.")
        doc.add_picture(image=ImageRef(mimetype="image/png", dpi=72, size=Size(width=1, height=1), uri="data:image/png;base64,AAAA"))
        doc.add_picture()
        self.assertEqual([block.pictures for block in jc.flatten(doc)], [[]])


class StructureTest(unittest.TestCase):
    def test_pictures_of_a_line_taken_for_a_heading_go_to_the_next_block(self):
        title = text_block("3.6.3.4 Clasificarea detectoarelor de căldură este dată în tabelul 3.1", picture(0))
        table = table_block(caption="Tabelul 3.1")
        units, _ = jc.build_units([title, table], jc.PROFILE_DECIMAL, trusted=False)
        found = [p.ref for unit in units for block in unit.blocks for p in block.pictures]
        self.assertEqual(found, ["#/pictures/0"])

    def test_pictures_of_a_heading_go_to_the_text_under_it_not_to_the_text_before(self):
        before = text_block("1.1 Text din secțiunea veche.")
        heading = Block(BLOCK_HEADING, "CAPITOLUL 2", level=1)
        heading.pictures = [picture(1)]
        after = text_block("2.1 Text din secțiunea nouă.")
        units, _ = jc.build_units([before, heading, after], jc.PROFILE_DECIMAL, trusted=True)
        self.assertEqual([[p.ref for block in unit.blocks for p in block.pictures] for unit in units], [[], ["#/pictures/1"]])

    def test_pictures_of_a_heading_skip_the_text_inside_their_own_figure(self):
        heading = Block(BLOCK_HEADING, "CAPITOLUL 2", level=1)
        heading.pictures = [picture(1)]
        scrap = text_block("17 000", in_picture=True)
        after = text_block("2.1 Textul despre figură.")
        units, _ = jc.build_units([heading, scrap, after], jc.PROFILE_DECIMAL, trusted=True)
        self.assertEqual([[p.ref for block in unit.blocks for p in block.pictures] for unit in units], [[], ["#/pictures/1"]])

    def test_pictures_stay_with_their_scraps_when_another_heading_comes_first(self):
        heading = Block(BLOCK_HEADING, "CAPITOLUL 2", level=1)
        heading.pictures = [picture(1)]
        scrap = text_block("harta", in_picture=True)
        next_heading = Block(BLOCK_HEADING, "CAPITOLUL 3", level=1)
        after = text_block("3.1 Alt capitol.")
        units, _ = jc.build_units([heading, scrap, next_heading, after], jc.PROFILE_DECIMAL, trusted=True)
        self.assertEqual([[p.ref for block in unit.blocks for p in block.pictures] for unit in units], [["#/pictures/1"], []])
        self.assertTrue(units[0].blocks[0].in_picture)

    def test_pictures_of_a_heading_reach_an_empty_heading_under_it_at_the_end(self):
        outer = Block(BLOCK_HEADING, "CAPITOLUL 2", level=1)
        outer.pictures = [picture(1)]
        inner = Block(BLOCK_HEADING, "SECȚIUNEA 1", level=2)
        units, _ = jc.build_units([outer, inner], jc.PROFILE_DECIMAL, trusted=True)
        self.assertEqual([p.ref for unit in units for block in unit.blocks for p in block.pictures], ["#/pictures/1"])

    def test_pictures_of_a_heading_that_is_joined_with_the_next_one_are_kept(self):
        keyword = Block(BLOCK_HEADING, "CAPITOLUL 3", level=1)
        keyword.pictures = [picture(0)]
        title = Block(BLOCK_HEADING, "ORGANIZAREA", level=1)
        title.pictures = [picture(1)]
        merged = jc.merge_split_headings([keyword, title], jc.PROFILE_DECIMAL, trusted=True)
        self.assertEqual(len(merged), 1)
        self.assertEqual([p.ref for p in merged[0].pictures], ["#/pictures/0", "#/pictures/1"])

    def test_a_heading_with_nothing_under_it_keeps_its_own_pictures_once(self):
        heading = Block(BLOCK_HEADING, "CAPITOLUL 1", level=1)
        heading.pictures = [picture(0)]
        units, _ = jc.build_units([heading], jc.PROFILE_DECIMAL, trusted=True)
        found = [p.ref for unit in units for block in unit.blocks for p in block.pictures]
        self.assertEqual(found, ["#/pictures/0"])


class UnitChunksTest(unittest.TestCase):
    def chunks(self, *blocks: Block, scanned: bool = False) -> list[jc.Chunk]:
        return jc.unit_chunks(Unit(("CAP 1",), list(blocks), ["1.1"], ["1.1"]), 1, CODE, WordSizer(), scanned)

    def test_picture_stays_with_its_paragraph_when_a_long_article_is_split(self):
        paragraphs = [text_block(f"({n}) " + "cuvânt " * 25, picture(n)) for n in range(1, 4)]
        chunks = self.chunks(*paragraphs)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(sorted(ref for chunk in chunks for ref in refs(chunk)), ["#/pictures/1", "#/pictures/2", "#/pictures/3"])
        for chunk in chunks:
            for ref in refs(chunk):
                self.assertIn(f"({ref[-1]})", chunk.text)

    def test_picture_of_a_line_cut_inside_goes_to_its_first_part(self):
        chunks = self.chunks(text_block("cuvânt " * 120, picture(0)))
        self.assertGreater(len(chunks), 1)
        self.assertEqual([refs(chunk) for chunk in chunks], [["#/pictures/0"], *[[] for _ in chunks[1:]]])

    def test_table_takes_the_pictures_of_the_table_and_of_its_caption_line(self):
        chunks = self.chunks(text_block("Tabel 3 Valori", picture(0)), table_block(picture(1)))
        table = [chunk for chunk in chunks if chunk.kind == KIND_TABLE]
        self.assertEqual(len(table), 1)
        self.assertEqual(refs(table[0]), ["#/pictures/0", "#/pictures/1"])

    def test_pictures_of_scraps_are_also_on_the_first_article_chunk(self):
        scraps = [text_block(word, in_picture=True) for word in "a b c d e f".split()]
        scraps[0].pictures = [picture(7)]
        chunks = self.chunks(text_block("1.1 Text normal."), *scraps)
        fragmented = [chunk for chunk in chunks if chunk.kind == KIND_FRAGMENTED]
        self.assertEqual([refs(chunk) for chunk in fragmented], [["#/pictures/7"]])
        self.assertEqual(refs(chunks[0]), ["#/pictures/7"])
        self.assertEqual(chunks[0].kind, KIND_ARTICLE)

    def test_pictures_of_a_table_with_no_text_are_not_lost(self):
        tiny = Block(BLOCK_TABLE, pages=(1,), rows=[], caption="")
        tiny.pictures = [picture(3)]
        chunks = self.chunks(text_block("1.1 Text."), tiny)
        self.assertEqual([ref for chunk in chunks for ref in refs(chunk)], ["#/pictures/3"])

    def test_no_pictures_gives_empty_lists(self):
        self.assertEqual([refs(chunk) for chunk in self.chunks(text_block("1.1 Text."))], [[]])


class RecordTest(unittest.TestCase):
    def test_image_path_is_relative_to_the_chunk_folder_and_keeps_the_letters(self):
        folder = Path("/p/json")
        image = jc.image_record(Picture("#/pictures/1", "construcțiilor_images/a.png", 4, 10, 20, "Figura 1"), folder, Path("/p/chunks"))
        self.assertEqual(image["path"], "../json/construcțiilor_images/a.png")
        self.assertEqual((image["ref"], image["page"], image["width"], image["height"], image["caption"]), ("#/pictures/1", 4, 10, 20, "Figura 1"))

    def test_a_chunk_carries_what_its_citation_needs(self):
        entry = {"code": CODE, "title": "Titlu", "year": 2013, "status": jc.STATUS_REPEALED, jc.REPEALED_ON_KEY: "2026-08-25", jc.AMENDED_BY_KEY: ["Ordinul 1"], jc.GAZETTE_KEY: "M. Of. 1"}
        table = jc.Chunk(jc.KIND_TABLE, "art-7.10-tab1", 0, ("Cap. 7",), ["7.10", "7.11"], "7.10–7.11", "a | b", (36, 37), 3, caption="TABEL 7.1")
        table.id = "t-1:art-7.10-tab1:0"
        record = jc.chunk_record(table, entry, "doc", WordSizer(), None, None, Path("/p/json"), Path("/p/chunks"))
        self.assertEqual((record["label"], record["caption"], record["pdf_pages"]), ("7.10–7.11", "TABEL 7.1", [36, 37]))
        self.assertEqual((record["status"], record[jc.REPEALED_ON_KEY], record[jc.AMENDED_BY_KEY], record[jc.GAZETTE_KEY]), (jc.STATUS_REPEALED, "2026-08-25", ["Ordinul 1"], "M. Of. 1"))
        self.assertEqual(record["embed_text"], f"{CODE} > Cap. 7 > 7.10–7.11 > TABEL 7.1\na | b")  # nothing of the citation is embedded
        self.assertEqual(search.citation(record), f"{CODE}, pct. 7.10–7.11, TABEL 7.1 — Titlu (ed. 2013, M. Of. 1, PDF p. 36–37)")
        self.assertEqual(search.status_words(record), "abrogat din 25.08.2026; text original, modificat ulterior prin: Ordinul 1")

    def test_a_chunk_of_a_norm_with_nothing_optional_has_empty_fields(self):
        entry = {"code": CODE, "title": "Titlu", "year": 2025, "status": jc.STATUS_IN_FORCE}
        article = jc.Chunk(KIND_ARTICLE, "art-1", 0, (), ["1"], "Art. 1", "t", (), 1)
        record = jc.chunk_record(article, entry, "doc", WordSizer(), None, None, Path("/p/json"), Path("/p/chunks"))
        self.assertEqual((record[jc.AMENDED_BY_KEY], record[jc.REPEALED_ON_KEY], record[jc.GAZETTE_KEY], record["pdf_pages"]), ([], None, None, None))
        self.assertEqual(search.citation(record), f"{CODE}, Art. 1 — Titlu (ed. 2025)")

    def test_note_adds_up_to_the_pictures_of_the_document(self):
        doc = DoclingDocument(name="t")
        for number in range(5):
            doc.add_picture(image=image_ref(number))
        doc.add_picture(image=ImageRef(mimetype="image/png", dpi=72, size=Size(width=1, height=1), uri="data:image/png;base64,AAAA"))
        article = jc.Chunk(KIND_ARTICLE, "art-1", 0, (), ["1"], "", "t", (), 0, pictures=[picture(0)])
        scrap = jc.Chunk(KIND_FRAGMENTED, "art-1-frag1", 0, (), ["1"], "", "t", (), 0, pictures=[picture(1)])
        in_body, cut = text_block("x", picture(0), picture(1), picture(3)), text_block("y", picture(2))
        note = jc.picture_note(doc, [in_body, cut], [in_body], [article, scrap], missing_files={"#/pictures/4"})
        self.assertEqual(
            note,
            "6 pictures: 1 in embedded chunks, 1 only in fragmented chunks, 1 cut with the contents, 1 not placed, 1 file missing, 1 without a file",
        )


class SearchDisplayTest(unittest.TestCase):
    def record(self, images: list[dict]) -> dict:
        return {
            "id": "t-1:art-1:0", "doc_code": CODE, "edition_year": 2025, "status": "in_force", "kind": "article", "flags": [],
            "embed_text": "T 1 > Art. 1\ntext", "text": "text", "images": images,
        }

    def test_chunk_without_images_shows_none(self):
        self.assertNotIn("image:", search.describe(self.record([])))

    def test_a_result_shows_its_citation_and_the_status_in_words(self):
        shown = search.describe(self.record([])).split("\n")
        self.assertEqual(shown[0], "t-1:art-1:0  article")
        self.assertEqual(shown[1].strip(), f"{CODE} (ed. 2025)  [în vigoare]")

    def test_images_are_listed_under_the_chunk(self):
        image = {"path": "../json/a_images/x.png", "ref": "#/pictures/1", "page": 4, "width": 1, "height": 1, "caption": "Figura 2.1  Indicator"}
        shown = search.describe(self.record([image]))
        self.assertIn("image: ../json/a_images/x.png  p. 4  Figura 2.1 Indicator", shown)

    def test_load_chunks_makes_the_path_usable_from_where_the_script_runs(self):
        with TemporaryDirectory() as folder:
            chunks_dir = Path(folder) / "chunks"
            chunks_dir.mkdir()
            record = self.record([{"path": "../json/a_images/x.png", "ref": "#/pictures/1", "page": None, "width": 1, "height": 1, "caption": ""}])
            (chunks_dir / "doc.jsonl").write_text(__import__("json").dumps(record) + "\n", encoding="utf-8")
            loaded = search.load_chunks(chunks_dir)
            image = next(iter(loaded.values()))[0]["images"][0]
            self.assertEqual(image[search.IMAGE_FILE_KEY], str(Path(folder) / "json" / "a_images" / "x.png"))
            with redirect_stdout(io.StringIO()):
                search.print_results([(0.5, next(iter(loaded.values()))[0])])


if __name__ == "__main__":
    unittest.main()
