"""Tiny but valid sample files for formats a tool may need to be verified on."""
import struct


def _el(cls: int, eid: int, params: bytes = b"") -> bytes:
    assert len(params) < 31
    out = struct.pack(">H", (cls << 12) | (eid << 5) | len(params)) + params
    return out + (b"\x00" if len(params) % 2 else b"")


def _str(t: str) -> bytes:
    b = t.encode("latin-1")
    return bytes([len(b)]) + b


def binary_cgm() -> bytes:
    """A small binary CGM (ISO 8632-3): a frame, a triangle, a circle and a label."""
    pts = lambda *p: b"".join(struct.pack(">h", v) for v in p)
    parts = [
        _el(0, 1, _str("constellation sample")),               # BEGIN METAFILE
        _el(1, 1, pts(1)),                                       # metafile version
        _el(2, 6, pts(0, 0, 1000, 1000)),                        # VDC extent
        _el(0, 3, _str("p1")), _el(0, 4),                        # BEGIN PICTURE, BODY
        _el(5, 4, bytes([30, 60, 200])),                         # line colour
        _el(4, 1, pts(100, 100, 900, 100, 900, 900, 100, 900, 100, 100)),   # polyline frame
        _el(5, 23, bytes([240, 120, 20])), _el(5, 22, struct.pack(">H", 1)),  # fill colour, solid
        _el(4, 7, pts(200, 200, 500, 700, 800, 200)),            # polygon
        _el(4, 12, pts(500, 500, 150)),                          # circle
        _el(4, 4, pts(120, 920) + struct.pack(">H", 1) + _str("SAMPLE")),   # text
        _el(0, 5),                                               # END PICTURE
        _el(0, 2),                                               # END METAFILE
    ]
    return b"".join(parts)


BINARY = {"image/cgm": binary_cgm}
