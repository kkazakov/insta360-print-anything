"""The camera's proprietary metadata: the post-EOI trailer and its index.

Layout of the trailer::

    [4-byte value][427-byte binary header][metadata protobuf][bulk data]
                                          ^ offset 431 in every sample

The same 1151-byte protobuf is copied verbatim into ``DCIM/fileinfo_list.list``,
which is why everything here is shared between the JPEG and the camera index.

Two of its fields are absolute and per-file and must be rewritten for every
clone, or the camera will seek to the wrong place and silently hide the file:

======  ==================================================================
field   meaning
======  ==================================================================
9       offset of the metadata blob inside the file (``body_end + 431``)
7       capture time as ``YYYYMMDDHHMMSS``, identical to the file name
======  ==================================================================
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from pathlib import Path

#: IMG_YYYYMMDD_HHMMSS_NNN.jpg -> 27 characters.  The slot is fixed in the
#: EXIF ImageDescription tag and inside the trailer's length-prefixed path.
NAME_LEN = 27
NAME_RE = re.compile(r"^IMG_(\d{8})_(\d{6})_(\d{3})\.jpg$")
DIR_RE = re.compile(rb"/DCIM/Camera\d{2}/")

#: the camera serial: 14 characters, "IB" + 12 upper-case base-36 characters
SERIAL_LEN = 14
SERIAL_RE = re.compile(rb"IB[A-Z0-9]{12}")
SERIAL_FMT = re.compile(r"^IB[A-Z0-9]{12}$")
SERIAL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

#: the device UID: the last 32 bytes of the blob, lower-case hex
UID_LEN = 32
UID_FMT = re.compile(r"^[0-9a-f]{32}$")

#: per-file fields inside the metadata protobuf
FIELD_TIMESTAMP = 7
FIELD_OFFSET = 9
FIELD_PATH = 26

FILEINFO_LIST = Path("DCIM/fileinfo_list.list")
EDITINFO_LIST = Path("DCIM/.editinfo_list.list")
CAMERA_DIR = Path("DCIM/Camera01")

# --------------------------------------------------------------------------
# protobuf wire format
# --------------------------------------------------------------------------


def read_varint(buf, i):
    value = shift = 0
    while True:
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return value, i


def encode_varint(value: int, pad_to: int = 0) -> bytes:
    """Encode a varint, optionally padded with redundant groups (same value)."""
    groups = []
    while True:
        groups.append(value & 0x7F)
        value >>= 7
        if not value:
            break
    if pad_to and len(groups) > pad_to:
        raise ValueError(f"value needs more than {pad_to} varint bytes")
    while len(groups) < pad_to:
        groups.append(0)
    out = bytearray()
    for g in groups[:-1]:
        out.append(g | 0x80)
    out.append(groups[-1])
    return bytes(out)


def pb_fields(buf, start: int = 0, limit: int | None = None):
    """Parse a protobuf message, stopping at the first non-field byte.

    Returns ``([(field, wire, field_start, value_end, value), ...], end)``.
    """
    end = len(buf) if limit is None else min(len(buf), limit)
    i, out = start, []
    while i < end:
        try:
            key, j = read_varint(buf, i)
        except IndexError:
            break
        f, w = key >> 3, key & 7
        if f == 0 or w in (3, 4, 6, 7):
            break
        if w == 0:
            try:
                v, j = read_varint(buf, j)
            except IndexError:
                break
            out.append((f, w, i, j, v))
        elif w == 2:
            n, j = read_varint(buf, j)
            if j + n > end:
                break
            out.append((f, w, i, j + n, bytes(buf[j:j + n])))
            j += n
        elif w == 5:
            if j + 4 > end:
                break
            out.append((f, w, i, j + 4, bytes(buf[j:j + 4])))
            j += 4
        elif w == 1:
            if j + 8 > end:
                break
            out.append((f, w, i, j + 8, bytes(buf[j:j + 8])))
            j += 8
        else:
            break
        i = j
    return out, i


def pb_get_varint(buf, start: int, field: int):
    for f, w, _vs, _ve, v in pb_fields(buf, start)[0]:
        if f == field and w == 0:
            return v
    return None


def pb_patch_varint(buf: bytearray, start: int, field: int, value: int) -> bool:
    """Rewrite a top-level varint field in place, keeping its byte length."""
    for f, w, field_start, value_end, _v in pb_fields(buf, start)[0]:
        if f == field:
            if w != 0:
                return False
            _, value_start = read_varint(buf, field_start)   # skip the key
            try:
                buf[value_start:value_end] = encode_varint(
                    value, pad_to=value_end - value_start)
            except ValueError:
                return False
            return True
    return False


# --------------------------------------------------------------------------
# the metadata blob inside the trailer
# --------------------------------------------------------------------------


def locate_blob(trailer: bytes, serial: str | None) -> int | None:
    """Offset of the metadata protobuf inside the trailer (431 in the samples)."""
    if not serial:
        return None
    pat = b"\x0a\x0e" + serial.encode("ascii")
    if trailer.count(pat) != 1:
        return None
    return trailer.index(pat)


def serial_of(trailer: bytes) -> str | None:
    """First ``IB…`` serial that is preceded by its protobuf tag."""
    m = re.search(rb"\x0a\x0e(IB[A-Z0-9]{12})", trailer)
    return m.group(1).decode("ascii") if m else None


def meta_info(trailer: bytes, serial: str | None = None) -> dict:
    """Read the per-file fields that must stay consistent with the image."""
    serial = serial or serial_of(trailer)
    off = locate_blob(trailer, serial)
    if off is None:
        return {}
    fields, end = pb_fields(trailer, off)
    by = {f: v for f, w, _s, _e, v in fields if w == 0}
    path = next((v for f, w, _s, _e, v in fields
                 if f == FIELD_PATH and w == 2), None)
    return {
        "serial": serial,
        "blob_off": off,
        "blob_len": end - off,
        "file_offset": by.get(FIELD_OFFSET),
        "timestamp": by.get(FIELD_TIMESTAMP),
        "path": path.split(b"\x00")[0].decode("ascii", "replace") if path else None,
    }


def capture_time(trailer: bytes) -> datetime | None:
    """Field 7 as a datetime, or None."""
    info = meta_info(trailer)
    ts = info.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.strptime(str(ts), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def patch_meta(trailer: bytes, *, serial: str | None, file_offset: int,
               dt: datetime) -> bytes:
    """Point field 9 at the real metadata location and set field 7 to ``dt``."""
    off = locate_blob(trailer, serial or serial_of(trailer))
    if off is None:
        raise ValueError("cannot locate the metadata protobuf in the trailer")
    buf = bytearray(trailer)
    if not pb_patch_varint(buf, off, FIELD_OFFSET, file_offset):
        raise ValueError("cannot patch the metadata offset field")
    pb_patch_varint(buf, off, FIELD_TIMESTAMP,
                    int(dt.strftime("%Y%m%d%H%M%S")))
    return bytes(buf)


#: the trailer carries a third copy of the picture as raw NV21 (video-range
#: BT.601, VU interleaved).  Header offsets are relative to the end of the
#: metadata blob, and the camera draws the gallery thumbnail from this buffer —
#: not from the JPEG and not from the APP2 preview.
BULK_LENGTH_AT = 10
BULK_WIDTH_AT = 22
BULK_HEIGHT_AT = 26
BULK_DATA_AT = 46


def bulk_info(trailer: bytes, serial: str | None = None) -> dict:
    """Where the raw NV21 thumbnail lives and how big it is."""
    info = meta_info(trailer, serial)
    if not info:
        return {}
    end = info["blob_off"] + info["blob_len"]
    if end + BULK_DATA_AT > len(trailer):
        return {}
    length = int.from_bytes(trailer[end + BULK_LENGTH_AT:end + BULK_LENGTH_AT + 4],
                            "little")
    width = int.from_bytes(trailer[end + BULK_WIDTH_AT:end + BULK_WIDTH_AT + 4],
                           "little")
    height = int.from_bytes(trailer[end + BULK_HEIGHT_AT:end + BULK_HEIGHT_AT + 4],
                            "little")
    size = width * height * 3 // 2
    offset = end + BULK_DATA_AT
    if width <= 0 or height <= 0 or offset + size > len(trailer):
        return {}
    return {"offset": offset, "width": width, "height": height,
            "length": length, "size": size}


def patch_bulk(trailer: bytes, info: dict, data: bytes) -> bytes:
    """Overwrite the raw thumbnail in place; the length never changes."""
    if len(data) != info["size"]:
        raise ValueError(f"bulk image is {len(data)} bytes, expected "
                         f"{info['size']}")
    off = info["offset"]
    return trailer[:off] + data + trailer[off + info["size"]:]



# --------------------------------------------------------------------------
# other per-file edits
# --------------------------------------------------------------------------


def patch_path(trailer: bytes, filename: str) -> bytes:
    """Replace the DCIM path inside the trailer, length-preserving."""
    if len(filename) != NAME_LEN:
        raise ValueError(f"file name must be exactly {NAME_LEN} characters")
    m = DIR_RE.search(trailer)
    if not m:
        return trailer
    start = m.start()
    old = trailer[start:start + len(m.group(0)) + NAME_LEN]
    if not NAME_RE.match(old[len(m.group(0)):].decode("ascii", "replace")):
        return trailer
    return trailer[:start] + m.group(0) + filename.encode("ascii") + \
        trailer[start + len(old):]


def random_serial() -> str:
    """A serial in the camera's format: 'IB' + 12 upper-case base-36 chars."""
    return "IB" + "".join(secrets.choice(SERIAL_ALPHABET) for _ in range(12))


