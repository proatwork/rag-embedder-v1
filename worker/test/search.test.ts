// The port of search.py's citation, status, figures and article-number code, against what Python gives
// (golden.ts) and against the literal strings of tests/test_citation.py and tests/test_search_tool.py.
import { describe, expect, it } from "vitest";
import {
  FIGURES_LINKED,
  FIGURES_NOT_SHOWN,
  FIGURES_SHOWN,
  IMAGE_CAPTION_CHARS,
  LOCATOR_CHARS,
  articleNumber,
  citation,
  figuresNote,
  locator,
  normalizeRomanian,
  pagesNote,
  reference,
  statusWords,
} from "../src/search";
import { excerpt } from "../src/tool";
import type { ChunkRecord, FigureRecord } from "../src/types";
import { GOLDEN, RECORDS } from "./golden";

const TITLE = "Normativ, Partea a II-a";

/** A chunk as tests/test_citation.py builds one. */
function chunk(fields: Partial<ChunkRecord> = {}): ChunkRecord {
  return {
    ...RECORDS.wall!,
    doc_code: "P118/2-2013",
    doc_title: TITLE,
    edition_year: 2013,
    status: "in_force",
    gazette: null,
    heading_path: ["ANEXA NR. 15", "Proprietăți"],
    label: "7.10",
    caption: "",
    articles: ["7.10"],
    pdf_pages: [36, 36],
    images: [],
    ...fields,
  };
}

function figure(page: number | null, caption: string): FigureRecord {
  return { id: "f", url: "", page, width: 1, height: 1, caption };
}

describe("as Python gives it, for every fixture chunk", () => {
  for (const [key, expected] of Object.entries(GOLDEN.perRecord)) {
    it(key, () => {
      const record = RECORDS[key]!;
      expect(citation(record)).toBe(expected.citation);
      expect(reference(record)).toBe(expected.reference);
      expect(locator(record)).toBe(expected.locator);
      expect(statusWords(record)).toBe(expected.status);
      expect(figuresNote(record)).toBe(expected.figures_shown);
      expect(figuresNote(record, false)).toBe(expected.figures_not_shown);
      expect(excerpt(1, record, record.embed_text, false)).toBe(expected.excerpt);
    });
  }
});

describe("locator", () => {
  it("gives a point that is only a number the point prefix", () => {
    expect(locator(chunk())).toBe("pct. 7.10");
    expect(locator(chunk({ label: "7.10–7.12" }))).toBe("pct. 7.10–7.12");
  });

  it("cites an article as the norm names it, and a table with its title", () => {
    expect(locator(chunk({ label: "Art. 2.1.1.2–2.1.1.3" }))).toBe("Art. 2.1.1.2–2.1.1.3");
    expect(locator(chunk({ caption: "TABEL 7.1  Înălțimea\nmaximă" }))).toBe("pct. 7.10, TABEL 7.1 Înălțimea maximă");
  });

  it("cites text without a number by its last headings, the one that says where one is first", () => {
    const cited = (heading_path: string[]) => locator(chunk({ label: "", articles: [], heading_path }));
    expect(cited(["ANEXA NR. 15", "Proprietăți"])).toBe("ANEXA NR. 15, Proprietăți");
    expect(cited(["A", "B", "C"])).toBe("B, C");
    expect(cited(["Doar una"])).toBe("Doar una");
    expect(cited(["CAPITOLUL 6 — Referințe", "Standarde", "Liste"])).toBe("CAPITOLUL 6 — Referințe, Liste");
    expect(cited(["Partea a ll-a PROIECTAREA", "5. Coloane uscate", "RACORD INCENDIU"])).toBe(
      "5. Coloane uscate, RACORD INCENDIU",
    );
    expect(cited(["Ceva", "ANEXA NR. 26"])).toBe("ANEXA NR. 26");
    expect(cited(["Secțiunea II", "Ceva"])).toBe("Secțiunea II, Ceva");
    expect(cited(["SECŢIUNEA 3", "Ceva"])).toBe("SECŢIUNEA 3, Ceva"); // with the cedilla
    expect(cited([])).toBe("");
  });

  it("does not call a bare number a point, but keeps an annex's own numbering", () => {
    for (const label of ["7", "58", "25–25.2", "50"]) {
      expect(locator(chunk({ label }))).toBe("ANEXA NR. 15, Proprietăți");
    }
    expect(locator(chunk({ label: "A.10. 2.3.26" }))).toBe("A.10. 2.3.26");
  });

  it("cuts a long heading or title", () => {
    const cut = locator(chunk({ label: "", articles: [], heading_path: ["x".repeat(200)], caption: "y".repeat(200) }));
    expect(cut).toBe("x".repeat(LOCATOR_CHARS) + "…, " + "y".repeat(LOCATOR_CHARS) + "…");
  });

  it("cites a chunk from before labels were kept by its numbers", () => {
    expect(locator(chunk({ label: null, caption: null, articles: ["1.2", "1.3", "1.4"] }))).toBe("pct. 1.2–1.4");
    expect(locator(chunk({ label: null, caption: null, articles: ["1.2"] }))).toBe("pct. 1.2");
  });
});

