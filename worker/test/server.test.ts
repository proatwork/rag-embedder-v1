// The MCP server (the port of mcp_server.py's tool parts), driven over an in-memory transport with plain JSON-RPC:
// what a client is told of the tool, and what a call is answered with.
import { InMemoryTransport, type JSONRPCMessage, type McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FIGURE_TOOL_NAME, MAX_FIGURE_BYTES, NO_FIGURE_ID, PNG_MEDIA_TYPE, TOO_LARGE } from "../src/figure";
import { createServer, FIGURE_FAILED, SEARCH_FAILED, SEARCH_UNAVAILABLE, SERVER_INSTRUCTIONS } from "../src/server";
import { TOOL_NAME } from "../src/tool";
import { EmbeddingUnavailable, type Embedder, type FigureFile, type Store } from "../src/types";
import { GOLDEN } from "./golden";
import { FakeEmbedder, FakeStore } from "./fakes";

const PROTOCOL_VERSION = "2025-11-25";
const INTERNAL_ADDRESS = "http://10.0.0.5:1234/v1/embeddings"; // what an error inside a search may name

interface Response {
  id: number;
  result?: Record<string, unknown>;
  error?: { code: number; message: string };
}

/** A client of `server` that speaks JSON-RPC by hand, after the handshake. */
async function connect(server: McpServer) {
  const [client, side] = InMemoryTransport.createLinkedPair();
  const waiting = new Map<number, (response: Response) => void>();
  client.onmessage = (message: JSONRPCMessage) => {
    const response = message as unknown as Response;
    if (typeof response.id === "number") waiting.get(response.id)?.(response);
  };
  await server.connect(side);
  await client.start();
  let lastId = 0;
  const request = (method: string, params: Record<string, unknown> = {}) =>
    new Promise<Response>((resolve) => {
      const id = ++lastId;
      waiting.set(id, resolve);
      void client.send({ jsonrpc: "2.0", id, method, params } as JSONRPCMessage);
    });
  const initialized = await request("initialize", {
    protocolVersion: PROTOCOL_VERSION,
    capabilities: {},
    clientInfo: { name: "test", version: "1" },
  });
  await client.send({ jsonrpc: "2.0", method: "notifications/initialized" } as JSONRPCMessage);
  return { initialized, request };
}

