"""CGM (clear-text subset) to SVG. Demo of wrapping a converter that browsers and LLMs cannot read."""
import re


def cgm_to_svg(cgm_text: str, stroke_color: str = "#6b70f2", stroke_width: float = 2) -> str:
    """Convert a clear-text CGM drawing (LINE, RECT, CIRCLE statements) into an SVG image."""
    shapes, xs, ys = [], [0], [0]
    for stmt in re.split(r";\s*", cgm_text):
        m = re.match(r"\s*(LINE|RECT|CIRCLE)\s+(.*)", stmt, re.I)
        if not m:
            continue
        kind, nums = m.group(1).upper(), [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", m.group(2))]
        if kind in ("LINE", "RECT") and len(nums) >= 4:
            x1, y1, x2, y2 = nums[:4]
            xs += [x1, x2]; ys += [y1, y2]
            shapes.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}"/>' if kind == "LINE" else
                          f'<rect x="{min(x1, x2)}" y="{min(y1, y2)}" width="{abs(x2 - x1)}" height="{abs(y2 - y1)}" fill="none"/>')
        elif kind == "CIRCLE" and len(nums) >= 3:
            cx, cy, r = nums[:3]
            xs += [cx - r, cx + r]; ys += [cy - r, cy + r]
            shapes.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none"/>')
    w, h = max(xs) + 10, max(ys) + 10
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" stroke="{stroke_color}" '
            f'stroke-width="{stroke_width}">' + "".join(shapes) + "</svg>")
