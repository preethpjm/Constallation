"""Data types that flow between nodes, plus content sniffing.

Types are MIME strings. Compatibility is checked on every edge, so a pipeline
the planner proposes can never connect a GLB into something expecting CSV.
"""
import json
import os

EXT_MIME = {
    ".step": "model/step", ".stp": "model/step",
    ".glb": "model/gltf-binary", ".gltf": "model/gltf+json",
    ".stl": "model/stl", ".obj": "model/obj",
    ".json": "application/json", ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".html": "text/html", ".htm": "text/html", ".txt": "text/plain", ".md": "text/markdown",
    ".xml": "application/xml", ".yaml": "application/yaml", ".yml": "application/yaml",
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip", ".svg": "image/svg+xml", ".cgm": "image/cgm",
    ".gif": "image/gif", ".webp": "image/webp",
}
MIME_EXT = {}
for _e, _m in EXT_MIME.items():
    MIME_EXT.setdefault(_m, _e)

# Words people use in requests/arg names -> type
WORD_MIME = {
    "step": "model/step", "stp": "model/step", "cad": "model/step",
    "glb": "model/gltf-binary", "gltf": "model/gltf-binary", "3d": "model/gltf-binary", "mesh": "model/gltf-binary",
    "csv": "text/csv", "bom": "text/csv", "json": "application/json",
    "html": "text/html", "listing": "text/html", "page": "text/html", "website": "text/html",
    "pdf": "application/pdf", "png": "image/png", "image": "image/png",
    "svg": "image/svg+xml", "cgm": "image/cgm", "vector": "image/svg+xml",
}


def mime_from_name(name: str) -> str | None:
    return EXT_MIME.get(os.path.splitext(name.lower())[1])


def sniff(path: str) -> str:
    """Decide what a produced file actually is, by content, not by name."""
    if os.path.isdir(path):
        return "inode/directory"
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return "application/octet-stream"
    if head[:4] == b"glTF":
        return "model/gltf-binary"
    if head[:4] == b"%PDF":
        return "application/pdf"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if head[:2] == b"PK":
        return mime_from_name(path) or "application/zip"
    if b"ISO-10303-21" in head[:200]:
        return "model/step"
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return mime_from_name(path) or "application/octet-stream"
    t = text.lstrip().lower()
    if t.startswith("<!doctype html") or t.startswith("<html"):
        return "text/html"
    if t.startswith("<svg") or "<svg" in t[:200]:
        return "image/svg+xml"
    if t.startswith("<?xml"):
        return "application/xml"
    if t[:1] in "{[":
        try:
            with open(path, encoding="utf-8") as f:
                json.load(f)
            return "application/json"
        except Exception:
            pass
    lines = [l for l in text.splitlines()[:5] if l.strip()]
    if len(lines) >= 2 and all(l.count(",") == lines[0].count(",") > 0 for l in lines[:3]):
        return "text/csv"
    return mime_from_name(path) or "text/plain"


def compatible(produced: str, accepted: str) -> bool:
    if accepted in ("*/*", "any", None) or produced == accepted:
        return True
    if accepted.endswith("/*"):
        return produced.startswith(accepted[:-1])
    # text/plain accepts structured text, not the other way round
    if accepted == "text/plain" and (produced.startswith(("text/", "application/json", "application/xml", "application/yaml"))
                                      or produced.endswith("+xml")):
        return True
    return False
