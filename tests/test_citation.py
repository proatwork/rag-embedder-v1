"""Tests of how a chunk is cited and of the status of its norm: the texts of search.py, and the config that feeds them.

Run from the project folder: venvs/bin/python -m unittest discover -s tests -v
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import json_to_chunks
import search
from json_to_chunks import AMENDED_BY_KEY, GAZETTE_KEY, REPEALED_ON_KEY, STATUS_IN_FORCE, STATUS_REPEALED, STATUS_UNVERIFIED

TITLE = "Normativ, Partea a II-a"


def chunk(**fields) -> dict:
    record = {
        "doc_code": "P118/2-2013", "doc_title": TITLE, "edition_year": 2013, "status": STATUS_IN_FORCE, "heading_path": ["ANEXA NR. 15", "Proprietăți"],
        "label": "7.10", "caption": "", "articles": ["7.10"], "pdf_pages": [36, 36], "images": [],
    }
    record.update(fields)
    return record


class LocatorTest(unittest.TestCase):
    def test_a_point_that_is_only_a_number_gets_the_point_prefix(self):
        self.assertEqual(search.locator(chunk()), "pct. 7.10")
        self.assertEqual(search.locator(chunk(label="7.10–7.12")), "pct. 7.10–7.12")

    def test_an_article_is_cited_as_the_norm_names_it(self):
        self.assertEqual(search.locator(chunk(label="Art. 2.1.1.2–2.1.1.3")), "Art. 2.1.1.2–2.1.1.3")

    def test_a_table_adds_its_title(self):
        self.assertEqual(search.locator(chunk(caption="TABEL 7.1  Înălțimea\nmaximă")), "pct. 7.10, TABEL 7.1 Înălțimea maximă")

    def test_text_without_a_number_is_cited_by_its_last_headings(self):
        self.assertEqual(search.locator(chunk(label="", articles=[])), "ANEXA NR. 15, Proprietăți")
        self.assertEqual(search.locator(chunk(label="", articles=[], heading_path=["A", "B", "C"])), "B, C")
        self.assertEqual(search.locator(chunk(label="", articles=[], heading_path=["Doar una"])), "Doar una")

    def test_the_heading_that_says_where_one_is_comes_first(self):
        for path, cited in (
            (["ANEXA 4 - ALCĂTUIRI", "ANEXA 4.1 - ASCENSOARE", "Ascensoare pentru intervenție", "Prevederi generale"], "ANEXA 4.1 - ASCENSOARE, Prevederi generale"),
            (["CAPITOLUL 6 — Referințe", "Standarde", "Liste"], "CAPITOLUL 6 — Referințe, Liste"),
            (["Partea a ll-a PROIECTAREA", "5. Coloane uscate", "RACORD INCENDIU"], "5. Coloane uscate, RACORD INCENDIU"),
            (["Ceva", "ANEXA NR. 26"], "ANEXA NR. 26"), (["Secțiunea II", "Ceva"], "Secțiunea II, Ceva"), (["SECŢIUNEA 3", "Ceva"], "SECŢIUNEA 3, Ceva"),
        ):
            self.assertEqual(search.locator(chunk(label="", articles=[], heading_path=path)), cited, path)

    def test_a_bare_number_is_not_called_a_point(self):
        for label in ("7", "58", "25–25.2", "50"):
            self.assertEqual(search.locator(chunk(label=label)), "ANEXA NR. 15, Proprietăți", label)
        self.assertEqual(search.locator(chunk(label="A.10. 2.3.26")), "A.10. 2.3.26")  # an annex's own numbering, as the norm writes it

    def test_a_long_heading_or_title_is_cut(self):
        cut = search.locator(chunk(label="", articles=[], heading_path=["x" * 200], caption="y" * 200))
        self.assertEqual(cut, "x" * search.LOCATOR_CHARS + "…, " + "y" * search.LOCATOR_CHARS + "…")

    def test_text_under_no_heading_has_no_locator(self):
        self.assertEqual(search.locator(chunk(label="", articles=[], heading_path=[])), "")

    def test_a_chunk_file_from_before_labels_were_kept_is_cited_by_its_numbers(self):
        old = chunk(articles=["1.2", "1.3", "1.4"])
        del old["label"], old["caption"]
        self.assertEqual(search.locator(old), "pct. 1.2–1.4")
        old["articles"] = ["1.2"]
        self.assertEqual(search.locator(old), "pct. 1.2")


class CitationTest(unittest.TestCase):
    def test_the_whole_citation(self):
        self.assertEqual(search.citation(chunk()), f"P118/2-2013, pct. 7.10 — {TITLE} (ed. 2013, PDF p. 36)")

    def test_the_gazette_comes_between_the_edition_and_the_page(self):
        cited = search.citation(chunk(**{GAZETTE_KEY: "M. Of. nr. 595 bis/2013"}))
        self.assertTrue(cited.endswith("(ed. 2013, M. Of. nr. 595 bis/2013, PDF p. 36)"))

    def test_several_pages_are_a_range(self):
        self.assertTrue(search.citation(chunk(pdf_pages=[95, 96])).endswith("(ed. 2013, PDF p. 95–96)"))

    def test_a_norm_converted_from_word_has_no_page(self):
        for pages in (None, []):
            cited = search.citation(chunk(pdf_pages=pages))
            self.assertTrue(cited.endswith("(ed. 2013)"), cited)
            self.assertNotIn("None", cited)

    def test_text_under_no_heading_is_cited_by_the_norm_alone(self):
        self.assertEqual(search.citation(chunk(label="", articles=[], heading_path=[])), f"P118/2-2013 — {TITLE} (ed. 2013, PDF p. 36)")

    def test_the_short_name_is_the_norm_and_the_place(self):
        self.assertEqual(search.reference(chunk()), "P118/2-2013, pct. 7.10")
        self.assertEqual(search.reference(chunk(label="", articles=[], heading_path=[])), "P118/2-2013")

    def test_a_record_without_a_title_is_still_cited(self):
        untitled = chunk()
        del untitled["doc_title"]
        self.assertEqual(search.citation(untitled), "P118/2-2013, pct. 7.10 (ed. 2013, PDF p. 36)")


class StatusTest(unittest.TestCase):
    def test_the_three_statuses_in_words(self):
        self.assertEqual(search.status_words(chunk()), "în vigoare")
        self.assertEqual(search.status_words(chunk(status=STATUS_UNVERIFIED)), "neverificat")
        self.assertEqual(search.status_words(chunk(status=STATUS_REPEALED, **{REPEALED_ON_KEY: "2026-08-25"})), "abrogat din 25.08.2026")

    def test_the_acts_that_changed_the_norm_are_named(self):
        words = search.status_words(chunk(**{AMENDED_BY_KEY: ["Ordinul 6026/2018", "Ordinul 1/2020"]}))
        self.assertEqual(words, "în vigoare; text original, modificat ulterior prin: Ordinul 6026/2018; Ordinul 1/2020")
        self.assertIn(search.AMENDED_MARK, words)

    def test_a_status_from_an_older_chunk_file_is_given_as_it_is(self):
        self.assertEqual(search.status_words(chunk(status="unknown")), "unknown")

    def test_only_a_norm_in_force_and_unchanged_needs_no_remark(self):
        self.assertTrue(search.applies_as_written(chunk()))
        self.assertTrue(search.applies_as_written(chunk(**{AMENDED_BY_KEY: []})))
        for other in (chunk(status=STATUS_UNVERIFIED), chunk(status=STATUS_REPEALED), chunk(**{AMENDED_BY_KEY: ["Ordinul 1"]}), chunk(status="unknown")):
            self.assertFalse(search.applies_as_written(other))

    def test_every_status_the_config_allows_has_words(self):
        self.assertEqual(set(search.STATUS_WORDS), set(json_to_chunks.STATUSES))


class FiguresTest(unittest.TestCase):
    def test_no_pictures_no_note(self):
        self.assertEqual(search.figures_note(chunk()), "")
        self.assertEqual(search.figures_note({"doc_code": "x"}), "")

    def test_the_number_the_pages_and_that_the_reader_sees_them(self):
        note = search.figures_note(chunk(images=[{"page": 36, "caption": ""}, {"page": 37, "caption": None}, {"page": 36}]))
        self.assertEqual(note, f"{search.FIGURES_LABEL}: 3 (PDF p. 36–37), {search.FIGURES_SHOWN}")

    def test_pictures_without_a_page(self):
        self.assertEqual(search.figures_note(chunk(images=[{"page": None, "caption": ""}])), f"{search.FIGURES_LABEL}: 1, {search.FIGURES_SHOWN}")

    def test_the_captions_there_are(self):
        note = search.figures_note(chunk(images=[{"page": 4, "caption": "Figura  1\nPerete"}, {"page": 4, "caption": ""}, {"page": 4, "caption": "y" * 200}]))
        self.assertTrue(note.endswith('; legende: "Figura 1 Perete", "' + "y" * search.IMAGE_CAPTION_CHARS + '…"'), note)

    def test_only_the_first_captions_are_quoted(self):
        note = search.figures_note(chunk(images=[{"page": 4, "caption": f"Figura {number}"} for number in range(1, 8)]))
        self.assertTrue(note.startswith(f"{search.FIGURES_LABEL}: 7 (PDF p. 4)"))
        self.assertTrue(note.endswith('; legende: "Figura 1", "Figura 2", "Figura 3", și alte 4'), note)


class PagesTest(unittest.TestCase):
    def test_pages(self):
        for pages, said in ((None, ""), ([], ""), ([None, 0], ""), ([4], "PDF p. 4"), ([4, 4], "PDF p. 4"), ([9, None, 4], "PDF p. 4–9")):
            self.assertEqual(search.pages_note(pages), said, pages)


class ConfigStatusTest(unittest.TestCase):
    ENTRY = {"code": "T 1", "title": "T", "year": 2020, "profile": json_to_chunks.PROFILES[0]}

    def load(self, **fields) -> dict:
        with TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps({"doc": {**self.ENTRY, **fields}}), encoding="utf-8")
            return json_to_chunks.load_config(path)

    def test_a_good_entry_loads(self):
        for fields in (
            {"status": STATUS_IN_FORCE}, {"status": STATUS_UNVERIFIED, GAZETTE_KEY: "M. Of. 1"}, {"status": STATUS_IN_FORCE, AMENDED_BY_KEY: ["Ordinul 1"]},
            {"status": STATUS_REPEALED, REPEALED_ON_KEY: "2026-08-25"}, {"status": STATUS_IN_FORCE, AMENDED_BY_KEY: [], REPEALED_ON_KEY: None, GAZETTE_KEY: None},
        ):
            self.assertEqual(self.load(**fields)["doc"]["status"], fields["status"])

    def test_a_bad_entry_stops_the_run_and_says_why(self):
        for fields, reason in (
            ({"status": "current"}, "known: in_force, unverified, repealed"), ({"status": "unknown"}, "has status"),
            ({"status": STATUS_REPEALED}, REPEALED_ON_KEY), ({"status": STATUS_IN_FORCE, REPEALED_ON_KEY: "2026-08-25"}, "only when"),
            ({"status": STATUS_REPEALED, REPEALED_ON_KEY: "25.08.2026"}, "YYYY-MM-DD"), ({"status": STATUS_REPEALED, REPEALED_ON_KEY: 2026}, "YYYY-MM-DD"),
            ({"status": STATUS_REPEALED, REPEALED_ON_KEY: "20260825"}, "YYYY-MM-DD"), ({"status": STATUS_REPEALED, REPEALED_ON_KEY: "2026-W35-1"}, "YYYY-MM-DD"),
            ({"status": STATUS_REPEALED, REPEALED_ON_KEY: "2026-02-30"}, "YYYY-MM-DD"), ({"status": STATUS_REPEALED, REPEALED_ON_KEY: "2026-08-25T10:00"}, "YYYY-MM-DD"),
            ({"status": STATUS_IN_FORCE, AMENDED_BY_KEY: "Ordinul 1"}, "list of names"), ({"status": STATUS_IN_FORCE, AMENDED_BY_KEY: [""]}, "list of names"),
            ({"status": STATUS_IN_FORCE, AMENDED_BY_KEY: ["Ordinul 1\nStatut: în vigoare"]}, "one line"),
            ({"status": STATUS_IN_FORCE, GAZETTE_KEY: " "}, GAZETTE_KEY), ({"status": STATUS_IN_FORCE, GAZETTE_KEY: 5}, GAZETTE_KEY),
            ({"status": STATUS_IN_FORCE, "amendedBy": ["x"]}, "amendedBy, which no script reads"), ({"status": STATUS_IN_FORCE, "year": None}, "whole number"),
            ({"status": STATUS_IN_FORCE, "year": "2020"}, "whole number"), ({"status": STATUS_IN_FORCE, "title": ""}, "one line"),
            ({"status": ["in_force"]}, "has status"),
        ):
            with self.assertRaises(SystemExit, msg=fields) as stopped:
                self.load(**fields)
            self.assertIn(reason, str(stopped.exception), fields)
            self.assertIn("'doc'", str(stopped.exception))

    def test_a_config_that_is_not_entries_stops_the_run_with_a_message(self):
        for content in ("[]", '{"doc": null}', '{"doc": "x"}', "not json"):
            with TemporaryDirectory() as folder:
                path = Path(folder) / "config.json"
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(SystemExit, msg=content) as stopped:
                    json_to_chunks.load_config(path)
                self.assertIn("config.json", str(stopped.exception))

    def test_the_keys_the_other_scripts_read_are_known(self):
        self.load(status=STATUS_IN_FORCE, first_body_page=4, embed_field="text")

    def test_the_project_config_is_valid_and_names_only_known_statuses(self):
        config = json_to_chunks.load_config(json_to_chunks.CONFIG_FILE)
        self.assertTrue(config)
        self.assertLessEqual({entry["status"] for entry in config.values()}, set(json_to_chunks.STATUSES))


class StaleChunksTest(unittest.TestCase):
    """A chunk is cited from what it carries; search.py stops when that is no longer what the config says."""
    ENTRY = {"code": "T 1", "title": "T", "year": 2020, "status": STATUS_IN_FORCE, "profile": json_to_chunks.PROFILES[0]}

    def chunks(self, **fields) -> list[dict]:
        return [{"doc_key": "doc", **json_to_chunks.norm_fields({**self.ENTRY, **fields})}]

    def test_chunks_made_from_this_config_are_not_stale(self):
        self.assertEqual(search.stale_norm_fields(self.chunks(), {"doc": self.ENTRY}), [])
        amended = {AMENDED_BY_KEY: ["Ordinul 1"], GAZETTE_KEY: "M. Of. 1"}
        self.assertEqual(search.stale_norm_fields(self.chunks(**amended), {"doc": {**self.ENTRY, **amended}}), [])

    def test_every_field_of_the_norm_changed_in_the_config_is_named(self):
        for change, field in (
            ({"status": STATUS_REPEALED, REPEALED_ON_KEY: "2026-08-25"}, ["repealed_on", "status"]), ({"status": STATUS_UNVERIFIED}, ["status"]),
            ({AMENDED_BY_KEY: ["Ordinul 1"]}, ["amended_by"]), ({GAZETTE_KEY: "M. Of. 1"}, ["gazette"]), ({"title": "Altul"}, ["doc_title"]),
            ({"year": 2021}, ["edition_year"]), ({"code": "T 2"}, ["doc_code"]),
        ):
            self.assertEqual(search.stale_norm_fields(self.chunks(), {"doc": {**self.ENTRY, **change}}), field, change)

    def test_chunks_from_before_they_carried_these_fields_are_stale(self):
        old = [{"doc_key": "doc", "doc_code": "T 1", "doc_title": "T", "edition_year": 2020, "status": "current"}]
        self.assertEqual(search.stale_norm_fields(old, {"doc": self.ENTRY}), ["amended_by", "gazette", "repealed_on", "status"])

    def test_the_chunker_writes_exactly_these_fields(self):
        chunk_fields = json_to_chunks.norm_fields({**self.ENTRY, "first_body_page": 3})
        self.assertEqual(set(chunk_fields), {"doc_code", "doc_title", "edition_year", "status", AMENDED_BY_KEY, REPEALED_ON_KEY, GAZETTE_KEY})
        self.assertEqual((chunk_fields[AMENDED_BY_KEY], chunk_fields[REPEALED_ON_KEY], chunk_fields[GAZETTE_KEY]), ([], None, None))


CHUNKS_DIR = json_to_chunks.CONFIG_FILE.with_name("docs_processed") / "chunks"


@unittest.skipUnless(CHUNKS_DIR.is_dir() and any(CHUNKS_DIR.glob("*.jsonl")), "no chunk files in docs_processed/chunks")
class RealChunksTest(unittest.TestCase):
    """Every chunk of the project's own data can be cited, and none of it is stale."""

    @classmethod
    def setUpClass(cls):
        cls.config = json_to_chunks.load_config(json_to_chunks.CONFIG_FILE)
        cls.files = {path: [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] for path in sorted(CHUNKS_DIR.glob("*.jsonl"))}

    def test_the_chunks_say_of_their_norm_what_the_config_says(self):
        for path, chunks in self.files.items():
            self.assertEqual(search.stale_norm_fields(chunks, self.config), [], path.name)

    def test_every_chunk_has_a_well_formed_citation_and_status(self):
        for path, chunks in self.files.items():
            for record in chunks:
                cited, words, figures = search.citation(record), search.status_words(record), search.figures_note(record)
                for text in (cited, words, figures):
                    for junk in ("None", "()", ", ,", "(, ", ", )", "\n"):
                        self.assertNotIn(junk, text, record["id"])
                self.assertTrue(cited.startswith(record["doc_code"]) and cited.endswith(")"), record["id"])
                self.assertEqual("PDF p." in cited, bool(record["pdf_pages"]), record["id"])
                self.assertIn(words.split(";")[0].split(" din ")[0], search.STATUS_WORDS.values(), record["id"])
                self.assertEqual(bool(figures), bool(record["images"]), record["id"])

    def test_no_bare_number_is_called_a_point(self):
        for chunks in self.files.values():
            for record in chunks:
                where = search.locator(record)
                if where.startswith(search.POINT_PREFIX):
                    self.assertRegex(where, r"^pct\. \d+\.\d", record["id"])


if __name__ == "__main__":
    unittest.main()
