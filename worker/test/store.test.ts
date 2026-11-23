import { env } from "cloudflare:workers";
import { beforeEach, describe, expect, it } from "vitest";
import {
  CloudflareStore,
  EMBEDDING_DIMENSIONS,
  EMBEDDING_MODEL,
  QUERY_INSTRUCTION,
  WorkersAiEmbedder,
} from "../src/store";
import { EmbeddingUnavailable } from "../src/types";

const BASE = "https://figures.example.test";

interface QueryCall {
  vector: number[];
  options: Record<string, unknown>;
}

function fakeIndex(matches: { id: string; score: number }[]): { index: VectorizeIndex; calls: QueryCall[] } {
  const calls: QueryCall[] = [];
  const index = {
    query: async (vector: number[], options: Record<string, unknown>) => {
      calls.push({ vector, options });
      return { matches, count: matches.length };
    },
  } as unknown as VectorizeIndex;
  return { index, calls };
}

function storeWith(matches: { id: string; score: number }[] = [], base = BASE) {
  const { index, calls } = fakeIndex(matches);
  return { store: new CloudflareStore(env.DB, index, env.FIGURES, base), calls };
}

async function addNorm(docKey: string, code: string, title: string, extra: Partial<Record<string, unknown>> = {}) {
  const row = { edition_year: 2013, status: "in_force", amended_by: "[]", repealed_on: null, gazette: null, ...extra };
  await env.DB.prepare(
    "INSERT INTO norms (doc_key, code, title, edition_year, status, amended_by, repealed_on, gazette) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
  )
    .bind(docKey, code, title, row.edition_year, row.status, row.amended_by, row.repealed_on, row.gazette)
    .run();
}

