// The port of what the MCP tool uses of search.py (and of the constants it takes from json_to_chunks.py,
// docx_to_txt.py and pipeline_common.py): how a chunk is cited, the status of its norm and its figures in words, and
// the number in what someone wrote for an article. The strings are Romanian, because answers quote them.
//
// Python's regular expressions and str methods are Unicode-aware where JavaScript's are not: `\s`, `\d`, `\b`,
// strip() and len() are spelled out below so that both give the same strings.
import type { ChunkRecord, FigureRecord } from "./types";

export const DEFAULT_TOP = 5;
export const IMAGE_CAPTION_CHARS = 80;
export const LOCATOR_CHARS = 70; // of a heading or a table's title, in a citation
export const HEADINGS_CITED = 2; // a chunk without a number is cited by its last headings
export const CAPTIONS_CITED = 3; // of a chunk's figures; the rest are counted, not quoted
export const ELLIPSIS = "…";
export const LIST_SEPARATOR = ", ";
export const LINE_SEPARATOR = "\n";
export const RANGE_SEPARATOR = "–"; // en dash
export const NUMBER_PART_SEPARATOR = "."; // between the parts of an article's number
export const CELL_SEPARATOR = " | "; // between the cells of a table row (docx_to_txt.py)

// The statuses a norm can have in the config (json_to_chunks.py).
export const STATUS_IN_FORCE = "in_force";
export const STATUS_UNVERIFIED = "unverified";
export const STATUS_REPEALED = "repealed";

// What is said about a chunk to the model and to the reader.
export const ARTICLE_PREFIX = "Art. "; // how the norms name an article (json_to_chunks.py)
export const POINT_PREFIX = "pct. "; // before a point that the norm gives only a number ("7.10")
export const PARAGRAPH_PREFIX = "alin. "; // before a numbered paragraph; only the instructions use it
export const TITLE_SEPARATOR = " — "; // em dash
export const EDITION = "ed. {year}";
export const PAGE_LABEL = "PDF p."; // the page of the PDF file, which is not always the number printed on it
export const STATUS_LABEL = "Statut";
export const STATUS_WORDS: Readonly<Record<string, string>> = {
  [STATUS_IN_FORCE]: "în vigoare",
  [STATUS_UNVERIFIED]: "neverificat",
  [STATUS_REPEALED]: "abrogat",
};
export const REPEALED_FROM = " din {day}";
export const AMENDED_MARK = "modificat ulterior"; // the instructions name it
export const AMENDED = "text original, " + AMENDED_MARK + " prin: {acts}";
export const ACT_SEPARATOR = "; ";
export const FIGURES_LABEL = "Figuri"; // the instructions name it
export const FIGURES_SHOWN = "afișate utilizatorului în sursele de sub răspuns";
export const FIGURES_NOT_SHOWN = "neafișate utilizatorului"; // where nothing shows the reader the pictures (MCP)
// Where each picture is listed under the note with its link (the Worker; not search.py): its captions are there.
export const FIGURES_LINKED = "cu linkuri mai jos";
export const CAPTIONS_LABEL = "legende";
export const MORE_CAPTIONS = "și alte {count}";

// The characters Python's str.isspace() and the `\s` of its str patterns take for whitespace (JavaScript's `\s` and
// trim() add U+FEFF and leave out U+001C–U+001F and U+0085).
const PY_SPACE = "\\t\\n\\v\\f\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const WHITESPACE = new RegExp(`[${PY_SPACE}]+`, "gu");
const LEADING_SPACE = new RegExp(`^[${PY_SPACE}]+`, "u");
const TRAILING_SPACE = new RegExp(`[${PY_SPACE}]+$`, "u");
const PY_DIGIT = "\\p{Nd}"; // Python's `\d` of a str pattern: any decimal digit
const PY_WORD_CHAR = "[\\p{L}\\p{N}_]"; // Python's `\w` of a str pattern
// A heading that says where in the norm one is: an annex, a chapter, a part, a section, or a numbered title.
const ANCHOR_HEADING = new RegExp(`^[${PY_SPACE}]*(?:anexa|capitolul|partea|sec[țţt]iunea|${PY_DIGIT})`, "iu");
const POINT_NUMBER = new RegExp(`^${PY_DIGIT}+\\.${PY_DIGIT}`, "u"); // how the label of a point starts: "7.10"
const STARTS_WITH_DIGIT = new RegExp(`^${PY_DIGIT}`, "u");
// The number of an article or point in what someone wrote for it: "Art. 2.3.1.", "pct. 7.10", "A.10. 1.1.1" (of an
// annex). The letter of an annex stands alone: the "a." that ends a word ("anexa. 7.10") is not one.
const ARTICLE_NUMBER = new RegExp(
  `(?:(?<!${PY_WORD_CHAR})A\\.[${PY_SPACE}]*)?${PY_DIGIT}+(?:\\.[${PY_SPACE}]*${PY_DIGIT}+)*`,
  "giu",
);
const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})$/;
const MIN_YEAR = 1; // datetime.MINYEAR
const CEDILLA_TO_COMMA: Readonly<Record<string, string>> = {
  ş: "ș", // ş → ș
  ţ: "ț", // ţ → ț
  Ş: "Ș", // Ş → Ș
  Ţ: "Ț", // Ţ → Ț
};
const CEDILLA_LETTERS = /[şţŞŢ]/g;
const TEMPLATE_FIELD = /\{(\w+)\}/g;

