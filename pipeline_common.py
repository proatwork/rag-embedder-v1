"""Constants and helpers shared by split_pages.py, pdf_to_md.py, docx_to_md.py, docx_to_txt.py, json_to_chunks.py and
embed_chunks.py."""

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path

PDF_SUFFIX = ".pdf"
DOCX_SUFFIX = ".docx"
MARKDOWN_SUFFIX = ".md"
JSON_SUFFIX = ".json"
JSONL_SUFFIX = ".jsonl"
NPZ_SUFFIX = ".npz"  # numpy's archive: the vectors of one chunk file
TEXT_SUFFIX = ".txt"
HIDDEN_PREFIX = "."
LOCK_FILE_PREFIX = "~$"  # Word's temporary lock files
UNSAFE_NAME_TAIL = ". "  # source names like "...construcțiilor..docx" would otherwise give "...construcțiilor..md"
PARTIAL_MARK = ".partial"  # in the name of an output that is still being written
# Romanian s and t with a cedilla (the legacy code points OCR and old Word files use) -> with a comma below.
CEDILLA_TO_COMMA = str.maketrans("şţŞŢ", "șțȘȚ")
PASSTHROUGH_SEPARATOR = "--"
DOCLING_MODULE = "docling"
# Docling is run through the interpreter, not through bin/docling: that launcher stops working when a venv is moved.
DOCLING_CONVERT = [sys.executable, "-m", f"{DOCLING_MODULE}.cli.main", "convert"]
TO_FLAG = "--to"  # the same name on these scripts and on `docling convert`
MARKDOWN_FORMAT = "md"  # the formats, as Docling names them
JSON_FORMAT = "json"
OUTPUT_FORMATS = {MARKDOWN_FORMAT: MARKDOWN_SUFFIX, JSON_FORMAT: JSON_SUFFIX}  # format -> the suffix of its files
DEFAULT_OUTPUT_FORMAT = MARKDOWN_FORMAT
# Docling embeds every image as base64 text by default, which swamps the text; mark where an image was instead.
# In JSON this only stops Docling from adding images: the pictures a Word file carries stay in, as base64.
IMAGE_EXPORT_FLAG = "--image-export-mode"
IMAGE_FLAGS = (IMAGE_EXPORT_FLAG, "placeholder")
# With --images the pictures are written as PNG files instead, in a folder of Docling's naming (<output stem>_artifacts).
REFERENCED_IMAGE_FLAGS = (IMAGE_EXPORT_FLAG, "referenced")
IMAGES_FLAG = "--images"
PAGE_IMAGES_FLAG = "--page-images"
TEXT_ENCODING = "utf-8"
URI_SEPARATOR = "/"
ARTIFACTS_DIR_SUFFIX = "_artifacts"
IMAGES_DIR_SUFFIX = "_images"  # the folder the pictures end up in, beside the .json: <name>_images
JSON_INDENT = 2  # as Docling writes it
PAGES_KEY = "pages"  # keys of Docling's JSON
IMAGE_KEY = "image"
URI_KEY = "uri"
OUTPUT_FILE_FLAG = "--output-file"  # names the result exactly, so odd source names get a clean output name

ConvertFile = Callable[[Path, Path], str]  # (source, file to write) -> a note for the status line; raises when it fails


class ConversionError(Exception):
    """A file could not be converted; the message says why."""


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def normalize_romanian(text: str) -> str:
    """NFC, with one form for each Romanian letter: the documents mix "ş ţ" and "ș ț"."""
    return nfc(text).translate(CEDILLA_TO_COMMA)


def output_path(out_dir: Path, name: str, suffix: str) -> Path:
    return out_dir / f"{name.rstrip(UNSAFE_NAME_TAIL)}{suffix}"


def add_dir_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input-dir", type=Path, required=True, help="folder with the files to convert")
    parser.add_argument("--output-dir", type=Path, required=True, help="folder the results are written to (created if missing)")
    parser.add_argument("--skip-existing", action="store_true", help="skip files whose output exists and is not empty")


def add_format_argument(parser: argparse.ArgumentParser) -> None:
    """The output format of the scripts that run Docling."""
    parser.add_argument(
        TO_FLAG, choices=list(OUTPUT_FORMATS), default=DEFAULT_OUTPUT_FORMAT,
        help="output format; json is Docling's full document, not only its text (default: %(default)s)",
    )