async function addChunk(
  id: string,
  docKey: string,
  seq: number,
  numbers: string[] = [],
  extra: Partial<Record<string, unknown>> = {},
) {
  const row = {
    kind: "article",
    heading_path: "[]",
    label: null,
    caption: null,
    article: null,
    articles: "[]",
    pdf_pages: null,
    text: `text ${id}`,
    embed_text: `embed ${id}`,
    prev_id: null,
    next_id: null,
    parent_id: null,
    flags: "[]",
    ...extra,
  };
  await env.DB.prepare(
    `INSERT INTO chunks (id, doc_key, seq, kind, heading_path, label, caption, article, articles, pdf_pages, text, embed_text, prev_id, next_id, parent_id, flags)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  )
    .bind(
      id,
      docKey,
      seq,
      row.kind,
      row.heading_path,
      row.label,
      row.caption,
      row.article,
      row.articles,
      row.pdf_pages,
      row.text,
      row.embed_text,
      row.prev_id,
      row.next_id,
      row.parent_id,
      row.flags,
    )
    .run();
  for (const number of numbers) {
    await env.DB.prepare("INSERT INTO chunk_articles (chunk_id, number) VALUES (?, ?)").bind(id, number).run();
  }
}

async function addFigure(
  id: string,
  docKey: string,
  r2Key: string,
  page: number | null,
  width: number,
  height: number,
  caption: string,
  sha256 = "sha", // '' for a figure uploaded without --bucket
) {
  await env.DB.prepare(
    "INSERT INTO figures (id, doc_key, r2_key, sha256, page, width, height, caption) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
  )
    .bind(id, docKey, r2Key, sha256, page, width, height, caption)
    .run();
}

async function link(chunkId: string, figureId: string, position: number) {
  await env.DB.prepare("INSERT INTO chunk_figures (chunk_id, figure_id, position) VALUES (?, ?, ?)")
    .bind(chunkId, figureId, position)
    .run();
}

beforeEach(async () => {
  for (const table of ["chunk_figures", "figures", "chunk_articles", "chunks", "norms"]) {
    await env.DB.prepare(`DELETE FROM ${table}`).run();
  }
  const { objects } = await env.FIGURES.list();
  if (objects.length) await env.FIGURES.delete(objects.map((object) => object.key));
});

describe("CloudflareStore.norms", () => {
  it("lists every norm as code and title, ordered by code and not by doc_key", async () => {
    await addNorm("a-doc", "Z9", "Last by code");
    await addNorm("b-doc", "A1", "First by code");
    await addNorm("c-doc", "M5", "Middle by code");
    const { store } = storeWith();

    expect(await store.norms()).toEqual([
      { code: "A1", title: "First by code" },
      { code: "M5", title: "Middle by code" },
      { code: "Z9", title: "Last by code" },
    ]);
  });

  it("returns an empty list when there are no norms", async () => {
    expect(await storeWith().store.norms()).toEqual([]);
  });
});

describe("CloudflareStore.nearest", () => {
  it("queries Vectorize with topK, no values, no metadata and no filter when no code is given", async () => {
    const { store, calls } = storeWith([]);

    await store.nearest([0.1, 0.2], 7);

    expect(calls).toHaveLength(1);
    expect(calls[0]?.vector).toEqual([0.1, 0.2]);
    expect(calls[0]?.options).toEqual({ topK: 7, returnValues: false, returnMetadata: "none" });
  });

  it("filters on the norm's code when one is given", async () => {
    const { store, calls } = storeWith([]);

    await store.nearest([1], 3, "P118/2-2013");

    expect(calls[0]?.options).toEqual({
      topK: 3,
      returnValues: false,
      returnMetadata: "none",
      filter: { norm: "P118/2-2013" },
    });
  });

  it("returns scored records in Vectorize's order, not D1's", async () => {
    await addNorm("doc", "N1", "Norm one");
    await addChunk("c-a", "doc", 1);
    await addChunk("c-b", "doc", 2);
    await addChunk("c-c", "doc", 3);
    const { store } = storeWith([
      { id: "c-c", score: 0.9 },
      { id: "c-a", score: 0.5 },
      { id: "c-b", score: 0.1 },
    ]);

    const found = await store.nearest([0], 3);

    expect(found.map((s) => [s.record.id, s.score])).toEqual([
      ["c-c", 0.9],
      ["c-a", 0.5],
      ["c-b", 0.1],
    ]);
  });

  it("drops ids that have no chunk in D1 and keeps the rest in order", async () => {
    await addNorm("doc", "N1", "Norm one");
    await addChunk("c-a", "doc", 1);
    await addChunk("c-b", "doc", 2);
    const { store } = storeWith([
      { id: "c-b", score: 0.8 },
      { id: "ghost", score: 0.7 },
      { id: "c-a", score: 0.6 },
    ]);

    const found = await store.nearest([0], 3);

    expect(found.map((s) => s.record.id)).toEqual(["c-b", "c-a"]);
  });

  it("returns nothing, without failing, when Vectorize matches nothing", async () => {
    expect(await storeWith([]).store.nearest([0], 5)).toEqual([]);
  });

  it("joins the norm's fields and parses the JSON columns into the record", async () => {
    await addNorm("doc", "P118/2-2013", "Normativ", {
      edition_year: 2013,
      status: "repealed",
      amended_by: '["Order 1","Order 2"]',
      repealed_on: "2020-01-31",
      gazette: "MO 12",
    });
    await addChunk("c1", "doc", 4, ["2.56"], {
      kind: "article",
      heading_path: '["Part 1","Chapter 2"]',
      label: "Art. 2.56",
      caption: "cap",
      article: "2.56",
      articles: '["2.56","2.57"]',
      pdf_pages: "[3,4]",
      text: "the text",
      embed_text: "the embed text",
      prev_id: "p",
      next_id: "n",
      parent_id: "up",
      flags: '["table"]',
    });
    const { store } = storeWith([{ id: "c1", score: 0.42 }]);

    const [found] = await store.nearest([0], 1);

    expect(found).toEqual({
      score: 0.42,
      record: {
        id: "c1",
        kind: "article",
        doc_key: "doc",
        doc_code: "P118/2-2013",
        doc_title: "Normativ",
        edition_year: 2013,
        status: "repealed",
        amended_by: ["Order 1", "Order 2"],
        repealed_on: "2020-01-31",
        gazette: "MO 12",
        heading_path: ["Part 1", "Chapter 2"],
        label: "Art. 2.56",
        caption: "cap",
        article: "2.56",
        articles: ["2.56", "2.57"],
        pdf_pages: [3, 4],
        text: "the text",
        embed_text: "the embed text",
        prev_id: "p",
        next_id: "n",
        parent_id: "up",
        flags: ["table"],
        images: [],
      },
    });
  });

  it("keeps null pdf_pages, label, repealed_on and gazette as null", async () => {
    await addNorm("doc", "N1", "Norm one");
    await addChunk("c1", "doc", 1);
    const { store } = storeWith([{ id: "c1", score: 1 }]);

    const [found] = await store.nearest([0], 1);

    expect(found?.record).toMatchObject({ pdf_pages: null, label: null, repealed_on: null, gazette: null, images: [] });
  });

  it("attaches each chunk's own figures ordered by position, with encoded urls", async () => {
    await addNorm("doc", "N1", "Norm one");
    await addChunk("c1", "doc", 1);
    await addChunk("c2", "doc", 2);
    await addFigure("n1/fig-1", "doc", "n1/fig 1.png", 5, 100, 200, "first");
    await addFigure("n1/fig-2", "doc", "n1/fig-2.png", null, 30, 40, "second");
    await addFigure("n1/fig-3", "doc", "n1/fig-3.png", 7, 1, 2, "other chunk");
    await link("c1", "n1/fig-1", 2);
    await link("c1", "n1/fig-2", 1);
    await link("c2", "n1/fig-3", 1);
    const { store } = storeWith([
      { id: "c1", score: 0.9 },
      { id: "c2", score: 0.8 },
    ]);

    const [first, second] = await store.nearest([0], 2);

    expect(first?.record.images).toEqual([
      { id: "n1/fig-2", url: `${BASE}/n1/fig-2.png`, page: null, width: 30, height: 40, caption: "second" },
      { id: "n1/fig-1", url: `${BASE}/n1/fig%201.png`, page: 5, width: 100, height: 200, caption: "first" },
    ]);
    expect(second?.record.images).toEqual([
      { id: "n1/fig-3", url: `${BASE}/n1/fig-3.png`, page: 7, width: 1, height: 2, caption: "other chunk" },
    ]);
  });

  it("shares one figure between two chunks", async () => {
    await addNorm("doc", "N1", "Norm one");
    await addChunk("c1", "doc", 1);
    await addChunk("c2", "doc", 2);
    await addFigure("n1/fig-1", "doc", "n1/fig-1.png", 1, 1, 1, "");
    await link("c1", "n1/fig-1", 1);
    await link("c2", "n1/fig-1", 1);
    const { store } = storeWith([
      { id: "c1", score: 0.9 },
      { id: "c2", score: 0.8 },
    ]);

    const found = await store.nearest([0], 2);

    expect(found.map((s) => s.record.images.map((i) => i.id))).toEqual([["n1/fig-1"], ["n1/fig-1"]]);
  });

  it("gives no link to a figure whose PNG is not in the bucket, nor when no address is set", async () => {
    await addNorm("doc", "N1", "Norm one");
    await addChunk("c1", "doc", 1);
    await addFigure("n1/fig-1", "doc", "n1/fig-1.png", 1, 1, 1, "", "");
    await addFigure("n1/fig-2", "doc", "n1/fig-2.png", 1, 1, 1, "");
    await link("c1", "n1/fig-1", 1);
    await link("c1", "n1/fig-2", 2);

    const [found] = await storeWith([{ id: "c1", score: 0.9 }]).store.nearest([0], 1);
    const [unaddressed] = await storeWith([{ id: "c1", score: 0.9 }], "").store.nearest([0], 1);

    expect(found?.record.images.map((image) => image.url)).toEqual(["", `${BASE}/n1/fig-2.png`]);
    expect(unaddressed?.record.images.map((image) => image.url)).toEqual(["", ""]);
  });
});

describe("CloudflareStore.figure", () => {
  const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3]);

  async function stored(sha256 = "sha") {
    await addNorm("doc", "N1", "Norm one");
    await addFigure("n1/fig-1", "doc", "n1/fig 1.png", 4, 10, 20, "Figura 1", sha256);
    await env.FIGURES.put("n1/fig 1.png", PNG);
  }

  it("reads the PNG from the bucket, with the figure, its link and its norm's code", async () => {
    await stored();
    const file = await storeWith().store.figure("n1/fig-1", PNG.length);
    expect(file).toMatchObject({
      code: "N1",
      size: PNG.length,
      figure: { id: "n1/fig-1", url: `${BASE}/n1/fig%201.png`, page: 4, width: 10, height: 20, caption: "Figura 1" },
    });
    expect(new Uint8Array(file!.bytes!)).toEqual(PNG);
  });

  it("leaves the PNG out when it is larger than asked, but says how large it is", async () => {
    await stored();
    const file = await storeWith().store.figure("n1/fig-1", PNG.length - 1);
    expect(file).toMatchObject({ size: PNG.length, bytes: null, code: "N1" });
  });

  it("hands over the PNG without a link when no public address is set", async () => {
    await stored();
    const file = await storeWith([], "").store.figure("n1/fig-1", PNG.length);
    expect(file?.figure.url).toBe("");
    expect(new Uint8Array(file!.bytes!)).toEqual(PNG);
  });

  it("has nothing for an unknown id or a figure not uploaded to the bucket", async () => {
    const { store } = storeWith();
    expect(await store.figure("n1/fig-1", PNG.length)).toBeNull();
    await stored("");
    expect(await store.figure("n1/fig-1", PNG.length)).toBeNull();
  });

  it("fails, as the server's fault, when D1 says the PNG is in the bucket and it is not", async () => {
    await stored();
    await env.FIGURES.delete("n1/fig 1.png");
    await expect(storeWith().store.figure("n1/fig-1", PNG.length)).rejects.toThrow("missing from the bucket");
  });
});

describe("CloudflareStore.withNumber", () => {
  it("returns only chunks holding exactly that number", async () => {
    await addNorm("doc", "N1", "Norm one");
    await addChunk("c1", "doc", 1, ["2.5"]);
    await addChunk("c2", "doc", 2, ["2.56"]);
    await addChunk("c3", "doc", 3, ["12.5", "2.5"]);
    await addChunk("c4", "doc", 4, ["3"]);
    const { store } = storeWith();

    const found = await store.withNumber("2.5");

    expect(found.map((r) => r.id)).toEqual(["c1", "c3"]);
  });

  it("orders by norm doc_key, then by seq, whatever the insertion order or the code order", async () => {
    await addNorm("b-doc", "A1", "Code first, doc_key second");
    await addNorm("a-doc", "Z9", "Code last, doc_key first");
    await addChunk("b-2", "b-doc", 2, ["1"]);
    await addChunk("a-9", "a-doc", 9, ["1"]);
    await addChunk("b-1", "b-doc", 1, ["1"]);
    await addChunk("a-3", "a-doc", 3, ["1"]);
    const { store } = storeWith();

    const found = await store.withNumber("1");

    expect(found.map((r) => r.id)).toEqual(["a-3", "a-9", "b-1", "b-2"]);
  });

  it("restricts to the norm with the given code", async () => {
    await addNorm("a-doc", "Z9", "Other");
    await addNorm("b-doc", "A1", "Wanted");
    await addChunk("a-1", "a-doc", 1, ["4"]);
    await addChunk("b-1", "b-doc", 1, ["4"]);
    const { store } = storeWith();

    const found = await store.withNumber("4", "A1");

    expect(found.map((r) => [r.id, r.doc_code])).toEqual([["b-1", "A1"]]);
  });

  it("returns nothing for an unknown number or an unknown code", async () => {
    await addNorm("doc", "N1", "Norm one");
    await addChunk("c1", "doc", 1, ["4"]);
    const { store } = storeWith();

    expect(await store.withNumber("99")).toEqual([]);
    expect(await store.withNumber("4", "NOPE")).toEqual([]);
  });

  it("returns a chunk once even if it lists several numbers and carries figures", async () => {
    await addNorm("doc", "N1", "Norm one");
    await addChunk("c1", "doc", 1, ["7", "8"]);
    await addFigure("n1/fig-1", "doc", "n1/fig-1.png", 1, 10, 20, "cap");
    await link("c1", "n1/fig-1", 1);
    const { store } = storeWith();

    const found = await store.withNumber("7");

    expect(found).toHaveLength(1);
    expect(found[0]?.images.map((i) => i.id)).toEqual(["n1/fig-1"]);
  });

  it("joins the norm fields into the records", async () => {
    await addNorm("doc", "N1", "Norm one", { amended_by: '["X"]', status: "unverified", edition_year: 1999 });
    await addChunk("c1", "doc", 1, ["7"], { flags: '["a","b"]', articles: '["7"]', pdf_pages: "[1]" });

    const [found] = await storeWith().store.withNumber("7");

    expect(found).toMatchObject({
      doc_code: "N1",
      doc_title: "Norm one",
      edition_year: 1999,
      status: "unverified",
      amended_by: ["X"],
      flags: ["a", "b"],
      articles: ["7"],
      pdf_pages: [1],
    });
  });
});

describe("figure urls", () => {
  async function urlFor(base: string, r2Key: string): Promise<string | undefined> {
    await addNorm("doc", "N1", "Norm one");
    await addChunk("c1", "doc", 1, ["1"]);
    await addFigure("f", "doc", r2Key, 1, 1, 1, "");
    await link("c1", "f", 1);
    const [found] = await storeWith([], base).store.withNumber("1");
    return found?.images[0]?.url;
  }

  it("is empty when no base url is set", async () => {
    expect(await urlFor("", "p118/fig-1.png")).toBe("");
  });

  it("strips trailing slashes from the base url", async () => {
    expect(await urlFor(`${BASE}///`, "p118/fig-1.png")).toBe(`${BASE}/p118/fig-1.png`);
  });

  it("encodes each path segment but keeps the slashes between them", async () => {
    expect(await urlFor(BASE, "norm a/fig #1?.png")).toBe(`${BASE}/norm%20a/fig%20%231%3F.png`);
  });

  it("percent-encodes non-ASCII characters", async () => {
    expect(await urlFor(BASE, "normă/fig.png")).toBe(`${BASE}/norm%C4%83/fig.png`);
  });
});