describe("citation", () => {
  it("is the norm, the place, the title, the edition, the gazette and the page", () => {
    expect(citation(chunk())).toBe(`P118/2-2013, pct. 7.10 — ${TITLE} (ed. 2013, PDF p. 36)`);
    expect(citation(chunk({ gazette: "M. Of. nr. 595 bis/2013" }))).toMatch(
      /\(ed\. 2013, M\. Of\. nr\. 595 bis\/2013, PDF p\. 36\)$/,
    );
    expect(citation(chunk({ pdf_pages: [95, 96] }))).toMatch(/\(ed\. 2013, PDF p\. 95–96\)$/u);
  });

  it("leaves out what is not known", () => {
    expect(citation(chunk({ pdf_pages: null }))).toBe(`P118/2-2013, pct. 7.10 — ${TITLE} (ed. 2013)`);
    expect(citation(chunk({ pdf_pages: [] }))).toBe(`P118/2-2013, pct. 7.10 — ${TITLE} (ed. 2013)`);
    expect(citation(chunk({ label: "", articles: [], heading_path: [] }))).toBe(
      `P118/2-2013 — ${TITLE} (ed. 2013, PDF p. 36)`,
    );
    expect(citation(chunk({ doc_title: "" }))).toBe("P118/2-2013, pct. 7.10 (ed. 2013, PDF p. 36)");
    expect(reference(chunk({ label: "", articles: [], heading_path: [] }))).toBe("P118/2-2013");
  });
});

describe("statusWords", () => {
  it("says the three statuses in words", () => {
    expect(statusWords(chunk())).toBe("în vigoare");
    expect(statusWords(chunk({ status: "unverified" }))).toBe("neverificat");
    expect(statusWords(chunk({ status: "repealed", repealed_on: "2026-08-25" }))).toBe("abrogat din 25.08.2026");
  });

  it("names the acts that changed the norm", () => {
    expect(statusWords(chunk({ amended_by: ["Ordinul 6026/2018", "Ordinul 1/2020"] }))).toBe(
      "în vigoare; text original, modificat ulterior prin: Ordinul 6026/2018; Ordinul 1/2020",
    );
  });

  it("gives a status it does not know as it is", () => {
    expect(statusWords(chunk({ status: "unknown" }))).toBe("unknown");
  });

  it("fails on a day that is not one, as date.fromisoformat does", () => {
    expect(() => statusWords(chunk({ status: "repealed", repealed_on: "25.08.2026" }))).toThrow();
    expect(() => statusWords(chunk({ status: "repealed", repealed_on: "2026-02-30" }))).toThrow();
    expect(() => statusWords(chunk({ status: "repealed", repealed_on: "0000-01-01" }))).toThrow();
  });
});

describe("figuresNote", () => {
  it("says the figures are linked below, without the captions the list gives, when they are", () => {
    const pictured = chunk({ images: [figure(4, "Figura 1"), figure(5, "")] });
    expect(figuresNote(pictured, false, true)).toBe(`Figuri: 2 (PDF p. 4–5), ${FIGURES_LINKED}`);
    expect(figuresNote(chunk(), false, true)).toBe("");
  });

  it("says nothing of a chunk without pictures", () => {
    expect(figuresNote(chunk())).toBe("");
  });

  it("gives the number, the pages and whether the reader sees them", () => {
    const note = figuresNote(chunk({ images: [figure(36, ""), figure(37, ""), figure(36, "")] }));
    expect(note).toBe(`Figuri: 3 (PDF p. 36–37), ${FIGURES_SHOWN}`);
    expect(figuresNote(chunk({ images: [figure(null, "")] }))).toBe(`Figuri: 1, ${FIGURES_SHOWN}`);
  });

  it("says when the reader is not shown them, and nothing else differs", () => {
    const pictured = chunk({ images: [figure(4, "Figura 1")] });
    const note = figuresNote(pictured, false);
    expect(note).toContain(FIGURES_NOT_SHOWN);
    expect(note).not.toContain(FIGURES_SHOWN);
    expect(note.replace(FIGURES_NOT_SHOWN, FIGURES_SHOWN)).toBe(figuresNote(pictured));
  });

  it("quotes the start of the first captions and counts the rest", () => {
    const note = figuresNote(
      chunk({ images: [figure(4, "Figura  1\nPerete"), figure(4, ""), figure(4, "y".repeat(200))] }),
    );
    expect(note.endsWith('; legende: "Figura 1 Perete", "' + "y".repeat(IMAGE_CAPTION_CHARS) + '…"')).toBe(true);
    const many = figuresNote(chunk({ images: Array.from({ length: 7 }, (_, n) => figure(4, `Figura ${n + 1}`)) }));
    expect(many).toBe(`Figuri: 7 (PDF p. 4), ${FIGURES_SHOWN}; legende: "Figura 1", "Figura 2", "Figura 3", și alte 4`);
  });
});

describe("pagesNote", () => {
  it("gives the pages as Python does", () => {
    for (const [pages, said] of GOLDEN.pages) {
      expect(pagesNote(pages as (number | null)[] | null)).toBe(said);
    }
  });
});

describe("articleNumber", () => {
  it("takes the number out of what someone wrote, as Python does", () => {
    for (const [written, number] of GOLDEN.articleNumbers) expect(articleNumber(written!), written).toBe(number);
  });

  it("does not take a Romanian letter for the end of a word before the letter of an annex", () => {
    expect(articleNumber("ordonanța. 7.10")).toBe("7.10");
    expect(articleNumber("legea.A.7.10")).toBe("A.7.10");
  });
});

describe("normalizeRomanian", () => {
  it("turns the cedilla letters into the comma ones and composes", () => {
    expect(normalizeRomanian("şţŞŢ")).toBe("șțȘȚ");
    expect(normalizeRomanian("î")).toBe("î");
  });
});