def random_uid() -> str:
    """A device UID in the camera's format: 32 lower-case hex chars."""
    return secrets.token_hex(16)


def patch_serial(trailer: bytes, serial: str, old_serial: str | None = None) -> bytes:
    """Swap the 14-byte serial in place, length-preserving.

    The blob is mostly opaque binary, so a bare ``IB[A-Z0-9]{12}`` scan also
    finds decoys in the compressed data.  The real one is the protobuf field
    ``0a 0e``, so the known value is preferred and the tag is the fallback.
    """
    if not SERIAL_FMT.match(serial):
        raise ValueError(f"serial must look like IBXXXXXXXXXXXX, got {serial!r}")
    old_serial = old_serial or serial_of(trailer)
    if old_serial and SERIAL_FMT.match(old_serial):
        old = old_serial.encode("ascii")
        if trailer.count(old) != 1:
            raise ValueError(f"template serial {old_serial!r} is not unique "
                             f"in the trailer")
        start = trailer.index(old)
        return trailer[:start] + serial.encode("ascii") + trailer[start + SERIAL_LEN:]

    hits = list(re.finditer(rb"\x0a\x0e(IB[A-Z0-9]{12})", trailer))
    if len(hits) != 1:
        raise ValueError(f"expected exactly one serial field, found {len(hits)}")
    m = hits[0]
    return trailer[:m.start(1)] + serial.encode("ascii") + trailer[m.end(1):]