def add_images_arguments(parser: argparse.ArgumentParser) -> None:
    """The picture options of the scripts that run Docling (they need --to json)."""
    parser.add_argument(
        IMAGES_FLAG, action="store_true",
        help="write the pictures as PNG files in <name>_images beside the .json, which refers to them (needs --to json)",
    )
    parser.add_argument(
        PAGE_IMAGES_FLAG, action="store_true",
        help="with --images, also keep the image of every page (left out by default: large, and not a picture of the document)",
    )


def check_images_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if (args.images or args.page_images) and args.to != JSON_FORMAT:
        parser.error(f"{IMAGES_FLAG} and {PAGE_IMAGES_FLAG} need {TO_FLAG} {JSON_FORMAT}")
    if args.page_images and not args.images:
        parser.error(f"{PAGE_IMAGES_FLAG} needs {IMAGES_FLAG}")


def split_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    """(the script's own arguments, the arguments after "--" that go to `docling convert` unchanged)."""
    if PASSTHROUGH_SEPARATOR not in argv:
        return argv, []
    split_at = argv.index(PASSTHROUGH_SEPARATOR)
    return argv[:split_at], argv[split_at + 1:]


def collect_files(input_dir: Path, suffix: str) -> list[Path]:
    """The files with `suffix` directly inside `input_dir`, sorted. Hidden files and Word lock files are left out.
    Refused: a name with nothing left once its trailing dots and spaces are dropped, and two files whose outputs
    would overwrite each other (names that differ only in case, in trailing dots or spaces, or in how an accented
    letter is stored)."""
    if not input_dir.is_dir():
        sys.exit(f"not a folder: {input_dir}")
    files = sorted(
        path for path in input_dir.iterdir()
        if path.is_file() and path.suffix.lower() == suffix and not path.name.startswith((HIDDEN_PREFIX, LOCK_FILE_PREFIX))
    )
    if not files:
        sys.exit(f"no {suffix} files in {input_dir}")
    names: dict[str, Path] = {}
    for path in files:
        key = nfc(path.stem.rstrip(UNSAFE_NAME_TAIL)).casefold()
        if not key:
            sys.exit(f"cannot name the output of {path.name!r}: rename the file")
        if key in names:
            sys.exit(f"two inputs would write the same output ({key}): {names[key].name} and {path.name}")
        names[key] = path
    return files


