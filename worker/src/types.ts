// The contract between the data layer (store.ts) and the search logic (search.ts, tool.ts, server.ts).
// Field names follow the chunk records json_to_chunks.py writes, so that the port of search.py and
// search_tool.py reads the same keys.

/** A picture of a chunk. `url` is empty while it has no public address: no address for the figures is set, or its PNG
 * was not put in the bucket. */
export interface FigureRecord {
  id: string;
  url: string;
  page: number | null;
  width: number;
  height: number;
  caption: string;
}

/** One chunk with the fields of its norm, as search.py has it after loading. */
export interface ChunkRecord {
  id: string;
  kind: string;
  doc_key: string;
  doc_code: string;
  doc_title: string;
  edition_year: number;
  status: string;
  amended_by: string[];
  repealed_on: string | null;
  gazette: string | null;
  heading_path: string[];
  label: string | null;
  caption: string | null;
  article: string | null;
  articles: string[];
  pdf_pages: number[] | null;
  text: string;
  embed_text: string;
  prev_id: string | null;
  next_id: string | null;
  parent_id: string | null;
  flags: string[];
  images: FigureRecord[];
}

/** A norm as the tool's schema names it. */
export interface NormInfo {
  code: string;
  title: string;
}

/** A chunk found by meaning, with its score (higher is closer). */
export interface Scored {
  score: number;
  record: ChunkRecord;
}

/** A figure to hand to the model: what is known of it, the code of its norm, and its PNG (`bytes`), which is null
 * when the PNG is larger than the caller allows (`size` says how large it is). */
export interface FigureFile {
  figure: FigureRecord;
  code: string;
  size: number;
  bytes: ArrayBuffer | null;
}

/** Where the chunks are. Every method may throw; only EmbeddingUnavailable is told to the model as such. */
export interface Store {
  /** Every norm, in the order the norms are listed (by code). */
  norms(): Promise<NormInfo[]>;
  /** The `top` chunks closest to `vector`, closest first; with `code`, of that norm only. */
  nearest(vector: number[], top: number, code?: string): Promise<Scored[]>;
  /** The chunks that hold the article or point `number`, in the order of the norms and of the text; with `code`, of that norm only. */
  withNumber(number: string, code?: string): Promise<ChunkRecord[]>;
  /** The figure `id` with its PNG when that is at most `maxBytes`; null when there is no such figure in the bucket. */
  figure(id: string, maxBytes: number): Promise<FigureFile | null>;
}

/** Turns a question, already spelled the way the chunks are, into its vector. */
export interface Embedder {
  embed(text: string): Promise<number[]>;
}

/** The embedding model could not be used: the search by meaning fails, the lookup by number still works. */
export class EmbeddingUnavailable extends Error {
  override name = "EmbeddingUnavailable";
}
