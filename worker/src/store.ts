// The chunks in D1, their vectors in Vectorize, and the question's vector from Workers AI.
import {
  EmbeddingUnavailable,
  type ChunkRecord,
  type Embedder,
  type FigureFile,
  type FigureRecord,
  type NormInfo,
  type Scored,
  type Store,
} from "./types";

// The chunks' vectors must be made with the same model, as passages (its `documents` input, with no instruction).
export const EMBEDDING_MODEL = "@cf/qwen/qwen3-embedding-0.6b";
export const EMBEDDING_DIMENSIONS = 1024; // of its vectors, as the Vectorize index is made
// Qwen3 embeds a question with a one-line task in front of it; the passages are embedded without one.
export const QUERY_INSTRUCTION =
  "Given a question about the Romanian fire-safety norms, retrieve the passages of the norms that answer it";
export const NORM_FILTER_KEY = "norm"; // the Vectorize metadata field (an indexed string) holding the norm's code
const URL_SEPARATOR = "/";
const NOT_IN_BUCKET = ""; // the sha256 of a figure whose PNG the upload did not put in the bucket

interface ChunkRow {
  id: string;
  kind: string;
  doc_key: string;
  doc_code: string;
  doc_title: string;
  edition_year: number;
  status: string;
  amended_by: string;
  repealed_on: string | null;
  gazette: string | null;
  heading_path: string;
  label: string | null;
  caption: string | null;
  article: string | null;
  articles: string;
  pdf_pages: string | null;
  text: string;
  embed_text: string;
  prev_id: string | null;
  next_id: string | null;
  parent_id: string | null;
  flags: string;
}

interface FigureRow {
  chunk_id: string;
  id: string;
  r2_key: string;
  sha256: string;
  page: number | null;
  width: number;
  height: number;
  caption: string;
}

// A chunk with the fields of its norm, as one row.
const CHUNK_COLUMNS = `
  c.id, c.kind, c.doc_key, n.code AS doc_code, n.title AS doc_title, n.edition_year, n.status, n.amended_by,
  n.repealed_on, n.gazette, c.heading_path, c.label, c.caption, c.article, c.articles, c.pdf_pages, c.text,
  c.embed_text, c.prev_id, c.next_id, c.parent_id, c.flags`;
// As search.py loads the chunk files: by file name (<doc_key>.jsonl, so the suffix takes part in the order), then in
// the text's order.
const NORM_ORDER = "n.doc_key || '.jsonl', c.seq";

const MAX_BOUND_PARAMETERS = 100; // D1's limit for one statement

function placeholders(count: number): string {
  return Array.from({ length: count }, () => "?").join(", ");
}

/** `items` cut into runs that each fit in one statement's bound parameters. */
function batches<T>(items: T[]): T[][] {
  const runs: T[][] = [];
  for (let start = 0; start < items.length; start += MAX_BOUND_PARAMETERS) {
    runs.push(items.slice(start, start + MAX_BOUND_PARAMETERS));
  }
  return runs;
}

export class CloudflareStore implements Store {
  constructor(
    private readonly db: D1Database,
    private readonly index: VectorizeIndex,
    private readonly bucket: R2Bucket,
    private readonly figuresBaseUrl: string,
  ) {}

  async norms(): Promise<NormInfo[]> {
    const { results } = await this.db.prepare("SELECT code, title FROM norms ORDER BY code").all<NormInfo>();
    return results;
  }

  async nearest(vector: number[], top: number, code?: string): Promise<Scored[]> {
    const { matches } = await this.index.query(vector, {
      topK: top,
      returnValues: false,
      returnMetadata: "none",
      ...(code ? { filter: { [NORM_FILTER_KEY]: code } } : {}),
    });
    const records = new Map(
      (await this.records(matches.map((match) => match.id))).map((record) => [record.id, record]),
    );
    // A vector whose chunk is not in D1 (an upload half done) is left out rather than failing the search.
    return matches.flatMap((match) => {
      const record = records.get(match.id);
      return record ? [{ score: match.score, record }] : [];
    });
  }

  async withNumber(number: string, code?: string): Promise<ChunkRecord[]> {
    const { results } = await this.db
      .prepare(
        `SELECT ${CHUNK_COLUMNS} FROM chunk_articles a JOIN chunks c ON c.id = a.chunk_id JOIN norms n ON n.doc_key = c.doc_key
         WHERE a.number = ?1 AND (?2 IS NULL OR n.code = ?2) ORDER BY ${NORM_ORDER}`,
      )
      .bind(number, code ?? null)
      .all<ChunkRow>();
    return this.withFigures(results);
  }

