"""Byte-level TIFF/EXIF editing.

The camera's EXIF block is copied verbatim and poked in place: Pillow's
``getexif()`` round-trip would reorder rationals and drop the 320-byte
MakerNote, which is part of what makes the file look like a camera original.
"""

from __future__ import annotations

import io
import struct
from datetime import datetime

from PIL import Image

TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4,
              10: 8, 11: 4, 12: 8}

#: tags the tool reports and rewrites
TAG_IMAGE_DESCRIPTION = 0x010E
TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_SOFTWARE = 0x0131
TAG_DATETIME = 0x0132
TAG_EXIF_IFD = 0x8769
TAG_DATETIME_ORIGINAL = 0x9003
TAG_DATETIME_DIGITIZED = 0x9004
TAG_PIXEL_X = 0xA002
TAG_PIXEL_Y = 0xA003
TAG_THUMB_OFFSET = 0x0201
TAG_THUMB_LENGTH = 0x0202


class TiffEntry:
    __slots__ = ("tag", "typ", "count", "slot")

    def __init__(self, tag: int, typ: int, count: int, slot: int):
        self.tag, self.typ, self.count, self.slot = tag, typ, count, slot


class Tiff:
    """Just enough TIFF to read tags and poke new values into the template."""

    def __init__(self, data: bytes):
        self.d = bytearray(data)
        if self.d[:2] == b"II":
            self.bo = "<"
        elif self.d[:2] == b"MM":
            self.bo = ">"
        else:
            raise ValueError("not a TIFF block")

    def u16(self, off):
        return struct.unpack_from(self.bo + "H", self.d, off)[0]

    def u32(self, off):
        return struct.unpack_from(self.bo + "I", self.d, off)[0]

    def ifd(self, off):
        """Return ``({tag: TiffEntry}, next_ifd_offset)``."""
        n = self.u16(off)
        entries = {}
        for k in range(n):
            slot = off + 2 + 12 * k
            tag, typ, cnt = struct.unpack_from(self.bo + "HHI", self.d, slot)
            entries[tag] = TiffEntry(tag, typ, cnt, slot)
        return entries, self.u32(off + 2 + 12 * n)

    def raw(self, e: TiffEntry) -> bytes:
        size = TYPE_SIZES.get(e.typ, 1) * e.count
        if size <= 4:
            return bytes(self.d[e.slot + 8:e.slot + 8 + size])
        off = self.u32(e.slot + 8)
        return bytes(self.d[off:off + size])

    def set_bytes(self, e: TiffEntry, data: bytes):
        size = TYPE_SIZES.get(e.typ, 1) * e.count
        if len(data) > size:
            raise ValueError(f"tag 0x{e.tag:04X}: value does not fit "
                             f"({len(data)} > {size})")
        data = data.ljust(size, b"\x00")
        if size <= 4:
            self.d[e.slot + 8:e.slot + 8 + size] = data
        else:
            off = self.u32(e.slot + 8)
            self.d[off:off + size] = data

    def set_ascii(self, entries, tag, text: str):
        if tag in entries:
            self.set_bytes(entries[tag], text.encode("ascii", "replace"))

    def set_long(self, entries, tag, value: int):
        if tag in entries:
            self.set_bytes(entries[tag],
                           value.to_bytes(4, "little" if self.bo == "<" else "big"))

    def get_long(self, entries, tag, default=None):
        e = entries.get(tag)
        if e is None:
            return default
        return int.from_bytes(self.raw(e)[:4],
                              "little" if self.bo == "<" else "big")

    def exif_ifd(self) -> dict:
        ifd0, _ = self.ifd(self.u32(4))
        off = self.get_long(ifd0, TAG_EXIF_IFD)
        return self.ifd(off)[0] if off else {}


def parse_app1(payload: bytes) -> Tiff | None:
    """payload is the APP1 body, i.e. b'Exif\\0\\0' + TIFF."""
    if not payload.startswith(b"Exif\x00\x00"):
        return None
    try:
        return Tiff(payload[6:])
    except ValueError:
        return None


def extract_thumbnail(tiff: Tiff):
    """Return ``(jpeg_bytes, (w, h), offset, length)`` or None."""
    ifd1_off = tiff.ifd(tiff.u32(4))[1]
    if not ifd1_off:
        return None
    ifd1, _ = tiff.ifd(ifd1_off)
    off = tiff.get_long(ifd1, TAG_THUMB_OFFSET)
    ln = tiff.get_long(ifd1, TAG_THUMB_LENGTH)
    if not off or not ln or off + ln > len(tiff.d):
        return None
    blob = bytes(tiff.d[off:off + ln])
    try:
        return blob, Image.open(io.BytesIO(blob)).size, off, ln
    except Exception:
        return None


def read_summary(app1: bytes | None) -> dict:
    """The handful of tags the tool prints and cross-checks."""
    tiff = parse_app1(app1) if app1 else None
    if tiff is None:
        return {}
    ifd0, _ = tiff.ifd(tiff.u32(4))
    out = {}
    for tag, name in ((TAG_IMAGE_DESCRIPTION, "ImageDescription"),
                      (TAG_MAKE, "Make"), (TAG_MODEL, "Model"),
                      (TAG_SOFTWARE, "Software"), (TAG_DATETIME, "DateTime")):
        e = ifd0.get(tag)
        if e:
            out[name] = tiff.raw(e).split(b"\x00")[0].decode("ascii", "replace")
    exif = tiff.exif_ifd()
    for tag, name in ((TAG_DATETIME_ORIGINAL, "DateTimeOriginal"),):
        e = exif.get(tag)
        if e:
            out[name] = tiff.raw(e).split(b"\x00")[0].decode("ascii", "replace")
    for tag, name in ((TAG_PIXEL_X, "PixelXDimension"),
                      (TAG_PIXEL_Y, "PixelYDimension")):
        e = exif.get(tag)
        if e:
            out[name] = int.from_bytes(tiff.raw(e)[:4], "little")
    return out


def patch(app1: bytes, *, filename: str, dt: datetime,
          size: tuple[int, int], thumbnail: bytes | None) -> bytes:
    """Take the template's APP1 and replace the photo-specific fields."""
    tiff = parse_app1(app1)
    if tiff is None:
        return app1

    ifd0, ifd1_off = tiff.ifd(tiff.u32(4))
    exif = tiff.exif_ifd()
    date_str = dt.strftime("%Y:%m:%d %H:%M:%S")

    tiff.set_ascii(ifd0, TAG_IMAGE_DESCRIPTION, filename)
    tiff.set_ascii(ifd0, TAG_DATETIME, date_str)
    tiff.set_ascii(exif, TAG_DATETIME_ORIGINAL, date_str)
    tiff.set_ascii(exif, TAG_DATETIME_DIGITIZED, date_str)
    tiff.set_long(exif, TAG_PIXEL_X, size[0])
    tiff.set_long(exif, TAG_PIXEL_Y, size[1])

    if thumbnail and ifd1_off:
        ifd1, _ = tiff.ifd(ifd1_off)
        off = tiff.get_long(ifd1, TAG_THUMB_OFFSET)
        ln = tiff.get_long(ifd1, TAG_THUMB_LENGTH)
        if off and ln and off + ln == len(tiff.d):
            # the camera stores the thumbnail as the very last bytes of the
            # block, so a different-sized one can simply replace it
            tiff.set_long(ifd1, TAG_THUMB_LENGTH, len(thumbnail))
            tiff.d = tiff.d[:off] + thumbnail
        # else: keep the original thumbnail rather than corrupt the offsets

    return b"Exif\x00\x00" + bytes(tiff.d)
