// The MCP server (the port of mcp_server.py's tool parts), driven over an in-memory transport with plain JSON-RPC:
// what a client is told of the tool, and what a call is answered with.
import { InMemoryTransport, type JSONRPCMessage, type McpServer } from "@modelcontextprotocol/server";
import { createMcpHandler } from "agents/mcp/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createServer, SEARCH_FAILED, SEARCH_UNAVAILABLE, SERVER_INSTRUCTIONS } from "../src/server";
import { TOOL_NAME } from "../src/tool";
import { EmbeddingUnavailable, type Embedder, type Store } from "../src/types";
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

async function callTool(args: Record<string, unknown>, embedder: Embedder = new FakeEmbedder(), name = TOOL_NAME) {
  const { request } = await connect(await createServer(new FakeStore(), embedder));
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

  it("lists one tool, described as Python describes it, with the rules after its description", async () => {
    const { request } = await connect(await createServer(new FakeStore(), new FakeEmbedder()));
    const tools = (await request("tools/list")).result?.tools as Record<string, unknown>[];
    expect(tools).toHaveLength(1);
    const [tool] = tools;
    expect(tool).toMatchObject({
      name: GOLDEN.tool.name,
      title: GOLDEN.tool.title,
      description: GOLDEN.tool.description,
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
