// The port of search_tool.py for the MCP server: one call of search_norms(query, norm, article, count), on its own,
// as mcp_server.py runs it (a session without a limit of calls, whose excerpts are numbered from 1). Unlike Python's,
// an excerpt lists each figure that has a public link on a line of its own (FIGURE_LINE), for the user to open and
// get_figure to load; figures without one the reader is not shown. The words that frame the excerpts are English;
// the excerpts quote the norms in Romanian.
import { z } from "zod";
import {
  DEFAULT_TOP,
  LINE_SEPARATOR,
  LIST_SEPARATOR,
  STATUS_LABEL,
  articleNumber,
  citation,
  figuresNote,
  format,
  normalizeRomanian,
  pagesNote,
  pyStrip,
  reference,
  shortCaption,
  statusWords,
} from "./search";
import type { ChunkRecord, Embedder, FigureRecord, NormInfo, Store } from "./types";

export const TOOL_NAME = "search_norms";
// The tool's parameters. English, as all names a program reads.
export const ARG_QUERY = "query";
export const ARG_NORM = "norm";
export const ARG_ARTICLE = "article";
export const ARG_COUNT = "count";
export const MAX_COUNT = 20; // the most excerpts one call may ask for
export const MAX_SEARCHES = 5; // calls for one question in ask.py and the chatbot; the MCP server has no limit
export const TOOL_DESCRIPTION =
  "Search the Romanian fire-safety norms. Returns numbered excerpts, each under its citation and the status of its norm. " +
  `Give \`${ARG_QUERY}\` to search by meaning, or \`${ARG_ARTICLE}\` to fetch an article or point by its number.`;
export const QUERY_HELP =
  "What to look for, in Romanian, in the words a norm would use; complete on its own, without relying on the conversation.";
export const NORM_HELP =
  "Search this norm only (its code). Leave out to search all of them, unless you know which norm holds the answer.";
export const NORMS_ARE = " The norms: {norms}.";
export const NORM_WITH_TITLE = "{code} ({title})";
export const NORM_SEPARATOR = "; ";
export const ARTICLE_HELP = `The number of an article or point to fetch as it is, for example "2.3.2.1.2" or "7.10". With it, \`${ARG_QUERY}\` is not used.`;
export const COUNT_HELP = "How many excerpts to return (default {count}).";

export const EXCERPT_HEADER = "[{number}] {citation}";
export const NOTE_SEPARATOR = " · "; // between the status and the figures, on the line under the citation
export const EXCERPT_SEPARATOR = LINE_SEPARATOR.repeat(2);
// A picture of an excerpt that has a public link, on a line of its own under the status: what get_figure takes, where
// it is, and its caption. The rules (rules.ts) name the "Figure" that starts it.
export const FIGURE_LINE_PREFIX = "Figure";
export const FIGURE_LINE = FIGURE_LINE_PREFIX + " {id}{page}: {url}{caption}";
export const FIGURE_PAGE = " ({page})";
export const FIGURE_CAPTION = ' — "{caption}"';
export const RESULT_HEADER_UNLIMITED = "Searched {what}: {found}"; // a session without a limit does not count its calls
export const FOR_QUERY = 'for "{query}"';
export const FOR_ARTICLE = "the article or point {article}";
export const IN_NORM = " in {norm}";
export const FOUND_ONE = "1 excerpt";
export const FOUND_MANY = "{count} excerpts";
export const REPEATS = " ({count} given before, under the same number)";
export const NOT_SHOWN =
  "; {count} more with that number are not shown: ask with a larger `" +
  ARG_COUNT +
  `\` (at most ${MAX_COUNT}), or search with \`${ARG_QUERY}\` for the part you need`;
export const GIVEN_ABOVE = "[{number}] {reference}: given above, under this number.";
export const NOT_RUN_UNLIMITED = "The search was not run: {problem}";
export const NOTHING_ASKED = `give \`${ARG_QUERY}\` (what to look for) or \`${ARG_ARTICLE}\` (a number to fetch).`;
export const NO_SUCH_NORM = "there is no norm with code {norm}. The norms are: {codes}."; // {norm} as Python's repr
export const NO_SUCH_NUMBER = "no excerpt holds the number {article}{where}. Search with `" + ARG_QUERY + "` instead.";
export const BAD_COUNT = `\`${ARG_COUNT}\` must be a whole number from 1 to ${MAX_COUNT}.`;

