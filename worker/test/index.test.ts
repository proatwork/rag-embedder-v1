// The Worker as a client reaches it: the key check, then MCP over HTTP at /mcp with D1 behind it.
import { createExecutionContext, waitOnExecutionContext } from "cloudflare:test";
import { env } from "cloudflare:workers";
import { beforeAll, describe, expect, it } from "vitest";
import { FIGURE_TOOL_NAME } from "../src/figure";
import worker, { MCP_PATH } from "../src/index";
import { EMBEDDING_DIMENSIONS } from "../src/store";

const URL_BASE = "http://localhost"; // a Host the handler's default allowlist accepts
const PROTOCOL_VERSION = "2025-06-18";
const VECTOR = Array.from({ length: EMBEDDING_DIMENSIONS }, () => 0.1);

const FIGURE_ID = "p118-2-2013/fig-12";
const FIGURE_KEY = "p118-2-2013/fig-12-abc.png";
const FIGURES_BASE_URL = "https://figures.example.test";
const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47]);
// The test config leaves the address empty; Env types it as the deployed one.
const WITH_LINKS = { FIGURES_BASE_URL } as unknown as Partial<Env>;

const fakeAi = { run: async () => ({ data: [VECTOR] }) } as unknown as Ai;
const fakeIndex = {
  query: async () => ({ count: 1, matches: [{ id: "p118-2-2013:art-2.56:0", score: 0.9 }] }),
} as unknown as VectorizeIndex;

function testEnv(overrides: Partial<Env> = {}): Env {
  return { ...env, AI: fakeAi, VECTORIZE: fakeIndex, ...overrides };
}

async function call(body: unknown, key: string | null = env.MCP_KEY, overrides: Partial<Env> = {}): Promise<Response> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json, text/event-stream",
    "MCP-Protocol-Version": PROTOCOL_VERSION,
    Host: new URL(URL_BASE).host, // a real request has it; a Request made here does not, and the handler checks it
  };
  if (key !== null) headers.Authorization = `Bearer ${key}`;
  const request = new Request(URL_BASE + MCP_PATH, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  }) as Request<unknown, IncomingRequestCfProperties>;
  const ctx = createExecutionContext();
  const response = await worker.fetch(request, testEnv(overrides), ctx);
  await waitOnExecutionContext(ctx);
  return response;
}

const initialize = {
  jsonrpc: "2.0",
  id: 1,
  method: "initialize",
  params: { protocolVersion: PROTOCOL_VERSION, capabilities: {}, clientInfo: { name: "test", version: "1" } },
};

beforeAll(async () => {
  await env.DB.batch([
    env.DB.prepare(
      "INSERT INTO norms (doc_key, code, title, edition_year, status) VALUES ('P118-II', 'P118/2-2013', 'Instalații de stingere', 2013, 'in_force')",
    ),
    env.DB.prepare(
      `INSERT INTO chunks (id, doc_key, seq, kind, label, article, articles, text, embed_text)
       VALUES ('p118-2-2013:art-2.56:0', 'P118-II', 1, 'article', 'Art. 2.56', '2.56', '["2.56"]', 'Textul articolului.', 'P118/2-2013 › Art. 2.56\nTextul articolului.')`,
    ),
    env.DB.prepare("INSERT INTO chunk_articles (chunk_id, number) VALUES ('p118-2-2013:art-2.56:0', '2.56')"),
    env.DB.prepare(
      `INSERT INTO figures (id, doc_key, r2_key, sha256, page, width, height, caption)
       VALUES ('${FIGURE_ID}', 'P118-II', '${FIGURE_KEY}', 'sha', 36, 1, 1, 'Figura 12')`,
    ),
    env.DB.prepare(
      `INSERT INTO chunk_figures (chunk_id, figure_id, position) VALUES ('p118-2-2013:art-2.56:0', '${FIGURE_ID}', 1)`,
    ),
  ]);
  await env.FIGURES.put(FIGURE_KEY, PNG);
});

describe("the key", () => {
  it("is refused when missing", async () => {
    const response = await call(initialize, null);
    expect(response.status).toBe(401);
    expect(response.headers.get("WWW-Authenticate")).toBe("Bearer");
  });

  it("is refused when wrong", async () => {
    expect((await call(initialize, "x".repeat(40))).status).toBe(401);
  });

  it("answers 500 to everything when the server has no usable key", async () => {
    expect((await call(initialize, env.MCP_KEY, { MCP_KEY: "short" })).status).toBe(500);
  });
});

