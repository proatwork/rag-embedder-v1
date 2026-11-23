"""Tests of the upload to Cloudflare (upload_to_cloudflare.py): the checks, the SQL, the vectors and the wrangler calls.

Run from the project folder with the virtual environment's interpreter:
    venvs/bin/python -m unittest tests.test_upload_to_cloudflare -v
Nothing is sent anywhere: wrangler (subprocess.run) and Workers AI (post_json) are stand-ins.
"""

import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import numpy as np

import settings
import upload_to_cloudflare as up
from embed_chunks import text_hash
from pipeline_common import ConversionError

MIGRATION = up.WORKER_DIR / "migrations" / "0001_init.sql"
STORE_TS = up.WORKER_DIR / "src" / "store.ts"
DOC_KEY = "P118-II"
CODE = "P118/2-2013"
SLUG = "p118-2-2013"
TOKEN = "token-that-must-stay-secret-0123456789"
ACCOUNT = "0123456789abcdef0123456789abcdef"
ENTRY = {
    "code": CODE, "title": "Normativ, Partea a II-a", "year": 2013, "status": "repealed", "profile": "decimal",
    "amended_by": ["Ordinul 1/2020"], "repealed_on": "2026-01-02", "gazette": "M. Of. nr. 595 bis/2013",
}
IMAGES_DIR = "../out/P118-II_images"


def png_name(number: int) -> str:
    return f"image_{number:06d}_{'ab' * 32}{number}.png"


def chunk(key: str, kind: str = "article", articles: tuple[str, ...] = (), images: tuple[int, ...] = (), code: str = CODE, **fields) -> dict:
    """A chunk record as json_to_chunks.py writes it, its norm fields made from an older config."""
    slug = re.sub(r"[^a-z0-9]+", "-", code.lower()).strip("-")
    record = {
        "id": f"{slug}:{key}:0", "kind": kind, "doc_key": DOC_KEY, "doc_code": code, "doc_title": "Old title",
        "edition_year": 1999, "status": "in_force", "amended_by": [], "repealed_on": None, "gazette": None,
        "heading_path": ["CAPITOLUL 1", "Generalități"], "label": key, "caption": "", "article": articles[0] if articles else None,
        "articles": list(articles), "pdf_pages": [4, 5], "text": f"Textul {key}.", "embed_text": f"{code} > {key}\nTextul {key}.",
        "prev_id": None, "next_id": None, "parent_id": None, "flags": ["has_table"],
        "images": [
            {"path": f"{IMAGES_DIR}/{png_name(number)}", "ref": f"#/pictures/{number}", "page": 10 + number, "width": 441,
             "height": 219, "caption": f"Figura {number}"}
            for number in images
        ],
    }
    record.update(fields)
    return record


class Fixture:
    """A chunks folder beside an out folder with the PNGs, as json_to_chunks.py leaves them."""

    def __init__(self, root: Path):
        self.root = root
        self.chunks_dir = root / "text_chunks"
        self.images_dir = root / "out" / "P118-II_images"
        self.chunks_dir.mkdir()
        self.images_dir.mkdir(parents=True)
        self.cache_dir = root / "cache"
        self.config_file = root / "chunk-config.json"
        self.write_config({DOC_KEY: ENTRY})

    def write_config(self, config: dict) -> None:
        self.config_file.write_text(json.dumps(config), encoding="utf-8")

    def png(self, number: int, content: bytes | None = None) -> Path:
        path = self.images_dir / png_name(number)
        path.write_bytes(content if content is not None else f"png {number}".encode())
        return path

    def write(self, records: list[dict], name: str = DOC_KEY) -> Path:
        for record in records:
            for image in record["images"]:
                number = int(image["ref"].rsplit("/", 1)[1])
                if not (self.images_dir / png_name(number)).exists():
                    self.png(number)
        path = self.chunks_dir / f"{name}.jsonl"
        path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
        return path

    def document(self, records: list[dict], config: dict | None = None) -> up.Document:
        return up.prepare(self.write(records), config or {DOC_KEY: ENTRY})


def schema() -> sqlite3.Connection:
    """An empty D1 database: the Worker's migration in SQLite, with the foreign keys D1 enforces."""
    db = sqlite3.connect(":memory:")
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(MIGRATION.read_text(encoding="utf-8"))
    return db


def rows(db: sqlite3.Connection, query: str) -> list[tuple]:
    return db.execute(query).fetchall()


FIRST = [
    chunk("art-1.1", articles=("1.1", "1.1", "1.2"), images=(0, 1, 0)),
    chunk("frag-1", kind="fragmented", images=(2,)),  # gets no vector, so it is not loaded
    chunk("art-1.3", articles=("1.3",), images=(1,), pdf_pages=None),
]
SECOND = [chunk("art-1.1", articles=("1.1",), images=(1,))]  # a re-chunk: fewer chunks and figures