/** A call of the tool that cannot be run; the message says why, for the model. */
export class BadCall extends Error {
  override name = "BadCall";
}

/** What one call is answered with, and whether it is an error (a call that could not be run). */
export interface Reply {
  text: string;
  isError: boolean;
}

/** A picture as an excerpt lists it (FIGURE_LINE). */
export function figureLine(image: FigureRecord): string {
  const page = pagesNote([image.page]);
  const caption = shortCaption(image);
  return format(FIGURE_LINE, {
    id: image.id,
    page: page ? format(FIGURE_PAGE, { page }) : "",
    url: image.url,
    caption: caption ? format(FIGURE_CAPTION, { caption }) : "",
  });
}

/** One chunk as the model is given it: its number and citation, then a line with the status of its norm and with its
 * figures when it has any (`figuresShown`: whether the reader is shown them), then a line for each figure that has a
 * link, then `text`. Without any link the excerpt is the one search_tool.py gives. The note says the figures are
 * linked below only when all of them are (an upload that left some PNGs out of the bucket): otherwise it keeps the
 * captions, the only thing the model may say of a figure it cannot load. */
export function excerpt(number: number, record: ChunkRecord, text: string, figuresShown = true): string {
  const images = record.images ?? [];
  const linked = images.filter((image) => image.url);
  const allLinked = linked.length > 0 && linked.length === images.length;
  const notes = [`${STATUS_LABEL}: ${statusWords(record)}`, figuresNote(record, figuresShown, allLinked)]
    .filter(Boolean)
    .join(NOTE_SEPARATOR);
  return [format(EXCERPT_HEADER, { number, citation: citation(record) }), notes, ...linked.map(figureLine), text].join(
    LINE_SEPARATOR,
  );
}

/** The norms a call can search, by code in Python's order (by code point), each with its title. */
export function knownNorms(norms: readonly NormInfo[]): NormInfo[] {
  const byCode = new Map(norms.map((norm) => [norm.code, norm.title ?? ""]));
  return [...byCode.keys()].sort(byCodePoint).map((code) => ({ code, title: byCode.get(code) ?? "" }));
}

function byCodePoint(a: string, b: string): number {
  const left = Array.from(a, (char) => char.codePointAt(0) ?? 0);
  const right = Array.from(b, (char) => char.codePointAt(0) ?? 0);
  for (let place = 0; place < Math.min(left.length, right.length); place++) {
    const difference = (left[place] ?? 0) - (right[place] ?? 0);
    if (difference) return difference;
  }
  return left.length - right.length;
}

/** The help of `norm`: with the title of each norm that has one, so that the model can tell which one to search. */
export function normHelp(norms: readonly NormInfo[]): string {
  const named = norms
    .filter((norm) => norm.title)
    .map((norm) => format(NORM_WITH_TITLE, norm as unknown as Record<string, string>));
  return NORM_HELP + (named.length ? format(NORMS_ARE, { norms: named.join(NORM_SEPARATOR) }) : "");
}

/** The tool's arguments as registerTool takes them. Every field is optional and of any type, so that what the model
 * sends reaches the call as it is and is answered as search_tool.py answers it; each field carries the JSON Schema
 * search_tool.tool_schema gives a chat model, which is what tools/list shows. */
export function inputSchema(norms: readonly NormInfo[], count: number = DEFAULT_TOP) {
  return z.object({
    [ARG_QUERY]: z.unknown().optional().meta({ type: "string", description: QUERY_HELP }),
    [ARG_NORM]: z
      .unknown()
      .optional()
      .meta({ type: "string", enum: norms.map((norm) => norm.code), description: normHelp(norms) }),
    [ARG_ARTICLE]: z.unknown().optional().meta({ type: "string", description: ARTICLE_HELP }),
    [ARG_COUNT]: z
      .unknown()
      .optional()
      .meta({ type: "integer", minimum: 1, maximum: MAX_COUNT, description: format(COUNT_HELP, { count }) }),
  });
}

