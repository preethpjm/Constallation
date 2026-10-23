"""Small text and data converters used as a test library."""
import csv
import io
import json
import re


def csv_to_json(text: str, delimiter: str = ",") -> list:
    """Convert CSV text into a list of JSON records."""
    return list(csv.DictReader(io.StringIO(text), delimiter=delimiter))


def markdown_to_html(markdown: str) -> str:
    """Turn simple Markdown (headings, bold, paragraphs) into an HTML page."""
    out = []
    for line in markdown.splitlines():
        m = re.match(r"(#+)\s+(.*)", line)
        if m:
            out.append(f"<h{len(m.group(1))}>{m.group(2)}</h{len(m.group(1))}>")
        elif line.strip():
            out.append("<p>" + re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line) + "</p>")
    return "<html><body>\n" + "\n".join(out) + "\n</body></html>\n"


def word_stats(text: str, top: int) -> dict:
    """Count words and return the most frequent ones."""
    words = re.findall(r"[a-z']+", text.lower())
    freq = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    best = sorted(freq.items(), key=lambda kv: -kv[1])[:top]
    return {"words": len(words), "unique": len(freq), "top": best}


def json_to_csv(text: str, out_path: str) -> None:
    """Flatten a JSON list of records into a CSV file."""
    rows = json.loads(text)
    rows = rows if isinstance(rows, list) else [rows]
    keys = sorted({k for r in rows for k in r})
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def fetch_page(url: str) -> str:
    """Download a page."""
    import requests
    return requests.get(url).text


def apply(callback, items: list):
    return [callback(i) for i in items]
