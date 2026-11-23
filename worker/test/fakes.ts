// In-memory stand-ins for the store and the embedder, over the fixture chunks of golden.ts: every chunk has a
// direction of its own and every question a vector chosen there, as in tests/test_search_tool.py.
import { GOLDEN, RECORDS } from "./golden";
import type { ChunkRecord, Embedder, FigureFile, NormInfo, Scored, Store } from "../src/types";

export const ORDERED: ChunkRecord[] = GOLDEN.order.map((key) => RECORDS[key]!);
const VECTORS: Record<string, number[]> = GOLDEN.vectors;
const QUESTIONS: Record<string, number[]> = GOLDEN.questions;

export class FakeStore implements Store {
  calls: { method: string; args: unknown[] }[] = [];

  constructor(
    private readonly records: ChunkRecord[] = ORDERED,
    private readonly figures: ReadonlyMap<string, FigureFile> = new Map(),
    private readonly failure?: Error,
  ) {}

  async norms(): Promise<NormInfo[]> {
    this.calls.push({ method: "norms", args: [] });
    const titles = new Map<string, string>();
    for (const record of this.records) titles.set(record.doc_code, record.doc_title || ""); // the last one's, as Python
    return [...titles].map(([code, title]) => ({ code, title }));
  }

  async nearest(vector: number[], top: number, code?: string): Promise<Scored[]> {
    this.calls.push({ method: "nearest", args: [vector, top, code] });
    return this.records
      .map((record, row) => ({ score: dot(vector, VECTORS[GOLDEN.order[row]!]!), record }))
      .sort((a, b) => b.score - a.score) // stable: chunks as far from the question keep the order of the norms
      .filter(({ record }) => code === undefined || record.doc_code === code)
      .slice(0, top);
  }

  async withNumber(number: string, code?: string): Promise<ChunkRecord[]> {
    this.calls.push({ method: "withNumber", args: [number, code] });
    return this.records.filter(
      (record) => record.articles.includes(number) && (code === undefined || record.doc_code === code),
    );
  }

  /** As the real store: the PNG only when it fits in `maxBytes`. */
  async figure(id: string, maxBytes: number): Promise<FigureFile | null> {
    this.calls.push({ method: "figure", args: [id, maxBytes] });
    if (this.failure) throw this.failure;
    const file = this.figures.get(id);
    if (!file) return null;
    return file.size > maxBytes ? { ...file, bytes: null } : file;
  }
}

function dot(a: number[], b: number[]): number {
  return a.reduce((sum, value, place) => sum + value * (b[place] ?? 0), 0);
}

export class FakeEmbedder implements Embedder {
  asked: string[] = [];

  constructor(private readonly failure?: Error) {}

  async embed(text: string): Promise<number[]> {
    this.asked.push(text);
    if (this.failure) throw this.failure;
    const vector = QUESTIONS[text];
    if (!vector) throw new Error(`no vector for the question ${JSON.stringify(text)}`);
    return vector;
  }
}
