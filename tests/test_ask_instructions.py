"""Tests of the instructions file ask.py reads (ask-instructions.md and --instructions).

Run from the project folder: venvs/bin/python -m unittest discover -s tests -v
"""

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import ask
from docx_to_txt import CELL_SEPARATOR
from embed_chunks import texts_to_embed
from json_to_chunks import STATUS_IN_FORCE, STATUS_REPEALED
from pipeline_common import ConversionError
from search import AMENDED_MARK, FIGURES_LABEL, FIGURES_SHOWN, IMAGE_CAPTION_CHARS, STATUS_LABEL, STATUS_WORDS

DOC_KEY = "doc"


def written(folder: str, text: str) -> Path:
    path = Path(folder) / "rules.md"
    path.write_text(text, encoding="utf-8")
    return path


class LoadInstructionsTest(unittest.TestCase):
    def test_tokens_are_replaced_by_their_values(self):
        with TemporaryDirectory() as folder:
            text = ask.load_instructions(written(folder, 'cells by "{cell_separator}"; "{status_label}: {status_in_force}", "{status_unverified}", "{status_repealed}", "{amended_mark}"; "{figures_label}: 2"'))
        self.assertEqual(text, f'cells by "{CELL_SEPARATOR}"; "Statut: în vigoare", "neverificat", "abrogat", "modificat ulterior"; "Figuri: 2"')

    def test_other_braces_are_left_alone(self):
        with TemporaryDirectory() as folder:
            text = ask.load_instructions(written(folder, 'answer as {"a": 1} and {unknown}'))
        self.assertEqual(text, 'answer as {"a": 1} and {unknown}')

    def test_surrounding_blank_lines_are_dropped(self):
        with TemporaryDirectory() as folder:
            self.assertEqual(ask.load_instructions(written(folder, "\n\n  Rule one.\n\n")), "Rule one.")

    def test_a_missing_file_is_an_error_that_names_it(self):
        with self.assertRaisesRegex(ConversionError, "nope.md"):
            ask.load_instructions(Path("nope.md"))

    def test_an_empty_file_is_an_error(self):
        with TemporaryDirectory() as folder, self.assertRaisesRegex(ConversionError, "empty"):
            ask.load_instructions(written(folder, " \n"))

    def test_the_default_file_exists_and_has_no_token_left(self):
        text = ask.load_instructions()
        self.assertIn("Romanian fire-safety norms", text)
        self.assertEqual(re.findall(r"\{[a-z_]+\}", text), [])  # every token it uses is one ask.py knows

    def test_the_default_file_uses_the_words_the_excerpts_use(self):
        text = ask.load_instructions()
        for words in (f'"{STATUS_LABEL}: …"', f'"{FIGURES_LABEL}: 2 …"', f'"{AMENDED_MARK}"', *(f'"{word}"' for word in STATUS_WORDS.values())):
            self.assertIn(words, text)

    def test_every_token_stands_for_words_the_model_meets(self):
        record = chunk([{"caption": "", "page": 4}])
        record.update(status=STATUS_REPEALED, repealed_on="2026-08-25", amended_by=["Ordinul 1"], label="7.10", pdf_pages=[4, 4])
        article = chunk()
        article.update(label="Art. 1", status=STATUS_IN_FORCE)
        unverified = chunk()
        unverified.update(status="unverified")
        request = request_for(record, article, unverified)
        only_in_the_rules = {"{cell_separator}", "{paragraph_prefix}"}  # a table's cells, and the paragraphs of a text: no fixture has them
        for token, words in ask.INSTRUCTION_TOKENS.items():
            if token not in only_in_the_rules:
                self.assertIn(words, request, token)

    def test_the_default_file_uses_every_token(self):
        raw = ask.INSTRUCTIONS_FILE.read_text(encoding="utf-8")
        for token in ask.INSTRUCTION_TOKENS:
            self.assertIn(token, raw, token)

