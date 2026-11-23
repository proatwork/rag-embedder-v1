"""Load chunk files and their PNGs into Cloudflare, for the MCP Worker in worker/: D1, Vectorize and R2.

Every .jsonl directly inside --chunks-dir (written by json_to_chunks.py) is one norm. Its chunks that get a vector
(embed_chunks.texts_to_embed) are embedded by Workers AI with the Worker's model, as passages; their vectors go to the
Vectorize index, the norm, its chunks, article numbers and figures to the D1 database, and with --bucket its PNGs to
R2. A norm loaded before is replaced: what the new chunk file no longer holds is deleted.

Usage (run with the virtual environment's interpreter, from the project folder):
    venvs/bin/python upload_to_cloudflare.py --chunks-dir docs_processed/chunks
    venvs/bin/python upload_to_cloudflare.py --chunks-dir DIR --bucket norms-figures     # with the PNGs
    venvs/bin/python upload_to_cloudflare.py --chunks-dir DIR --dry-run                  # only writes the files
    venvs/bin/python upload_to_cloudflare.py --remove P118-II [--bucket norms-figures] [--yes]

CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN come from settings.env or the environment (settings.py). Wrangler is
run from worker/ (`npx wrangler`, as installed there; it reads worker/wrangler.jsonc) and gets them through its
environment, never on its command line.

Every norm is checked and embedded before anything on Cloudflare is touched, so that a failure there leaves it as it
was. Then, per norm: what D1 holds of it now is read; the vectors are upserted; the PNGs that changed are put in R2;
the vectors of chunks that are gone are deleted; the norm is replaced in D1 in one file (one transaction). In this
order a run that fails half way can simply be run again. The vectors of a text embedded before are kept in
--cache-dir, so a run after a re-chunk embeds only what changed.
--dry-run checks the files and writes the SQL and the vectors' NDJSON (with the cached vectors, null for the others)
to a folder it names; it uses no network and no wrangler.
--remove deletes a norm (by its doc_key, the chunk file's name): its vectors, its PNGs with --bucket, its rows in D1.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import pipeline_common
import settings
from embed_chunks import (
    KEY_HASHES, KEY_IDS, KEY_MODEL, KEY_VECTORS, post_json, read_chunks, stored_vectors, text_hash, texts_to_embed,
)
from json_to_chunks import CONFIG_FILE, TEXT_ENCODING, code_slug, load_config, norm_fields
from pipeline_common import HIDDEN_PREFIX, JSONL_SUFFIX, NPZ_SUFFIX, PARTIAL_MARK, ConversionError

# Must be the Worker's own (worker/src/store.ts): the questions are embedded with it there.
EMBEDDING_MODEL = "@cf/qwen/qwen3-embedding-0.6b"
EMBEDDING_DIMENSIONS = 1024
NORM_FILTER_KEY = "norm"  # the Vectorize metadata field holding the norm's code
AI_RUN_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
AI_PASSAGES = "documents"  # the model's input for passages, embedded with no instruction
AI_RESULT = "result"
AI_DATA = "data"
AI_SUCCESS = "success"
AI_ERRORS = "errors"
AI_BATCH_SIZE = 32  # texts per request to Workers AI: the most the model takes (its schema's maxItems)
# The model reads at most 8,192 tokens per request, all its texts together. Romanian takes about 2.5 characters per
# token at worst, so this many characters stay under it; a request it still finds too big is split (embed_batch).
AI_BATCH_CHARS = 16_000
AI_TOO_BIG = "input too big"  # what Workers AI answers to a request over the model's input ...
AI_TOO_BIG_CODE = '"code":3030'  # ... and the error's code, in case the wording changes
AI_REQUEST_TIMEOUT = 120  # seconds
AUTHORIZATION = "Authorization"
BEARER = "Bearer {token}"
ACCOUNT_ID = re.compile(r"[0-9a-fA-F]{32}")  # it becomes part of an address

# As worker/wrangler.jsonc names them.
WORKER_DIR = Path(__file__).with_name("worker")
WRANGLER = ["npx", "wrangler"]
D1_DATABASE = "norms"
VECTORIZE_INDEX = "norms"
MAX_VECTOR_ID_BYTES = 64  # Vectorize's limit; the schema checks it too
MAX_STATEMENT_BYTES = 100_000  # D1's limit for one SQL statement
UPSERT_BATCH_SIZE = 1000  # vectors per request of `wrangler vectorize upsert`
DELETE_BATCH_SIZE = 100  # ids per `wrangler vectorize delete-vectors`
PNG_CONTENT_TYPE = "image/png"
BUCKET_NAME = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")  # R2's rule for a bucket's name
WRANGLER_OUTPUT_CHARS = 500  # of what a failing wrangler printed, in the message

DEFAULT_CACHE_DIR = Path(__file__).with_name("docs_processed") / "cloudflare_cache"
DRY_RUN_PREFIX = "cloudflare-upload-"
SQL_SUFFIX = ".sql"
NDJSON_SUFFIX = ".ndjson"
FIGURE_REF = re.compile(r"#/pictures/(\d+)")  # Docling's name for a picture
FIGURE_ID = "{slug}/fig-{number}"
R2_KEY = "{slug}/{name}"
CONFIRMATION = "yes"
CHUNK_AGAIN = "chunk it again (json_to_chunks.py) with a short code in chunk-config.json, such as \"P118/2-2013\""

# What the read of D1 gives per row: what it is, and its fields.
ROW_KIND = "kind"
ROW_ID = "id"
ROW_SHA = "sha256"
ROW_KEY = "r2_key"
KIND_NORM = "norm"
KIND_CHUNK = "chunk"
KIND_FIGURE = "figure"
KIND_CODE_HOLDER = "code_holder"  # another norm that has the same code
D1_RESULTS = "results"


@dataclass
class Figure:
    """A picture as the figures table holds it, and the file it is made from."""
    id: str
    r2_key: str
    sha256: str
    page: int | None
    width: int
    height: int
    caption: str
    file: Path


@dataclass
class Document:
    """One chunk file, checked: the chunks that get a vector, the texts they are made from, and the norm's row."""
    doc_key: str
    source: Path
    norm: dict
    records: list[dict]  # the kept chunks, each with its `seq`
    texts: list[str]
    figures: dict[str, Figure]  # by Docling's ref
    vectors: np.ndarray | None = None  # None in a dry run
    embedded: int = 0  # texts sent to Workers AI; the rest came from the cache
    cached: dict[str, np.ndarray] = field(default_factory=dict)

    @property
    def code(self) -> str:
        return self.norm["code"]

    @property
    def ids(self) -> list[str]:
        return [record["id"] for record in self.records]


