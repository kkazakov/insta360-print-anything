"""JPEG container plumbing: markers, quantisation tables, re-assembly.

Nothing camera-specific lives here — the GO Ultra conventions are in
:mod:`insta360_print.template`.
"""

from __future__ import annotations

import io

from PIL import Image

#: marker byte -> printable name, for reports
MARKER_NAMES = {
    0xC0: "SOF0", 0xC1: "SOF1", 0xC2: "SOF2", 0xC3: "SOF3",
    0xC4: "DHT", 0xC8: "JPG", 0xC9: "SOF9", 0xCA: "SOF10",
    0xCC: "DAC", 0xDB: "DQT", 0xDD: "DRI", 0xDA: "SOS",
    0xE0: "APP0", 0xE1: "APP1", 0xE2: "APP2", 0xE3: "APP3",
    0xE4: "APP4", 0xE5: "APP5", 0xE6: "APP6", 0xE7: "APP7",
    0xE8: "APP8", 0xE9: "APP9", 0xEA: "APP10", 0xEB: "APP11",
    0xEC: "APP12", 0xED: "APP13", 0xEE: "APP14", 0xEF: "APP15",
    0xFE: "COM",
}

#: zigzag -> natural index table (JPEG Annex K)
ZIGZAG = [
    0, 1, 8, 16, 9, 2, 3, 10,
    17, 24, 32, 25, 18, 11, 4, 5,
    12, 19, 26, 33, 40, 48, 41, 34,
    27, 20, 13, 6, 7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,
    29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,
    53, 60, 61, 54, 47, 55, 62, 63,
]


def split_markers(data: bytes):
    """Split a JPEG up to its SOS marker.

    Returns ``(segments, scan_start)`` where segments is a list of
    ``(marker: int, payload: bytes)`` for every marker before SOS.
    """
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("not a JPEG (missing SOI)")
    i, segs = 2, []
    while True:
        if i + 4 > len(data) or data[i] != 0xFF:
            raise ValueError(f"corrupt JPEG marker at offset {i}")
        m = data[i + 1]
        if m == 0xD8:            # stray SOI
            i += 2
            continue
        if m == 0x00 or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        payload = data[i + 4:i + 2 + length]
        if m == 0xDA:
            return segs, i
        segs.append((m, payload))
        i += 2 + length


def body_end(data: bytes) -> int:
    """Offset just past the last EOI (i.e. the size of the JPEG itself)."""
    eoi = data.rfind(b"\xff\xd9")
    if eoi < 0:
        raise ValueError("no EOI found")
    return eoi + 2


def marker_desc(segs) -> str:
    """Compact printable description of a marker list."""
    return " ".join(f"{MARKER_NAMES.get(m, f'FF{m:02X}')}({len(p)})"
                    for m, p in segs)


def parse_dqt(payload: bytes) -> dict[int, list[int]]:
    """Parse a DQT segment into ``{table_id: [64 values in zigzag order]}``."""
    tables, i = {}, 0
    while i < len(payload):
        pq, tq = payload[i] >> 4, payload[i] & 0x0F
        i += 1
        if pq == 0:
            vals = list(payload[i:i + 64])
            i += 64
        else:  # 16-bit tables, never used by the camera but keep it correct
            vals = [int.from_bytes(payload[i + 2 * k:i + 2 * k + 2], "big")
                    for k in range(64)]
            i += 128
        tables[tq] = vals
    return tables


def zigzag_to_natural(zig: list[int]) -> list[int]:
    natural = [0] * 64
    for i, v in enumerate(zig):
        natural[ZIGZAG[i]] = v
    return natural


def qtables_of(data: bytes) -> list[list[int]]:
    """All quantisation tables of a JPEG, in Pillow's natural order."""
    segs, _ = split_markers(data)
    payload = b"".join(p for m, p in segs if m == 0xDB)
    tables = parse_dqt(payload)
    return [zigzag_to_natural(tables[k]) for k in sorted(tables)]


def dimensions_of(data: bytes) -> tuple[int, int]:
    """(width, height) from the SOF0/SOF1/SOF2 header."""
    segs, _ = split_markers(data)
    for m, p in segs:
        if m in (0xC0, 0xC1, 0xC2) and len(p) >= 6:
            return int.from_bytes(p[3:5], "big"), int.from_bytes(p[1:3], "big")
    raise ValueError("no SOF marker")


def embedded_previews(data: bytes) -> bytes | None:
    """Concatenate APP2 payloads — the camera hides a whole JPEG in them."""
    segs, _ = split_markers(data)
    chunks = [p for m, p in segs if m == 0xE2]
    if not chunks:
        return None
    blob = b"".join(chunks)
    return blob if blob.startswith(b"\xff\xd8") else None