/** An argument that is text, trimmed; empty when it is left out, null or anything else. */
function textArgument(args: Readonly<Record<string, unknown>>, name: string): string {
  const value = args[name];
  return typeof value === "string" ? pyStrip(value) : "";
}

// Python's int() of a str: whitespace around it, a sign, and underscores between digits. Only ASCII digits: Python
// would also read the digits of other scripts, which no model writes for a count.
const PY_INT = /^[+-]?[0-9]+(?:_[0-9]+)*$/;

/** How many excerpts a call gets: what it asks for (a model may write the number as text), else `fallback`. Throws
 * BadCall for anything that is not a whole number from 1 to MAX_COUNT. */
export function countAsked(count: unknown, fallback: number): number {
  if (count === undefined || count === null || count === "") return fallback;
  let wanted: number;
  if (typeof count === "number") wanted = count;
  else if (typeof count === "string" && PY_INT.test(pyStrip(count)))
    wanted = Number(pyStrip(count).replaceAll("_", ""));
  else throw new BadCall(BAD_COUNT); // booleans too, which Python's int() would take for 1 and 0
  if (!Number.isInteger(wanted) || wanted < 1 || wanted > MAX_COUNT) throw new BadCall(BAD_COUNT);
  return wanted;
}

/** A value as Python's repr() writes it, for the reasons that quote what the model sent. */
export function pyRepr(value: unknown): string {
  if (value === null || value === undefined) return "None";
  if (value === true) return "True";
  if (value === false) return "False";
  if (typeof value === "number") return floatRepr(value);
  if (typeof value === "string") return stringRepr(value);
  if (Array.isArray(value)) return `[${value.map(pyRepr).join(", ")}]`;
  if (typeof value === "object") {
    return `{${Object.entries(value)
      .map(([key, item]) => `${stringRepr(key)}: ${pyRepr(item)}`)
      .join(", ")}}`;
  }
  return String(value);
}

const FIXED_FROM_EXPONENT = -4; // Python's repr of a float writes it without an exponent from 1e-4 ...
const FIXED_BELOW_EXPONENT = 16; // ... to below 1e16
const NOT_PRINTABLE = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]/u;
const NAMED_ESCAPES: Readonly<Record<string, string>> = { "\\": "\\\\", "\t": "\\t", "\n": "\\n", "\r": "\\r" };
const HEX = 16;

function floatRepr(value: number): string {
  if (Number.isNaN(value)) return "nan";
  if (!Number.isFinite(value)) return value > 0 ? "inf" : "-inf";
  if (Number.isInteger(value)) return BigInt(value).toString(); // JSON's 5 is Python's int 5
  const [mantissa = "", exponentText = "0"] = value.toExponential().split("e");
  const exponent = Number(exponentText);
  if (exponent < FIXED_FROM_EXPONENT || exponent >= FIXED_BELOW_EXPONENT) {
    const sign = exponent < 0 ? "-" : "+";
    return `${mantissa}e${sign}${String(Math.abs(exponent)).padStart(2, "0")}`;
  }
  return String(value);
}

function stringRepr(text: string): string {
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'";
  let written = "";
  for (const char of text) {
    const code = char.codePointAt(0) ?? 0;
    if (char === quote) written += "\\" + char;
    else if (char in NAMED_ESCAPES) written += NAMED_ESCAPES[char];
    else if (char !== " " && NOT_PRINTABLE.test(char)) {
      written += code < 0x100 ? "\\x" + hex(code, 2) : code < 0x10000 ? "\\u" + hex(code, 4) : "\\U" + hex(code, 8);
    } else written += char;
  }
  return quote + written + quote;
}

function hex(code: number, digits: number): string {
  return code.toString(HEX).padStart(digits, "0");
}

/** One call of the tool, standing alone (mcp_server.new_session): `norms` are those that can be searched, `count` how
 * many excerpts a call gets when it does not say. */
export class SearchCall {
  private readonly codes: string[];

  constructor(
    private readonly store: Store,
    private readonly embedder: Embedder,
    norms: readonly NormInfo[],
    private readonly count: number = DEFAULT_TOP,
  ) {
    this.codes = knownNorms(norms).map((norm) => norm.code);
  }