@dataclass
class Remote:
    """What D1 holds of one norm now."""
    exists: bool
    chunk_ids: set[str]
    figures: dict[str, tuple[str, str]]  # id -> (r2_key, sha256)
    code_holders: list[str]  # other doc_keys with the norm's code


@dataclass
class Credentials:
    account_id: str
    api_token: str

    def environment(self) -> dict[str, str]:
        """The process's environment with the credentials, as wrangler reads them."""
        return {**os.environ, settings.CLOUDFLARE_ACCOUNT_ID: self.account_id, settings.CLOUDFLARE_API_TOKEN: self.api_token}


# ---- checks, from the files alone --------------------------------------------------------------------------------

def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def figure_of(image: dict, slug: str, chunks_dir: Path) -> Figure:
    """The figure an image record of a chunk names. Raises when its ref is not Docling's or its file is gone."""
    found = FIGURE_REF.fullmatch(str(image.get("ref", "")))
    if not found:
        raise ConversionError(f"a picture has the ref {image.get('ref')!r}, not Docling's \"#/pictures/N\"")
    file = Path(os.path.normpath(chunks_dir / image["path"]))
    if not file.is_file():
        raise ConversionError(f"the picture {image['ref']} is not at {file}")
    return Figure(
        id=FIGURE_ID.format(slug=slug, number=found.group(1)), r2_key=R2_KEY.format(slug=slug, name=file.name),
        sha256=file_sha256(file), page=image.get("page"), width=image["width"], height=image["height"],
        caption=image.get("caption") or "", file=file,
    )


