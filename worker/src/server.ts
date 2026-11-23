// The search as an MCP server: search_norms as mcp_server.py serves it (every call stands alone, and the rules for
// searching and citing follow the tool's description, which every client shows its model), and get_figure, which
// only the Worker has, to hand the model a figure an excerpt links.
import { McpServer, type CallToolResult } from "@modelcontextprotocol/server";
import { z } from "zod";
import {
  ARG_FIGURE_ID,
  FIGURE_ID_HELP,
  FIGURE_TOOL_DESCRIPTION,
  FIGURE_TOOL_NAME,
  FIGURE_TOOL_TITLE,
  loadFigure,
} from "./figure";
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
const IMAGE_CONTENT = "image";
// What a client is told when a search fails; the reason itself, which may name an internal address, goes to the log.
export const SEARCH_UNAVAILABLE =
  "The search by meaning cannot be used right now: the server that embeds the question did not answer. " +
  `Fetching an article or point by its number (\`${ARG_ARTICLE}\`) still works.`;
export const SEARCH_FAILED =
  "The search failed on the server, for a reason written to its own output. Nothing was searched.";
export const FIGURE_FAILED =
  "Loading the figure failed on the server, for a reason written to its own output. Give the user its link instead.";
const READ_ONLY = { readOnlyHint: true, idempotentHint: true, openWorldHint: false } as const;

function textResult(text: string, isError = false): CallToolResult {
  return { content: [{ type: TEXT_CONTENT, text }], isError };
}

/** The MCP server with its two tools, over `store` and `embedder`; a search gets DEFAULT_TOP excerpts unless it says. */
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
      annotations: READ_ONLY,
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
  server.registerTool(
    FIGURE_TOOL_NAME,
    {
      title: FIGURE_TOOL_TITLE,
      description: FIGURE_TOOL_DESCRIPTION,
      // Of any type, as search_norms' arguments, so that loadFigure answers what the model sends.
      inputSchema: z.object({
        [ARG_FIGURE_ID]: z.unknown().optional().meta({ type: "string", description: FIGURE_ID_HELP }),
      }),
      annotations: READ_ONLY,
    },
    async (args) => {
      try {
        const reply = await loadFigure(store, args);
        const result = textResult(reply.text, reply.isError);
        if (reply.image) result.content.push({ type: IMAGE_CONTENT, ...reply.image });
        return result;
      } catch (error) {
        console.error(error);
        return textResult(FIGURE_FAILED, true);
      }
    },
  );
  return server;
}
