#!/usr/bin/env python3
"""STEP -> GLB converter (internal tool, written by 'someone on the CAD team').

Reads an ISO-10303-21 STEP file, extracts the product/assembly structure,
writes a GLB preview (one box per part) and a parts.json with quantities.

Usage:
    python convert.py assembly.step -o out/ [--scale 1.0]
"""
import argparse
import json
import os
import re
import struct
import sys


def parse_step(text):
    products = {}  # entity id -> (part_number, name)
    for m in re.finditer(r"#(\d+)\s*=\s*PRODUCT\s*\(\s*'([^']*)'\s*,\s*'([^']*)'", text):
        products[m.group(1)] = (m.group(2), m.group(3))
    # PRODUCT_DEFINITION refers (indirectly) to a product; keep it simple: map PD -> product via formation
    form = {m.group(1): m.group(2) for m in re.finditer(
        r"#(\d+)\s*=\s*PRODUCT_DEFINITION_FORMATION\w*\s*\([^#]*#(\d+)", text)}
    pdef = {m.group(1): form.get(m.group(2)) for m in re.finditer(
        r"#(\d+)\s*=\s*PRODUCT_DEFINITION\s*\([^#]*#(\d+)", text)}
    qty = {}
    for m in re.finditer(r"NEXT_ASSEMBLY_USAGE_OCCURRENCE\s*\([^#]*#(\d+)\s*,\s*#(\d+)", text):
        child = pdef.get(m.group(2))
        if child:
            qty[child] = qty.get(child, 0) + 1
    parts = []
    for pid, (pn, name) in products.items():
        if pid in qty:  # only leaf/usage parts count for BOM
            parts.append({"part_number": pn, "name": name, "quantity": qty[pid]})
    if not parts:  # single-part file
        parts = [{"part_number": pn, "name": n, "quantity": 1} for pn, n in products.values()]
    return parts


def box(cx, s):
    v = []
    for x in (-1, 1):
        for y in (-1, 1):
            for z in (-1, 1):
                v.append((cx + x * s, y * s, z * s))
    idx = [0, 1, 3, 0, 3, 2, 4, 6, 7, 4, 7, 5, 0, 4, 5, 0, 5, 1,
           2, 3, 7, 2, 7, 6, 0, 2, 6, 0, 6, 4, 1, 5, 7, 1, 7, 3]
    return v, idx


def write_glb(parts, path, scale):
    verts, inds = [], []
    for i, _ in enumerate(parts):
        v, ix = box(i * 3.0 * scale, 1.0 * scale)
        base = len(verts)
        verts += v
        inds += [base + k for k in ix]
    vb = b"".join(struct.pack("<3f", *p) for p in verts)
    ib = b"".join(struct.pack("<I", k) for k in inds)
    binbuf = vb + ib
    binbuf += b"\x00" * ((4 - len(binbuf) % 4) % 4)
    mins = [min(p[i] for p in verts) for i in range(3)]
    maxs = [max(p[i] for p in verts) for i in range(3)]
    gltf = {
        "asset": {"version": "2.0", "generator": "step2glb-internal"},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "buffers": [{"byteLength": len(binbuf)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(vb)},
                        {"buffer": 0, "byteOffset": len(vb), "byteLength": len(ib)}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(verts), "type": "VEC3",
                       "min": mins, "max": maxs},
                      {"bufferView": 1, "componentType": 5125, "count": len(inds), "type": "SCALAR"}],
    }
    js = json.dumps(gltf).encode()
    js += b" " * ((4 - len(js) % 4) % 4)
    total = 12 + 8 + len(js) + 8 + len(binbuf)
    with open(path, "wb") as f:
        f.write(struct.pack("<4sII", b"glTF", 2, total))
        f.write(struct.pack("<I4s", len(js), b"JSON") + js)
        f.write(struct.pack("<I4s", len(binbuf), b"BIN\x00") + binbuf)


def main():
    ap = argparse.ArgumentParser(description="Convert a STEP assembly to GLB preview + parts JSON")
    ap.add_argument("input", help="input .step/.stp file")
    ap.add_argument("-o", "--outdir", default="out", help="output directory")
    ap.add_argument("--scale", type=float, default=1.0, help="geometry scale factor")
    a = ap.parse_args()
    with open(a.input, encoding="utf-8", errors="ignore") as f:
        text = f.read()
    if "ISO-10303-21" not in text:
        sys.exit("not a STEP file")
    parts = parse_step(text)
    os.makedirs(a.outdir, exist_ok=True)
    write_glb(parts, os.path.join(a.outdir, "model.glb"), a.scale)
    with open(os.path.join(a.outdir, "parts.json"), "w") as f:
        json.dump({"source": os.path.basename(a.input), "parts": parts}, f, indent=2)
    print(f"wrote {len(parts)} parts")


if __name__ == "__main__":
    main()