def prepare(source: Path, config: dict) -> Document:
    """One chunk file, checked against the config and Cloudflare's limits. Raises ConversionError when it cannot be
    uploaded as it is."""
    doc_key = pipeline_common.nfc(source.stem)
    entry = config.get(doc_key)
    if entry is None:
        raise ConversionError(f"no entry for {doc_key!r} in the config file")
    records = read_chunks(source)
    for seq, record in enumerate(records):
        record["seq"] = seq
    others = sorted({record["doc_key"] for record in records} - {doc_key})
    if others:
        raise ConversionError(f"it holds chunks of other documents too: {', '.join(others)}")
    kept, texts = texts_to_embed(records, config)
    if not kept:
        raise ConversionError("no chunks to upload")
    long = [record["id"] for record in kept if len(record["id"].encode(TEXT_ENCODING)) > MAX_VECTOR_ID_BYTES]
    if long:
        raise ConversionError(
            f"{len(long)} chunk ids are over {MAX_VECTOR_ID_BYTES} bytes, which Vectorize refuses (such as {long[0]!r}): {CHUNK_AGAIN}"
        )
    fields = norm_fields(entry)
    norm = {
        "doc_key": doc_key, "code": fields["doc_code"], "title": fields["doc_title"], "edition_year": fields["edition_year"],
        "status": fields["status"], "amended_by": json.dumps(fields["amended_by"], ensure_ascii=False),
        "repealed_on": fields["repealed_on"], "gazette": fields["gazette"],
    }
    slug = code_slug(norm["code"])
    figures: dict[str, Figure] = {}
    for record in kept:
        for image in record.get("images", []):
            if image.get("ref") not in figures:
                figure = figure_of(image, slug, source.parent)
                figures[image["ref"]] = figure
    return Document(doc_key, source, norm, kept, texts, figures)


def prepare_all(files: list[Path], config: dict) -> list[Document]:
    """Every chunk file, checked; also that no two share a code or a chunk id. Stops at the first problem."""
    documents = []
    for source in files:
        try:
            document = prepare(source, config)
            replace_sql(document)  # its size check, before anything is embedded
            documents.append(document)
        except (ConversionError, OSError, ValueError) as error:
            sys.exit(f"{source.name}: {error}")
        except KeyError as error:
            sys.exit(f"{source.name}: a chunk or a picture lacks the field {error}: is it a chunk file of json_to_chunks.py?")
    codes: dict[str, str] = {}
    ids: dict[str, str] = {}
    for document in documents:
        if document.code in codes:
            sys.exit(f"{codes[document.code]} and {document.doc_key} have the same code {document.code!r}: upload only one of them")
        codes[document.code] = document.doc_key
        for chunk_id in document.ids:
            if chunk_id in ids:
                sys.exit(f"{ids[chunk_id]} and {document.doc_key} share the chunk id {chunk_id!r}")
            ids[chunk_id] = document.doc_key
    return documents


# ---- vectors ------------------------------------------------------------------------------------------------------

def cache_path(cache_dir: Path, document: Document) -> Path:
    return pipeline_common.output_path(cache_dir, document.source.stem, NPZ_SUFFIX)


def ai_vectors(answer: dict, count: int, url: str) -> np.ndarray:
    """The vectors of a Workers AI answer to `count` texts. Raises on anything but `count` vectors of the model's size."""
    if answer.get(AI_SUCCESS) is not True:
        raise ConversionError(f"{url} did not succeed: {str(answer.get(AI_ERRORS))[:WRANGLER_OUTPUT_CHARS]}")
    result = answer.get(AI_RESULT)
    data = result.get(AI_DATA) if isinstance(result, dict) else None
    if not isinstance(data, list) or len(data) != count:
        raise ConversionError(f"{url} did not answer with {count} vectors in {AI_RESULT}.{AI_DATA}")
    try:
        vectors = np.array(data, dtype=np.float32)
    except (TypeError, ValueError) as error:
        raise ConversionError(f"{url} answered with vectors that are not lists of numbers: {error}") from error
    if vectors.shape != (count, EMBEDDING_DIMENSIONS):
        raise ConversionError(f"{url} answered with vectors of shape {vectors.shape}, not {EMBEDDING_DIMENSIONS} numbers each")
    if not np.isfinite(vectors).all():
        raise ConversionError(f"{url} answered with a vector that is not a number")
    return vectors


