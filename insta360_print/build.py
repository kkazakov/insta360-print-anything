"""The clone itself: take a template + a picture, produce camera-shaped bytes."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageChops, ImageOps, ImageStat

from . import exif, jpeg, meta
from .template import Template

#: the fixed part of the metadata pointer (blob offset inside the trailer)
DEFAULT_BLOB_OFFSET = 431


@dataclass
class Options:
    """Everything that influences the output file."""

    fit: str = "crop"
    timestamp: datetime | None = None
    seq: int = 1
    serial: str | None = None       # None = keep the template's
    uid: str | None = None          # None = keep the template's
    no_preview: bool = False
    no_thumb: bool = False
    no_exif: bool = False
    no_trailer: bool = False
    meta_fix: bool = True


@dataclass
class Result:
    data: bytes
    filename: str
    size: tuple[int, int]
    template: Template
    timestamp: datetime
    info: dict = field(default_factory=dict)

    @property
    def meta_offset(self) -> int | None:
        return self.info.get("file_offset")

    @property
    def serial(self) -> str | None:
        return self.info.get("serial")


# --------------------------------------------------------------------------
# timestamps
# --------------------------------------------------------------------------

def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S", "%Y%m%d_%H%M%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise SystemExit(f"cannot parse timestamp: {value!r}")


def source_timestamp(src: Path) -> datetime:
    """Capture time of the source image: EXIF, else file mtime."""
    try:
        with Image.open(src) as im:
            ex = im.getexif()
            raw = ex.get(0x0132) or ex.get_ifd(0x8769).get(0x9003)
            if raw:
                return datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return datetime.fromtimestamp(src.stat().st_mtime)


def newest_timestamp(camera_time: datetime | None,
                     explicit: datetime | None) -> datetime:
    """Pick a capture time that is guaranteed to sort after every camera photo.

    The camera's clock is the authority — it was ~50 min ahead of this machine
    in testing — so "now" is floored at *newest photo + 1 minute* rather than
    trusting the host clock.
    """
    if explicit:
        return explicit
    now = datetime.now().replace(microsecond=0)
    if camera_time:
        floor = (camera_time + timedelta(minutes=1)).replace(microsecond=0)
        return max(now, floor)
    return now


def camera_filename(dt: datetime, seq: int = 1) -> str:
    """IMG_YYYYMMDD_HHMMSS_NNN.jpg — always 27 characters."""
    return f"IMG_{dt:%Y%m%d_%H%M%S}_{seq:03d}.jpg"


def next_seq(directory: Path | None, dt: datetime) -> int:
    """Sequence number after the highest one already used on that date."""
    if directory is None or not directory.is_dir():
        return 1
    same_day = dt.strftime("%Y%m%d")
    used = []
    for p in directory.glob("IMG_*.jpg"):
        m = meta.NAME_RE.match(p.name)
        if m and m.group(1) == same_day:
            used.append(int(m.group(3)))
    return (max(used) + 1) if used else 1


# --------------------------------------------------------------------------
# image loading / building
# --------------------------------------------------------------------------

def load_image(src: Path) -> Image.Image:
    """Open the source, apply its EXIF orientation and flatten transparency."""
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            return bg
        if im.mode != "RGB":
            im = im.convert("RGB")
        im.load()
        return im


def build(template: Template, image: Image.Image, opts: Options) -> Result:
    """Render ``image`` into a file that looks like it came from the camera."""
    if not template.qtables:
        raise ValueError(f"{template.path} has no quantisation tables")

    dt = opts.timestamp or datetime.now().replace(microsecond=0)
    filename = camera_filename(dt, opts.seq)
    target = template.size

    fitted = jpeg.fit_image(image, target, opts.fit)
    encoded = jpeg.encode_jpeg(fitted, template.qtables)
    if jpeg.qtables_of(encoded) != template.qtables:
        raise ValueError("encoder did not reproduce the camera's quant tables")

    # ---- EXIF + thumbnail ---------------------------------------------
    app1 = template.app1
    if app1 and not opts.no_exif:
        thumb = None
        if template.thumb_size and not opts.no_thumb:
            thumb = jpeg.encode_jpeg(
                jpeg.fit_image(fitted, template.thumb_size, "crop"),
                template.qtables)
        app1 = exif.patch(app1, filename=filename, dt=dt, size=target,
                          thumbnail=thumb)
    elif opts.no_exif:
        app1 = None

    # ---- hidden APP2 preview ------------------------------------------
    preview_img = None
    app2: list[bytes] = []
    if template.preview_size:
        preview_img = jpeg.fit_image(fitted, template.preview_size, "crop")
        if not opts.no_preview:
            app2 = jpeg.chunk_app2(
                jpeg.encode_jpeg(preview_img, template.qtables))

    body = jpeg.rebuild_jpeg(encoded, app1, app2)

    # ---- proprietary trailer ------------------------------------------
    info: dict = {}
    trailer = b""
    if template.trailer and not opts.no_trailer:
        trailer = meta.patch_path(template.trailer, filename)
        if opts.serial:
            trailer = meta.patch_serial(trailer, opts.serial, template.serial)
        if opts.uid:
            trailer = meta.patch_uid(trailer, opts.uid)

        current = meta.meta_info(trailer)
        info.update(current)
        info["serial"] = meta.serial_of(trailer)
        info["uid"] = meta.uid_of(trailer)
        if current and opts.meta_fix:
            blob_off = current["blob_off"]
            file_offset = len(body) + blob_off
            trailer = meta.patch_meta(
                trailer, serial=info["serial"], file_offset=file_offset, dt=dt)
            info["file_offset"] = file_offset
            info["template_offset"] = current.get("file_offset")

        # the trailer also carries a raw NV21 copy of the preview, and that is
        # what the camera draws in its gallery -- leave it and the clone shows
        # the template's photo
        bulk = meta.bulk_info(trailer)
        if bulk and preview_img is not None:
            if (bulk["width"], bulk["height"]) == preview_img.size:
                trailer = meta.patch_bulk(
                    trailer, bulk, jpeg.nv21_from_image(preview_img))
                info["bulk"] = (bulk["width"], bulk["height"], bulk["size"])
            else:
                info["bulk_mismatch"] = ((bulk["width"], bulk["height"]),
                                         preview_img.size)

    return Result(data=body + trailer, filename=filename, size=target,
                  template=template, timestamp=dt, info=info)


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def verify(data: bytes, template: Template) -> tuple[list[tuple[str, bool]], dict]:
    """Re-parse our own output and check the camera-specific invariants."""
    from .template import analyse      # local import: analyse needs a file

    segs, scan_start = jpeg.split_markers(data)
    end = jpeg.body_end(data)
    trailer = data[end:]
    width, height = jpeg.dimensions_of(data)
    app1 = next((p for m, p in segs if m == 0xE1 and p.startswith(b"Exif")), None)
    preview = jpeg.embedded_previews(data)
    summary = exif.read_summary(app1)
    info = meta.meta_info(trailer) if trailer else {}

    blob_off = info.get("blob_off", DEFAULT_BLOB_OFFSET)
    expected = end + blob_off
    name = summary.get("ImageDescription", "")
    name_time = re.sub(r"\D", "", name)[:14]

    checks = [
        ("first marker is APP1/EXIF (no APP0)",
         bool(segs) and segs[0][0] == 0xE1),
        ("Make/Model name the camera",
         (summary.get("Make"), summary.get("Model")) ==
         (summary.get("Make"), template.model)),
        ("Software looks like camera firmware",
         bool(re.match(r"^v\d+\.\d+\.\d+_build\d+$", summary.get("Software", "")))),
        ("EXIF thumbnail present", exif.parse_app1(app1) is not None),
        ("hidden APP2 preview present", preview is not None),
        ("proprietary trailer present", len(trailer) > 0),
        ("quantisation tables match template",
         jpeg.qtables_of(data) == template.qtables),
        ("ImageDescription has camera naming", bool(meta.NAME_RE.match(name))),
        ("EXIF pixel size matches frame",
         (summary.get("PixelXDimension"), summary.get("PixelYDimension")) ==
         (width, height)),
        ("metadata offset (field 9) points at the blob",
         info.get("file_offset") == expected if info else True),
        ("metadata timestamp (field 7) matches the file name",
         str(info.get("timestamp")) == name_time if info else True),
        ("serial has the camera format",
         bool(meta.SERIAL_RE.search(trailer)) if trailer else True),
        ("device UID present",
         meta.uid_of(trailer) is not None if trailer else True),
    ]

    # the gallery thumbnail comes from the raw NV21 buffer, not from the JPEG
    bulk = meta.bulk_info(trailer) if trailer else {}
    preview = jpeg.embedded_previews(data)
    if bulk and preview:
        ref = Image.open(io.BytesIO(preview)).convert("RGB")
        got = jpeg.nv21_to_rgb(data[end + bulk["offset"]:],
                               bulk["width"], bulk["height"])
        stats = ImageStat.Stat(ImageChops.difference(ref, got))
        worst = max(max(stats.mean), max(stats.extrema, key=lambda e: e[1])[1])
        checks.append((f"raw thumbnail matches the picture "
                       f"(colour diff {max(stats.mean):.1f})", max(stats.mean) < 12))

    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
        checks.append((f"decodes ({width}x{height})", True))
    except Exception:
        checks.append(("decodes", False))

    return checks, {"summary": summary, "info": info, "size": (width, height),
                    "trailer": len(trailer)}