/** The JSON-RPC reply in `response`: a JSON body, or the first SSE `data:` line (2025 clients get SSE). */
async function reply<T>(response: Response): Promise<T> {
  const body = await response.text();
  if (!(response.headers.get("Content-Type") ?? "").includes("text/event-stream")) return JSON.parse(body) as T;
  const data = body.split("\n").find((line) => line.startsWith("data:"));
  if (data === undefined) throw new Error(`no data in the event stream: ${body}`);
  return JSON.parse(data.slice("data:".length)) as T;
}

interface ToolResult {
  result: { content: { type: string; text?: string; data?: string; mimeType?: string }[]; isError?: boolean };
}

function toolCall(id: number, args: Record<string, string>, name = "search_norms") {
  return { jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: args } };
}

describe("MCP at /mcp", () => {
  it("initializes as the norms server", async () => {
    const response = await call(initialize);
    expect(response.status).toBe(200);
    expect((await reply<{ result: { serverInfo: { name: string } } }>(response)).result.serverInfo.name).toBe("norms");
  });

  it("lists search_norms with the norms from D1, and get_figure", async () => {
    const listed = await reply<{ result: { tools: { name: string; description: string }[] } }>(
      await call({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} }),
    );
    expect(listed.result.tools.map((tool) => tool.name)).toEqual(["search_norms", FIGURE_TOOL_NAME]);
    expect(listed.result.tools[0]?.description).toContain("P118/2-2013");
  });

  it("fetches an article by its number from D1", async () => {
    const result = await reply<ToolResult>(await call(toolCall(3, { article: "Art. 2.56" })));
    expect(result.result.isError).toBeFalsy();
    expect(result.result.content[0]?.text).toContain("Textul articolului.");
  });

  it("searches by meaning through Workers AI and Vectorize", async () => {
    const result = await reply<ToolResult>(await call(toolCall(4, { query: "hidranți interiori" })));
    expect(result.result.isError).toBeFalsy();
    expect(result.result.content[0]?.text).toContain("Art. 2.56");
  });

  it("lists each figure of an excerpt with its public link, and hands one over from R2", async () => {
    const found = await reply<ToolResult>(await call(toolCall(6, { article: "2.56" }), env.MCP_KEY, WITH_LINKS));
    expect(found.result.content[0]?.text).toContain(
      `Figure ${FIGURE_ID} (PDF p. 36): ${FIGURES_BASE_URL}/${FIGURE_KEY} — "Figura 12"`,
    );

    const figure = await reply<ToolResult>(
      await call(toolCall(7, { id: FIGURE_ID }, FIGURE_TOOL_NAME), env.MCP_KEY, WITH_LINKS),
    );
    expect(figure.result.isError).toBeFalsy();
    expect(figure.result.content[1]).toEqual({ type: "image", data: "iVBORw==", mimeType: "image/png" });
  });

  it("tells the model when the embedding model cannot be used", async () => {
    const failingAi = {
      run: async () => {
        throw new Error("down");
      },
    } as unknown as Ai;
    const result = await reply<ToolResult>(
      await call(toolCall(5, { query: "hidranți" }), env.MCP_KEY, { AI: failingAi }),
    );
    expect(result.result.isError).toBe(true);
    expect(result.result.content[0]?.text).toContain("Fetching an article");
  });

  it("answers a browser's CORS preflight without the key, and only that", async () => {
    const preflight = (method: string) =>
      new Request(URL_BASE + MCP_PATH, {
        method,
        headers: {
          Host: new URL(URL_BASE).host,
          Origin: URL_BASE,
          "Access-Control-Request-Method": "POST",
          "Access-Control-Request-Headers": "authorization, content-type",
        },
      }) as Request<unknown, IncomingRequestCfProperties>;
    const answered = await worker.fetch(preflight("OPTIONS"), testEnv(), createExecutionContext());
    expect(answered.status).toBeLessThan(300);
    expect(answered.headers.get("Access-Control-Allow-Origin")).not.toBeNull();
    expect((await worker.fetch(preflight("GET"), testEnv(), createExecutionContext())).status).toBe(401);
  });

  it("answers 404 off the MCP path", async () => {
    const request = new Request(URL_BASE + "/other", {
      headers: { Authorization: `Bearer ${env.MCP_KEY}`, Host: new URL(URL_BASE).host },
    }) as Request<unknown, IncomingRequestCfProperties>;
    expect((await worker.fetch(request, testEnv(), createExecutionContext())).status).toBe(404);
  });
});