def ai_batches(texts: list[str]) -> list[list[str]]:
    """`texts` in order, cut into requests of at most AI_BATCH_SIZE texts and AI_BATCH_CHARS characters; a text longer
    than that goes alone."""
    batches: list[list[str]] = []
    size = 0
    for text in texts:
        if not batches or len(batches[-1]) == AI_BATCH_SIZE or size + len(text) > AI_BATCH_CHARS:
            batches.append([])
            size = 0
        batches[-1].append(text)
        size += len(text)
    return batches


def embed_batch(texts: list[str], url: str, headers: dict[str, str]) -> np.ndarray:
    """The vectors of one request's texts. A request Workers AI finds too big is sent again as two halves, down to a
    single text, which is then too big for the model on its own."""
    try:
        return ai_vectors(post_json(url, {AI_PASSAGES: texts}, AI_REQUEST_TIMEOUT, headers), len(texts), url)
    except ConversionError as error:
        if not (AI_TOO_BIG in str(error) or AI_TOO_BIG_CODE in str(error)) or len(texts) == 1:
            raise
    half = len(texts) // 2
    return np.concatenate([embed_batch(texts[:half], url, headers), embed_batch(texts[half:], url, headers)])


def embed_passages(texts: list[str], credentials: Credentials) -> np.ndarray:
    """One vector per text, from Workers AI, the texts embedded as passages."""
    url = AI_RUN_URL.format(account=credentials.account_id, model=EMBEDDING_MODEL)
    headers = {AUTHORIZATION: BEARER.format(token=credentials.api_token)}
    parts = [np.zeros((0, EMBEDDING_DIMENSIONS), dtype=np.float32)]
    parts.extend(embed_batch(batch, url, headers) for batch in ai_batches(texts))
    return np.concatenate(parts)


def write_cache(path: Path, document: Document, hashes: list[str]) -> None:
    """Keep the document's vectors, by the hash of their text, as embed_chunks.py keeps its own."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{HIDDEN_PREFIX}{path.stem}.{os.getpid()}{PARTIAL_MARK}{NPZ_SUFFIX}")
    try:
        with partial.open("wb") as out:
            np.savez(out, **{
                KEY_IDS: np.array(document.ids), KEY_VECTORS: document.vectors, KEY_HASHES: np.array(hashes),
                KEY_MODEL: np.array(EMBEDDING_MODEL),
            })
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)


def embed(document: Document, cache_dir: Path, credentials: Credentials) -> None:
    """Give the document its vectors: those of the cache for texts embedded before, the others from Workers AI."""
    path = cache_path(cache_dir, document)
    known = stored_vectors(path, EMBEDDING_MODEL, EMBEDDING_DIMENSIONS)
    hashes = [text_hash(text) for text in document.texts]
    new = [index for index, text_id in enumerate(hashes) if text_id not in known]
    fresh = embed_passages([document.texts[index] for index in new], credentials)
    vectors = np.zeros((len(hashes), EMBEDDING_DIMENSIONS), dtype=np.float32)
    for row, index in enumerate(new):
        vectors[index] = fresh[row]
    for index, text_id in enumerate(hashes):
        if text_id in known:
            vectors[index] = known[text_id]
    document.vectors, document.embedded = vectors, len(new)
    write_cache(path, document, hashes)


def ndjson(document: Document) -> str:
    """The vectors as `wrangler vectorize upsert` reads them, one JSON object a line. Without vectors (a dry run), those
    of the cache are written and null for the texts that would be embedded."""
    lines = []
    for index, record in enumerate(document.records):
        if document.vectors is not None:
            values = document.vectors[index].tolist()
        else:
            cached = document.cached.get(text_hash(document.texts[index]))
            values = cached.tolist() if cached is not None else None
        lines.append(json.dumps({"id": record["id"], "values": values, "metadata": {NORM_FILTER_KEY: document.code}}))
    return "".join(line + "\n" for line in lines)


def stale_ids(old: set[str], new: list[str]) -> list[str]:
    """The ids of vectors that were uploaded before and that the new chunks no longer have, sorted."""
    return sorted(old - set(new))


# ---- SQL ----------------------------------------------------------------------------------------------------------

_QUOTER = sqlite3.connect(":memory:")


def literal(value) -> str:
    """`value` as an SQL literal, quoted by SQLite itself."""
    return _QUOTER.execute("SELECT quote(?)", (value,)).fetchone()[0]


def json_text(value) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def insert(table: str, row: dict) -> str:
    return f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join(literal(value) for value in row.values())});"


def norm_upsert(norm: dict) -> str:
    """The norm's row, inserted or updated in place: INSERT OR REPLACE would delete it, and its chunks with it."""
    updates = ", ".join(f"{column} = excluded.{column}" for column in norm if column != "doc_key")
    return insert("norms", norm).removesuffix(";") + f" ON CONFLICT (doc_key) DO UPDATE SET {updates};"


