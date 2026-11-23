"""Tests of the conversation of ask.py (--conversation): the earlier exchange in the prompt, and the file it is kept in.

Run from the project folder: venvs/bin/python -m unittest discover -s tests -v
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import ask
from pipeline_common import ConversionError

DOC_KEY = "doc"
RECORD = {
    "kind": "article", "doc_key": DOC_KEY, "doc_code": "T 1", "edition_year": 2025, "status": "in_force",
    "text": "Art. 1. Text.", "embed_text": "T 1 > Art. 1\nArt. 1. Text.",
}


def exchange(turns: int) -> list[dict]:
    return [message for turn in range(1, turns + 1) for message in ask.remembered(f"q{turn}", f"a{turn}")]


class HistoryInThePromptTest(unittest.TestCase):
    def test_the_exchange_comes_between_the_rules_and_the_question(self):
        messages = ask.build_messages("Și?", [(0.9, RECORD)], {DOC_KEY: {}}, "Rules.", exchange(1))
        self.assertEqual([message[ask.API_ROLE] for message in messages], ["system", "user", "assistant", "user"])
        self.assertEqual(messages[1][ask.API_CONTENT], "q1")
        self.assertEqual(messages[2][ask.API_CONTENT], "a1")
        self.assertTrue(messages[3][ask.API_CONTENT].endswith("Question: Și?"))

    def test_without_an_exchange_the_prompt_is_as_before(self):
        for history in (None, []):
            messages = ask.build_messages("Ce?", [(0.9, RECORD)], {DOC_KEY: {}}, "Rules.", history)
            self.assertEqual([message[ask.API_ROLE] for message in messages], ["system", "user"])

    def test_only_the_newest_messages_are_sent(self):
        messages = ask.build_messages("Și?", [(0.9, RECORD)], {DOC_KEY: {}}, "Rules.", exchange(10))
        sent = messages[1:-1]
        self.assertEqual(len(sent), ask.MAX_HISTORY_MESSAGES)
        self.assertEqual(sent[0][ask.API_CONTENT], f"q{10 - ask.MAX_HISTORY_MESSAGES // 2 + 1}")
        self.assertEqual(sent[-1][ask.API_CONTENT], "a10")

    def test_remembered_keeps_the_plain_question_not_the_excerpts(self):
        self.assertEqual(ask.remembered("q", "a"), [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}])

    def test_the_default_rules_speak_of_the_earlier_exchange(self):
        self.assertIn("Earlier questions and answers", ask.load_instructions())


class CheckedHistoryTest(unittest.TestCase):
    def test_a_good_exchange_is_kept_with_only_role_and_content(self):
        checked = ask.checked_history([{"role": "user", "content": "q", "extra": 1}, {"role": "assistant", "content": "a"}], "x")
        self.assertEqual(checked, ask.remembered("q", "a"))

    def test_anything_else_is_refused_naming_the_source(self):
        for bad in ("text", {"role": "user"}, [{"role": "system", "content": "x"}], [{"role": "user"}], [{"role": "user", "content": 3}], ["q"]):
            with self.assertRaisesRegex(ConversionError, "the file", msg=repr(bad)):
                ask.checked_history(bad, "the file")


class ConversationFileTest(unittest.TestCase):
    def test_a_new_file_is_an_empty_exchange_and_saving_creates_it(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "new" / "chat.json"
            self.assertEqual(ask.load_conversation(path), [])
            ask.save_conversation(path, ask.remembered("q", "ă"))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), ask.remembered("q", "ă"))
            self.assertIn("ă", path.read_text(encoding="utf-8"))  # not escaped
            self.assertEqual(ask.load_conversation(path), ask.remembered("q", "ă"))

    def test_a_broken_file_is_an_error_that_names_it(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "chat.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(ConversionError, "chat.json"):
                ask.load_conversation(path)
            path.write_text('{"role": "user"}', encoding="utf-8")
            with self.assertRaisesRegex(ConversionError, "chat.json"):
                ask.load_conversation(path)


if __name__ == "__main__":
    unittest.main()