class SqlTest(unittest.TestCase):
    """The SQL that replaces a norm, applied to the Worker's own schema."""

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.fixture = Fixture(Path(self.folder.name))
        self.db = schema()

    def tearDown(self):
        self.db.close()
        self.folder.cleanup()

    def test_figures_uploaded_without_a_bucket_match_no_file(self):
        self.db.executescript(up.replace_sql(self.fixture.document(FIRST), in_bucket=False))
        shas = {sha for (sha,) in self.db.execute("SELECT sha256 FROM figures")}
        self.assertEqual(shas, {up.NOT_IN_BUCKET})  # so a later upload with --bucket sends every PNG
        self.db.executescript(up.replace_sql(self.fixture.document(FIRST)))
        self.assertNotIn(up.NOT_IN_BUCKET, {sha for (sha,) in self.db.execute("SELECT sha256 FROM figures")})

    def test_the_rows_of_a_norm(self):
        self.db.executescript(up.replace_sql(self.fixture.document(FIRST)))
        self.assertEqual(rows(self.db, "SELECT * FROM norms"), [(
            DOC_KEY, CODE, ENTRY["title"], 2013, "repealed", '["Ordinul 1/2020"]', "2026-01-02", ENTRY["gazette"],
        )])
        chunks = rows(self.db, "SELECT id, doc_key, seq, kind, heading_path, articles, pdf_pages, flags, text FROM chunks ORDER BY seq")
        self.assertEqual(chunks, [
            (f"{SLUG}:art-1.1:0", DOC_KEY, 0, "article", '["CAPITOLUL 1", "Generalități"]', '["1.1", "1.1", "1.2"]', "[4, 5]", '["has_table"]', "Textul art-1.1."),
            (f"{SLUG}:art-1.3:0", DOC_KEY, 2, "article", '["CAPITOLUL 1", "Generalități"]', '["1.3"]', None, '["has_table"]', "Textul art-1.3."),
        ])
        self.assertEqual(rows(self.db, "SELECT chunk_id, number FROM chunk_articles ORDER BY chunk_id, number"), [
            (f"{SLUG}:art-1.1:0", "1.1"), (f"{SLUG}:art-1.1:0", "1.2"), (f"{SLUG}:art-1.3:0", "1.3"),
        ])
        self.assertEqual(rows(self.db, "SELECT id, page, width, height, caption FROM figures ORDER BY id"), [
            (f"{SLUG}/fig-0", 10, 441, 219, "Figura 0"), (f"{SLUG}/fig-1", 11, 441, 219, "Figura 1"),
        ])
        self.assertEqual(rows(self.db, "SELECT chunk_id, figure_id, position FROM chunk_figures ORDER BY chunk_id, position"), [
            (f"{SLUG}:art-1.1:0", f"{SLUG}/fig-0", 0), (f"{SLUG}:art-1.1:0", f"{SLUG}/fig-1", 1), (f"{SLUG}:art-1.3:0", f"{SLUG}/fig-1", 0),
        ])

    def test_loading_again_leaves_only_the_new_rows_and_keeps_the_norm(self):
        self.db.executescript(up.replace_sql(self.fixture.document(FIRST)))
        changed = {DOC_KEY: {**ENTRY, "title": "Titlu nou"}}
        self.db.executescript(up.replace_sql(self.fixture.document(SECOND, changed)))
        self.assertEqual(rows(self.db, "SELECT doc_key, title FROM norms"), [(DOC_KEY, "Titlu nou")])
        self.assertEqual(rows(self.db, "SELECT id FROM chunks"), [(f"{SLUG}:art-1.1:0",)])
        self.assertEqual(rows(self.db, "SELECT chunk_id, number FROM chunk_articles"), [(f"{SLUG}:art-1.1:0", "1.1")])
        self.assertEqual(rows(self.db, "SELECT id FROM figures"), [(f"{SLUG}/fig-1",)])
        self.assertEqual(rows(self.db, "SELECT figure_id, position FROM chunk_figures"), [(f"{SLUG}/fig-1", 0)])

    def test_another_norm_is_left_alone(self):
        other_key = "P118-III"
        other = chunk("art-9", articles=("9",), code="P118/3-2015", doc_key=other_key)
        config = {DOC_KEY: ENTRY, other_key: {**ENTRY, "code": "P118/3-2015"}}
        self.db.executescript(up.replace_sql(up.prepare(self.fixture.write([other], other_key), config)))
        self.db.executescript(up.replace_sql(self.fixture.document(FIRST)))
        self.db.executescript(up.replace_sql(self.fixture.document(SECOND)))
        self.assertEqual(rows(self.db, "SELECT doc_key, count(*) FROM chunks GROUP BY doc_key ORDER BY doc_key"), [(DOC_KEY, 1), (other_key, 1)])

    def test_the_file_has_no_transaction_and_never_replaces_the_norms_row(self):
        sql = up.replace_sql(self.fixture.document(FIRST))
        self.assertNotRegex(sql.upper(), r"\b(BEGIN|COMMIT|ROLLBACK)\b|INSERT OR REPLACE")
        self.assertIn("ON CONFLICT (doc_key) DO UPDATE", sql)

    def test_quotes_and_odd_text_are_literals(self):
        tricky = "O'Brien; DROP TABLE norms; -- \"x\"\nrând nou"
        self.db.executescript(up.replace_sql(self.fixture.document([chunk("art-1", text=tricky)])))
        self.assertEqual(rows(self.db, "SELECT text FROM chunks"), [(tricky,)])

    def test_a_statement_over_d1s_limit_is_refused(self):
        document = self.fixture.document([chunk("art-1", text="x" * (up.MAX_STATEMENT_BYTES + 1))])
        with self.assertRaisesRegex(ConversionError, "over D1's"):
            up.replace_sql(document)


