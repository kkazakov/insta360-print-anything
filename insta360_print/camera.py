"""Finding a connected Insta360 camera and writing to it.

The camera exposes itself as plain USB mass storage: a volume with ``DCIM`` and
``MISC``.  Everything that makes a file "belong" to it lives in
``DCIM/fileinfo_list.list`` and inside each photo's trailer, so the most
faithful template is a photo that is already on the camera.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import jpeg, meta
from .template import Template, analyse

#: how many of the newest photos to inspect for identity / templates
SCAN_LIMIT = 6


@dataclass
class Camera:
    """A mounted Insta360 volume."""

    root: Path
    photos: list[Path] = field(default_factory=list)
    templates: list[Template] = field(default_factory=list)
    read_only: bool = True

    @property
    def mode(self) -> str:
        return "ro" if self.read_only else "rw"

    @property
    def device(self) -> str | None:
        return mount_info(self.root)[0]

    @property
    def model(self) -> str | None:
        return self.templates[0].model if self.templates else None

    @property
    def serial(self) -> str | None:
        return self.templates[0].serial if self.templates else None

    @property
    def firmware(self) -> str | None:
        return self.templates[0].firmware if self.templates else None

    @property
    def uid(self) -> str | None:
        return self.templates[0].uid if self.templates else None

    @property
    def newest_capture(self):
        times = [t.captured for t in self.templates if t.captured]
        return max(times) if times else None

    @property
    def media_dir(self) -> Path:
        return self.root / meta.CAMERA_DIR

    @property
    def index_path(self) -> Path:
        return self.root / meta.FILEINFO_LIST

    def describe(self) -> str:
        bits = [str(self.root)]
        if self.model:
            bits.append(self.model)
        if self.serial:
            bits.append(self.serial)
        if self.firmware:
            bits.append(self.firmware)
        bits.append(f"{len(self.photos)} photo(s)")
        bits.append(f"[{self.mode}]")
        return "  ".join(bits)


def mount_roots() -> list[Path]:
    """Every plausible removable-volume mount point on this platform."""
    roots: set[Path] = set()
    for pattern in ("/run/media/*/*", "/media/*/*", "/media/*",
                    "/mnt/*", "/Volumes/*"):
        roots.update(Path(p) for p in glob.glob(pattern))
    if os.name == "nt":                      # D: .. Z:
        for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
            p = Path(f"{letter}:\\")
            if p.exists():
                roots.add(p)
    return sorted(roots, key=lambda p: str(p).lower())


def mount_info(path: Path):
    """(source device, mount options) for a mount point, or (None, [])."""
    try:
        target = Path(path).resolve()
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 4 and Path(parts[1]) == target:
                    return parts[0], parts[3].split(",")
    except OSError:
        pass
    return None, []


def is_read_only(path: Path) -> bool:
    """True when the volume is mounted read-only (the usual udisks default)."""
    _src, opts = mount_info(path)
    if opts:
        return "ro" in opts
    try:
        probe = Path(path) / ".insta360_write_test"
        probe.write_bytes(b"")
        probe.unlink()
        return False
    except OSError:
        return True


def looks_like_camera(root: Path) -> bool:
    """Cheap structural test, before reading any metadata."""
    dcim = root / "DCIM"
    if not dcim.is_dir():
        return False
    if (dcim / "Camera01").is_dir():
        return True
    return (root / meta.FILEINFO_LIST).exists()


def list_photos(root: Path, limit: int | None = None) -> list[Path]:
    """JPEGs in DCIM/Camera01 (and any sibling Camera02…), newest first."""
    found: list[Path] = []
    dcim = root / "DCIM"
    for folder in sorted(dcim.glob("Camera*")):
        if folder.is_dir():
            found.extend(p for p in folder.iterdir()
                         if p.suffix.lower() in (".jpg", ".jpeg"))
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[:limit] if limit else found


def open_camera(root: Path, limit: int = SCAN_LIMIT) -> Camera | None:
    """Wrap a mount point, analysing the newest photos for identity."""
    root = Path(root)
    if not looks_like_camera(root):
        return None
    cam = Camera(root=root, photos=list_photos(root),
                 read_only=is_read_only(root))
    for photo in cam.photos[:limit]:
        try:
            t = analyse(photo)
        except Exception:
            continue
        if t.is_camera_photo:
            cam.templates.append(t)
    return cam


def discover(explicit: Path | None = None) -> list[Camera]:
    """All connected Insta360 cameras, best candidate first."""
    if explicit:
        cam = open_camera(explicit)
        if cam is None:
            raise SystemExit(f"{explicit} does not look like an Insta360 volume "
                             f"(no DCIM/Camera01)")
        return [cam]

    cameras = []
    for root in mount_roots():
        try:
            cam = open_camera(root)
        except (PermissionError, OSError):
            continue
        if cam is not None:
            cameras.append(cam)

    # a writable camera that still has photos to copy metadata from wins
    cameras.sort(key=lambda c: (c.read_only, not c.templates, not c.photos,
                                str(c.root)))
    return cameras


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------

def is_writable(root: Path) -> bool:
    probe = root / ".insta360_write_test"
    try:
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def install(cam: Camera, data: bytes, filename: str, *,
            dry_run: bool = False) -> Path:
    """Copy one JPEG onto the camera and refresh both index lists."""
    if not meta.NAME_RE.match(filename):
        raise SystemExit(f"refusing to install {filename!r}: not a camera name")
    if not cam.media_dir.is_dir():
        raise SystemExit(f"{cam.media_dir} does not exist")

    dest = cam.media_dir / filename
    rel = "/" + str(dest.relative_to(cam.root))
    info = meta.meta_info(data[jpeg.body_end(data):])
    if not info:
        raise SystemExit("the file has no camera metadata block; build it "
                         "without --no-trailer first")

    blob = data[jpeg.body_end(data):][info["blob_off"]:]
    blob = blob[:info["blob_len"]]
    expected = jpeg.body_end(data) + info["blob_off"]
    if info.get("file_offset") != expected:
        raise SystemExit(f"field 9 points at {info.get('file_offset'):,} but the "
                         f"metadata is at {expected:,}; run the clone step "
                         f"again without --no-meta-fix")

    if dry_run:
        return dest

    if not is_writable(cam.root):
        raise SystemExit(READ_ONLY_HELP.format(root=cam.root,
                                                dev=cam.device or "/dev/sdX"))

    dest.write_bytes(data)

    index = cam.index_path
    old = index.read_bytes() if index.exists() else b""
    _write_atomic(index, meta.upsert_records(
        old, rel, meta.build_index_record(rel, blob), prepend=True))

    edit = cam.root / meta.EDITINFO_LIST
    old = edit.read_bytes() if edit.exists() else b""
    _write_atomic(edit, meta.upsert_records(
        old, rel, meta.build_edit_record(rel), prepend=True))

    # read back: the camera lists exactly what the index contains, so an index
    # that did not take is worth failing loudly over
    if dest.stat().st_size != len(data):
        raise SystemExit(f"short write: {dest} is {dest.stat().st_size:,} bytes, "
                         f"expected {len(data):,}")
    written = dict((p, b) for p, b, _r in meta.index_records(index.read_bytes()))
    if written.get(rel) != blob:
        raise SystemExit("the index record did not read back correctly")
    if rel not in dict((p, b) for p, b, _r in meta.index_records(edit.read_bytes())):
        raise SystemExit("the edit index record did not read back correctly")

    return dest


def remove(cam: "Camera", name: str, *, dry_run: bool = False) -> Path:
    """Delete one media file from the camera and forget it in both indexes."""
    if not meta.NAME_RE.match(name):
        raise SystemExit(f"refusing to remove {name!r}: not a camera name")
    dest = cam.media_dir / name
    rel = "/" + str(dest.relative_to(cam.root))
    index = cam.index_path
    if not index.exists():
        raise SystemExit(f"no {meta.FILEINFO_LIST} on {cam.root}")
    if not any(p == rel for p, _b, _r in meta.index_records(index.read_bytes())):
        raise SystemExit(f"{rel} is not in the index")
    if not dest.exists():
        raise SystemExit(f"{dest} does not exist")
    if dry_run:
        return dest

    dest.unlink()
    _write_atomic(index, meta.drop_records(index.read_bytes(), rel))
    edit = cam.root / meta.EDITINFO_LIST
    if edit.exists():
        _write_atomic(edit, meta.drop_records(edit.read_bytes(), rel))
    return dest


READ_ONLY_HELP = (
    "{root} is mounted READ-ONLY, so nothing can be copied.\n"
    "exFAT is mounted ro when the volume was not ejected cleanly, or when\n"
    "udisks decided to. Fix it once and mount it yourself:\n"
    "\n"
    "  sudo umount {root}\n"
    "  sudo fsck.exfat -y {dev}\n"
    "  sudo mkdir -p /mnt/cam\n"
    "  sudo mount -o rw {dev} /mnt/cam\n"
    "\n"
    "then re-run with --camera /mnt/cam (auto-detection also finds /mnt/*)."
)


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------

def audit(root: Path):
    """Compare every index record with the file it points at.

    Returns ``(records, problems)`` where problems is a list of
    ``(path, [messages])``.
    """
    root = Path(root)
    index = root / meta.FILEINFO_LIST
    if not index.exists():
        raise SystemExit(f"no {meta.FILEINFO_LIST} under {root} - is the "
                         f"camera mounted there?")
    records = meta.index_records(index.read_bytes())
    problems = []
    for path, blob, _raw in records:
        if not path.startswith("/"):
            problems.append((path or "?", ["record without a DCIM path"]))
            continue
        fields, _ = meta.pb_fields(blob)
        by = {f: v for f, w, _s, _e, v in fields if w == 0}
        serial = next((v.decode("ascii", "replace")
                       for f, w, _s, _e, v in fields if f == 1 and w == 2), None)
        target = root / path.lstrip("/")
        if not target.exists():
            problems.append((path, ["file is gone"]))
            continue
        raw = target.read_bytes()
        end = jpeg.body_end(raw)
        trailer = raw[end:]
        off = meta.locate_blob(trailer, serial)
        name_time = "".join(ch for ch in target.name if ch.isdigit())[:14]
        messages = []
        if off is None:
            messages.append("no metadata blob in the trailer")
        elif target.suffix.lower() in (".jpg", ".jpeg"):
            if by.get(9) != end + off:
                messages.append(f"field 9 = {by.get(9):,} but the blob is at "
                                f"{end + off:,}")
        if str(by.get(7)) != name_time:
            messages.append(f"field 7 = {by.get(7)} but the name says {name_time}")
        if messages:
            problems.append((path, messages))
    return records, problems