/** `template` with every {name} replaced by its value, as Python's str.format does for plain fields. */
export function format(template: string, values: Readonly<Record<string, string | number>>): string {
  return template.replace(TEMPLATE_FIELD, (field, name: string) => (name in values ? String(values[name]) : field));
}

/** Python's str.strip(): the text without the whitespace around it. */
export function pyStrip(text: string): string {
  return text.replace(LEADING_SPACE, "").replace(TRAILING_SPACE, "");
}

/** NFC, with one form for each Romanian letter: the documents mix "ş ţ" and "ș ț" (pipeline_common.normalize_romanian). */
export function normalizeRomanian(text: string): string {
  return text.normalize("NFC").replace(CEDILLA_LETTERS, (letter) => CEDILLA_TO_COMMA[letter] ?? letter);
}

/** The number in what someone wrote for an article or point, as the chunks keep it: "Art. 2.3.1." and "pct 2.3.1"
 * give "2.3.1", "A.10. 1.1.1" gives "A.10.1.1.1". Of several numbers, the one with the most parts is meant
 * ("P118/2-2013, pct. 7.10" gives "7.10"), the first of those that have as many. What holds no number is given back
 * trimmed. */
export function articleNumber(text: string): string {
  const numbers = Array.from(text.matchAll(ARTICLE_NUMBER), (found) => found[0].replace(WHITESPACE, "").toUpperCase());
  const parts = (number: string) => number.split(NUMBER_PART_SEPARATOR).length;
  return (
    numbers.reduce<string | undefined>(
      (best, number) => (best === undefined || parts(number) > parts(best) ? number : best),
      undefined,
    ) ?? pyStrip(text)
  );
}

/** The start of `text` on one line, at most `limit` characters and a mark that it was cut; empty for no text. */
export function shortened(text: string | null | undefined, limit: number): string {
  const line = Array.from(pyStrip((text ?? "").replace(WHITESPACE, " "))); // by code point, as Python counts
  return line.length > limit ? line.slice(0, limit).join("") + ELLIPSIS : line.join("");
}

export function shortCaption(image: FigureRecord): string {
  return shortened(image.caption, IMAGE_CAPTION_CHARS);
}

/** The PDF pages a chunk or its pictures are on, as a citation says them; empty when they are not known (a norm
 * converted from a Word file has none). */
export function pagesNote(pages: readonly (number | null)[] | null | undefined): string {
  const known = [...new Set((pages ?? []).filter((page): page is number => Boolean(page)))].sort((a, b) => a - b);
  const first = known[0];
  const last = known[known.length - 1];
  if (first === undefined || last === undefined) return "";
  return first === last ? `${PAGE_LABEL} ${first}` : `${PAGE_LABEL} ${first}${RANGE_SEPARATOR}${last}`;
}

/** The headings a chunk without a number is cited by: its last one, with the nearest heading above it that says where
 * in the norm one is when the last one does not; the last HEADINGS_CITED when none does. */
export function headingsCited(path: readonly string[]): string[] {
  const last = path[path.length - 1];
  if (last === undefined || ANCHOR_HEADING.test(last)) return path.slice(-1);
  const anchor = path
    .slice(0, -1)
    .reverse()
    .find((heading) => ANCHOR_HEADING.test(heading));
  return anchor !== undefined ? [anchor, last] : path.slice(-HEADINGS_CITED);
}

/** Where in its norm a chunk is: its article or point ("Art. 2.3.1", "pct. 7.10–7.11"), or the headings it is under
 * when it has no such number; a table adds its title. Empty for text that stands under no heading. */