class CheckTest(unittest.TestCase):
    """What is refused before anything is sent, and where a norm's fields come from."""

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.fixture = Fixture(Path(self.folder.name))

    def tearDown(self):
        self.folder.cleanup()

    def test_an_id_over_64_bytes_is_refused_with_what_to_do(self):
        long_code = "NORMATIV PRIVIND SECURITATEA LA INCENDIU A CONSTRUCȚIILOR P118/2-2013"
        with self.assertRaisesRegex(ConversionError, r"over 64 bytes.*chunk it again"):
            self.fixture.document([chunk("art-1.1", code=long_code)])

    def test_an_id_of_64_bytes_is_kept_and_a_long_fragmented_one_does_not_matter(self):
        exact = "x" * (up.MAX_VECTOR_ID_BYTES - 2) + "ș"  # 64 bytes in UTF-8, 63 characters
        document = self.fixture.document([chunk("a", id=exact), chunk("b", kind="fragmented", id="y" * 80)])
        self.assertEqual(document.ids, [exact])

    def test_two_documents_with_one_code_are_refused(self):
        self.fixture.write([chunk("art-1")])
        self.fixture.write([chunk("art-2", doc_key="P118-II-old")], "P118-II-old")
        config = {DOC_KEY: ENTRY, "P118-II-old": ENTRY}
        with self.assertRaises(SystemExit) as stop:
            up.prepare_all(sorted(self.fixture.chunks_dir.glob("*.jsonl")), config)
        self.assertIn("same code", str(stop.exception.code))

    def test_a_document_without_a_config_entry_is_refused(self):
        with self.assertRaisesRegex(ConversionError, "no entry"):
            self.fixture.document([chunk("art-1")], {"other": ENTRY})

    def test_the_norm_comes_from_the_config_not_the_chunks(self):
        document = self.fixture.document([chunk("art-1")])  # its chunks say "Old title", 1999, in_force
        self.assertEqual(document.norm, {
            "doc_key": DOC_KEY, "code": CODE, "title": ENTRY["title"], "edition_year": 2013, "status": "repealed",
            "amended_by": '["Ordinul 1/2020"]', "repealed_on": "2026-01-02", "gazette": ENTRY["gazette"],
        })

    def test_a_figure_is_named_by_the_code_and_doclings_number_and_hashed(self):
        content = b"\x89PNG fake bytes"
        self.fixture.png(12, content)
        document = self.fixture.document([chunk("art-1", images=(12,))])
        figure = document.figures["#/pictures/12"]
        self.assertEqual(figure.id, f"{SLUG}/fig-12")
        self.assertEqual(figure.r2_key, f"{SLUG}/{png_name(12)}")
        self.assertEqual(figure.sha256, hashlib.sha256(content).hexdigest())
        self.assertEqual((figure.page, figure.width, figure.height, figure.caption), (22, 441, 219, "Figura 12"))

    def test_a_missing_picture_file_is_refused(self):
        path = self.fixture.write([chunk("art-1", images=(3,))])
        (self.fixture.images_dir / png_name(3)).unlink()
        with self.assertRaisesRegex(ConversionError, "is not at"):
            up.prepare(path, {DOC_KEY: ENTRY})


def ai_answer(count: int, size: int = up.EMBEDDING_DIMENSIONS) -> dict:
    return {"success": True, "errors": [], "result": {"shape": [count, size], "data": [[0.5] * size for _ in range(count)]}}


def vector_of(text: str) -> list[float]:
    """The stand-in's vector of a text: its own, so that a vector given to the wrong chunk shows."""
    value = int(text_hash(text)[:4], 16) / 2 ** 16 + 0.5  # exact in float32
    return [value] * up.EMBEDDING_DIMENSIONS


def fake_post(url: str, body: dict, timeout: float, headers: dict | None = None) -> dict:
    texts = body[up.AI_PASSAGES]
    return {"success": True, "errors": [], "result": {"shape": [len(texts), up.EMBEDDING_DIMENSIONS], "data": [vector_of(text) for text in texts]}}


CREDENTIALS = up.Credentials(ACCOUNT, TOKEN)


