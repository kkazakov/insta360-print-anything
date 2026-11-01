"""Command line: auto-detect the camera, build the clone, install it."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import build, camera, jpeg, meta
from .template import PRESETS, Template, analyse, find_sample, pick, pick_preset

MAX_WIDTH = 78


def hr(title: str = "") -> None:
    if title:
        print(f"\n--- {title} " + "-" * max(0, MAX_WIDTH - len(title) - 5))
    else:
        print("-" * MAX_WIDTH)


def confirm(question: str, assume_yes: bool = False) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("  ! not a terminal; pass --yes to install without confirmation")
        return False
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _resolve_id(value: str | None, fmt: re.Pattern, generator, label: str):
    if value is None or value == "keep":
        return None
    if value == "random":
        return generator()
    if not fmt.match(value):
        expected = ("14 chars 'IB' + 12 of A-Z0-9" if label == "serial"
                    else "32 lower-case hex chars")
        raise SystemExit(f"invalid --{label} {value!r}: expected {expected}")
    return value


# --------------------------------------------------------------------------
# clone
# --------------------------------------------------------------------------

def cmd_clone(args) -> int:
    if not args.input:
        raise SystemExit(
            "no image given. Usage: insta360_clone.py IMAGE [/path/to/camera]\n"
            "  e.g. insta360_clone.py photo.jpg")
    src = Path(args.input)
    if not src.exists():
        raise SystemExit(f"input not found: {src}")

    serial = _resolve_id(args.serial, meta.SERIAL_FMT, meta.random_serial, "serial")
    uid = _resolve_id(args.uid, meta.UID_FMT, meta.random_uid, "uid")

    image = build.load_image(src)
    print(f"input      : {src}  {image.width}x{image.height} "
          f"({image.width / image.height:.3f})")

    # ---- who are we pretending to be? ---------------------------------
    cam = None
    if args.camera:
        cam = camera.open_camera(Path(args.camera))
        if cam is None:
            raise SystemExit(f"{args.camera} is not an Insta360 volume")
    elif not args.template and not args.offline:
        cameras = camera.discover()
        cam = cameras[0] if cameras else None

    if cam is not None:
        print(f"camera     : {cam.describe()}")
        if cam.read_only:
            print("  ! read-only mount: the file can only be written to out/")
            print(camera.READ_ONLY_HELP.format(
                root=cam.root, dev=cam.device or "/dev/sdX").replace("\n", "\n  "))
        if cam.photos and not cam.templates:
            raise SystemExit(
                "the camera has photos but none of them carry Insta360 "
                "metadata; take one photo with the camera and reconnect")
        if not cam.photos:
            raise SystemExit(
                "the camera has no photos yet. Take at least one photo with "
                "it, reconnect, and run this again - the tool copies the "
                "identity from a real photo (see --samples for offline use)")

    template: Template
    source: str
    if args.template:
        template = analyse(Path(args.template))
        source = f"{template.path}  (explicit)"
    elif cam is not None and cam.templates:
        preset_dims = None
        if args.preset != "auto":
            p = PRESETS[args.preset]
            preset_dims = (p["width"], p["height"])
        template = pick(cam.templates, image.size, preset_dims)
        source = f"{template.path.name}  (from camera)"
    else:
        preset_name = pick_preset(image.width, image.height, args.preset)
        path = find_sample(Path(args.samples), preset_name, None)
        template = analyse(path)
        source = f"{template.path}  (offline sample, preset {preset_name})"

    if not template.trailer:
        raise SystemExit(f"{template.path} has no Insta360 metadata block; "
                         f"pick another template")
    if not template.qtables:
        raise SystemExit(f"{template.path} has no quantisation tables")

    eff_serial = serial or template.serial
    eff_uid = uid or template.uid
    print(f"template   : {source}")
    print(f"identity   : {template.model or '?'}  firmware {template.firmware or '?'}")
    print(f"serial/uid : {eff_serial or '?'}"
          f"{'  (random)' if serial else ''}  /  {eff_uid or '?'}"
          f"{'  (random)' if uid else ''}")
    print(f"mode       : {template.width}x{template.height} "
          f"({template.width / template.height:.3f})")

    # ---- timestamp: newest photo wins, whatever the host clock says ----
    explicit_dt = build.parse_timestamp(args.timestamp)
    newest = cam.newest_capture if cam is not None else None
    dt = build.newest_timestamp(newest, explicit_dt)
    if explicit_dt:
        why = "explicit"
    elif newest:
        why = f"newest camera photo {newest:%Y-%m-%d %H:%M:%S} + 1 min"
    else:
        why = "now"
    seq = args.seq if args.seq is not None else build.next_seq(
        cam.media_dir if cam is not None else None, dt)
    filename = build.camera_filename(dt, seq)
    print(f"timestamp  : {dt:%Y-%m-%d %H:%M:%S}  ({why})")
    print(f"name       : {filename}")

    # ---- build ---------------------------------------------------------
    opts = build.Options(
        fit=args.fit, timestamp=dt, seq=seq, serial=serial, uid=uid,
        no_preview=args.no_preview, no_thumb=args.no_thumb,
        no_exif=args.no_exif, no_trailer=args.no_trailer,
        meta_fix=not args.no_meta_fix,
    )
    result = build.build(template, image, opts)

    out_path = Path(args.out) if args.out else Path("out")
    if (out_path.is_dir() or str(args.out or "").endswith(("/", "\\"))
            or out_path.suffix == ""):
        out_path = out_path / result.filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(result.data)
    print(f"written    : {out_path}  ({len(result.data):,} bytes)")
    if result.meta_offset:
        print(f"meta offset: {result.meta_offset:,}  (field 9)")

    ok = True
    if not args.quiet:
        checks, info = build.verify(result.data, template)
        hr("verification")
        for label, passed in checks:
            print(f"  [{'ok' if passed else 'FAIL'}] {label}")
            ok &= passed
        summary = info["summary"]
        print(f"  EXIF: {summary}")
        print(f"  result: " + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))

    # ---- install -------------------------------------------------------
    if cam is None:
        if not args.no_install:
            print("\nno camera detected; file left in "
                  f"{out_path.parent}. Use `camera` to check a mount or "
                  f"`install FILE ROOT` to place it manually.")
        return 0 if ok else 1

    if args.no_install or cam.read_only:
        if cam.read_only:
            print(f"\nnot installed: {cam.root} is read-only (fix above)")
        else:
            print(f"\n--no-install: nothing written to {cam.root}")
        return 0 if ok else 1

    hr("install")
    dest = cam.media_dir / result.filename
    print(f"  target : {dest}")
    if not confirm(f"  copy to the camera and add it to "
                   f"{meta.FILEINFO_LIST}?", args.yes):
        print("  skipped. The file is still at " f"{out_path}.")
        return 0 if ok else 1

    try:
        installed = camera.install(cam, result.data, result.filename)
    except SystemExit as exc:
        print(f"  ! {exc}")
        return 1
    print(f"  copied : {installed}  ({len(result.data):,} bytes)")
    print(f"  index  : {len(meta.index_records(cam.index_path.read_bytes()))} "
          f"records")
    print("  eject the camera and it should be first in the gallery")
    return 0 if ok else 1


# --------------------------------------------------------------------------
# analyze
# --------------------------------------------------------------------------

def cmd_analyze(path: Path) -> int:
    t = analyse(path)
    data = Path(path).read_bytes()
    print(f"file            : {path}  ({len(data):,} bytes)")
    print(f"dimensions      : {t.width}x{t.height}  "
          f"({t.width * t.height / 1e6:.1f} MP, {t.width / t.height:.4f})")
    print(f"model           : {t.model}  serial {t.serial}  uid {t.uid}")
    print(f"firmware        : {t.firmware}   captured {t.captured}")
    print(f"marker layout   : {jpeg.marker_desc(t.segments)}")
    print(f"quant tables    : {len(t.qtables)} tables, luma starts "
          f"{t.qtables[0][:8] if t.qtables else '-'}")
    print(f"hidden preview  : {t.preview_size} "
          f"({len(t.preview):,} bytes in APP2)" if t.preview
          else "hidden preview  : none")
    print(f"EXIF thumbnail  : {t.thumb_size} ({len(t.thumbnail):,} bytes)"
          if t.thumbnail else "EXIF thumbnail  : none")
    print(f"trailer         : {len(t.trailer):,} bytes")
    if t.trailer:
        info = meta.meta_info(t.trailer)
        body = len(data) - len(t.trailer)
        print(f"    serial      : {info.get('serial')}")
        print(f"    field 7     : {info.get('timestamp')}")
        bulk = meta.bulk_info(t.trailer)
        if bulk:
            print(f"    raw thumb   : {bulk['width']}x{bulk['height']} NV21 at "
                  f"+{bulk['offset']}, {bulk['size']:,} B")
        print(f"    field 9     : {info.get('file_offset'):,}  "
              f"(blob at {body + info.get('blob_off', 0):,}, "
              f"{'ok' if info.get('file_offset') == body + info.get('blob_off', 0) else 'STALE'})")
    from .exif import read_summary
    print("EXIF summary    :")
    for k, v in read_summary(t.app1).items():
        print(f"    {k:<18}: {v}")

    score = sum(bool(x) for x in (
        t.segments and t.segments[0][0] == 0xE1,
        t.model and "Insta360" in t.model,
        t.preview_size, t.thumb_size, len(t.trailer) > 1000,
        any(m == 0xDB and len(p) == 130 for m, p in t.segments),
        any(m == 0xC4 and len(p) == 416 for m, p in t.segments),
        t.serial,
    ))
    print(f"camera fingerprints: {score}/8")
    return 0


# --------------------------------------------------------------------------
# camera / install
# --------------------------------------------------------------------------

def cmd_camera(root: Path | None) -> int:
    if root is not None:
        cameras = [camera.open_camera(root)]
        if cameras[0] is None:
            raise SystemExit(f"{root} does not look like an Insta360 volume")
    else:
        cameras = camera.discover()
    if not cameras:
        print("no Insta360 camera found. Checked: "
              + ", ".join(str(p) for p in camera.mount_roots()))
        return 1

    for cam in cameras:
        print(f"camera : {cam.describe()}")
        if cam.read_only:
            print("         ! read-only mount - nothing can be installed")
            print("         " + camera.READ_ONLY_HELP.format(
                root=cam.root, dev=cam.device or "/dev/sdX").replace("\n", "\n         "))
        if cam.newest_capture:
            print(f"         newest capture {cam.newest_capture:%Y-%m-%d %H:%M:%S}"
                  f", {len(cam.templates)} usable template(s)")
        records, problems = camera.audit(cam.root)
        hr("index")
        bad = {p for p, _ in problems}
        for path, _blob, _raw in records:
            if path in bad:
                print(f"  STALE    {path}")
                for msg in dict(problems)[path]:
                    print(f"             - {msg}")
            else:
                print(f"  ok       {path}")
        print(f"\n  {len(records) - len(problems)}/{len(records)} records consistent")
        hr()
    return 0 if not problems else 1


def cmd_remove(name: str, root: Path | None, assume_yes: bool,
               dry_run: bool) -> int:
    if root is None:
        cameras = camera.discover()
        if not cameras:
            raise SystemExit("no Insta360 camera found; pass the mount point")
        cam = cameras[0]
    else:
        cam = camera.open_camera(root)
        if cam is None:
            raise SystemExit(f"{root} does not look like an Insta360 volume")
    print(f"camera : {cam.describe()}")
    print(f"remove : {cam.media_dir / name}")
    if not dry_run and not confirm("delete it from the camera?", assume_yes):
        print("skipped")
        return 0
    dest = camera.remove(cam, name, dry_run=dry_run)
    print(f"{'would delete' if dry_run else 'deleted'} : {dest}")
    if not dry_run:
        print(f"index  : {len(meta.index_records(cam.index_path.read_bytes()))} "
              f"records")
    return 0


def cmd_install(path: Path, root: Path | None, assume_yes: bool, dry_run: bool) -> int:
    if not path.exists():
        raise SystemExit(f"file not found: {path}")
    if root is None:
        cameras = camera.discover()
        if not cameras:
            raise SystemExit("no Insta360 camera found; pass the mount point")
        cam = cameras[0]
    else:
        cam = camera.open_camera(root)
        if cam is None:
            raise SystemExit(f"{root} does not look like an Insta360 volume")

    if not dry_run and not confirm(f"copy {path.name} to {cam.root}?", assume_yes):
        print("skipped")
        return 0

    data = path.read_bytes()
    print(f"camera : {cam.describe()}")
    print(f"source : {path}  ({len(data):,} bytes)")
    try:
        dest = camera.install(cam, data, path.name, dry_run=dry_run)
    except SystemExit as exc:
        raise SystemExit(str(exc))
    print(f"{'would copy' if dry_run else 'copied   '}: {dest}")
    if not dry_run:
        print(f"index  : {len(meta.index_records(cam.index_path.read_bytes()))} records")
    return 0


# --------------------------------------------------------------------------
# argparse
# --------------------------------------------------------------------------

def _clone_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="insta360_clone.py",
        usage="%(prog)s [IMAGE [OUTPUT]] [options]\n"
              "       %(prog)s analyze FILE\n"
              "       %(prog)s camera [ROOT]\n"
              "       %(prog)s install FILE [ROOT]\n"
              "       %(prog)s remove NAME [ROOT]",
        description="Turn any picture into a photo that a connected Insta360 "
                    "camera (and the Pocket Printer flow) accepts: it copies "
                    "the identity from a real photo already on the camera, "
                    "rewrites the in-file metadata pointers and registers the "
                    "result in DCIM/fileinfo_list.list.",
        epilog="Examples:\n"
               "  insta360_clone.py photo.jpg                 # detect, build, install\n"
               "  insta360_clone.py photo.jpg --no-install    # just build to out/\n"
               "  insta360_clone.py photo.jpg --serial random  # identity A/B test\n"
               "  insta360_clone.py camera                    # find + audit cameras\n"
               "  insta360_clone.py analyze FILE.jpg\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("input_arg", nargs="?", metavar="IMAGE",
                    help="source image to turn into a camera photo")
    ap.add_argument("output_arg", nargs="?", metavar="OUTPUT",
                    help="output file or directory (default: out/)")
    ap.add_argument("-i", "--input", dest="input_opt", metavar="IMAGE",
                    help="same as the IMAGE positional")
    ap.add_argument("-o", "--out", dest="out_opt", metavar="OUTPUT",
                    help="same as the OUTPUT positional")
    ap.add_argument("--camera", metavar="ROOT",
                    help="use this mount point instead of auto-detection")
    ap.add_argument("--offline", action="store_true",
                    help="ignore any connected camera and use --samples instead")
    ap.add_argument("--no-install", action="store_true",
                    help="build the file but never write to the camera")
    ap.add_argument("--yes", "-y", action="store_true",
                    help="do not ask before writing to the camera")
    ap.add_argument("--samples", default="real-samples",
                    help="directory holding at least one real Insta360 JPEG to "
                         "use as an offline template (default: real-samples)")
    ap.add_argument("--template", type=Path,
                    help="use this exact file as the metadata template")
    ap.add_argument("--preset", default="auto", choices=["auto", *PRESETS],
                    help="camera mode hint (default: auto, follows the photo "
                         "on the camera)")
    ap.add_argument("--fit", default="crop", choices=["crop", "pad", "stretch"],
                    help="how to reach the template's aspect (default: crop)")
    ap.add_argument("--timestamp",
                    help="capture time, e.g. '2025:01:01 12:00:00' "
                         "(default: newest camera photo + 1 min, or now)")
    ap.add_argument("--seq", type=int, help="DCF sequence number")
    ap.add_argument("--serial", default="keep",
                    help="trailer serial: 'keep' (default), 'random', or an "
                         "explicit 14-character value like IBEXAMPLE00001")
    ap.add_argument("--uid", default="keep",
                    help="trailer device UID: 'keep' (default), 'random', or "
                         "32 lower-case hex characters")
    ap.add_argument("--no-preview", action="store_true",
                    help="omit the hidden APP2 preview JPEG")
    ap.add_argument("--no-thumb", action="store_true",
                    help="keep the template's EXIF thumbnail")
    ap.add_argument("--no-exif", action="store_true", help="omit APP1 entirely")
    ap.add_argument("--no-trailer", action="store_true",
                    help="omit the proprietary post-EOI metadata blob")
    ap.add_argument("--no-meta-fix", action="store_true",
                    help="keep the template's in-file metadata offset (field 9); "
                         "the camera will not list the file")
    ap.add_argument("--quiet", action="store_true", help="skip verification")
    return ap


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] == "analyze":
        ap = argparse.ArgumentParser(prog="insta360_clone.py analyze",
                                     description="Show the camera-specific "
                                                 "structure of a JPEG.")
        ap.add_argument("file", type=Path)
        return cmd_analyze(ap.parse_args(argv[1:]).file)

    if argv and argv[0] == "camera":
        ap = argparse.ArgumentParser(prog="insta360_clone.py camera",
                                     description="Detect connected Insta360 "
                                                 "cameras and audit their media "
                                                 "index.")
        ap.add_argument("root", nargs="?", type=Path)
        return cmd_camera(ap.parse_args(argv[1:]).root)

    if argv and argv[0] == "install":
        ap = argparse.ArgumentParser(prog="insta360_clone.py install",
                                     description="Copy a generated JPEG onto "
                                                 "the camera and refresh its "
                                                 "media index.")
        ap.add_argument("file", type=Path)
        ap.add_argument("root", nargs="?", type=Path)
        ap.add_argument("--yes", "-y", action="store_true")
        ap.add_argument("--dry-run", action="store_true")
        a = ap.parse_args(argv[1:])
        return cmd_install(a.file, a.root, a.yes, a.dry_run)

    if argv and argv[0] == "remove":
        ap = argparse.ArgumentParser(
            prog="insta360_clone.py remove",
            description="Delete one media file from the camera and drop its "
                        "records from both index lists.")
        ap.add_argument("name", help="e.g. IMG_20250101_120500_004.jpg")
        ap.add_argument("root", nargs="?", type=Path)
        ap.add_argument("--yes", "-y", action="store_true")
        ap.add_argument("--dry-run", action="store_true")
        a = ap.parse_args(argv[1:])
        return cmd_remove(a.name, a.root, a.yes, a.dry_run)

    ap = _clone_parser()
    args = ap.parse_args(argv)
    if args.input_arg and args.input_opt:
        ap.error("give the input either as IMAGE or with -i/--input, not both")
    if args.output_arg and args.out_opt:
        ap.error("give the output either as OUTPUT or with -o/--out, not both")
    args.input = args.input_arg or args.input_opt
    args.out = args.output_arg or args.out_opt
    return cmd_clone(args)