class BuildMessagesTest(unittest.TestCase):
    def test_the_instructions_are_the_system_message(self):
        record = {
            "kind": "article", "doc_key": DOC_KEY, "doc_code": "T 1", "doc_title": "Titlu", "edition_year": 2025, "status": STATUS_IN_FORCE,
            "label": "Art. 1", "pdf_pages": [7, 7], "text": "Art. 1. Text.", "embed_text": "T 1 > Art. 1\nArt. 1. Text.",
        }
        messages = ask.build_messages("Ce spune?", [(0.9, record)], {DOC_KEY: {}}, "My rules.")
        self.assertEqual(messages[0], {ask.API_ROLE: ask.ROLE_SYSTEM, ask.API_CONTENT: "My rules."})
        self.assertIn("[1] T 1, Art. 1 — Titlu (ed. 2025, PDF p. 7)\nStatut: în vigoare\nT 1 > Art. 1\nArt. 1. Text.", messages[1][ask.API_CONTENT])
        self.assertTrue(messages[1][ask.API_CONTENT].endswith("Question: Ce spune?"))


def chunk(images=None, text="Art. 1. Text."):
    record = {
        "kind": "article", "doc_key": DOC_KEY, "doc_code": "T 1", "edition_year": 2025, "status": STATUS_IN_FORCE,
        "text": text, "embed_text": "T 1 > Art. 1\n" + text,
    }
    if images is not None:
        record["images"] = images
    return record


def request_for(*records):
    return ask.build_messages("Ce spune?", [(0.9, record) for record in records], {DOC_KEY: {}}, "My rules.")[1][ask.API_CONTENT]


class ImagesInPromptTest(unittest.TestCase):
    STATUS_LINE = "Statut: în vigoare"

    def test_a_chunk_with_pictures_says_so_under_its_citation(self):
        request = request_for(chunk([{"caption": ""}, {"caption": ""}]))
        self.assertIn(f"[1] T 1 (ed. 2025)\n{self.STATUS_LINE} · {FIGURES_LABEL}: 2, {FIGURES_SHOWN}\nT 1 > Art. 1\nArt. 1. Text.", request)

    def test_captions_and_pages_are_given_when_there_are_some(self):
        request = request_for(chunk([{"caption": "Figura  1\nPerete", "page": 36}, {"caption": ""}, {"caption": "Figura 2", "page": 38}]))
        self.assertIn(f'{FIGURES_LABEL}: 3 (PDF p. 36–38), {FIGURES_SHOWN}; legende: "Figura 1 Perete", "Figura 2"\n', request)

    def test_a_long_caption_is_cut(self):
        request = request_for(chunk([{"caption": "x" * 200}]))
        self.assertIn('"' + "x" * IMAGE_CAPTION_CHARS + '…"', request)
        self.assertNotIn("x" * (IMAGE_CAPTION_CHARS + 1), request)

    def test_chunks_without_pictures_say_nothing_of_figures(self):
        request = request_for(chunk([]), chunk(), chunk([], "Art. 2."))
        self.assertNotIn(FIGURES_LABEL, request)
        self.assertEqual(request.count(self.STATUS_LINE + "\n"), 3)

    def test_only_the_excerpt_with_pictures_says_so(self):
        request = request_for(chunk([], "Art. 1."), chunk([{"caption": ""}], "Art. 2."))
        first, second = request.split("[2] ")
        self.assertNotIn(FIGURES_LABEL, first)
        self.assertIn(FIGURES_LABEL, second)

    def test_a_picture_without_a_caption_or_with_a_null_one_counts_but_adds_none(self):
        request = request_for(chunk([{}, {"caption": None}, {"caption": "Figura 2"}]))
        self.assertIn(': 3, ' + FIGURES_SHOWN + '; legende: "Figura 2"', request)

    def test_a_norm_that_does_not_simply_apply_says_so_under_its_citation(self):
        record = chunk()
        record.update(status=STATUS_REPEALED, repealed_on="2026-08-25")
        self.assertIn("[1] T 1 (ed. 2025)\nStatut: abrogat din 25.08.2026\n", request_for(record))

    def test_the_text_that_is_embedded_does_not_depend_on_the_pictures(self):
        pictures = [{"caption": "Figura 1"}]
        _, with_pictures = texts_to_embed([chunk(pictures)], {DOC_KEY: {}})
        _, without = texts_to_embed([chunk([])], {DOC_KEY: {}})
        self.assertEqual(with_pictures, without)


if __name__ == "__main__":
    unittest.main()