async function callTool(
  args: Record<string, unknown>,
  embedder: Embedder = new FakeEmbedder(),
  name = TOOL_NAME,
  store: Store = new FakeStore(),
) {
  const { request } = await connect(await createServer(store, embedder));
  return request("tools/call", { name, arguments: args });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("the server", () => {
  it("names itself and says where its rules are", async () => {
    const { initialized } = await connect(await createServer(new FakeStore(), new FakeEmbedder()));
    expect(initialized.result).toMatchObject({
      serverInfo: { name: GOLDEN.server.name, title: GOLDEN.server.title, version: GOLDEN.server.version },
      instructions: GOLDEN.server.instructions,
      capabilities: { tools: expect.any(Object) },
    });
    expect(SERVER_INSTRUCTIONS).toBe(GOLDEN.server.instructions);
  });

  it("lists search_norms, described as Python describes it but for the rule on figures, and get_figure", async () => {
    const { request } = await connect(await createServer(new FakeStore(), new FakeEmbedder()));
    const tools = (await request("tools/list")).result?.tools as Record<string, unknown>[];
    expect(tools.map((listed) => listed.name)).toEqual([TOOL_NAME, FIGURE_TOOL_NAME]);
    const [tool, figureTool] = tools;
    expect(tool).toMatchObject({
      name: GOLDEN.tool.name,
      title: GOLDEN.tool.title,
      description: GOLDEN.tool.description,
    });
    expect(figureTool?.annotations).toEqual({ readOnlyHint: true, idempotentHint: true, openWorldHint: false });
    expect((figureTool?.inputSchema as { properties: Record<string, unknown> }).properties).toEqual({
      id: { type: "string", description: expect.stringContaining(TOOL_NAME) },
    });
    expect(tool?.annotations).toEqual({ readOnlyHint: true, idempotentHint: true, openWorldHint: false });
    const { $schema, ...schema } = tool?.inputSchema as Record<string, unknown>;
    expect($schema).toBe("https://json-schema.org/draft/2020-12/schema"); // the one key Python's schema does not have
    expect(schema).toEqual(GOLDEN.tool.inputSchema);
  });

  it("reads the norms from the store once, when it is made", async () => {
    const store = new FakeStore();
    await createServer(store, new FakeEmbedder());
    expect(store.calls).toEqual([{ method: "norms", args: [] }]);
  });

  it("can be served by the agents SDK's handler, as index.ts does", () => {
    const store: Store = new FakeStore();
    expect(typeof createMcpHandler(() => createServer(store, new FakeEmbedder()))).toBe("function");
  });
});

describe("a call", () => {
  it("fetches an article by its number", async () => {
    const expected = GOLDEN.calls.find(
      (c) => JSON.stringify(c.arguments) === JSON.stringify({ article: "pct. 7.10" }),
    )!;
    const response = await callTool({ article: "pct. 7.10" }, new FakeEmbedder(new EmbeddingUnavailable("down")));
    expect(response.result).toEqual({ content: [{ type: "text", text: expected.text }], isError: false });
  });

  it("searches by meaning", async () => {
    const args = { query: "pereți antifoc", count: 3 };
    const expected = GOLDEN.calls.find((c) => JSON.stringify(c.arguments) === JSON.stringify(args))!;
    const response = await callTool(args);
    expect(response.result).toEqual({ content: [{ type: "text", text: expected.text }], isError: false });
  });

  it("that cannot be run is an error that says why, whatever types the model sent", async () => {
    for (const args of [
      {},
      { query: 7 },
      { query: "pereți antifoc", count: "many" },
      { article: "99.9" },
      { query: "pere\u021bi antifoc", norm: "X 9" },
    ]) {
      const expected = GOLDEN.calls.find((c) => JSON.stringify(c.arguments) === JSON.stringify(args))!;
      const response = await callTool(args);
      expect(response.result, JSON.stringify(args)).toEqual({
        content: [{ type: "text", text: expected.text }],
        isError: true,
      });
    }
  });

  it("without the embedding model is told the search is unavailable, and the reason goes to the log", async () => {
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    const response = await callTool(
      { query: "pereți antifoc" },
      new FakeEmbedder(new EmbeddingUnavailable(`no answer at ${INTERNAL_ADDRESS}`)),
    );
    expect(response.result).toEqual({ content: [{ type: "text", text: SEARCH_UNAVAILABLE }], isError: true });
    expect(SEARCH_UNAVAILABLE).toBe(GOLDEN.searchUnavailable);
    expect(log).toHaveBeenCalledWith(`${TOOL_NAME}: no answer at ${INTERNAL_ADDRESS}`);
  });

  it("that fails in any other way is told only that it failed", async () => {
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    const failure = new Error(`boom at ${INTERNAL_ADDRESS}`);
    const response = await callTool({ query: "pereți antifoc" }, new FakeEmbedder(failure));
    expect(response.result).toEqual({ content: [{ type: "text", text: SEARCH_FAILED }], isError: true });
    expect(JSON.stringify(response)).not.toContain(INTERNAL_ADDRESS);
    expect(SEARCH_FAILED).toBe(GOLDEN.searchFailed);
    expect(log).toHaveBeenCalledWith(failure);
  });

  it("of another tool is refused by the SDK, not answered by the tool as Python's server does", async () => {
    const response = await callTool({ query: "pereți antifoc" }, new FakeEmbedder(), "read_norm");
    expect(response.error?.message).toContain("Tool read_norm not found");
  });

  it("stands alone: every call numbers its excerpts from 1", async () => {
    const { request } = await connect(await createServer(new FakeStore(), new FakeEmbedder()));
    const first = await request("tools/call", { name: TOOL_NAME, arguments: { article: "2.3.1" } });
    const again = await request("tools/call", { name: TOOL_NAME, arguments: { article: "2.3.1" } });
    expect(again.result).toEqual(first.result);
    expect(JSON.stringify(again.result)).toContain("[1] T 1/2025");
  });
});

describe("a call of get_figure", () => {
  const PNG = new Uint8Array([0x89, 0x50, 0x4e, 0x47]);
  const FILE: FigureFile = {
    figure: { id: "t-1-2025/fig-1", url: "https://f.example.test/1.png", page: 4, width: 1, height: 1, caption: "" },
    code: "T 1/2025",
    size: PNG.length,
    bytes: PNG.buffer,
  };

  it("answers with the figure's header and the picture as an image", async () => {
    const store = new FakeStore(undefined, new Map([[FILE.figure.id, FILE]]));
    const response = await callTool({ id: FILE.figure.id }, new FakeEmbedder(), FIGURE_TOOL_NAME, store);
    expect(response.result).toEqual({
      content: [
        { type: "text", text: "Figure t-1-2025/fig-1 · T 1/2025 (PDF p. 4)\nhttps://f.example.test/1.png" },
        { type: "image", data: "iVBORw==", mimeType: PNG_MEDIA_TYPE },
      ],
      isError: false,
    });
    expect(store.calls.at(-1)).toEqual({ method: "figure", args: [FILE.figure.id, MAX_FIGURE_BYTES] });
  });

  it("whatever types the model sent for the id, is answered by the tool and not refused by the SDK", async () => {
    for (const args of [{}, { id: 7 }, { id: null }, { id: "  " }]) {
      const response = await callTool(args, new FakeEmbedder(), FIGURE_TOOL_NAME);
      expect(response.result, JSON.stringify(args)).toEqual({
        content: [{ type: "text", text: NO_FIGURE_ID }],
        isError: true,
      });
    }
  });

  it("of a figure too large to hand over gives its link, as text only", async () => {
    const large = { ...FILE, size: MAX_FIGURE_BYTES + 1 };
    const store = new FakeStore(undefined, new Map([[FILE.figure.id, large]]));
    const response = await callTool({ id: FILE.figure.id }, new FakeEmbedder(), FIGURE_TOOL_NAME, store);
    const content = response.result?.content as { type: string; text: string }[];
    expect(response.result?.isError).toBe(false);
    expect(content).toHaveLength(1);
    expect(content[0]?.text).toContain(FILE.figure.url);
    expect(content[0]?.text).toContain(TOO_LARGE.split("{size}")[0]);
  });

  it("for an unknown figure is an error with text only", async () => {
    const response = await callTool({ id: "x" }, new FakeEmbedder(), FIGURE_TOOL_NAME);
    expect(response.result).toMatchObject({ isError: true, content: [{ type: "text" }] });
    expect((response.result?.content as unknown[]).length).toBe(1);
  });

  it("that fails is told only that it failed, and the reason goes to the log", async () => {
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    const failure = new Error(`boom at ${INTERNAL_ADDRESS}`);
    const store = new FakeStore(undefined, new Map(), failure);
    const response = await callTool({ id: "x" }, new FakeEmbedder(), FIGURE_TOOL_NAME, store);
    expect(response.result).toEqual({ content: [{ type: "text", text: FIGURE_FAILED }], isError: true });
    expect(JSON.stringify(response)).not.toContain(INTERNAL_ADDRESS);
    expect(log).toHaveBeenCalledWith(failure);
  });
});