def chunk_row(record: dict, doc_key: str) -> dict:
    return {
        "id": record["id"], "doc_key": doc_key, "seq": record["seq"], "kind": record["kind"],
        "heading_path": json_text(record.get("heading_path") or []), "label": record.get("label"),
        "caption": record.get("caption"), "article": record.get("article"), "articles": json_text(record.get("articles") or []),
        "pdf_pages": json_text(record.get("pdf_pages")), "text": record["text"], "embed_text": record["embed_text"],
        "prev_id": record.get("prev_id"), "next_id": record.get("next_id"), "parent_id": record.get("parent_id"),
        "flags": json_text(record.get("flags") or []),
    }


# The sha256 D1 records for a figure whose PNG was not put in R2 (an upload without --bucket): it matches no file, so a
# later upload with --bucket sends that PNG.
NOT_IN_BUCKET = ""


def figure_row(figure: Figure, doc_key: str, in_bucket: bool) -> dict:
    return {
        "id": figure.id, "doc_key": doc_key, "r2_key": figure.r2_key, "sha256": figure.sha256 if in_bucket else NOT_IN_BUCKET,
        "page": figure.page,
        "width": figure.width, "height": figure.height, "caption": figure.caption,
    }


def replace_sql(document: Document, in_bucket: bool = True) -> str:
    """The SQL that replaces the norm in D1, as the header of worker/migrations/0001_init.sql says: one file, which
    D1 runs as one transaction (so no BEGIN or COMMIT). `in_bucket`: whether the PNGs are put in R2 by this upload.
    Raises when a statement is over D1's size limit."""
    doc_key = literal(document.doc_key)
    statements = [
        norm_upsert(document.norm),
        f"DELETE FROM chunks WHERE doc_key = {doc_key};",  # the article numbers and the figure links go with them
        f"DELETE FROM figures WHERE doc_key = {doc_key};",
    ]
    links: list[str] = []
    for record in document.records:
        statements.append(insert("chunks", chunk_row(record, document.doc_key)))
        for number in dict.fromkeys(record.get("articles") or []):
            statements.append(insert("chunk_articles", {"chunk_id": record["id"], "number": number}))
        linked: set[str] = set()
        for position, image in enumerate(record.get("images", [])):
            figure = document.figures[image["ref"]]
            if figure.id not in linked:
                linked.add(figure.id)
                links.append(insert("chunk_figures", {"chunk_id": record["id"], "figure_id": figure.id, "position": position}))
    statements += [insert("figures", figure_row(figure, document.doc_key, in_bucket)) for figure in document.figures.values()]
    statements += links
    for statement in statements:
        size = len(statement.encode(TEXT_ENCODING))
        if size > MAX_STATEMENT_BYTES:
            raise ConversionError(f"a statement is {size} bytes, over D1's {MAX_STATEMENT_BYTES}: {statement[:WRANGLER_OUTPUT_CHARS]}")
    return "".join(statement + "\n" for statement in statements)


