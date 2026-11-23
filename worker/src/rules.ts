// The rules for searching and citing (worker/mcp-instructions.md), with the tokens of ask.SEARCH_TOKENS
// replaced, as mcp_server.load_rules gives them: they name the words the excerpts use, so that the two cannot differ.
// The file is the whole prompt. Its rule on figures names get_figure, its id and tool.ts FIGURE_LINE in plain text, not
// by tokens: rules.test.ts fails when they no longer match the code.
import rulesFile from "../mcp-instructions.md";
import {
  AMENDED_MARK,
  ARTICLE_PREFIX,
  CELL_SEPARATOR,
  FIGURES_LABEL,
  PAGE_LABEL,
  PARAGRAPH_PREFIX,
  POINT_PREFIX,
  STATUS_IN_FORCE,
  STATUS_LABEL,
  STATUS_REPEALED,
  STATUS_UNVERIFIED,
  STATUS_WORDS,
  pyStrip,
} from "./search";
import { ARG_ARTICLE, ARG_NORM, ARG_QUERY, MAX_SEARCHES, TOOL_NAME } from "./tool";

const NEWLINE = /\r\n?/g; // read_text turns every kind of line end into "\n"

/** The words of a status, which must exist (Python raises KeyError otherwise). */
function statusWord(status: string): string {
  const words = STATUS_WORDS[status];
  if (words === undefined) throw new Error(`no words for the status ${status}`);
  return words;
}

/** ask.SEARCH_TOKENS: ask.INSTRUCTION_TOKENS, then ask.TOOL_TOKENS, in that order. */
export const SEARCH_TOKENS: ReadonlyArray<readonly [string, string]> = [
  ["{cell_separator}", CELL_SEPARATOR],
  ["{status_label}", STATUS_LABEL],
  ["{status_in_force}", statusWord(STATUS_IN_FORCE)],
  ["{status_unverified}", statusWord(STATUS_UNVERIFIED)],
  ["{status_repealed}", statusWord(STATUS_REPEALED)],
  ["{amended_mark}", AMENDED_MARK],
  ["{figures_label}", FIGURES_LABEL],
  ["{article_prefix}", pyStrip(ARTICLE_PREFIX)],
  ["{point_prefix}", pyStrip(POINT_PREFIX)],
  ["{paragraph_prefix}", pyStrip(PARAGRAPH_PREFIX)],
  ["{page_label}", PAGE_LABEL],
  ["{tool_name}", TOOL_NAME],
  ["{max_searches}", String(MAX_SEARCHES)],
  ["{arg_query}", ARG_QUERY],
  ["{arg_norm}", ARG_NORM],
  ["{arg_article}", ARG_ARTICLE],
];

/** `text` trimmed, with every one of the tokens it uses replaced by its value (ask.load_instructions). Throws when
 * nothing is left. */
export function renderRules(text: string): string {
  let rules = pyStrip(text.replace(NEWLINE, "\n"));
  if (!rules) throw new Error("the rules file mcp-instructions.md is empty");
  for (const [token, value] of SEARCH_TOKENS) rules = rules.split(token).join(value);
  return rules;
}

/** The rules of mcp-instructions.md with their tokens replaced: the whole prompt. */
export function loadRules(): string {
  return renderRules(rulesFile);
}
