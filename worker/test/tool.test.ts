// One call of search_norms (the port of search_tool.SearchSession as mcp_server.py runs it), against what Python
// answers the same arguments with over the same chunks (golden.ts).
import { describe, expect, it } from "vitest";
import { pyRepr, SearchCall, BAD_COUNT, NOT_RUN_UNLIMITED, countAsked, excerpt, figureLine } from "../src/tool";
import { FIGURES_LINKED, figuresNote, format } from "../src/search";
import { EmbeddingUnavailable } from "../src/types";
import { GOLDEN, RECORDS } from "./golden";
import { FakeEmbedder, FakeStore } from "./fakes";

async function call(args: Record<string, unknown>, embedder = new FakeEmbedder()) {
  const store = new FakeStore();
  const reply = await new SearchCall(store, embedder, await store.norms()).reply(args);
  return { reply, store, embedder };
}

describe("every call answers as Python does", () => {
  for (const expected of GOLDEN.calls) {
    it(JSON.stringify(expected.arguments), async () => {
      const { reply, embedder } = await call(expected.arguments as Record<string, unknown>);
      expect(reply).toEqual({ text: expected.text, isError: expected.isError });
      expect(embedder.asked).toEqual(expected.embedded); // the question as embedded: spelled as the chunks are
    });
  }
});

describe("search by meaning", () => {
  it("normalizes the question for the embedder but shows it as it was asked", async () => {
    const { reply, embedder, store } = await call({ query: "uşi rezistente la foc", count: 2 });
    expect(embedder.asked).toEqual(["uși rezistente la foc"]);
    expect(reply.text.startsWith('Searched for "uşi rezistente la foc": 2 excerpts')).toBe(true);
    expect(store.calls.at(-1)).toMatchObject({ method: "nearest", args: [expect.any(Array), 2, undefined] });
  });

  it("asks the store for the norm's code as the norms spell it", async () => {
    const { store } = await call({ query: "pereți antifoc", norm: " t 2/2013 " });
    expect(store.calls.at(-1)).toMatchObject({ method: "nearest", args: [expect.any(Array), 5, "T 2/2013"] });
  });

  it("finds nothing and says so when nothing is close", async () => {
    const store = new FakeStore([]);
    const reply = await new SearchCall(store, new FakeEmbedder(), []).reply({ query: "nimic" });
    expect(reply).toEqual({ text: 'Searched for "nimic": 0 excerpts', isError: false });
  });

  it("lets the embedder's failure through for the server to tell", async () => {
    const failure = new EmbeddingUnavailable("no answer");
    await expect(call({ query: "pereți antifoc" }, new FakeEmbedder(failure))).rejects.toBe(failure);
  });
});

describe("article lookup", () => {
  it("uses the store's lookup by number and never the embedder", async () => {
    const { store, embedder } = await call({ article: "Art. 2.3.1.", norm: "t 1/2025" });
    expect(store.calls.at(-1)).toEqual({ method: "withNumber", args: ["2.3.1", "T 1/2025"] });
    expect(embedder.asked).toEqual([]);
  });

  it("works while the embedder cannot be used", async () => {
    const { reply } = await call({ article: "7.10" }, new FakeEmbedder(new EmbeddingUnavailable("down")));
    expect(reply.isError).toBe(false);
    expect(reply.text.startsWith("Searched the article or point 7.10: 1 excerpt")).toBe(true);
  });
});

describe("count", () => {
  it("refuses what JSON cannot carry but a caller can", async () => {
    for (const count of [Infinity, -Infinity, NaN]) {
      const { reply } = await call({ query: "pereți antifoc", count });
      expect(reply).toEqual({ text: format(NOT_RUN_UNLIMITED, { problem: BAD_COUNT }), isError: true });
    }
  });

  it("takes a whole number, as a number or as text", () => {
    expect(countAsked(undefined, 5)).toBe(5);
    expect(countAsked("", 5)).toBe(5);
    expect(countAsked(" 2 ", 5)).toBe(2);
    expect(countAsked("1_0", 5)).toBe(10);
    expect(() => countAsked(0, 5)).toThrow(BAD_COUNT);
    expect(() => countAsked(true, 5)).toThrow(BAD_COUNT);
  });
});

describe("pyRepr", () => {
  it("writes values as Python's repr does", () => {
    expect(pyRepr("X 9")).toBe("'X 9'");
    expect(pyRepr("O'Brien")).toBe(`"O'Brien"`);
    expect(pyRepr(`a'b"c`)).toBe(`'a\\'b"c'`);
    expect(pyRepr("tab\there\n")).toBe("'tab\\there\\n'");
    expect(pyRepr("ș​x ")).toBe("'ș\\u200bx\\xa0'");
    expect(pyRepr(["T 1", 5, 2.5, true, null, { code: "T" }])).toBe("['T 1', 5, 2.5, True, None, {'code': 'T'}]");
    expect(pyRepr(1.5e-7)).toBe("1.5e-07");
    expect(pyRepr(0.0001)).toBe("0.0001");
  });
});

describe("figures with links", () => {
  const URL = "https://figures.example.test/t-1-2025/fig-1.png";
  const pictured = RECORDS.pictured!;
  const linked = {
    ...pictured,
    images: pictured.images.map((image, place) => (place ? image : { ...image, url: URL })),
  };

  const allLinked = {
    ...pictured,
    images: pictured.images.map((image, place) => ({ ...image, url: `${URL}?${place}` })),
  };

  it("are listed one per line under the status, with their id, page, link and caption", () => {
    const lines = excerpt(1, allLinked, "Textul.", false).split("\n");
    expect(lines[1]).toBe(`Statut: în vigoare · Figuri: ${pictured.images.length} (PDF p. 4–5), ${FIGURES_LINKED}`);
    expect(lines[2]).toBe(`Figure t-1-2025/fig-1 (PDF p. 4): ${URL}?0 — "Figura 1 Perete"`);
    expect(lines.slice(2, -1)).toHaveLength(pictured.images.length);
    expect(lines.at(-1)).toBe("Textul.");
  });

  it("keep the captions in the note when only some figures have a link, and list only those", () => {
    const lines = excerpt(1, linked, "Textul.", false).split("\n");
    expect(lines[1]).toBe(`Statut: în vigoare · ${figuresNote(pictured, false)}`); // as Python gives it
    expect(lines[2]).toBe(`Figure t-1-2025/fig-1 (PDF p. 4): ${URL} — "Figura 1 Perete"`);
    expect(lines.slice(3)).toEqual(["Textul."]);
  });

  it("reach the model through a call", async () => {
    const store = new FakeStore([allLinked]);
    const reply = await new SearchCall(store, new FakeEmbedder(), await store.norms()).reply({ article: "9.1" });
    expect(reply).toEqual({
      text: ["Searched the article or point 9.1: 1 excerpt", excerpt(1, allLinked, allLinked.embed_text, false)].join(
        "\n\n",
      ),
      isError: false,
    });
    expect(reply.text).toContain(`Figure t-1-2025/fig-1 (PDF p. 4): ${URL}?0`);
  });

  it("leave out what is not known of a figure", () => {
    expect(figureLine({ id: "x/fig-2", url: URL, page: null, width: 1, height: 1, caption: " " })).toBe(
      `Figure x/fig-2: ${URL}`,
    );
  });

  it("change nothing in an excerpt whose figures have no link: it is the one Python gives", () => {
    const text = excerpt(1, pictured, "Textul.", false);
    expect(text).not.toContain("Figure ");
    expect(text).not.toContain(FIGURES_LINKED);
  });
});
