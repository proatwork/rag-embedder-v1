"""Tests of what the page needs to show a model that searches for itself: its files are read, no server is started.

Run from the project folder: venvs/bin/python -m unittest discover -s chatbot/tests -v
"""

import re
import unittest
from pathlib import Path

PAGE_FOLDER = Path(__file__).resolve().parent.parent
SEARCH_MODES = ["model", "once"]  # what /api/ask takes as "search"; the first is the default
SEARCH_CLASSES = (".searches", ".searches:empty", ".search-line", ".search-line.warn")


class PageSearchTest(unittest.TestCase):
    """The setting of how the norms are searched, and the events of an answer the model searched for."""

    def setUp(self):
        self.page, self.script, self.styles = ((PAGE_FOLDER / name).read_text(encoding="utf-8") for name in ("index.html", "chatbot.js", "chatbot.css"))

    def branch(self, event):
        """What the script does with one event of the stream."""
        return re.search(r'name === "%s"\) \{(.*?)\n    \} else if' % event, self.script, re.DOTALL).group(1)

    def test_the_page_offers_the_two_ways_to_search_and_the_script_knows_the_same_two(self):
        select = re.search(r'<select id="search_mode">(.*?)</select>', self.page, re.DOTALL).group(1)
        self.assertEqual(re.findall(r'<option value="([^"]*)"', select), SEARCH_MODES)
        self.assertNotIn("selected", select)  # the first one is the default
        known = [re.search(r'const %s = "([^"]+)";' % name, self.script).group(1) for name in ("SEARCH_BY_MODEL", "SEARCH_ONCE")]
        self.assertEqual(known, SEARCH_MODES)
        self.assertIn("const SEARCH_MODES = [SEARCH_BY_MODEL, SEARCH_ONCE];", self.script)

    def test_the_way_to_search_is_kept_like_the_other_settings(self):
        self.assertIn('"search_mode"', re.search(r"const FIELDS = \[([^\]]+)\]", self.script).group(1))
        self.assertIn('$("search_mode").value = SEARCH_MODES.includes(store.get("search_mode", "")) ? store.get("search_mode", "") : SEARCH_BY_MODEL;', self.script)
        saved_on_change = re.search(r'for \(const id of \[([^\]]+)\]\) \$\(id\)\.addEventListener\("change", saveSettings\)', self.script).group(1)
        self.assertIn('"search_mode"', saved_on_change)

    def test_only_an_openai_style_server_is_asked_to_let_the_model_search(self):
        body = re.search(r"const body = \{(.*?)\n  \};", self.script, re.DOTALL).group(1)
        self.assertIn('search: $("chat_api").value === "openai" ? $("search_mode").value : SEARCH_ONCE,', body)
        shown = re.search(r"function showUsualPath\(\) \{(.*?)\n\}", self.script, re.DOTALL).group(1)
        self.assertIn('$("search_mode").disabled = api !== "openai";', shown)
        self.assertIn('$("searchNote").textContent = api === "openai" ? "" : SEARCH_MODE_NOTE;', shown)

    def test_a_search_ends_the_draft_and_is_listed(self):
        branch = self.branch("search")
        for step in ("savedSearches.push(data)", "endRound()", "renderSearches(bot.searches, savedSearches)"):
            self.assertIn(step, branch)
        ended = re.search(r"const endRound = \(\) => \{(.*?)\n  \};", self.script, re.DOTALL).group(1)
        self.assertIn('rawAnswer = answerText = "";', ended)
        self.assertNotIn('reasoningText = ""', ended)  # what the model thought before a search stays
        self.assertNotIn("reasoningParts", ended)  # and its count goes on
        self.assertIn("reasoningText = earlierThinking + inline[1]", self.script)

    def test_a_fallback_drops_the_draft_and_the_sources_but_not_the_searches(self):
        branch = self.branch("fallback")
        for step in ("endRound()", "showSources(null)", "showNotice(data.message)"):
            self.assertIn(step, branch)
        for kept in ("savedSearches", "savedErrors", '"err"'):
            self.assertNotIn(kept, branch)

    def test_the_searches_are_saved_with_the_answer_and_drawn_again_by_the_same_function(self):
        self.assertRegex(self.script, r'role: "bot", [^\n]*\bsearches: savedSearches')
        saved = re.search(r"function renderSavedBotMessage\(box, msg\) \{(.*?)\n\}", self.script, re.DOTALL).group(1)
        self.assertIn("msg.searches || []", saved)
        self.assertIn("renderSearches(", saved)
        for name in ("renderSearches", "fmtSearch"):
            self.assertEqual(len(re.findall(r"function %s\(" % name, self.script)), 1, name)
        self.assertEqual(self.script.count("fmtSearch("), 2)  # where it is, and the one place that draws a line

    def test_a_source_fetched_by_its_number_has_no_score_to_show(self):
        self.assertIn('const SOURCE_BY_NUMBER = "by number";', self.script)
        self.assertIn("s.score == null ? SOURCE_BY_NUMBER : SOURCE_SCORE + s.score", self.script)
        self.assertEqual(self.script.count("+ s.score"), 1)
        self.assertIn("searchCount > 1 && s.search ? SOURCE_SEARCH + s.search", self.script)

    def test_the_list_of_searches_has_its_styles(self):
        styles = re.sub(r"/\*.*?\*/", "", self.styles, flags=re.DOTALL)
        for selector in SEARCH_CLASSES:
            self.assertRegex(styles, r"(?m)^%s \{" % re.escape(selector))
        for made in ('el("div", "searches")', 'el("div", "search-line"', 'line.classList.add("warn")'):
            self.assertIn(made, self.script)
        self.assertIn("select:disabled", styles)  # the choice is switched off for LM Studio, and looks it


if __name__ == "__main__":
    unittest.main()
