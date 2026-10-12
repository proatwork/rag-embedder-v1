"""Embed a folder of chunk files: one .npz of vectors per .jsonl.

Every .jsonl directly inside --input-dir (written by json_to_chunks.py) becomes --output-dir/<name>.npz, with one
vector per chunk. The text that is embedded is the chunk's `embed_text` (breadcrumb and text), or the field named by
"embed_field" in the document's entry in chunk-config.json. Chunks of kind `fragmented` get no vector.

Usage (run with the virtual environment's interpreter):
    venvs/bin/python embed_chunks.py --input-dir docs_processed/chunks --output-dir docs_processed/vectors
    venvs/bin/python embed_chunks.py --input-dir IN --output-dir OUT --skip-existing     # resume
    venvs/bin/python embed_chunks.py --input-dir IN --output-dir OUT --server http://localhost:1234

The model is bge-m3, run by a server that speaks the OpenAI embeddings API (LM Studio); the model is not loaded on
this machine, only the chunker's tokenizer, to count the texts that are too long. Before anything is embedded the model card's own example is reproduced, so that vectors from the wrong model
or made with the wrong pooling are never written. A chunk whose text is unchanged since the last run keeps its
vector, so a run after a chunker change embeds only what changed. search.py and eval_retrieval.py read the result.
A failing file is reported and skipped; the exit status is non-zero if any failed.
"""

import argparse
import functools
import hashlib
import http.client
import json
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

import pipeline_common
import settings
from json_to_chunks import (
    CONFIG_FILE, DEFAULT_MAX_TOKENS, DEFAULT_MIN_TOKENS, DEFAULT_TOKENIZER, EMBED_FIELD_KEY, KIND_FRAGMENTED, Sizer, TEXT_ENCODING, load_config,
)
from pipeline_common import JSONL_SUFFIX, NPZ_SUFFIX, ConversionError