def patch_uid(trailer: bytes, uid: str) -> bytes:
    """Swap the 32-hex device UID that closes the blob."""
    if not UID_FMT.match(uid):
        raise ValueError(f"uid must be 32 lower-case hex chars, got {uid!r}")
    if not UID_FMT.match(trailer[-UID_LEN:].decode("ascii", "replace")):
        raise ValueError("the trailer does not end with a 32-hex device UID")
    return trailer[:-UID_LEN] + uid.encode("ascii")


def uid_of(trailer: bytes) -> str | None:
    tail = trailer[-UID_LEN:].decode("ascii", "replace")
    return tail if UID_FMT.match(tail) else None


# --------------------------------------------------------------------------
# the camera's media index
# --------------------------------------------------------------------------
#
#   DCIM/fileinfo_list.list : 0a <len> 0a <len(path)> <path> 12 <len(blob)> <blob>
#   DCIM/.editinfo_list.list: 0a <len> 0a <len(path)> <path> 12 00


def index_records(data: bytes):
    """Split an index file into ``[(path, blob, full_record_bytes), ...]``."""
    out, i = [], 0
    while i < len(data):
        start = i
        try:
            key, i = read_varint(data, i)
            if key >> 3 != 1 or key & 7 != 2:
                break
            n, i = read_varint(data, i)
        except IndexError:
            break
        if i + n > len(data):
            break
        rec = data[i:i + n]
        i += n
        fields, _ = pb_fields(rec)
        path = next((v for f, w, _s, _e, v in fields if f == 1 and w == 2), b"")
        blob = next((v for f, w, _s, _e, v in fields if f == 2 and w == 2), b"")
        out.append((path.decode("ascii", "replace"), blob, data[start:i]))
    return out


def build_index_record(path: str, blob: bytes) -> bytes:
    p = path.encode("ascii")
    inner = (b"\x0a" + encode_varint(len(p)) + p
             + b"\x12" + encode_varint(len(blob)) + blob)
    return b"\x0a" + encode_varint(len(inner)) + inner


def build_edit_record(path: str) -> bytes:
    p = path.encode("ascii")
    inner = b"\x0a" + encode_varint(len(p)) + p + b"\x12\x00"
    return b"\x0a" + encode_varint(len(inner)) + inner


def drop_records(data: bytes, path: str) -> bytes:
    """Remove every record whose path matches."""
    return b"".join(raw for p, _b, raw in index_records(data) if p != path)


def upsert_records(data: bytes, path: str, record: bytes,
                   prepend: bool = False) -> bytes:
    """Drop the record whose path matches, then add the new one.

    The camera itself puts the most recently scanned file at the *front* of the
    list, so ``install`` mirrors that.
    """
    keep = b"".join(raw for p, _b, raw in index_records(data) if p != path)
    return record + keep if prepend else keep + record
