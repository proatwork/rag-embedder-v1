"""Convert a folder of Word files to Markdown or JSON with Docling: one .md (or .json) per .docx.

Every .docx directly inside --input-dir becomes --output-dir/<name>.md, or <name>.json with --to json. Docling reads
the Word structure directly (lists, tables, bold); there is no OCR.

Usage (run with the virtual environment's interpreter, the one Docling is installed in):
    venvs/bin/python docx_to_md.py --input-dir docs/docx --output-dir OUT
    venvs/bin/python docx_to_md.py --input-dir IN --output-dir OUT --to json         # Docling's full document
    venvs/bin/python docx_to_md.py --input-dir IN --output-dir OUT --skip-existing   # resume
    venvs/bin/python docx_to_md.py --input-dir IN --output-dir OUT --to json --images # pictures as PNG files in OUT/<name>_images
    venvs/bin/python docx_to_md.py --input-dir IN --output-dir OUT -- --some-docling-option   # after -- : passed to `docling convert`

The format is set with the script's own --to, before "--". Do not pass --to, --output or --output-file after "--":
the script sets the format and the output file itself.
--images (with --to json) writes the pictures as PNG files in <name>_images beside the .json, which refers to them by a
relative path; the image of each page is dropped unless --page-images. Do not pass --image-export-mode after "--".
Check the result before relying on it: Docling renumbers lists itself and has dropped paragraphs and Word's own
labels ("(1)", "a)") on these norms. docx_to_txt.py keeps all of the text and the labels.
A failing file is reported and skipped; the exit status is non-zero if any failed.
"""

import argparse
import sys

import pipeline_common
from pipeline_common import DOCX_SUFFIX


def main() -> None:
    argv, extra = pipeline_common.split_passthrough(sys.argv[1:])
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    pipeline_common.add_dir_arguments(parser)
    pipeline_common.add_format_argument(parser)
    pipeline_common.add_images_arguments(parser)
    args = parser.parse_args(argv)
    pipeline_common.check_images_arguments(parser, args)

    pipeline_common.run_docling_batch(args, DOCX_SUFFIX, extra)


if __name__ == "__main__":
    main()
