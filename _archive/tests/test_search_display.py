"""How search.py showed the pictures of a chunk; moved out of tests/test_chunk_images.py with search.py."""

import io
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

import search

CODE = "T 1"


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
