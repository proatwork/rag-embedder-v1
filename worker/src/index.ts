// The norms search as an MCP server on Cloudflare: Streamable HTTP at /mcp, every call stateless, behind the bearer
// key MCP_KEY. Clients of the 2026 protocol get JSON replies; 2025 clients get a short event stream (the handler's
// compatibility path has no JSON mode), which they all read. The tool itself is in server.ts.
//
// Host and Origin: the handler checks them for localhost and *.workers.dev. On a custom domain it checks no Host
// (Cloudflare routes only that domain here, and nothing passes without the key) and refuses browser Origins other than
// localhost; set allowedHostnames / allowedOriginHostnames below if a browser client on another site is ever wanted.
import { createMcpHandler } from "agents/mcp/server";
import { carriesKey, notConfigured, refusedKeyShape, unauthorized, usableKey } from "./auth";
import { createServer } from "./server";
import { CloudflareStore, WorkersAiEmbedder } from "./store";

export const MCP_PATH = "/mcp";
const PREFLIGHT = "OPTIONS"; // a browser never sends the key with it: the handler answers it, with its Origin check
const REFUSED_KEY_LOG = "refused key:"; // followed by the header's shape, to tell a mistyped key from a missing one

export default {
  async fetch(request, env, ctx): Promise<Response> {
    if (!usableKey(env.MCP_KEY)) return notConfigured();
    if (request.method !== PREFLIGHT && !(await carriesKey(request, env.MCP_KEY))) {
      console.warn(REFUSED_KEY_LOG, JSON.stringify(refusedKeyShape(request, env.MCP_KEY)));
      return unauthorized();
    }
    const store = new CloudflareStore(env.DB, env.VECTORIZE, env.FIGURES, env.FIGURES_BASE_URL);
    const embedder = new WorkersAiEmbedder(env.AI);
    const handler = createMcpHandler(() => createServer(store, embedder), { route: MCP_PATH, responseMode: "json" });
    return handler(request, env, ctx);
  },
} satisfies ExportedHandler<Env>;