  /** What the model is given back for `args`: the excerpts found, numbered from 1, under a line that says what was
   * searched; or, for a call that cannot be run, the reason, as an error. Throws what the store and the embedder throw
   * (EmbeddingUnavailable when the question cannot be embedded). */
  async reply(args: Readonly<Record<string, unknown>>): Promise<Reply> {
    let asked: { what: string; found: ChunkRecord[]; more: number };
    try {
      asked = await this.find(args);
    } catch (error) {
      if (!(error instanceof BadCall)) throw error;
      return { text: format(NOT_RUN_UNLIMITED, { problem: error.message }), isError: true };
    }
    // As search_tool.py numbers them; in a call that stands alone no chunk was given before, unless it is found twice.
    const numbers = new Map<string, number>();
    const fresh = asked.found.filter((record) => !numbers.has(record.id));
    const texts = new Map<number, string>();
    fresh.forEach((record, place) => {
      texts.set(place + 1, excerpt(place + 1, record, record.embed_text, false));
      numbers.set(record.id, place + 1);
    });
    const given = asked.found.map((record) => numbers.get(record.id) ?? 0);
    const found = given.length === 1 ? FOUND_ONE : format(FOUND_MANY, { count: given.length });
    const repeats = given.length - fresh.length;
    const header =
      format(RESULT_HEADER_UNLIMITED, { what: asked.what, found }) +
      (repeats ? format(REPEATS, { count: repeats }) : "") +
      (asked.more ? format(NOT_SHOWN, { count: asked.more }) : "");
    const parts = given.map(
      (number) => texts.get(number) ?? format(GIVEN_ABOVE, { number, reference: reference(fresh[number - 1]!) }),
    );
    return { text: [header, ...parts].join(EXCERPT_SEPARATOR), isError: false };
  }

  /** The chunks a call asks for, what it looked for as the header says it, and how many more hold the number it asked
   * for than it is given. Throws BadCall for a call that cannot be run, in the order search_tool.find checks. */
  private async find(
    args: Readonly<Record<string, unknown>>,
  ): Promise<{ what: string; found: ChunkRecord[]; more: number }> {
    const article = textArgument(args, ARG_ARTICLE);
    const number = article ? articleNumber(article) : "";
    const query = article ? "" : textArgument(args, ARG_QUERY);
    const rawNorm = args[ARG_NORM];
    if (rawNorm !== undefined && rawNorm !== null && typeof rawNorm !== "string") {
      throw new BadCall(this.noSuchNorm(pyRepr(rawNorm))); // a list of norms, a number: not searched as if none was named
    }
    const norm = this.normAsked(textArgument(args, ARG_NORM));
    const count = countAsked(args[ARG_COUNT], this.count);
    const where = norm ? format(IN_NORM, { norm }) : "";
    if (article) {
      const holding = await this.store.withNumber(number, norm);
      if (!holding.length) throw new BadCall(format(NO_SUCH_NUMBER, { article: number, where }));
      return {
        what: format(FOR_ARTICLE, { article: number }) + where,
        found: holding.slice(0, count),
        more: Math.max(holding.length - count, 0),
      };
    }
    if (!query) throw new BadCall(NOTHING_ASKED);
    const vector = await this.embedder.embed(normalizeRomanian(query));
    const nearest = await this.store.nearest(vector, count, norm);
    return { what: format(FOR_QUERY, { query }) + where, found: nearest.map((scored) => scored.record), more: 0 };
  }

  /** The norm a call is to search, as the norms spell its code (the call may write it in any case); undefined for
   * all. Throws BadCall for a code that is no norm. */
  private normAsked(norm: string): string | undefined {
    if (!norm) return undefined;
    const wanted = caseFolded(norm);
    const code = this.codes.find((known) => caseFolded(known) === wanted);
    if (code === undefined) throw new BadCall(this.noSuchNorm(pyRepr(norm)));
    return code;
  }

  private noSuchNorm(norm: string): string {
    return format(NO_SUCH_NORM, { norm, codes: this.codes.join(LIST_SEPARATOR) });
  }
}

/** Close to Python's str.casefold(): lower case, with the letters it folds to more than one ("ß" is "ss"). */
function caseFolded(text: string): string {
  return text.toLowerCase().replaceAll("ß", "ss");
}