  /** Throws when D1 says the figure's PNG is in the bucket and it is not. */
  async figure(id: string, maxBytes: number): Promise<FigureFile | null> {
    const row = await this.db
      .prepare(
        `SELECT f.id, f.r2_key, f.sha256, f.page, f.width, f.height, f.caption, n.code
         FROM figures f JOIN norms n ON n.doc_key = f.doc_key WHERE f.id = ?1 AND f.sha256 <> ?2`,
      )
      .bind(id, NOT_IN_BUCKET)
      .first<Omit<FigureRow, "chunk_id"> & { code: string }>();
    if (!row) return null;
    const object = await this.bucket.get(row.r2_key);
    // D1 says it was uploaded: a missing PNG is the server's fault, not the caller's id, and its link answers 404.
    if (!object) throw new Error(`figure ${row.id}: ${row.r2_key} is missing from the bucket`);
    const figure = this.toFigure(row);
    if (object.size > maxBytes) {
      await object.body.cancel();
      return { figure, code: row.code, size: object.size, bytes: null };
    }
    return { figure, code: row.code, size: object.size, bytes: await object.arrayBuffer() };
  }

  /** The chunks `ids` names, in no particular order; an id with no chunk is left out. */
  private async records(ids: string[]): Promise<ChunkRecord[]> {
    const rows: ChunkRow[] = [];
    for (const run of batches(ids)) {
      const { results } = await this.db
        .prepare(
          `SELECT ${CHUNK_COLUMNS} FROM chunks c JOIN norms n ON n.doc_key = c.doc_key WHERE c.id IN (${placeholders(run.length)})`,
        )
        .bind(...run)
        .all<ChunkRow>();
      rows.push(...results);
    }
    return this.withFigures(rows);
  }

  private async withFigures(rows: ChunkRow[]): Promise<ChunkRecord[]> {
    const found: FigureRow[] = [];
    for (const run of batches(rows.map((row) => row.id))) {
      const { results } = await this.db
        .prepare(
          `SELECT cf.chunk_id, f.id, f.r2_key, f.sha256, f.page, f.width, f.height, f.caption
           FROM chunk_figures cf JOIN figures f ON f.id = cf.figure_id
           WHERE cf.chunk_id IN (${placeholders(run.length)}) ORDER BY cf.chunk_id, cf.position`,
        )
        .bind(...run)
        .all<FigureRow>();
      found.push(...results);
    }
    const figures = new Map<string, FigureRecord[]>();
    for (const row of found) {
      const list = figures.get(row.chunk_id) ?? [];
      list.push(this.toFigure(row));
      figures.set(row.chunk_id, list);
    }
    return rows.map((row) => toRecord(row, figures.get(row.id) ?? []));
  }

  private toFigure(row: Omit<FigureRow, "chunk_id">): FigureRecord {
    return {
      id: row.id,
      url: row.sha256 === NOT_IN_BUCKET ? "" : this.figureUrl(row.r2_key), // no link that would answer 404
      page: row.page,
      width: row.width,
      height: row.height,
      caption: row.caption,
    };
  }

  private figureUrl(key: string): string {
    if (!this.figuresBaseUrl) return "";
    return (
      this.figuresBaseUrl.replace(/\/+$/, "") +
      URL_SEPARATOR +
      key.split(URL_SEPARATOR).map(encodeURIComponent).join(URL_SEPARATOR)
    );
  }
}

function toRecord(row: ChunkRow, images: FigureRecord[]): ChunkRecord {
  return {
    ...row,
    amended_by: JSON.parse(row.amended_by) as string[],
    heading_path: JSON.parse(row.heading_path) as string[],
    articles: JSON.parse(row.articles) as string[],
    pdf_pages: row.pdf_pages === null ? null : (JSON.parse(row.pdf_pages) as number[]),
    flags: JSON.parse(row.flags) as string[],
    images,
  };
}

export class WorkersAiEmbedder implements Embedder {
  constructor(private readonly ai: Ai) {}

  async embed(text: string): Promise<number[]> {
    const output = await this.ai
      .run(EMBEDDING_MODEL, { queries: [text], instruction: QUERY_INSTRUCTION })
      .catch((error: unknown) => {
        throw new EmbeddingUnavailable(`Workers AI did not embed the question: ${String(error)}`);
      });
    const vector = (output as { data?: number[][] } | null | undefined)?.data?.[0]; // anything else is no vector
    if (vector?.length !== EMBEDDING_DIMENSIONS) {
      throw new EmbeddingUnavailable(`Workers AI answered with no vector of ${EMBEDDING_DIMENSIONS} numbers`);
    }
    return vector;
  }
}