describe("WorkersAiEmbedder", () => {
  function fakeAi(reply: unknown, calls: unknown[][] = []): Ai {
    return {
      run: async (...args: unknown[]) => {
        calls.push(args);
        if (reply instanceof Error) throw reply;
        return reply;
      },
    } as unknown as Ai;
  }

  it("asks Qwen3 to embed the text as a query, with the instruction, and returns the first vector", async () => {
    const first = Array.from({ length: EMBEDDING_DIMENSIONS }, (_, place) => place / EMBEDDING_DIMENSIONS);
    const calls: unknown[][] = [];
    const embedder = new WorkersAiEmbedder(
      fakeAi(
        {
          shape: [2, EMBEDDING_DIMENSIONS],
          data: [first, first.map((value) => value + 1)],
        },
        calls,
      ),
    );

    const vector = await embedder.embed("hello");

    expect(vector).toEqual(first);
    expect(calls).toEqual([["@cf/qwen/qwen3-embedding-0.6b", { queries: ["hello"], instruction: QUERY_INSTRUCTION }]]);
    expect(EMBEDDING_MODEL).toBe("@cf/qwen/qwen3-embedding-0.6b");
  });

  it("raises EmbeddingUnavailable when the call throws", async () => {
    const embedder = new WorkersAiEmbedder(fakeAi(new Error("quota")));

    await expect(embedder.embed("x")).rejects.toBeInstanceOf(EmbeddingUnavailable);
  });

  it.each([
    ["no data field", { shape: [0, 0] }],
    ["an empty data list", { data: [] }],
    ["an empty first vector", { data: [[]] }],
    ["a vector of another size", { data: [[1, 2, 3]] }],
    ["a null data field", { data: null }],
  ])("raises EmbeddingUnavailable on a reply with %s", async (_name, reply) => {
    const embedder = new WorkersAiEmbedder(fakeAi(reply));

    await expect(embedder.embed("x")).rejects.toBeInstanceOf(EmbeddingUnavailable);
  });
});