def remote_query(doc_key: str, code: str | None) -> str:
    """One statement that reads what D1 holds of a norm: whether it is there, its chunk ids, its figures, and the
    other norms that have its code (whose vectors an upload would overwrite)."""
    key = literal(doc_key)
    parts = [
        f"SELECT '{KIND_NORM}' AS {ROW_KIND}, doc_key AS {ROW_ID}, NULL AS {ROW_SHA}, NULL AS {ROW_KEY} FROM norms WHERE doc_key = {key}",
        f"SELECT '{KIND_CHUNK}', id, NULL, NULL FROM chunks WHERE doc_key = {key}",
        f"SELECT '{KIND_FIGURE}', id, sha256, r2_key FROM figures WHERE doc_key = {key}",
    ]
    if code is not None:
        parts.append(f"SELECT '{KIND_CODE_HOLDER}', doc_key, NULL, NULL FROM norms WHERE code = {literal(code)} AND doc_key <> {key}")
    return " UNION ALL ".join(parts)


# ---- wrangler -----------------------------------------------------------------------------------------------------

def wrangler(arguments: list[str], credentials: Credentials) -> str:
    """Run `npx wrangler` with `arguments` in worker/, the credentials in its environment only; its output. Raises when
    it fails."""
    command = [*WRANGLER, *arguments]
    try:
        done = subprocess.run(
            command, cwd=WORKER_DIR, env=credentials.environment(), stdin=subprocess.DEVNULL, capture_output=True,
            text=True, encoding=TEXT_ENCODING, errors="replace",
        )
    except OSError as error:
        raise ConversionError(f"cannot run {' '.join(WRANGLER)}: {error} (is Node.js installed, and `npm install` run in {WORKER_DIR.name}/?)") from error
    if done.returncode != 0:
        said = (done.stderr.strip() + "\n" + done.stdout.strip()).strip()[-WRANGLER_OUTPUT_CHARS:]
        raise ConversionError(f"wrangler {' '.join(arguments[:2])} failed (exit {done.returncode}): {said}")
    return done.stdout


def d1_rows(output: str) -> list[dict]:
    """The rows of `wrangler d1 execute --json`: a list with one result per statement. Raises when it is not that."""
    start = min((index for index in (output.find("["), output.find("{")) if index >= 0), default=-1)
    try:
        answer, _ = json.JSONDecoder().raw_decode(output[start:]) if start >= 0 else (None, 0)
    except ValueError:
        answer = None
    answer = [answer] if isinstance(answer, dict) else answer
    if not isinstance(answer, list) or not all(isinstance(item, dict) and isinstance(item.get(D1_RESULTS), list) for item in answer):
        raise ConversionError(f"wrangler d1 execute --json did not answer with results: {output[:WRANGLER_OUTPUT_CHARS]}")
    return [row for item in answer for row in item[D1_RESULTS]]


def read_remote(doc_key: str, code: str | None, credentials: Credentials) -> Remote:
    rows = d1_rows(wrangler(
        ["d1", "execute", D1_DATABASE, "--remote", "--json", "--yes", "--command", remote_query(doc_key, code)], credentials,
    ))
    try:
        return Remote(
            exists=any(row[ROW_KIND] == KIND_NORM for row in rows),
            chunk_ids={row[ROW_ID] for row in rows if row[ROW_KIND] == KIND_CHUNK},
            figures={row[ROW_ID]: (row[ROW_KEY], row[ROW_SHA]) for row in rows if row[ROW_KIND] == KIND_FIGURE},
            code_holders=sorted(row[ROW_ID] for row in rows if row[ROW_KIND] == KIND_CODE_HOLDER),
        )
    except (KeyError, TypeError) as error:
        raise ConversionError(f"D1 answered with rows that lack {error}") from error


def delete_vectors(ids: list[str], credentials: Credentials) -> None:
    for start in range(0, len(ids), DELETE_BATCH_SIZE):
        wrangler(["vectorize", "delete-vectors", VECTORIZE_INDEX, "--ids", *ids[start:start + DELETE_BATCH_SIZE]], credentials)


def object_path(bucket: str, key: str) -> str:
    return f"{bucket}/{key}"


# ---- one norm -----------------------------------------------------------------------------------------------------

