"""Convert a folder of PDFs to Markdown or JSON with Docling, OCR included: one .md (or .json) per PDF.

Every .pdf directly inside --input-dir becomes --output-dir/<name>.md, or <name>.json with --to json. Docling reads
each PDF page as one page: give it PDFs with one book page per page (see split_pages.py for scanned double pages).

Usage (run with the virtual environment's interpreter, the one Docling is installed in):
    venvs/bin/python pdf_to_md.py --input-dir docs/single_paged --output-dir OUT
    venvs/bin/python pdf_to_md.py --input-dir IN --output-dir OUT --to json           # Docling's full document
    venvs/bin/python pdf_to_md.py --input-dir IN --output-dir OUT --skip-existing     # resume
    venvs/bin/python pdf_to_md.py --input-dir IN --output-dir OUT -- --page-range 1-4 # after -- : passed to `docling convert`

The format is set with the script's own --to, before "--". Do not pass --to, --output or --output-file after "--":
the script sets the format and the output file itself.
--images (with --to json) writes the pictures as PNG files in <name>_images beside the .json, which refers to them by a
relative path; the image of each page is dropped unless --page-images. Do not pass --image-export-mode after "--".
The OCR engine is Tesseract with Romanian by default (the `tesseract` command and its `ron` language data must be
installed). Docling's own default on a Mac is Apple Vision (ocrmac), so the engine is always named here. The whole
page is OCR'd and any text layer ignored (--ocr-mode full_page): the text layer of a scan is not to be trusted.
A failing file is reported and skipped; the exit status is non-zero if any failed.
"""

import argparse
import sys

import pipeline_common
from pipeline_common import PDF_SUFFIX

DEFAULT_OCR_ENGINE = "tesseract"
DEFAULT_OCR_LANG = "ron"
OCR_MODES = ["full_page", "layout_regions", "pdf_aware_layout_regions", "default"]
DEFAULT_OCR_MODE = "full_page"
DEVICES = ["auto", "cpu", "mps"]
DEFAULT_DEVICE = "auto"


def ocr_options(args: argparse.Namespace) -> list[str]:
    return [
        "--ocr-engine", args.ocr_engine, "--ocr-lang", args.ocr_lang, "--ocr-mode", args.ocr_mode,
        "--device", args.device,
    ]


def main(ocr_engine: str = DEFAULT_OCR_ENGINE, ocr_lang: str = DEFAULT_OCR_LANG, description: str | None = __doc__) -> None:
    """Run the script. The arguments are the defaults of --ocr-engine and --ocr-lang and the --help text, so that a
    script for another OCR engine (pdf_to_md_vision.py) is this one with other defaults."""
    argv, extra = pipeline_common.split_passthrough(sys.argv[1:])
    parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
    pipeline_common.add_dir_arguments(parser)
    pipeline_common.add_format_argument(parser)
    pipeline_common.add_images_arguments(parser)
    parser.add_argument("--ocr-engine", default=ocr_engine, help="default: %(default)s")
    parser.add_argument("--ocr-lang", default=ocr_lang, help="language, as the OCR engine names it (default: %(default)s)")
    parser.add_argument(
        "--ocr-mode", choices=OCR_MODES, default=DEFAULT_OCR_MODE,
        help="which regions are OCR'd; full_page replaces any text layer (default: %(default)s)",
    )
    parser.add_argument("--device", choices=DEVICES, default=DEFAULT_DEVICE, help="default: %(default)s")
    args = parser.parse_args(argv)
    pipeline_common.check_images_arguments(parser, args)

    pipeline_common.run_docling_batch(args, PDF_SUFFIX, [*ocr_options(args), *extra])


if __name__ == "__main__":
    main()
