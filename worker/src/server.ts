// The search as an MCP server with its one tool, search_norms, as mcp_server.py serves it: every call stands alone,
// and the rules for searching and citing follow the tool's description, which every client shows its model.
import { McpServer, type CallToolResult } from "@modelcontextprotocol/server";
import { loadRules } from "./rules";
import { DEFAULT_TOP } from "./search";
import { ARG_ARTICLE, EXCERPT_SEPARATOR, SearchCall, TOOL_DESCRIPTION, TOOL_NAME, inputSchema } from "./tool";
import { EmbeddingUnavailable, type Embedder, type Store } from "./types";

export const SERVER_NAME = "norms";
export const SERVER_TITLE = "Romanian fire-safety norms";
export const SERVER_VERSION = "1.0";
export const TOOL_TITLE = "Search the fire-safety norms";
export const SERVER_INSTRUCTIONS = `Call ${TOOL_NAME} for any question about the Romanian fire-safety norms; its description says how to search and how to cite.`;
const TEXT_CONTENT = "text";
// What a client is told when a search fails; the reason itself, which may name an internal address, goes to the log.
export const SEARCH_UNAVAILABLE =
  "The search by meaning cannot be used right now: the server that embeds the question did not answer. " +
  `Fetching an article or point by its number (\`${ARG_ARTICLE}\`) still works.`;
export const SEARCH_FAILED =
  "The search failed on the server, for a reason written to its own output. Nothing was searched.";

function textResult(text: string, isError = false): CallToolResult {
  return { content: [{ type: TEXT_CONTENT, text }], isError };
}

/** The MCP server with its one tool, over `store` and `embedder`; a call gets DEFAULT_TOP excerpts unless it says. */
export async function createServer(store: Store, embedder: Embedder): Promise<McpServer> {
  const norms = await store.norms();
  const server = new McpServer(
    { name: SERVER_NAME, title: SERVER_TITLE, version: SERVER_VERSION },
    { instructions: SERVER_INSTRUCTIONS },
  );
  server.registerTool(
    TOOL_NAME,
    {
      title: TOOL_TITLE,
      description: TOOL_DESCRIPTION + EXCERPT_SEPARATOR + loadRules(),
      inputSchema: inputSchema(norms, DEFAULT_TOP),
      annotations: { readOnlyHint: true, idempotentHint: true, openWorldHint: false },
    },
    async (args) => {
      try {
        const reply = await new SearchCall(store, embedder, norms, DEFAULT_TOP).reply(args);
        return textResult(reply.text, reply.isError);
      } catch (error) {
        // Whatever went wrong, the client is told only that it did: the SDK would send it the error's own text.
        if (error instanceof EmbeddingUnavailable) {
          console.error(`${TOOL_NAME}: ${error.message}`);
          return textResult(SEARCH_UNAVAILABLE, true);
        }
        console.error(error);
        return textResult(SEARCH_FAILED, true);
      }
    },
  );
  return server;
}