def has_content(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def run_batch(files: list[Path], out_dir: Path, out_suffix: str, convert: ConvertFile, skip_existing: bool) -> None:
    """Convert every file to `out_dir/<name><out_suffix>`, printing one status line each. `convert` writes a hidden
    temporary file beside the output, which becomes the output only when `convert` did not raise and wrote something.
    So a failed file leaves no partial output for --skip-existing to trust, and does not destroy the output of an
    earlier run. The other files still run and the exit status is non-zero if any failed."""
    if out_dir.exists() and not out_dir.is_dir():
        sys.exit(f"not a folder: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)
    failed: list[str] = []
    for index, source in enumerate(files, start=1):
        out_path = output_path(out_dir, source.stem, out_suffix)
        print(f"== [{index}/{len(files)}] {source.name}  ->  {out_path}", flush=True)
        if out_path.exists() and out_path.samefile(source):
            print("   FAILED (the output would overwrite the source)", flush=True)
            failed.append(source.name)
            continue
        if skip_existing and has_content(out_path):
            print("   skipped (output exists)", flush=True)
            continue
        # The process id keeps two runs into one folder apart; the suffix stays last for tools that look at it.
        partial = out_path.with_name(f"{HIDDEN_PREFIX}{out_path.stem}.{os.getpid()}{PARTIAL_MARK}{out_suffix}")
        started = time.monotonic()
        note = ""
        try:
            note = convert(source, partial)
            if not partial.exists():
                problem = "no output written"
            elif not has_content(partial):
                problem = "empty output"
            else:
                os.replace(partial, out_path)
                problem = None
        except Exception as error:  # a malformed file must not stop the batch, whatever way it is malformed
            problem = str(error) if isinstance(error, ConversionError) else f"{type(error).__name__}: {error}"
        finally:
            partial.unlink(missing_ok=True)
        elapsed = f"{time.monotonic() - started:.0f}s"
        if problem is None:
            print(f"   ok in {elapsed}" + (f" ({note})" if note else ""), flush=True)
        else:
            print(f"   FAILED ({problem}) in {elapsed}", flush=True)
            failed.append(source.name)
    if failed:
        sys.exit("failed: " + ", ".join(failed))


def require_docling() -> None:
    if importlib.util.find_spec(DOCLING_MODULE) is None:
        sys.exit(f"{DOCLING_MODULE} is not installed for {sys.executable}; run this script with venvs/bin/python")


def run_docling(source: Path, out_path: Path, output_format: str, options: list[str], image_flags: tuple[str, ...] = IMAGE_FLAGS) -> str:
    """Convert one file to `output_format` (a key of OUTPUT_FORMATS) with `docling convert`. The paths are made
    absolute, so that a name starting with "-" is not taken for an option."""
    command = [
        *DOCLING_CONVERT, str(source.absolute()), TO_FLAG, output_format, *image_flags,
        OUTPUT_FILE_FLAG, str(out_path.absolute()), *options,
    ]
    returncode = subprocess.run(command).returncode
    if returncode != 0:
        raise ConversionError(f"{DOCLING_MODULE} exit {returncode}")
    return ""


def artifacts_dir_of(partial: Path) -> Path:
    """The folder Docling puts the pictures of a referenced output in, beside `partial`."""
    return partial.with_name(f"{partial.stem}{ARTIFACTS_DIR_SUFFIX}")


def attach_images(partial_json: Path, images_dir: Path, keep_page_images: bool) -> str:
    """Move the pictures Docling wrote beside `partial_json` to `images_dir` and make the JSON refer to them there,
    by a path relative to the JSON's own folder (Docling writes the absolute path of its temporary folder). The page
    images are deleted and unlinked unless `keep_page_images`. Nothing of an earlier run is touched until the new
    JSON is complete. Returns a note for the status line."""
    artifacts_dir = artifacts_dir_of(partial_json)
    if not has_content(partial_json) or not artifacts_dir.is_dir():
        raise ConversionError("no output written")  # Docling can exit 0 without writing when it fails on a document
    document = json.loads(partial_json.read_text(encoding=TEXT_ENCODING))
    in_artifacts = lambda uri: isinstance(uri, str) and Path(uri).parent.name == artifacts_dir.name  # noqa: E731
    if not keep_page_images:
        for page in document.get(PAGES_KEY, {}).values():
            image = page.get(IMAGE_KEY)
            if image and in_artifacts(image.get(URI_KEY)):
                (artifacts_dir / Path(image[URI_KEY]).name).unlink(missing_ok=True)
            page[IMAGE_KEY] = None
    pictures = 0

    def relink(node: object) -> None:
        nonlocal pictures
        if isinstance(node, dict):
            if in_artifacts(node.get(URI_KEY)):
                node[URI_KEY] = f"{images_dir.name}{URI_SEPARATOR}{Path(node[URI_KEY]).name}"
                pictures += 1
            for child in node.values():
                relink(child)
        elif isinstance(node, list):
            for child in node:
                relink(child)

    relink(document)
    partial_json.write_text(json.dumps(document, indent=JSON_INDENT), encoding=TEXT_ENCODING)
    shutil.rmtree(images_dir, ignore_errors=True)  # the pictures of an earlier run
    artifacts_dir.rename(images_dir)
    return f"{pictures} images in {images_dir.name}{URI_SEPARATOR}"


def run_docling_batch(args: argparse.Namespace, input_suffix: str, options: list[str]) -> None:
    """Convert every file with `input_suffix` in args.input_dir to the format args.to in args.output_dir, one Docling
    call per file, with `options` added to each call. `args` must come from a parser that add_format_argument and
    add_images_arguments set up."""
    require_docling()
    if args.images and IMAGE_EXPORT_FLAG in options:
        sys.exit(f"do not pass {IMAGE_EXPORT_FLAG} after {PASSTHROUGH_SEPARATOR} with {IMAGES_FLAG}: the script sets it")
    files = collect_files(args.input_dir, input_suffix)
    out_suffix = OUTPUT_FORMATS[args.to]

    def convert(source: Path, partial: Path) -> str:
        if not args.images:
            return run_docling(source, partial, args.to, options)
        try:
            run_docling(source, partial, args.to, options, REFERENCED_IMAGE_FLAGS)
            final = output_path(args.output_dir, source.stem, out_suffix)
            return attach_images(partial, final.with_name(f"{final.stem}{IMAGES_DIR_SUFFIX}"), args.page_images)
        finally:
            shutil.rmtree(artifacts_dir_of(partial), ignore_errors=True)  # still there only when the conversion failed

    run_batch(files, args.output_dir, out_suffix, convert, args.skip_existing)
