// The rules for searching and citing, rendered as mcp_server.load_rules() renders mcp-instructions.md.
import { describe, expect, it } from "vitest";
import rulesFile from "../../mcp-instructions.md";
import { loadRules, renderRules, SEARCH_TOKENS } from "../src/rules";
import { GOLDEN } from "./golden";

describe("rules", () => {
  it("equal what Python renders from the same file", () => {
    expect(loadRules()).toBe(GOLDEN.rules);
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