def write_files(document: Document, folder: Path, in_bucket: bool) -> tuple[Path, Path]:
    """The SQL and the NDJSON of a document, written to `folder`; `in_bucket` as for replace_sql."""
    name = pipeline_common.output_path(folder, document.source.stem, "").name
    sql, vectors = folder / f"{name}{SQL_SUFFIX}", folder / f"{name}{NDJSON_SUFFIX}"
    sql.write_text(replace_sql(document, in_bucket), encoding=TEXT_ENCODING)
    vectors.write_text(ndjson(document), encoding=TEXT_ENCODING)
    return sql, vectors


def upload(document: Document, bucket: str | None, credentials: Credentials, folder: Path) -> str:
    """Replace the norm on Cloudflare with the document; the note counts what was done."""
    remote = read_remote(document.doc_key, document.code, credentials)
    if remote.code_holders:
        raise ConversionError(
            f"D1 holds {', '.join(remote.code_holders)} with the same code {document.code!r}, whose vectors this would "
            f"overwrite: remove it first (--remove)"
        )
    sql, vectors = write_files(document, folder, in_bucket=bucket is not None)
    wrangler(["vectorize", "upsert", VECTORIZE_INDEX, "--file", str(vectors.absolute()), "--batch-size", str(UPSERT_BATCH_SIZE)], credentials)
    uploaded = skipped = 0
    if bucket:
        known = set(remote.figures.values())
        for figure in document.figures.values():
            if (figure.r2_key, figure.sha256) in known:
                skipped += 1
                continue
            wrangler([
                "r2", "object", "put", object_path(bucket, figure.r2_key), "--file", str(figure.file.absolute()), "--remote",
                "--content-type", PNG_CONTENT_TYPE,
            ], credentials)
            uploaded += 1
    stale = stale_ids(remote.chunk_ids, document.ids)
    delete_vectors(stale, credentials)
    wrangler(["d1", "execute", D1_DATABASE, "--remote", "--yes", "--file", str(sql.absolute())], credentials)
    pngs = f"{uploaded} PNGs uploaded, {skipped} unchanged" if bucket else f"{len(document.figures)} PNGs not uploaded (no --bucket)"
    return (
        f"{len(document.records)} chunks ({document.embedded} embedded, {len(document.records) - document.embedded} reused), "
        f"{len(stale)} stale vectors deleted, {len(document.figures)} figures, {pngs}"
    )


def remove(doc_key: str, bucket: str | None, credentials: Credentials) -> str:
    """Delete a norm from Cloudflare: its vectors, then its PNGs, then its rows (the schema cascades from norms), so
    that a run that fails half way can be run again. The note counts what was deleted."""
    remote = read_remote(doc_key, None, credentials)
    if not (remote.exists or remote.chunk_ids or remote.figures):
        return f"D1 holds nothing of {doc_key!r}: nothing deleted"
    delete_vectors(sorted(remote.chunk_ids), credentials)
    if bucket:
        for r2_key, _ in sorted(remote.figures.values()):
            wrangler(["r2", "object", "delete", object_path(bucket, r2_key), "--remote"], credentials)
    wrangler(["d1", "execute", D1_DATABASE, "--remote", "--yes", "--command", f"DELETE FROM norms WHERE doc_key = {literal(doc_key)}"], credentials)
    pngs = f"{len(remote.figures)} PNGs deleted" if bucket else f"{len(remote.figures)} PNGs left in R2 (no --bucket)"
    return f"{len(remote.chunk_ids)} vectors and the rows deleted, {pngs}"


# ---- the command --------------------------------------------------------------------------------------------------

def read_credentials() -> Credentials:
    """The account id and token from settings.env or the environment. Stops when either is missing or malformed."""
    values = settings.everything()
    account_id = settings.cloudflare_account_id(values)
    api_token = settings.cloudflare_api_token(values)
    if not account_id or not api_token:
        sys.exit(
            f"set {settings.CLOUDFLARE_ACCOUNT_ID} and {settings.CLOUDFLARE_API_TOKEN} in {settings.SETTINGS_FILE.name} "
            f"(or the environment) first"
        )
    if not ACCOUNT_ID.fullmatch(account_id):
        sys.exit(f"{settings.CLOUDFLARE_ACCOUNT_ID} is not an account id (32 hexadecimal characters)")
    return Credentials(account_id, api_token)