DEFAULT_SERVER = settings.embedding_server()  # this machine's own, from settings.env: LM Studio, with the model loaded
DEFAULT_MODEL = "text-embedding-bge-m3"  # the model's id on the server; the chunks were sized with its tokenizer
EMBEDDINGS_PATH = "/v1/embeddings"  # of the OpenAI API
JSON_HEADERS = {"Content-Type": "application/json"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses to follow a redirect: urllib would send the request's Authorization header, an API key, on to wherever the
    server points, and turn a POST into a GET. A redirect becomes an HTTP error that names where it pointed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OPENER = urllib.request.build_opener(_NoRedirect)  # use OPENER.open(...) in place of urllib.request.urlopen(...) for any call that may carry a key
REDIRECT_CODES = (301, 302, 303, 307, 308)
REQUEST_TIMEOUT = 60  # seconds, to connect and for every wait on the reply; a batch was measured at under 1
ERROR_BODY_CHARS = 200  # of a refusal by the server, shown in the message
# The fields of a request to the server and of its reply.
API_MODEL = "model"
API_INPUT = "input"
API_DATA = "data"
API_INDEX = "index"
API_EMBEDDING = "embedding"
API_ERROR = "error"  # in a reply that reports a failure
MAX_INPUT_TOKENS = 512  # json_to_chunks.py keeps the chunks under this
BATCH_SIZE = 32  # texts per request
SIZE_PROBE = "."  # embedded to learn how long the model's vectors are

DEFAULT_EMBED_FIELD = "embed_text"
EMBED_FIELDS = (DEFAULT_EMBED_FIELD, "text")
SKIPPED_KINDS = (KIND_FRAGMENTED,)  # a figure read word by word: noise in a search

# The arrays of an .npz file.
KEY_IDS = "ids"
KEY_VECTORS = "vectors"
KEY_HASHES = "hashes"  # of the text each vector was made from
KEY_MODEL = "model"

# The example on a model's card, per model id on the server: (questions, passages, the similarity of each question
# to each passage).
REFERENCE = {
    DEFAULT_MODEL: (
        ["What is BGE M3?", "Defination of BM25"],
        [
            "BGE M3 is an embedding model supporting dense retrieval, lexical matching and multi-vector interaction.",
            "BM25 is a bag-of-words retrieval function that ranks a set of documents based on the query terms appearing in each document",
        ],
        [[0.6265, 0.3477], [0.3499, 0.678]],
    ),
}
REFERENCE_TOLERANCE = 0.01  # the card's numbers were made in half precision; the server runs a compressed copy
REFERENCE_DECIMALS = 4  # as the card prints them


def text_hash(text: str) -> str:
    """Names the text a vector was made from; search.py uses it to notice chunks that changed after they were embedded."""
    return hashlib.sha256(text.encode(TEXT_ENCODING)).hexdigest()


def read_chunks(path: Path) -> list[dict]:
    """The chunks of one .jsonl file, in reading order."""
    with path.open(encoding=TEXT_ENCODING) as lines:
        return [json.loads(line) for line in lines if line.strip()]


def texts_to_embed(records: list[dict], config: dict) -> tuple[list[dict], list[str]]:
    """The chunks that get a vector, and the text each one's vector is made from."""
    kept = [record for record in records if record["kind"] not in SKIPPED_KINDS]
    texts = []
    for record in kept:
        entry = config.get(record["doc_key"])
        if entry is None:
            raise ConversionError(f"no entry for {record['doc_key']!r} in the config file")
        field = entry.get(EMBED_FIELD_KEY, DEFAULT_EMBED_FIELD)
        if field not in EMBED_FIELDS:
            raise ConversionError(f"{EMBED_FIELD_KEY} of {record['doc_key']!r} is {field!r}; known: {', '.join(EMBED_FIELDS)}")
        texts.append(record[field])
    return kept, texts


def endpoint(server: str, path: str) -> str:
    """The address of one service of the server."""
    return server.rstrip("/") + path


def post_json(url: str, body: dict, timeout: float, headers: dict[str, str] | None = None) -> dict:
    """What the server answers to a JSON request, sent with `headers` besides the JSON one. Raises when the address is
    malformed, when the server cannot be reached, refuses or reports an error, and when the answer is not a JSON object."""
    try:
        call = urllib.request.Request(url, data=json.dumps(body).encode(TEXT_ENCODING), headers={**JSON_HEADERS, **(headers or {})})
    except ValueError as error:
        raise ConversionError(f"{url} is not an address: the server's must look like {settings.LOCAL_SERVER}") from error
    try:
        with OPENER.open(call, timeout=timeout) as reply:
            answer = json.load(reply)
    except urllib.error.HTTPError as error:
        detail = (
            f"it redirected to {error.headers.get('Location')} (not followed: use that address)" if error.code in REDIRECT_CODES
            else error.read().decode(TEXT_ENCODING, errors="replace").strip()[:ERROR_BODY_CHARS]
        )
        raise ConversionError(f"{url} refused the request with status {error.code}: {detail}") from error
    except (OSError, http.client.HTTPException) as error:
        raise ConversionError(f"no answer from {url}: {str(error) or type(error).__name__}") from error
    except ValueError as error:
        raise ConversionError(f"{url} did not answer with JSON: {error}") from error
    if not isinstance(answer, dict):
        raise ConversionError(f"{url} did not answer with a JSON object")
    if API_ERROR in answer:  # LM Studio reports its errors with status 200
        raise ConversionError(f"{url} answered with an error: {str(answer[API_ERROR])[:ERROR_BODY_CHARS]}")
    return answer


def check_answered_by(answer: dict, model_name: str, url: str) -> None:
    """Raises when another model answered than the one asked for. LM Studio answers an id it does not know with a
    model it has loaded, and names that model in its answer."""
    answered_by = answer.get(API_MODEL)
    if answered_by != model_name:
        raise ConversionError(f"{url} answered with model {answered_by!r}, not {model_name!r}: load that model on the server")


class Embedder:
    """Turns texts into the model's dense vectors, each scaled to length 1, by asking the server for them."""

    def __init__(self, model_name: str, server: str):
        self.name = model_name
        self.url = endpoint(server, EMBEDDINGS_PATH)
        self._size: int | None = None

    @property
    def size(self) -> int:
        """How many numbers a vector has. Asked from the server when nothing was embedded yet."""
        if self._size is None:
            self.request([SIZE_PROBE])
        return self._size

    def request(self, texts: list[str]) -> np.ndarray:
        """One row per text, as the server returns them. Raises when the server cannot be reached, refuses, answers
        with another model than the one asked for (LM Studio does, for an id it does not know), or returns anything
        but one vector per text."""
        answer = post_json(self.url, {API_MODEL: self.name, API_INPUT: texts}, REQUEST_TIMEOUT)
        try:
            items = sorted(answer[API_DATA], key=lambda item: item[API_INDEX])
            numbers = [item[API_INDEX] for item in items]
            vectors = np.array([item[API_EMBEDDING] for item in items], dtype=np.float32)
        except (ValueError, KeyError, TypeError) as error:
            raise ConversionError(f"{self.url} did not answer with vectors: {type(error).__name__}: {error}") from error
        if numbers != list(range(len(texts))):
            raise ConversionError(f"{self.url} returned {len(items)} vectors for {len(texts)} texts, or numbered them wrongly")
        check_answered_by(answer, self.name, self.url)
        if vectors.ndim != 2 or not vectors.shape[1]:
            raise ConversionError(f"{self.url} returned {vectors.shape} numbers for {len(texts)} texts")
        if self._size is None:
            self._size = vectors.shape[1]
        elif vectors.shape[1] != self._size:
            raise ConversionError(f"{self.url} returned vectors of {vectors.shape[1]} numbers after vectors of {self._size}")
        return vectors

    def encode(self, texts: list[str]) -> np.ndarray:
        """One row per text, each of length 1."""
        if not texts:
            return np.zeros((0, self.size), dtype=np.float32)
        vectors = np.concatenate([self.request(texts[start:start + BATCH_SIZE]) for start in range(0, len(texts), BATCH_SIZE)])
        lengths = np.linalg.norm(vectors, axis=1, keepdims=True)
        if not (np.isfinite(lengths).all() and lengths.all()):
            raise ConversionError("the model returned a vector that is not a number, or all zeros")
        return vectors / lengths


@functools.cache
def chunk_sizer() -> Sizer:
    """Counts tokens the way json_to_chunks.py did when it sized the chunks. Loaded at the first count: search.py and
    eval_retrieval.py never need it."""
    return Sizer(DEFAULT_TOKENIZER, DEFAULT_MIN_TOKENS, DEFAULT_MAX_TOKENS)


def long_count(texts: list[str]) -> int:
    """How many of the texts are over the size the chunks are meant to stay under. The server cannot say: it does not
    report how many tokens it read."""
    sizer = chunk_sizer()
    return sum(sizer.count(text) + sizer.special_tokens > MAX_INPUT_TOKENS for text in texts)


def check_reference(embedder: Embedder) -> str:
    """Reproduce the example on the model's card. A wrong pooling gives plausible vectors and worse search results,
    which no later check would notice; here it is a different number. Stops when the server cannot be used."""
    if embedder.name not in REFERENCE:
        return f"no published example for {embedder.name}: its vectors are not checked"
    questions, passages, expected = REFERENCE[embedder.name]
    try:
        got = embedder.encode(questions) @ embedder.encode(passages).T
    except ConversionError as error:
        sys.exit(str(error))
    shown = np.round(got.astype(float), REFERENCE_DECIMALS).tolist()  # as float32, 0.6254 prints as 0.6254000067710876
    if not np.allclose(got, np.array(expected), atol=REFERENCE_TOLERANCE):
        sys.exit(f"{embedder.name} does not reproduce its model card: expected {expected}, got {shown}")
    return f"{embedder.name} reproduces its model card: {shown}"


def stored_vectors(path: Path, model_name: str, size: int) -> dict[str, np.ndarray]:
    """The vectors of an earlier run, by the hash of their text. Empty when there is no earlier result, when it was
    made with another model, or when it cannot be read."""
    if not pipeline_common.has_content(path):
        return {}
    try:
        with np.load(path) as stored:
            if str(stored[KEY_MODEL]) != model_name or stored[KEY_VECTORS].shape[1:] != (size,):
                return {}
            return dict(zip(stored[KEY_HASHES].tolist(), stored[KEY_VECTORS]))
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return {}


def convert(source: Path, out_path: Path, previous: Path, config: dict, embedder: Embedder) -> str:
    """Write the vectors of the chunks in `source` to `out_path`; `previous` is the result of an earlier run, if any.
    The returned note counts what was embedded, reused and left out."""
    records = read_chunks(source)
    kept, texts = texts_to_embed(records, config)
    if not kept:
        raise ConversionError("no chunks to embed")
    long = long_count(texts)  # before the server is asked: a tokenizer that cannot be loaded fails the file here
    hashes = [text_hash(text) for text in texts]
    known = stored_vectors(previous, embedder.name, embedder.size)
    new = [index for index, text_id in enumerate(hashes) if text_id not in known]
    fresh = embedder.encode([texts[index] for index in new])
    vectors = np.zeros((len(kept), embedder.size), dtype=np.float32)
    for row, index in enumerate(new):
        vectors[index] = fresh[row]
    for index, text_id in enumerate(hashes):
        if text_id in known:
            vectors[index] = known[text_id]
    if not np.isfinite(vectors).all():
        raise ConversionError("the model returned a vector that is not a number")

    with out_path.open("wb") as out:
        np.savez(out, **{
            KEY_IDS: np.array([record["id"] for record in kept]), KEY_VECTORS: vectors,
            KEY_HASHES: np.array(hashes), KEY_MODEL: np.array(embedder.name),
        })
    notes = [f"{len(kept)} vectors: {len(new)} embedded, {len(kept) - len(new)} reused"]
    if len(records) > len(kept):
        notes.append(f"{len(records) - len(kept)} {KIND_FRAGMENTED} left out")
    if long:
        notes.append(f"{long} over {MAX_INPUT_TOKENS} tokens")
    return "; ".join(notes)


def add_server_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server", default=DEFAULT_SERVER, help="address of the server that runs the models (default: %(default)s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    pipeline_common.add_dir_arguments(parser)
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="per-document settings (default: %(default)s)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="the embedding model's id on the server (default: %(default)s)")
    add_server_argument(parser)
    args = parser.parse_args()

    config = load_config(args.config)
    files = pipeline_common.collect_files(args.input_dir, JSONL_SUFFIX)
    embedder = Embedder(args.model, args.server)
    print(f"{check_reference(embedder)} (on {args.server})", flush=True)
    pipeline_common.run_batch(
        files, args.output_dir, NPZ_SUFFIX,
        lambda source, out_path: convert(
            source, out_path, pipeline_common.output_path(args.output_dir, source.stem, NPZ_SUFFIX), config, embedder,
        ),
        args.skip_existing,
    )


if __name__ == "__main__":
    main()