class VectorTest(unittest.TestCase):
    """The embedding, its cache and the NDJSON."""

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.fixture = Fixture(Path(self.folder.name))

    def tearDown(self):
        self.folder.cleanup()

    def test_the_request_is_the_workers_ai_one(self):
        document = self.fixture.document([chunk("art-1")])
        with mock.patch.object(up, "post_json", side_effect=fake_post) as post:
            up.embed(document, self.fixture.cache_dir, CREDENTIALS)
        url, body, _, headers = post.call_args.args
        self.assertEqual(url, f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/ai/run/@cf/qwen/qwen3-embedding-0.6b")
        self.assertEqual(body, {"documents": [document.texts[0]]})
        self.assertEqual(headers, {"Authorization": f"Bearer {TOKEN}"})

    def test_the_texts_are_sent_in_batches(self):
        document = self.fixture.document([chunk(f"art-{n}") for n in range(up.AI_BATCH_SIZE + 1)])
        with mock.patch.object(up, "post_json", side_effect=fake_post) as post:
            up.embed(document, self.fixture.cache_dir, CREDENTIALS)
        self.assertEqual([len(call.args[1]["documents"]) for call in post.call_args_list], [up.AI_BATCH_SIZE, 1])
        self.assertTrue(np.array_equal(document.vectors, [vector_of(text) for text in document.texts]))  # each on its own chunk

    def test_a_request_holds_at_most_so_many_characters(self):
        half = "x" * (up.AI_BATCH_CHARS // 2)  # two of them fill a request exactly
        long = "y" * (up.AI_BATCH_CHARS + 1)
        batches = up.ai_batches([half, half, "z", long, "w"])
        self.assertEqual([[len(text) for text in batch] for batch in batches],
                         [[len(half)] * 2, [1], [len(long)], [1]])  # the long one alone, the rest in order
        self.assertEqual(up.ai_batches([]), [])

    def test_a_request_too_big_for_the_model_is_split_until_it_fits(self):
        def picky_post(url, body, timeout, headers=None):
            if len(body[up.AI_PASSAGES]) > 2:
                raise ConversionError(f'{url} refused the request with status 400: {{"errors":[{{"message":"AiError: AiError: {up.AI_TOO_BIG} (x)","code":3030}}]}}')
            return fake_post(url, body, timeout, headers)
        document = self.fixture.document([chunk(f"art-{n}") for n in range(5)])
        with mock.patch.object(up, "post_json", side_effect=picky_post) as post:
            up.embed(document, self.fixture.cache_dir, CREDENTIALS)
        self.assertEqual([len(call.args[1]["documents"]) for call in post.call_args_list], [5, 2, 3, 1, 2])
        self.assertTrue(np.array_equal(document.vectors, [vector_of(text) for text in document.texts]))  # in order

    def test_the_error_code_alone_also_splits_a_request(self):
        def coded_post(url, body, timeout, headers=None):
            if len(body[up.AI_PASSAGES]) > 1:
                raise ConversionError(f'{url} refused the request with status 400: {{"errors":[{{"message":"reworded","code":3030}}]}}')
            return fake_post(url, body, timeout, headers)
        document = self.fixture.document([chunk("art-1"), chunk("art-2")])
        with mock.patch.object(up, "post_json", side_effect=coded_post) as post:
            up.embed(document, self.fixture.cache_dir, CREDENTIALS)
        self.assertEqual(post.call_count, 3)

    def test_a_single_text_too_big_and_any_other_refusal_are_not_sent_again(self):
        too_big = ConversionError(f"refused the request with status 400: {up.AI_TOO_BIG}")
        for error, texts in ((too_big, 1), (ConversionError("refused the request with status 401"), 3)):
            document = self.fixture.document([chunk(f"art-{n}") for n in range(texts)])
            with self.subTest(error=str(error)), mock.patch.object(up, "post_json", side_effect=error) as post:
                with self.assertRaises(ConversionError):
                    up.embed(document, self.fixture.cache_dir, CREDENTIALS)
                self.assertEqual(post.call_count, 1)

    def test_only_changed_texts_are_embedded_again(self):
        with mock.patch.object(up, "post_json", side_effect=fake_post):
            up.embed(self.fixture.document([chunk("art-1"), chunk("art-2")]), self.fixture.cache_dir, CREDENTIALS)
        again = self.fixture.document([chunk("art-1"), chunk("art-2", embed_text="Text schimbat")])
        with mock.patch.object(up, "post_json", side_effect=fake_post) as post:
            up.embed(again, self.fixture.cache_dir, CREDENTIALS)
        self.assertEqual([call.args[1]["documents"] for call in post.call_args_list], [["Text schimbat"]])
        self.assertEqual(again.embedded, 1)
        self.assertTrue(np.array_equal(again.vectors, [vector_of(again.texts[0]), vector_of("Text schimbat")]))

    def test_nothing_is_sent_when_every_text_is_cached(self):
        records = [chunk("art-1")]
        with mock.patch.object(up, "post_json", side_effect=fake_post):
            up.embed(self.fixture.document(records), self.fixture.cache_dir, CREDENTIALS)
        with mock.patch.object(up, "post_json", side_effect=AssertionError("sent")):
            up.embed(self.fixture.document(records), self.fixture.cache_dir, CREDENTIALS)

    def test_vectors_of_the_wrong_size_or_number_are_refused(self):
        document = self.fixture.document([chunk("art-1")])
        for answer in (ai_answer(1, up.EMBEDDING_DIMENSIONS - 1), ai_answer(2), {"success": False, "errors": ["no"]}, {"success": True}):
            with self.subTest(answer=str(answer)[:60]), mock.patch.object(up, "post_json", return_value=answer):
                with self.assertRaises(ConversionError):
                    up.embed(document, self.fixture.cache_dir, CREDENTIALS)
        self.assertFalse(up.cache_path(self.fixture.cache_dir, document).exists())

    def test_a_vector_that_is_not_a_number_is_refused(self):
        answer = ai_answer(1)
        answer["result"]["data"][0][0] = float("nan")
        with mock.patch.object(up, "post_json", return_value=answer), self.assertRaises(ConversionError):
            up.embed(self.fixture.document([chunk("art-1")]), self.fixture.cache_dir, CREDENTIALS)

    def test_the_ndjson_lines(self):
        document = self.fixture.document([chunk("art-1"), chunk("art-2")])
        document.vectors = np.array([[0.25] * up.EMBEDDING_DIMENSIONS, [0.5] * up.EMBEDDING_DIMENSIONS], dtype=np.float32)
        lines = [json.loads(line) for line in up.ndjson(document).splitlines()]
        self.assertEqual([line["id"] for line in lines], [f"{SLUG}:art-1:0", f"{SLUG}:art-2:0"])
        self.assertEqual([line["metadata"] for line in lines], [{"norm": CODE}, {"norm": CODE}])
        self.assertEqual(lines[1]["values"], [0.5] * up.EMBEDDING_DIMENSIONS)
        self.assertEqual(set(lines[0]), {"id", "values", "metadata"})

    def test_the_stale_ids_are_the_old_ones_no_longer_there(self):
        self.assertEqual(up.stale_ids({"a", "b", "c"}, ["b", "d"]), ["a", "c"])
        self.assertEqual(up.stale_ids(set(), ["b"]), [])


class FakeWrangler:
    """Stands in for subprocess.run: records every command, keeps what the files it was given held, and answers the
    read of D1 with `remote_rows`."""

    def __init__(self, remote_rows: list[dict] | None = None):
        self.remote_rows = remote_rows or []
        self.calls: list[tuple[list[str], dict]] = []
        self.files: dict[str, str] = {}

    def __call__(self, command: list[str], **options) -> subprocess.CompletedProcess:
        self.calls.append((command, options))
        if "--file" in command:
            path = command[command.index("--file") + 1]
            self.files[" ".join(command[2:4])] = Path(path).read_bytes().decode("utf-8", errors="replace")
        stdout = ""
        if command[2:4] == ["d1", "execute"] and "--json" in command:
            stdout = json.dumps([{"results": self.remote_rows, "success": True, "meta": {}}])
        return subprocess.CompletedProcess(command, 0, stdout, "")

    def commands(self) -> list[str]:
        return [" ".join(command[2:5]) for command, _ in self.calls]


class CommandTest(unittest.TestCase):
    """The script as it is run: what it calls, in what order, and what it never does."""

    def setUp(self):
        self.folder = TemporaryDirectory()
        self.fixture = Fixture(Path(self.folder.name))
        self.environment = mock.patch.dict(os.environ, {
            settings.FILE_VARIABLE: "", settings.CLOUDFLARE_ACCOUNT_ID: ACCOUNT, settings.CLOUDFLARE_API_TOKEN: TOKEN,
        })
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.folder.cleanup()

    def run_main(
        self, *options: str, wrangler: FakeWrangler | None = None, posted: mock.Mock | None = None,
        answer: str | BaseException = "", terminal: bool = True,
    ) -> tuple[FakeWrangler, mock.Mock, str]:
        """Runs main with `options`; the stand-ins for wrangler and Workers AI can be passed in, to look at them after
        main stopped. `answer` is what is typed at the terminal, or what input() raises."""
        wrangler = wrangler or FakeWrangler()
        posted = posted or mock.Mock(side_effect=fake_post)
        out = io.StringIO()
        with mock.patch.object(up.sys, "argv", ["upload_to_cloudflare.py", *options]), \
                mock.patch.object(up.subprocess, "run", side_effect=wrangler), \
                mock.patch.object(up, "post_json", posted), \
                mock.patch("embed_chunks.OPENER.open", side_effect=AssertionError("no request may be made")), \
                mock.patch("builtins.input", side_effect=[answer] if isinstance(answer, str) else answer), \
                mock.patch.object(up.sys, "stdin", mock.Mock(isatty=mock.Mock(return_value=terminal))), redirect_stdout(out):
            up.main()
        return wrangler, posted, out.getvalue()

    def upload_options(self, *more: str) -> list[str]:
        return ["--chunks-dir", str(self.fixture.chunks_dir), "--config", str(self.fixture.config_file), "--cache-dir", str(self.fixture.cache_dir), *more]

    def test_an_upload_reads_upserts_puts_deletes_then_replaces_in_d1(self):
        self.fixture.write(FIRST)
        old_png = self.fixture.png(0)
        remote = [
            {"kind": "norm", "id": DOC_KEY, "sha256": None, "r2_key": None},
            {"kind": "chunk", "id": f"{SLUG}:art-gone:0", "sha256": None, "r2_key": None},
            {"kind": "chunk", "id": f"{SLUG}:art-1.1:0", "sha256": None, "r2_key": None},
            {"kind": "figure", "id": f"{SLUG}/fig-0", "sha256": up.file_sha256(old_png), "r2_key": f"{SLUG}/{png_name(0)}"},
        ]
        wrangler, _, out = self.run_main(*self.upload_options("--bucket", "norms-figures"), wrangler=FakeWrangler(remote))
        self.assertEqual(wrangler.commands(), [
            "d1 execute norms", "vectorize upsert norms", "r2 object put", "vectorize delete-vectors norms", "d1 execute norms",
        ])
        put = wrangler.calls[2][0]
        self.assertEqual(put[5:], [
            f"norms-figures/{SLUG}/{png_name(1)}", "--file", str(self.fixture.images_dir / png_name(1)), "--remote", "--content-type", "image/png",
        ])
        self.assertEqual(wrangler.calls[3][0][5:], ["--ids", f"{SLUG}:art-gone:0"])
        for command, options in wrangler.calls:
            self.assertEqual(command[:2], ["npx", "wrangler"])
            self.assertEqual(options["cwd"], up.WORKER_DIR)
            if command[2] in ("d1", "r2"):
                self.assertIn("--remote", command)
        self.assertIn("2 chunks (2 embedded, 0 reused), 1 stale vectors deleted, 2 figures, 1 PNGs uploaded, 1 unchanged", out)

        db = schema()
        db.executescript(wrangler.files["d1 execute"])
        self.assertEqual(rows(db, "SELECT count(*) FROM chunks"), [(2,)])
        upserted = [json.loads(line) for line in wrangler.files["vectorize upsert"].splitlines()]
        self.assertEqual(len(upserted[0]["values"]), up.EMBEDDING_DIMENSIONS)

    def test_an_upload_records_a_figure_hash_only_when_its_png_went_to_the_bucket(self):
        self.fixture.write(FIRST)
        for options, expected in ((self.upload_options(), {up.NOT_IN_BUCKET}), (self.upload_options("--bucket", "norms-figures"), None)):
            wrangler, _, _ = self.run_main(*options)
            db = schema()
            db.executescript(wrangler.files["d1 execute"])
            hashes = {sha for (sha,) in db.execute("SELECT sha256 FROM figures")}
            files = {up.file_sha256(self.fixture.images_dir / png_name(number)) for number in (0, 1)}
            self.assertEqual(hashes, expected if expected is not None else files)

    def test_the_token_is_never_on_a_command_line_nor_in_a_file(self):
        self.fixture.write(FIRST)
        wrangler, _, out = self.run_main(*self.upload_options("--bucket", "norms-figures"))
        self.assertTrue(wrangler.calls)
        for command, options in wrangler.calls:
            self.assertFalse(any(TOKEN in argument for argument in command), command)
            self.assertEqual(options["env"][settings.CLOUDFLARE_API_TOKEN], TOKEN)
            self.assertEqual(options["env"][settings.CLOUDFLARE_ACCOUNT_ID], ACCOUNT)
        for text in wrangler.files.values():
            self.assertNotIn(TOKEN, text)
        self.assertNotIn(TOKEN, out)

    def test_credentials_from_the_settings_file_reach_wrangler_only_through_its_environment(self):
        settings_file = self.fixture.root / "settings.env"
        settings_file.write_text(f"CLOUDFLARE_ACCOUNT_ID={ACCOUNT}\nCLOUDFLARE_API_TOKEN={TOKEN}\n", encoding="utf-8")
        environment = {key: value for key, value in os.environ.items() if not key.startswith("CLOUDFLARE_")}
        with mock.patch.dict(os.environ, {**environment, settings.FILE_VARIABLE: str(settings_file)}, clear=True):
            self.fixture.write(FIRST)
            wrangler, posted, _ = self.run_main(*self.upload_options())
            self.assertNotIn(settings.CLOUDFLARE_API_TOKEN, os.environ)
        self.assertEqual(wrangler.calls[0][1]["env"][settings.CLOUDFLARE_API_TOKEN], TOKEN)
        self.assertEqual(posted.call_args.args[3], {"Authorization": f"Bearer {TOKEN}"})

    def test_without_a_bucket_no_png_is_sent(self):
        self.fixture.write(FIRST)
        wrangler, _, out = self.run_main(*self.upload_options())
        self.assertNotIn("r2 object put", wrangler.commands())
        self.assertNotIn("vectorize delete-vectors norms", wrangler.commands())  # nothing was there
        self.assertIn("2 PNGs not uploaded (no --bucket)", out)

    def test_a_failing_check_leaves_cloudflare_untouched(self):
        self.fixture.write(FIRST)
        self.fixture.write([chunk("art-1", doc_key="Other")], "Other")  # no config entry
        wrangler, posted = FakeWrangler(), mock.Mock(side_effect=fake_post)
        with self.assertRaises(SystemExit) as stop:
            self.run_main(*self.upload_options(), wrangler=wrangler, posted=posted)
        self.assertIn("no entry for 'Other'", str(stop.exception.code))
        self.assertEqual(wrangler.calls, [])
        posted.assert_not_called()

    def test_a_failing_embedding_leaves_cloudflare_untouched(self):
        self.fixture.write(FIRST)
        wrangler = FakeWrangler()
        with self.assertRaises(SystemExit) as stop:
            self.run_main(*self.upload_options(), wrangler=wrangler, posted=mock.Mock(return_value=ai_answer(2, 3)))
        self.assertIn("not 1024 numbers", str(stop.exception.code))
        self.assertEqual(wrangler.calls, [])

    def test_a_code_another_norm_holds_in_d1_stops_before_the_vectors(self):
        self.fixture.write(FIRST)
        wrangler = FakeWrangler([{"kind": "code_holder", "id": "Normativ_P118_2_2013", "sha256": None, "r2_key": None}])
        with self.assertRaises(SystemExit) as stop:
            self.run_main(*self.upload_options(), wrangler=wrangler)
        self.assertEqual(wrangler.commands(), ["d1 execute norms"])
        self.assertIn("failed: P118-II", str(stop.exception.code))

    def test_without_credentials_nothing_is_sent(self):
        self.fixture.write(FIRST)
        wrangler, posted = FakeWrangler(), mock.Mock(side_effect=fake_post)
        with mock.patch.dict(os.environ, {settings.CLOUDFLARE_API_TOKEN: ""}), self.assertRaises(SystemExit) as stop:
            self.run_main(*self.upload_options(), wrangler=wrangler, posted=posted)
        self.assertIn(settings.CLOUDFLARE_API_TOKEN, str(stop.exception.code))
        self.assertEqual(wrangler.calls, [])
        posted.assert_not_called()

    def dry_run(self, wrangler: FakeWrangler, posted: mock.Mock) -> tuple[Path, str]:
        """Runs --dry-run without credentials; the folder it wrote to, and what it printed."""
        folder = self.fixture.root / "dry-run"
        folder.mkdir(exist_ok=True)
        with mock.patch.dict(os.environ, {settings.CLOUDFLARE_API_TOKEN: "", settings.CLOUDFLARE_ACCOUNT_ID: ""}), \
                mock.patch.object(up.tempfile, "mkdtemp", return_value=str(folder)):
            _, _, out = self.run_main(*self.upload_options("--dry-run", "--bucket", "norms-figures"), wrangler=wrangler, posted=posted)
        return folder, out

    def test_a_dry_run_sends_nothing_and_writes_the_files(self):
        self.fixture.write(FIRST)
        wrangler, posted = FakeWrangler(), mock.Mock(side_effect=fake_post)
        folder, out = self.dry_run(wrangler, posted)
        self.assertEqual(wrangler.calls, [])
        posted.assert_not_called()
        self.assertIn(f"files are in {folder}", out)
        db = schema()
        db.executescript((folder / f"{DOC_KEY}.sql").read_text(encoding="utf-8"))
        self.assertEqual(rows(db, "SELECT count(*) FROM chunks"), [(2,)])
        lines = [json.loads(line) for line in (folder / f"{DOC_KEY}.ndjson").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([line["values"] for line in lines], [None, None])  # nothing cached yet
        self.assertIn("2 to embed, 0 cached", out)

    def test_a_dry_run_writes_the_cached_vectors_and_null_for_the_others(self):
        path = self.fixture.write(FIRST)
        with mock.patch.object(up, "post_json", side_effect=fake_post):
            up.embed(up.prepare(path, {DOC_KEY: ENTRY}), self.fixture.cache_dir, CREDENTIALS)
        changed = [FIRST[0], FIRST[1], {**FIRST[2], "embed_text": "Text schimbat"}]
        self.fixture.write(changed)
        folder, out = self.dry_run(FakeWrangler(), mock.Mock(side_effect=AssertionError("sent")))
        lines = [json.loads(line) for line in (folder / f"{DOC_KEY}.ndjson").read_text(encoding="utf-8").splitlines()]
        self.assertIsNone(lines[1]["values"])
        self.assertTrue(np.array_equal(lines[0]["values"], vector_of(FIRST[0]["embed_text"])))
        self.assertIn("1 to embed, 1 cached", out)

    def test_remove_without_yes_and_no_answer_does_nothing(self):
        for answer in ("no", "", "y"):
            with self.subTest(answer=answer):
                wrangler, posted, out = self.run_main("--remove", DOC_KEY, "--bucket", "norms-figures", answer=answer)
                self.assertEqual(wrangler.calls, [])
                posted.assert_not_called()
                self.assertIn("nothing deleted", out)

    def test_remove_asks_and_goes_on_only_on_yes(self):
        remote = [{"kind": "norm", "id": DOC_KEY, "sha256": None, "r2_key": None}]
        wrangler, _, _ = self.run_main("--remove", DOC_KEY, answer="yes", wrangler=FakeWrangler(remote))
        self.assertEqual(wrangler.commands(), ["d1 execute norms", "d1 execute norms"])

    def test_remove_with_no_answer_or_no_terminal_does_nothing(self):
        for options in ({"answer": EOFError()}, {"answer": "yes", "terminal": False}):
            with self.subTest(**{key: str(value) for key, value in options.items()}):
                wrangler, _, out = self.run_main("--remove", DOC_KEY, **options)
                self.assertEqual(wrangler.calls, [])
                self.assertIn("nothing deleted", out)

    def test_remove_deletes_the_vectors_the_pngs_then_the_rows(self):
        remote = [
            {"kind": "norm", "id": DOC_KEY, "sha256": None, "r2_key": None},
            {"kind": "chunk", "id": f"{SLUG}:art-1:0", "sha256": None, "r2_key": None},
            {"kind": "figure", "id": f"{SLUG}/fig-0", "sha256": "abc", "r2_key": f"{SLUG}/{png_name(0)}"},
        ]
        wrangler, _, out = self.run_main("--remove", DOC_KEY, "--bucket", "norms-figures", "--yes", wrangler=FakeWrangler(remote))
        self.assertEqual(wrangler.commands(), [
            "d1 execute norms", "vectorize delete-vectors norms", "r2 object delete", "d1 execute norms",
        ])
        self.assertEqual(wrangler.calls[2][0][5:], [f"norms-figures/{SLUG}/{png_name(0)}", "--remote"])
        self.assertEqual(wrangler.calls[3][0][-1], f"DELETE FROM norms WHERE doc_key = '{DOC_KEY}'")
        for command, _ in wrangler.calls:
            self.assertNotIn(TOKEN, " ".join(command))
        self.assertIn("1 vectors and the rows deleted, 1 PNGs deleted", out)

    def test_remove_of_what_is_not_there_deletes_nothing(self):
        wrangler, _, out = self.run_main("--remove", DOC_KEY, "--yes")
        self.assertEqual(wrangler.commands(), ["d1 execute norms"])
        self.assertIn("nothing deleted", out)

    def test_the_remove_statement_cascades_to_every_row_of_the_norm(self):
        db = schema()
        db.executescript(up.replace_sql(self.fixture.document(FIRST)))
        db.execute(f"DELETE FROM norms WHERE doc_key = {up.literal(DOC_KEY)}")
        for table in ("norms", "chunks", "chunk_articles", "figures", "chunk_figures"):
            self.assertEqual(rows(db, f"SELECT count(*) FROM {table}"), [(0,)], table)


class D1OutputTest(unittest.TestCase):
    """The reading of `wrangler d1 execute --json`."""

    def test_the_rows_of_every_result(self):
        output = json.dumps([{"results": [{"a": 1}], "success": True}, {"results": [{"a": 2}], "success": True}])
        self.assertEqual(up.d1_rows(output), [{"a": 1}, {"a": 2}])

    def test_a_line_before_the_json_is_skipped(self):
        self.assertEqual(up.d1_rows('Proxy warning\n[{"results": []}]'), [])

    def test_anything_else_is_refused(self):
        for output in ("", "not json", '{"error": "x"}', '[{"rows": []}]'):
            with self.subTest(output=output), self.assertRaises(ConversionError):
                up.d1_rows(output)


class WorkerAgreementTest(unittest.TestCase):
    """The constants the Worker reads its data with: the model, the vectors' size, the filter field and the names."""

    def test_the_model_and_the_vectors_are_the_workers(self):
        source = STORE_TS.read_text(encoding="utf-8")
        constant = lambda name: re.search(rf'export const {name} = "?([^";]+)"?;', source).group(1)  # noqa: E731
        self.assertEqual(up.EMBEDDING_MODEL, constant("EMBEDDING_MODEL"))
        self.assertEqual(str(up.EMBEDDING_DIMENSIONS), constant("EMBEDDING_DIMENSIONS"))
        self.assertEqual(up.NORM_FILTER_KEY, constant("NORM_FILTER_KEY"))

    def test_the_database_and_index_are_named_as_in_wrangler_jsonc(self):
        config = (up.WORKER_DIR / "wrangler.jsonc").read_text(encoding="utf-8")
        self.assertIn(f'"database_name": "{up.D1_DATABASE}"', config)
        self.assertIn(f'"index_name": "{up.VECTORIZE_INDEX}"', config)

    def test_the_id_limit_is_the_schemas(self):
        self.assertIn(f"length(CAST(id AS BLOB)) <= {up.MAX_VECTOR_ID_BYTES}", MIGRATION.read_text(encoding="utf-8"))


class CredentialsTest(unittest.TestCase):
    def test_an_account_id_that_is_not_one_is_refused(self):
        with mock.patch.dict(os.environ, {settings.FILE_VARIABLE: "", settings.CLOUDFLARE_ACCOUNT_ID: "../x", settings.CLOUDFLARE_API_TOKEN: TOKEN}), \
                self.assertRaises(SystemExit) as stop:
            up.read_credentials()
        self.assertIn(settings.CLOUDFLARE_ACCOUNT_ID, str(stop.exception.code))


if __name__ == "__main__":
    unittest.main()
