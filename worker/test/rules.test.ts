// The rules for searching and citing, rendered as mcp_server.load_rules() renders mcp-instructions.md.
import { describe, expect, it } from "vitest";
import rulesFile from "../mcp-instructions.md";
import { ARG_FIGURE_ID, FIGURE_TOOL_NAME } from "../src/figure";
import { loadRules, renderRules, SEARCH_TOKENS } from "../src/rules";
import { FIGURE_LINE_PREFIX } from "../src/tool";
import { GOLDEN } from "./golden";

describe("rules", () => {
  it("render as Python renders the same file", () => {
    expect(renderRules(rulesFile)).toBe(GOLDEN.rules);
  });

  it("are the whole file, rendered", () => {
    expect(loadRules()).toBe(renderRules(rulesFile));
  });

  it("name the figure tool, its argument and the excerpts' figure lines as the code has them", () => {
    expect(loadRules()).toContain(`\`${FIGURE_TOOL_NAME}\` with its \`${ARG_FIGURE_ID}\``);
    expect(loadRules()).toContain(`"${FIGURE_LINE_PREFIX} <id> …: <link>"`);
    expect(loadRules()).toContain('"Figuri: 2 …"');
  });

  it("tell the model to put the picture mark right before every figure link", () => {
    expect(loadRules()).toContain("🖼️ right before it");
    expect(loadRules()).toContain("🖼️ [aici](<link>)");
  });

  it("leave no token unreplaced", () => {
    expect(SEARCH_TOKENS.some(([token]) => rulesFile.includes(token))).toBe(true);
    expect(loadRules()).not.toMatch(/\{[a-z_]+\}/);
  });

  it("are trimmed, replace every occurrence and refuse an empty file", () => {
    expect(renderRules('\n  `{arg_query}` and `{arg_query}`: "{status_label}" $& \n')).toBe(
      '`query` and `query`: "Statut" $&',
    );
    expect(() => renderRules(" \n ")).toThrow("empty");
    expect(renderRules("a\r\nb\rc")).toBe("a\nb\nc"); // line ends as read_text gives them
  });
});
