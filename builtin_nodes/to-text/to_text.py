"""Save any text-based file (SVG, XML, HTML, JSON, CSV, Markdown) as a plain .txt file holding its source code."""
import argparse
import sys

ap = argparse.ArgumentParser(description="Save any text-based file (SVG, XML, HTML, JSON, CSV, Markdown) as a plain .txt file holding its source code")
ap.add_argument("--input_file", required=True, help="input file (any text-based format, for example svg)")
ap.add_argument("--output_txt", required=True, help="output txt file")
a = ap.parse_args()
data = open(a.input_file, "rb").read()
try:
    text = data.decode("utf-8")
except UnicodeDecodeError:
    sys.exit("this file is binary, not text: nothing to save as txt")
with open(a.output_txt, "w", encoding="utf-8", newline="") as f:
    f.write(text)