def bucket_name(value: str) -> str:
    """The value of --bucket."""
    if not BUCKET_NAME.fullmatch(value):
        raise argparse.ArgumentTypeError("not an R2 bucket name (3 to 63 lowercase letters, digits and hyphens)")
    return value


def confirmed(doc_key: str, bucket: str | None) -> bool:
    """Whether the person at the terminal answers yes to removing the norm; never when no one is at a terminal."""
    if not sys.stdin.isatty():
        return False
    where = f"D1, Vectorize and the bucket {bucket}" if bucket else "D1 and Vectorize"
    try:
        answer = input(f"Delete {doc_key!r} from {where}? Type {CONFIRMATION} to go on: ")
    except EOFError:
        return False
    return answer.strip().lower() == CONFIRMATION


def run_upload(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    files = pipeline_common.collect_files(args.chunks_dir, JSONL_SUFFIX)
    documents = prepare_all(files, config)
    if args.dry_run:
        folder = Path(tempfile.mkdtemp(prefix=DRY_RUN_PREFIX))
        for index, document in enumerate(documents, start=1):
            document.cached = stored_vectors(cache_path(args.cache_dir, document), EMBEDDING_MODEL, EMBEDDING_DIMENSIONS)
            write_files(document, folder, in_bucket=args.bucket is not None)
            reused = sum(text_hash(text) in document.cached for text in document.texts)
            print(
                f"== [{index}/{len(documents)}] {document.doc_key}: {len(document.records)} chunks ({len(document.records) - reused} "
                f"to embed, {reused} cached), {len(document.figures)} figures",
                flush=True,
            )
        print(f"dry run: nothing was sent; the SQL and NDJSON files are in {folder}", flush=True)
        return

    credentials = read_credentials()
    for document in documents:  # all of it before anything on Cloudflare changes
        print(f"embedding {document.doc_key}: {len(document.texts)} chunks", flush=True)
        try:
            embed(document, args.cache_dir, credentials)
        except (ConversionError, OSError) as error:
            sys.exit(f"{document.source.name}: {error}")
    failed = []
    with tempfile.TemporaryDirectory(prefix=DRY_RUN_PREFIX) as folder:
        for index, document in enumerate(documents, start=1):
            print(f"== [{index}/{len(documents)}] {document.doc_key}", flush=True)
            try:
                print(f"   ok ({upload(document, args.bucket, credentials, Path(folder))})", flush=True)
            except (ConversionError, OSError) as error:
                print(f"   FAILED ({error})", flush=True)
                failed.append(document.doc_key)
    if failed:
        sys.exit("failed: " + ", ".join(failed))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--chunks-dir", type=Path, help="folder with the .jsonl files of json_to_chunks.py, to upload")
    action.add_argument("--remove", metavar="DOC_KEY", help="delete this norm (the name of its chunk file) from Cloudflare")
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="per-document settings (default: %(default)s)")
    parser.add_argument("--bucket", type=bucket_name, help="the R2 bucket of the PNGs; without it no PNG is uploaded or deleted")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="the vectors of earlier runs (default: %(default)s)")
    parser.add_argument("--dry-run", action="store_true", help="only check and write the files; no network, no wrangler")
    parser.add_argument("--yes", action="store_true", help="with --remove: do not ask first")
    args = parser.parse_args()
    if args.remove is not None and args.dry_run:
        parser.error("--dry-run goes with --chunks-dir")

    if args.chunks_dir is not None:
        run_upload(args)
        return
    doc_key = pipeline_common.nfc(args.remove)
    credentials = read_credentials()
    if not args.yes and not confirmed(doc_key, args.bucket):
        print("nothing deleted", flush=True)
        return
    try:
        print(f"{doc_key}: {remove(doc_key, args.bucket, credentials)}", flush=True)
    except ConversionError as error:
        sys.exit(f"{doc_key}: {error}")


if __name__ == "__main__":
    main()