def fit_image(img: Image.Image, size: tuple[int, int], mode: str = "crop") -> Image.Image:
    """Resize img to exactly ``size`` using crop / pad / stretch."""
    tw, th = size
    w, h = img.size
    if mode == "stretch":
        return img.resize((tw, th), Image.LANCZOS)

    if mode == "pad":
        scale = min(tw / w, th / h)
        nw, nh = max(1, round(w * scale)), max(1, round(h * scale))
        out = Image.new("RGB", (tw, th), (0, 0, 0))
        out.paste(img.resize((nw, nh), Image.LANCZOS),
                  ((tw - nw) // 2, (th - nh) // 2))
        return out

    # default: cover + centre crop
    scale = max(tw / w, th / h)
    nw, nh = max(tw, round(w * scale)), max(th, round(h * scale))
    resized = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    return resized.crop((left, top, left + tw, top + th))


def encode_jpeg(img: Image.Image, qtables_natural: list[list[int]]) -> bytes:
    """Encode with the camera's exact quantisation tables, 4:2:0, baseline."""
    buf = io.BytesIO()
    img.save(buf, "JPEG", qtables=qtables_natural, subsampling=2,
             optimize=False, progressive=False)
    return buf.getvalue()


def rebuild_jpeg(encoded: bytes, app1: bytes | None,
                 app2_chunks: list[bytes]) -> bytes:
    """Reassemble a Pillow-produced JPEG into the camera's marker layout.

    The camera puts a DRI marker (restart interval 65535) in the 8192-wide
    modes, but it is deliberately not reproduced: a DRI without the matching
    RSTn markers makes libjpeg — and therefore most printer firmware — stop at
    the first expected restart and fill the rest of the frame with grey.
    """
    segs, scan_start = split_markers(encoded)
    scan = encoded[scan_start:body_end(encoded)]

    dqt_payloads = [p for m, p in segs if m == 0xDB]
    sof = next((p for m, p in segs if m == 0xC0), None)
    dht_payloads = [p for m, p in segs if m == 0xC4]
    if sof is None:
        raise ValueError("encoder produced no SOF0")

    out = bytearray(b"\xff\xd8")
    if app1:
        out += b"\xff\xe1" + (len(app1) + 2).to_bytes(2, "big") + app1
    for chunk in app2_chunks:
        out += b"\xff\xe2" + (len(chunk) + 2).to_bytes(2, "big") + chunk

    dqt = b"".join(dqt_payloads)          # one segment, both tables
    out += b"\xff\xdb" + (len(dqt) + 2).to_bytes(2, "big") + dqt
    out += b"\xff\xc0" + (len(sof) + 2).to_bytes(2, "big") + sof
    dht = b"".join(dht_payloads)          # one segment, all four tables
    out += b"\xff\xc4" + (len(dht) + 2).to_bytes(2, "big") + dht
    out += scan
    return bytes(out)


def chunk_app2(preview: bytes, chunk_size: int = 65533) -> list[bytes]:
    """Split the hidden preview into APP2-sized chunks."""
    return [preview[i:i + chunk_size] for i in range(0, len(preview), chunk_size)]


def nv21_from_image(img: Image.Image) -> bytes:
    """Encode the camera's raw thumbnail: video-range BT.601, interleaved U,V.

    The trailer stores a raw copy of the preview in this format and the camera
    draws the gallery thumbnail from it, so a clone has to rewrite it or it will
    keep showing the template's picture.
    """
    width, height = img.size
    y_band, cb, cr = img.convert("YCbCr").split()

    # JFIF YCbCr is full range; the camera's buffer is video range
    y_band = y_band.point([int(round(16 + i * 219 / 255)) for i in range(256)])
    limited = [int(round(128 + (i - 128) * 224 / 255)) for i in range(256)]
    cb = cb.point(limited).resize((width // 2, height // 2), Image.BOX)
    cr = cr.point(limited).resize((width // 2, height // 2), Image.BOX)

    u, v = cb.tobytes(), cr.tobytes()
    uv = bytearray(2 * len(u))
    uv[0::2] = u
    uv[1::2] = v
    return y_band.tobytes() + bytes(uv)


def nv21_luma(blob: bytes, width: int, height: int) -> Image.Image:
    """The Y plane of the camera buffer, as a Pillow "L" image."""
    return Image.frombytes("L", (width, height), blob[:width * height])


def nv21_to_rgb(blob: bytes, width: int, height: int) -> Image.Image:
    """Decode the camera's raw thumbnail back to RGB (video-range BT.601)."""
    half_w, half_h = width // 2, height // 2
    n = width * height
    m = half_w * half_h
    pairs = blob[n:n + 2 * m]                 # interleaved Cb,Cr
    luma = Image.frombytes("L", (width, height), blob[:n])
    cb = Image.frombytes("L", (half_w, half_h), pairs[0::2]).resize(
        (width, height), Image.BILINEAR)
    cr = Image.frombytes("L", (half_w, half_h), pairs[1::2]).resize(
        (width, height), Image.BILINEAR)
    luma = luma.point([max(0, min(255, int(round((i - 16) * 255 / 219))))
                       for i in range(256)])
    shift = [max(0, min(255, int(round(128 + (i - 128) * 255 / 224))))
             for i in range(256)]
    return Image.merge("YCbCr", (luma, cb.point(shift), cr.point(shift))).convert("RGB")
