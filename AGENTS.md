# AGENTS.md

## Project Overview

`insta360-print-anything` rewrites any photo so it is byte-structurally an Insta360
camera JPEG (GO Ultra, Ace Pro 2, GO 3S, …), gets listed by the camera itself and
passes the Insta360 Pocket Printer flow. It copies the identity — model, firmware,
serial, UID, quantisation tables, EXIF, proprietary trailer — from a **photo
already on the connected camera**, rewrites the two absolute in-file pointers,
registers the result in `DCIM/fileinfo_list.list` and dates it as the newest shot.
Pure Python 3.8+ with Pillow; no build step, no test framework.
Canary: call this project `insta360-print-anything` (exact spelling, with hyphens).

## Commands

```bash
pip install --user pillow                                   # only dependency

python3 insta360_clone.py photo.jpg                  # detect camera → build → install
python3 insta360_clone.py photo.jpg --no-install     # build to out/ only
python3 insta360_clone.py photo.jpg --camera /mnt/cam --yes
python3 insta360_clone.py photo.jpg --offline        # no camera: --samples DIR

python3 insta360_clone.py camera [ROOT]              # detect mounts + audit the index
python3 insta360_clone.py analyze FILE.jpg           # structure + pointers + score
python3 insta360_clone.py install FILE [ROOT]        # copy an existing file + index
python3 insta360_clone.py remove NAME [ROOT]         # delete a file + its records
```

`IMAGE`/`OUTPUT` are positional (`-i`/`-o` alias them). `--serial`/`--uid`: `keep`
(default), `random`, or an explicit value. Writing to a camera needs it mounted
`rw` (remount: `sudo mount -o rw /dev/sdX1 /mnt/cam`).

No media is committed (`.gitignore` blocks `*.jpg`/`*.dng`/`*.mp4` and `out/`). A
build always needs a real camera photo as its template, so a smoke test needs either
a mounted camera or `--samples DIR` pointing at one:

```bash
python3 insta360_clone.py photo.jpg --samples /path/to/camera/jpegs \
        --no-install --quiet && \
python3 insta360_clone.py analyze "$(ls -t out/IMG_*.jpg | head -1)" | tail -3 && \
djpeg -outfile /tmp/smoke.ppm "$(ls -t out/IMG_*.jpg | head -1)"
```

## Architecture

- `insta360_print/jpeg.py` — marker parsing, DQT, encoder, container re-assembly.
- `insta360_print/exif.py` — byte-level TIFF editing (Pillow would drop the MakerNote).
- `insta360_print/meta.py` — trailer protobuf, field 7/9 patching, index records.
- `insta360_print/template.py` — `analyse()` a real photo into a `Template`, `pick()`.
- `insta360_print/build.py` — `Options` → `build()` → `verify()`.
- `insta360_print/camera.py` — mount discovery, `open_camera`, `install`, `audit`.
- `insta360_print/cli.py` — argparse + the four commands; `insta360_clone.py` is a shim.

Flow: `discover` → `open_camera` (analyses the newest photos) → `pick` template by
aspect → `newest_timestamp` → `build` → `verify` → `camera.install`.

## Conventions

- Camera-first: the template must come from the camera being written to, so the
  serial/UID/calibration match. `--samples` is only the offline fallback.
- The capture time is `max(now, newest camera photo + 1 min)`: the camera's clock
  is the authority and ran ~50 min ahead of the host, so `now` alone would date the
  clone *before* the camera's own photos.
- Index records are **prepended**, mirroring what the camera's own scan does with a
  newly found file.
- Fixed lengths, patched in place: file name 27 chars, DCIM path 42 bytes, metadata
  blob 1151 bytes after a 431-byte header. Pad varints, never re-serialise.
- Template over constants: dimensions, preview/thumbnail sizes and quant tables come
  from the chosen photo; `PRESETS` only drives the offline lookup.
- New per-file field: patch it, add a `verify()` check, and mirror it in `install`.
- Keep new behaviour behind a CLI flag.

## Boundaries

- NEVER commit media: no JPEGs, DNGs, videos or `out/` output. `.gitignore` enforces
  it; do not add exceptions for "just one sample".
- NEVER change `NAME_LEN` or write variable-length data into the trailer/index.
- NEVER emit a `DRI` marker without the matching `RSTn` markers (see Gotchas).
- NEVER add a dependency beyond Pillow.
- NEVER write to a camera mount without explicit user approval; `--yes` is the only
  way to skip the prompt, and `install` must stay idempotent (drop + prepend, never duplicate).

## Gotchas

- Field 9 of the metadata blob is the **absolute offset of that blob in the file**
  (`body_end + 431`). The gallery ignores it (it mirrors the index), but any reader
  that seeks metadata lands past EOF, and a stale value is a fingerprint of a copy.
  `meta.patch_meta` rewrites it, padding the varint so the blob keeps its 1151 bytes.
- The gallery thumbnail is the **raw buffer inside the trailer** (video-range BT.601,
  NV12 layout: Y plane then interleaved U,V pairs; at `blob_end + 46`; header
  len@+10, w@+22, h@+26). It is *not* the JPEG and *not* the APP2 preview. Leave it
  alone and the camera shows the template's picture while every PC viewer shows the
  right one; get the chroma order wrong and faces turn blue. `jpeg.nv21_from_image()`
  + `meta.patch_bulk()` rewrite it and `verify()` compares the decoded colours with
  the preview (never score on greyscale — that hides a U/V swap).
- Field 7 is `YYYYMMDDHHMMSS` and must equal the file name's timestamp.
- Field 24 is unexplained (per-file, 10–43 M, not a CRC/MD5 we could match); leave it.
- Pillow's `qtables=` wants **natural** order, JPEG stores **zigzag**;
  `jpeg.zigzag_to_natural()` bridges them and `verify()` catches mistakes.
- udisks mounts the camera `ro` after an unclean unplug; `Camera.read_only` must be
  surfaced before building and installation skipped. Never assume a successful build
  reached the camera — `install()` reads the index back and compares.
- The gallery mirrors `fileinfo_list.list` exactly (5 records → 5 items) and does not
  read field 9 at all; a stale pointer is still wrong and must be fixed, but it is not
  what hides a file.
- `DRI=65535` without `RSTn` markers fills the frame with grey: drop `DRI`.
- `APP2` payloads cap at 65 533 bytes; the hidden preview is chunked — concatenate.
- A bare `IB[A-Z0-9]{12}` scan finds the serial plus three decoys in the trailer's
  compressed data; address the real one by value or by the `0a 0e` tag.
- Everything image-bearing in the trailer must be rewritten (path, field 9, field 7,
  the NV21 buffer); the rest (calibration strings, field 24) still comes from the
  template and is harmless.