export function locator(record: ChunkRecord): string {
  let label = record.label ?? "";
  const numbers = record.articles ?? [];
  const firstNumber = numbers[0];
  if (!label && firstNumber !== undefined) {
    // a chunk file written before chunks kept their label
    label = numbers.length === 1 ? firstNumber : firstNumber + RANGE_SEPARATOR + numbers[numbers.length - 1];
  }
  if (POINT_NUMBER.test(label)) label = POINT_PREFIX + label;
  else if (STARTS_WITH_DIGIT.test(label)) label = ""; // a bare number: the opening of a chapter, an item of a list
  if (!label) {
    label = headingsCited(record.heading_path ?? [])
      .map((heading) => shortened(heading, LOCATOR_CHARS))
      .join(LIST_SEPARATOR);
  }
  return [label, shortened(record.caption, LOCATOR_CHARS)].filter(Boolean).join(LIST_SEPARATOR);
}

/** The short name of a chunk: its norm and where in it, "P118/2-2013, pct. 7.10". */
export function reference(record: ChunkRecord): string {
  return [record.doc_code, locator(record)].filter(Boolean).join(LIST_SEPARATOR);
}

/** How a chunk is cited in full: "P118/2-2013, pct. 7.10 — <the norm's title> (ed. 2013, <gazette>, PDF p. 36)".
 * The gazette and the page are there when they are known. */
export function citation(record: ChunkRecord): string {
  const title = record.doc_title ? TITLE_SEPARATOR + record.doc_title : "";
  const details = [format(EDITION, { year: record.edition_year }), record.gazette, pagesNote(record.pdf_pages)];
  return `${reference(record)}${title} (${details.filter(Boolean).join(LIST_SEPARATOR)})`;
}

/** A day written YYYY-MM-DD, as DAY_FORMAT (%d.%m.%Y) gives it. Throws for anything else, as date.fromisoformat
 * does for what the config check lets through. */
function dayWords(iso: string): string {
  const parts = ISO_DAY.exec(iso);
  if (!parts) throw new RangeError(`Invalid isoformat string: '${iso}'`);
  const [, year, month, day] = parts as unknown as [string, string, string, string];
  const date = new Date(Date.UTC(Number(year), Number(month) - 1, Number(day)));
  if (Number(year) < MIN_YEAR || date.getUTCMonth() !== Number(month) - 1 || date.getUTCDate() !== Number(day)) {
    throw new RangeError(`day is out of range for month: '${iso}'`);
  }
  return `${day}.${month}.${year}`;
}

/** Whether a chunk's norm applies, in words: "în vigoare", "neverificat", "abrogat din 25.08.2026"; then the acts that
 * changed it since, when the config names any. A status this code does not know is given as it is. */
export function statusWords(record: ChunkRecord): string {
  let words = Object.hasOwn(STATUS_WORDS, record.status) ? STATUS_WORDS[record.status] : String(record.status);
  if (record.repealed_on) words += format(REPEALED_FROM, { day: dayWords(record.repealed_on) });
  if (record.amended_by?.length) {
    words += ACT_SEPARATOR + format(AMENDED, { acts: record.amended_by.join(ACT_SEPARATOR) });
  }
  return words ?? "";
}

/** What is said of a chunk's pictures: how many, on which PDF pages, whether the reader is shown them (`shown`), and
 * the start of the captions they have; empty when it has none. With `linked`, that they are listed below with their
 * links instead, and no captions, which the list gives. */
export function figuresNote(record: ChunkRecord, shown = true, linked = false): string {
  const images = record.images ?? [];
  if (!images.length) return "";
  const pages = pagesNote(images.map((image) => image.page));
  const seen = linked ? FIGURES_LINKED : shown ? FIGURES_SHOWN : FIGURES_NOT_SHOWN;
  const note = `${FIGURES_LABEL}: ${images.length}${pages ? " (" + pages + ")" : ""}${LIST_SEPARATOR}${seen}`;
  if (linked) return note;
  let captions = images
    .map(shortCaption)
    .filter(Boolean)
    .map((caption) => `"${caption}"`);
  if (captions.length > CAPTIONS_CITED) {
    captions = [
      ...captions.slice(0, CAPTIONS_CITED),
      format(MORE_CAPTIONS, { count: captions.length - CAPTIONS_CITED }),
    ];
  }
  return note + (captions.length ? `${ACT_SEPARATOR}${CAPTIONS_LABEL}: ${captions.join(LIST_SEPARATOR)}` : "");
}
