"""Reading a camera photo as a template.

A :class:`Template` is everything the clone needs from a real GO Ultra photo:
the EXIF block, the hidden preview, the quantisation tables and the proprietary
trailer.  It can come from ``real-samples/`` (offline) or — preferably — from a
photo already on the connected camera, which carries that camera's own serial
and calibration.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image

from . import exif, jpeg, meta

#: the sizes the camera can produce, used for the offline sample lookup
PRESETS: dict[str, dict] = {
    "37mp": dict(width=8192, height=4608, preview=(704, 416), thumb=(160, 96)),
    "12.5mp": dict(width=4096, height=3072, preview=(704, 544), thumb=(160, 120)),
    "50mp": dict(width=8192, height=6144, preview=(704, 544), thumb=(160, 120)),
}
AUTO_PRESETS = ("37mp", "12.5mp")


@dataclass
class Template:
    """A real camera photo, decomposed into the parts the clone reuses."""

    path: Path
    width: int
    height: int
    segments: list
    app1: bytes | None = None
    preview: bytes | None = None
    preview_size: tuple[int, int] | None = None
    thumbnail: bytes | None = None
    thumb_size: tuple[int, int] | None = None
    qtables: list[list[int]] = field(default_factory=list)
    dqt_payload: bytes | None = None
    trailer: bytes = b""
    model: str | None = None
    serial: str | None = None
    uid: str | None = None
    firmware: str | None = None
    captured: datetime | None = None

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    @property
    def is_camera_photo(self) -> bool:
        return bool(self.trailer and self.model and "Insta360" in self.model)

    def describe(self) -> str:
        bits = [f"{self.width}x{self.height}"]
        if self.model:
            bits.append(self.model)
        if self.serial:
            bits.append(self.serial)
        if self.firmware:
            bits.append(self.firmware)
        if self.captured:
            bits.append(self.captured.strftime("%Y-%m-%d %H:%M:%S"))
        return "  ".join(bits)


def _image_size(blob: bytes | None):
    if not blob:
        return None
    try:
        with Image.open(io.BytesIO(blob)) as im:
            return im.size
    except Exception:
        return None


def analyse(path: Path) -> Template:
    """Read a JPEG and extract everything the clone needs."""
    data = Path(path).read_bytes()
    segs, scan_start = jpeg.split_markers(data)
    end = jpeg.body_end(data)
    trailer = data[end:]
    width, height = jpeg.dimensions_of(data)

    app1 = next((p for m, p in segs if m == 0xE1 and p.startswith(b"Exif")), None)
    dqt_payload = next((p for m, p in segs if m == 0xDB), None)
    preview = jpeg.embedded_previews(data)

    thumb = thumb_size = None
    tiff = exif.parse_app1(app1) if app1 else None
    if tiff is not None:
        got = exif.extract_thumbnail(tiff)
        if got:
            thumb, thumb_size, _off, _ln = got

    model = serial = firmware = uid = None
    captured = None
    if trailer:
        m = re.search(rb"Insta360 [A-Za-z0-9 ]{1,20}", trailer)
        model = m.group(0).decode("ascii", "replace").strip() if m else None
        m = re.search(rb"v\d+\.\d+\.\d+_build\d+", trailer)
        firmware = m.group(0).decode("ascii", "replace") if m else None
        serial = meta.serial_of(trailer)
        uid = meta.uid_of(trailer)
        captured = meta.capture_time(trailer)

    return Template(
        path=Path(path), width=width, height=height, segments=segs,
        app1=app1, preview=preview, preview_size=_image_size(preview),
        thumbnail=thumb, thumb_size=thumb_size,
        qtables=jpeg.qtables_of(data) if dqt_payload else [],
        dqt_payload=dqt_payload, trailer=trailer,
        model=model, serial=serial, uid=uid, firmware=firmware,
        captured=captured,
    )


def pick(templates: list[Template], want: tuple[int, int],
         preset_dims: tuple[int, int] | None = None) -> Template:
    """Choose the template whose aspect ratio is closest to the source image."""
    if not templates:
        raise ValueError("no templates to choose from")
    aspect = want[0] / want[1]

    def score(t: Template) -> float:
        s = abs(t.aspect - aspect)
        s += (t.width * t.height) / 1e12   # tie-break: prefer the smaller mode
        if preset_dims and t.size == preset_dims:
            s -= 0.05          # prefer the mode that was asked for
        if not t.trailer:
            s += 10.0          # a template without metadata is nearly useless
        if t.captured:
            name_time = "".join(ch for ch in t.path.name if ch.isdigit())[:14]
            if t.captured.strftime("%Y%m%d%H%M%S") != name_time:
                s += 0.5       # inconsistent photo: probably not a camera original
        return s

    return min(templates, key=score)


def pick_preset(width: int, height: int, requested: str) -> str:
    """Resolve ``auto`` into a concrete camera mode name."""
    if requested != "auto":
        return requested
    aspect = width / height
    best, best_d = None, 1e9
    for name in AUTO_PRESETS:
        p = PRESETS[name]
        d = abs(aspect - p["width"] / p["height"])
        if d < best_d:
            best, best_d = name, d
    return best


def find_sample(samples_dir: Path, preset_name: str,
                explicit: Path | None = None) -> Path:
    """Offline fallback: locate a sample JPEG that matches a preset."""
    if explicit:
        if not explicit.exists():
            raise SystemExit(f"template not found: {explicit}")
        return explicit

    p = PRESETS[preset_name]
    candidates = sorted(samples_dir.glob("*.jpg")) + sorted(samples_dir.glob("*.JPG"))
    if not candidates:
        raise SystemExit(f"no template JPEGs in {samples_dir}")
    target_ar = p["width"] / p["height"]
    scored = []
    for c in candidates:
        try:
            t = analyse(c)
        except Exception:
            continue
        if not t.width:
            continue
        scored.append((abs(t.width / t.height - target_ar)
                       + abs(t.width - p["width"]) / 1e6, c))
    if not scored:
        raise SystemExit("no usable template found")
    return min(scored)[1]
