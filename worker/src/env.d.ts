// What worker-configuration.d.ts (from `wrangler types`) cannot know: the secret, and the tests' migrations.
interface WorkerSecrets {
  /** The bearer key every MCP request must carry (`wrangler secret put MCP_KEY`). */
  MCP_KEY: string;
  /** Only in tests: the D1 migrations, applied by test/apply-migrations.ts. */
  TEST_MIGRATIONS: import("cloudflare:test").D1Migration[];
}

interface Env extends WorkerSecrets {}

declare namespace Cloudflare {
  interface Env extends WorkerSecrets {}
}
