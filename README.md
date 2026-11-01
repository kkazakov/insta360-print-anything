# insta360-print-anything

Make **any** photo look like a genuine *Insta360 GO Ultra* capture, so software that
only accepts "camera photos" — the **Insta360 Pocket Printer** flow in the Insta360
app is the case this was built for — will accept it.

Everything below was reverse-engineered from three real GO Ultra files (kept out of
this repository — they are somebody's photos) and from a camera mounted as a USB
drive, using nothing but `file`, `exiv2` and Python.

```bash
python3 insta360_clone.py photo.jpg
# detects the camera, copies its identity from a photo already on it,
# dates the clone as the newest shot, verifies it and installs it:
# -> /media/CAMERA/DCIM/Camera01/IMG_20250101_120500_004.jpg
```

---

## 1. TL;DR

A GO Ultra JPEG is **not** "a JPEG plus some EXIF". It is a very specific container:

| Layer | What the camera writes | Why it matters |
|---|---|---|
| **1. Marker order** | `SOI → APP1(Exif) → APP2 × N → DQT → SOF0 → DHT → [DRI] → SOS` — **APP1 comes first, there is no JFIF `APP0`** | A naive "is this ours?" parser may only look at the first marker. Phone photos start with `APP0`. |
| **2. EXIF** | `Make = Arashi Vision`, `Model = Insta360 GO Ultra`, `Software = v1.9.29_build1`, orientation 1, 72 dpi, `YCbCrPositioning = 1`, Exif 2.31, Interop `R98`, 320-byte `MakerNote` whose payload contains the ASCII magic **`ABMA`** | The cheapest, most common provenance test there is. |
| **3. Hidden preview** | A **complete second JPEG** (704×416 or 704×544, ~100–170 kB) split across one or more `APP2` segments, chunked at 65 533 bytes. It is invisible to normal decoders and to `exiv2`. | Exactly the thing a phone app uses to draw a thumbnail grid without decoding a 37 MP image. Very plausible recognition/unlock marker. |
| **4. Quantisation tables** | Two **custom** tables (`[2,1,1,2,12,14,15,16,…]` for luma), emitted as one 130-byte `DQT` segment, 4:2:0, standard Huffman tables in one 416-byte `DHT` | Cheap to compare, hard to hit by accident — a strong fingerprint. |
| **5. Sizes** | `8192×4608` (37.7 MP, 16:9), `8192×6144` (50.3 MP, 4:3), `4096×3072` (12.6 MP, 4:3) | The app only offers camera modes; arbitrary dimensions scream "not from a camera". |
| **6. Proprietary trailer** | **400–580 kB appended after the `EOI` marker.** A metadata protobuf (model, firmware, **serial**, DCIM path, device UID), the absolute offset of that blob, and then a **raw NV21 copy of the preview** — the buffer the camera actually draws in its gallery. | Easy to find, specific, and a normal photo never has it. Miss the NV21 part and the camera will show the *template's* picture. |
| **7. The camera's own index** | `DCIM/fileinfo_list.list` holds one record per photo, and that record contains a **copy of the trailer's 1151-byte metadata**, including **field 9 = the absolute offset of that metadata inside the file**. | The gallery works from this file. A byte-copied trailer points at the *template's* offset, which can land past the end of the clone — see §3.5. |

`insta360_clone.py` reproduces **all seven** layers from a real sample (the "template"),
swapping only the pixels, the timestamps, the dimensions, the file name and the two
file-specific pointers inside layer 6/7.

---

## 2. The captures examined

Three flat photos from one camera, in its three output modes:

| Capture | Size | Dimensions | MP | Ratio | Trailer | Hidden preview | EXIF thumbnail |
|---|---|---|---|---|---|---|---|
| 37.7 MP, 16:9 | 8 192 401 B | 8192×4608 | 37.7 | 16:9 | 441 002 B | 704×416 (104 582 B) | 160×96 (6 365 B) |
| 50.3 MP, 4:3 | 10 373 929 B | 8192×6144 | 50.3 | 4:3 | 576 170 B | 704×544 (133 490 B) | 160×120 (7 840 B) |
| 12.6 MP, 4:3 | 3 898 735 B | 4096×3072 | 12.6 | 4:3 | 576 170 B | 704×544 (169 807 B) | 160×120 (9 386 B) |

All three carry the **same** serial (14 characters, `IB` + 12) and the **same**
32-hex device UID — that pair is what identifies a body. Placeholders are used
throughout this document: `IBEXAMPLE00001` and
`0123456789abcdef0123456789abcdef`.

The third file — an ordinary Google-processed 3:2 JPEG, `APP0` JFIF first, a
130-byte EXIF block with three tags, an ICC profile in `APP2`, a Photoshop `APP13`,
**no thumbnail, no trailer** — is what the first clone was built from. It could not
look less like a camera file.

None of those images ship with the code: this repository is code only (see
[`.gitignore`](.gitignore)). The tool takes its identity from a photo on *your*
camera, which is better anyway.

---

## 3. Full inventory of a camera file

`python3 insta360_clone.py analyze FILE` prints all of this.

### 3.1 Marker layout (8192×4608 capture)

```
FFD8                      SOI
FFE1 len=8409  "Exif\0\0" + TIFF   (11 IFD0 entries, 6 IFD1 entries, 160x96 JPEG thumb)
FFE2 len=65535  FFD8 FFE0 ...       ┐ hidden preview, chunk 1 of 2
FFE2 len=39051  ... FFD9            ┘ (65 533-byte chunks)
FFDB len=132   two quantisation tables in ONE segment
FFC0 len=17    8-bit, 4608 high, 8192 wide, YCbCr 4:2:0 (0x22/0x11/0x11)
FFC4 len=418   four Huffman tables in ONE segment
FFDD len=4     restart interval 65535        <-- only in the 8192-wide modes
FFDA len=12    baseline scan
...            entropy data
FFD9           EOI
<441 002 bytes of proprietary metadata>
```

Sample 004 (12.6 MP) is identical except that it has **no `FFDD`** and no restart
markers. Samples 002/003 do contain `RSTn` markers in the scan.

### 3.2 EXIF

`IFD0` (11 tags, in this order): `ImageDescription` = the file name, `Make` =
`Arashi Vision`, `Model` = `Insta360 GO Ultra`, `Orientation` = 1, `XResolution` /
`YResolution` = 72/1, `ResolutionUnit` = inch, `Software` = `v1.9.29_build1`,
`DateTime`, `YCbCrPositioning` = 1 (centred), `ExifTag` → ExifIFD, and the next IFD
(`IFD1`) → JPEG thumbnail.

`ExifIFD` (40 tags): the usual exposure block (`f/2.8`, ISO 113, 3.8 mm, 14 mm
equivalent, "Landscape" scene type) plus `ExifVersion 2.31`, `InteroperabilityIndex
R98`, and a **320-byte `MakerNote`**. The MakerNote is a zero-padded blob whose only
non-zero bytes are the ASCII string **`ABMA`** and a trailing
`40 00 00 00 01 00`. It is copied verbatim by the tool.

`IFD1` holds a 160-px-wide JPEG thumbnail, stored as the **last** bytes of the EXIF
block, so it can be replaced by a different-sized one by patching
`JPEGInterchangeFormatLength`.

### 3.3 The hidden APP2 preview

The first `APP2` payload starts with `FFD8 FFE0 0010 "JFIF"` — i.e. it is a complete
JPEG. The next `APP2` payloads continue it and the last one ends with `FFD9`. Decoders
skip `APP2`, so this image is invisible in every normal viewer; concatenating the
payloads yields a perfectly valid 704×416 JPEG.

Its quantisation tables are **the same custom tables as the main image**, and it even
carries its own `FFDD`. The camera clearly encodes it with the same pipeline, just at
704 px wide (height padded up to the next multiple of 32: 396 → 416, 528 → 544).

### 3.4 The proprietary trailer

After the `EOI` of the main image, the file simply keeps going:

```
offset 0x000000   c6 38 00 00           <- 4-byte little-endian size field (14 534)
offset 0x0001b1   0a 0e "IBEXAMPLE00001"             <- serial number
offset 0x0001c1   12 11 "Insta360 GO Ultra"
offset 0x0001d4   1a 0e "v1.9.29_build1"
offset 0x0001e4   2a 3f "1_4800.407_4091.740_3064.700_0.292_-0.405_89.756_8192_6144_11568"
offset 0x000259   b2 01 08 "standard"               <- colour profile
offset 0x000270   d2 01 32 08 03 10 00 1a 2a "/DCIM/Camera01/IMG_YYYYMMDD_HHMMSS_NNN.jpg"
offset 0x06BA8A   aa ca 08 00 03 00 00 00 "0123456789abcdef0123456789abcdef"  (last 32 bytes)
```

The record around the serial is laid out exactly like protobuf (tags `0a`, `12`, `1a`,
`2a`, `b2 01`, `d2 01`, … with explicit lengths), and the rest is bulk per-frame data
(byte maps hovering around `0x80`, smooth `0xB5…` gradient fields — calibration,
tone-mapping and possibly depth/AI data; ~95 % of the trailer differs between two
photos of the same scene). The same trailer length for both 4:3 files (576 170 B) and
a different one for 16:9 (441 002 B) says the payload is sized by the capture mode.

Two length-prefixed fields matter for editing: the **DCIM path** (42-byte prefix, the
file name inside it always 27 characters, `IMG_YYYYMMDD_HHMMSS_NNN.jpg`) and the
**serial** (`0a 0e` — protobuf field 1, length 14 — followed by `IB` + 12 base-36
characters). The tool therefore only ever swaps bytes length-for-length and never
edits the prefixes. Note that the blob also contains three *decoy* `IB…` runs inside
its compressed data, so the real serial must be addressed by its known value or its
tag, not by "first pattern match" (see 5.6).

### 3.4b The raw thumbnail inside the trailer

The bulk data after the metadata blob is not opaque — it is **another complete
copy of the preview image**, uncompressed:

```
[metadata protobuf, 1151 B]
[46-byte header]
     u16 @ +2   blob length (1151)
     u32 @ +10  data length (574 504 = 574 464 + 40 padding)
     u32 @ +22  width  (704)
     u32 @ +26  height (544)
[NV21 buffer at +46]            Y plane 704x544, then interleaved V,U pairs
[40 bytes of padding][38 bytes of trailer]
```

It is **NV12, video-range BT.601**: decode it with the usual limited-range matrix
and you get the preview back (luma correlates 0.9988 with the `APP2` JPEG; full-range
decoding is visibly washed out, limited-range matches to an MSE of 783 vs 20 480).

And this is what the camera draws in its gallery. A clone that leaves the template's
buffer alone shows **the template's photo** as its thumbnail — the file itself is fine,
so `djpeg` and every PC viewer show the right picture, which makes it a maddening bug
to chase. The clone rewrites this buffer from the same fitted preview it puts in
`APP2`, and `verify()` decodes it back and compares it with that preview.

### 3.5 The camera's own media index

The volume is not just a folder of JPEGs — it carries its own catalogue:

```
DCIM/
├── Camera01/                 the media
├── fileinfo_list.list        one record per photo/video   <-- the gallery reads this
└── .editinfo_list.list       one minimal record per media file
MISC/
├── RECRST_IDX0.SUCC, RECRST_IDX1.SUCC
└── p2pta_download.list, p2pta_download_tmp.list
```

`fileinfo_list.list` is a sequence of length-delimited records:

```
0a <len>   0a <len(path)> <path>   12 <len(blob)> <blob>
```

…and `<blob>` is **byte-for-byte the same 1151-byte protobuf that sits inside the
JPEG's trailer at offset 431**. The camera copies one into the other, which is why a
cloned JPEG automatically produced a cloned index entry — and why that entry inherited
the template's *pointers*.

Two fields in that blob are absolute and per-file:

| field | meaning | 8192×4608 | 4096×3072 |
|---|---|---|---|
| **9** | offset of the metadata blob inside the file, always `jpeg_body + 431` | 7 751 830 | 3 322 996 |
| **7** | capture time `YYYYMMDDHHMMSS`, identical to the file name | 20250101120400 | 20250101120500 |

A clone that keeps the template's values points **outside itself**: the first clone we
put on the camera was 2 019 575 bytes but claimed its metadata lived at 3 322 996,
i.e. 1.3 MB past its own end.

Observed: that file *was* still listed by the camera. The gallery is exactly the
records in `fileinfo_list.list` (5 records → 5 items), it does not seek to field 9 and
does not validate it; the camera's own scan computes `body_end + 431` itself and copies
the blob verbatim. So the pointer is not what gates display — but it is wrong, it is
trivially detectable, and anything that *does* seek the metadata lands past EOF. The
tool therefore rewrites it on every build (`meta.patch_meta`) and verifies it.

Field 24 stays unexplained (per-file, 10–43 M range, not a CRC/MD5 of anything we
tried); it is left untouched. Fields 19/27/44 carry the dimensions and are already
correct because the template is chosen to match the preset.

---

## 4. What is probably used for "recognition" — ranked

Nothing here can be proven without the printer/app itself, so treat this as a
priority list. The tool implements **all** of them at once, which is why it does not
matter which one the app actually checks.

1. **The post-`EOI` trailer** *(most likely)* — unique to the camera, contains the
   model, firmware and serial number, trivial to detect (bytes after `EOI`), and
   effectively impossible for a phone/edited photo to have by accident. It also lets
   the app associate a photo with *the* camera it is paired with.
2. **The hidden `APP2` preview** — a free, pre-decoded thumbnail grid; an app can
   list "camera media" without touching the 37 MP main image.
3. **EXIF `Make`/`Model`** — the obvious check. Some apps also look at `Software`
   (`v1.9.29_build1`) or the `ABMA` MakerNote.
4. **Dimensions** — the picker only offers what the camera can produce
   (8192×4608 / 8192×6144 / 4096×3072). Arbitrary sizes get filtered.
5. **Quantisation tables / marker order** — cheap fingerprints for "was this encoded
   by our firmware?".
6. **The embedded EXIF thumbnail** — an image without one may simply never render in
   the picker.

The Insta360 manual for the Pocket Printer states that the *Album* source accepts
"JPEG, PNG and RAW", while the *Camera / Downloaded* source is what it advertises for
camera files — i.e. the app distinguishes sources, which is exactly the split this
tool attacks. See <https://onlinemanual.insta360.com/app/en-us/operation-tutorial/camera-connection/connect-pocketprinter>.

---

## 5. The tool

Python 3.8+, Pillow only, organised as a small package:

```
insta360_clone.py        entry point (`python3 insta360_clone.py …` or `-m insta360_print`)
insta360_print/
├── jpeg.py              markers, quantisation tables, encoder, container re-assembly
├── exif.py              byte-level TIFF editing (keeps the MakerNote intact)
├── meta.py              trailer protobuf + camera index reader/writer
├── template.py          reads a real camera photo into a Template
├── build.py             render → assemble → verify
├── camera.py            mount discovery, install, audit
└── cli.py               argparse and the four commands
```

### 5.1 Install

```bash
python3 -m pip install --user pillow      # only dependency
```

### 5.2 The normal flow: camera plugged in

```bash
$ python3 insta360_clone.py photo.jpg
input      : photo.jpg  1346x898 (1.499)
camera     : /media/CAMERA  Insta360 GO Ultra  IBEXAMPLE00001  v1.9.29_build1  4 photo(s)
template   : IMG_20250101_120200_003.jpg  (from camera)
identity   : Insta360 GO Ultra  serial IBEXAMPLE00001  uid 01234567…  firmware v1.9.29_build1
mode       : 4096x3072 (1.333)
timestamp  : 2025-01-01 12:05:00  (newest camera photo 2025-01-01 12:04:00 + 1 min)
name       : IMG_20250101_120500_004.jpg
written    : out/IMG_20250101_120500_004.jpg  (2,019,575 bytes)
meta offset: 1,443,836  (field 9)

--- verification -------------------------------------------------------------
  [ok] first marker is APP1/EXIF (no APP0)     [ok] ImageDescription has camera naming
  ...                                          [ok] metadata offset (field 9) points at the blob
  result: ALL CHECKS PASSED

--- install ------------------------------------------------------------------
  target : /media/CAMERA/DCIM/Camera01/IMG_20250101_120500_004.jpg
  copy to the camera and add it to DCIM/fileinfo_list.list? [y/N]
```

In order, it:

1. **Detects** Insta360 volumes — `/run/media/*/*`, `/media/*/*`, `/mnt/*`,
   `/Volumes/*` and Windows drive letters, validated by a `DCIM/Camera01`.
   Anything with photos wins; the model in the metadata (`GO Ultra`, `Ace Pro 2`,
   `GO 3S`, …) is reported, not hard-coded.
2. **Takes the identity from a photo already on that camera**: model, firmware,
   serial, device UID, quantisation tables, the whole EXIF block, the preview and
   thumbnail sizes and the calibration blob. The capture **mode** follows that
   photo (the one whose aspect is closest to your image); `--preset` overrides.
3. **Dating it as the newest** — `max(now, newest camera photo + 1 min)`. The
   camera's clock is the authority (it ran ~50 min ahead of the host in testing),
   so trusting the host clock would date the clone *before* the camera's own
   photos and bury it in the list.
4. **Builds** the file and rewrites the two absolute in-file pointers (field 9,
   field 7) plus the DCIM path.
5. **Verifies** it, then (after confirmation) **installs**: copy into
   `DCIM/Camera01/`, then add the record at the **front** of `fileinfo_list.list`
   and `.editinfo_list.list` — the camera does the same when its own scan picks up
   a new file — and date it after every existing shot.

The camera must contain **at least one photo** — that is where the identity comes
from. With none, the tool stops rather than inventing a serial:

```
the camera has no photos yet. Take at least one photo with it, reconnect, and
run this again - the tool copies the identity from a real photo
```

### 5.3 Commands

| Command | What it does |
|---|---|
| `insta360_clone.py IMAGE [OUTPUT]` | detect the camera, build, verify, install |
| `insta360_clone.py analyze FILE` | print a file's layout, EXIF, pointers and fingerprint score |
| `insta360_clone.py camera [ROOT]` | list detected cameras and audit their media index |
| `insta360_clone.py install FILE [ROOT]` | copy an existing file + refresh the index |
| `insta360_clone.py remove NAME [ROOT]` | delete a file from the camera and drop its records |

`--no-install` builds and verifies without touching the camera; `--yes` skips the
prompt; `--camera ROOT` pins a mount; `--offline` ignores cameras entirely.

### 5.4 Options

| Option | Default | Meaning |
|---|---|---|
| `IMAGE` / `-i` | *required* | source image (JPEG/PNG/WEBP/BMP/TIFF) |
| `OUTPUT` / `-o` | `out/` | where to write the built file |
| `--camera ROOT` | auto | pin a mount point |
| `--offline` | off | ignore cameras, use `--samples` |
| `--no-install` | off | never write to the camera |
| `--yes` / `-y` | off | no confirmation prompt |
| `--samples DIR` | `real-samples` | offline template directory (your own camera JPEGs) |
| `--template FILE` | auto | explicit metadata template |
| `--preset` | `auto` | `auto`, `37mp`, `12.5mp`, `50mp` (mode hint) |
| `--fit` | `crop` | `crop`, `pad`, `stretch` |
| `--timestamp` | newest photo + 1 min | `YYYY:MM:DD HH:MM:SS` |
| `--seq` | max used that day + 1 | DCF `_NNN` |
| `--serial` | `keep` | `keep`, `random`, or `IB` + 12 chars |
| `--uid` | `keep` | `keep`, `random`, or 32 hex |
| `--no-preview` / `--no-thumb` / `--no-exif` / `--no-trailer` | off | omit layers (debugging) |
| `--no-meta-fix` | off | keep the template's field 9 — reproduces the failure |
| `--quiet` | off | skip verification |

### 5.5 What gets copied vs regenerated

| Piece | Source |
|---|---|
| Marker layout, DQT, DHT, 4:2:0 | template (byte-identical tables) |
| EXIF: every tag, the MakerNote, Interop block | template |
| EXIF: `ImageDescription`, three date tags, `PixelX/YDimension`, thumbnail | **new image** |
| `APP2` hidden preview | **new image**, encoded with the camera's tables |
| Trailer: model, firmware, serial, UID, calibration | template |
| Trailer: raw NV21 thumbnail (704×416 / 704×544) | **new image** |
| Trailer: DCIM path, field 9 (offset), field 7 (time) | **rewritten** |
| Pixels | **new image** |

### 5.6 Verification

Every run re-parses its own output and checks 14 invariants, including:

```
[ok] first marker is APP1/EXIF (no APP0)     [ok] metadata offset (field 9) points at the blob
[ok] Make/Model name the camera              [ok] metadata timestamp (field 7) matches the name
[ok] quantisation tables match template      [ok] serial has the camera format
[ok] hidden APP2 preview present             [ok] device UID present
[ok] raw thumbnail matches the picture (luma diff 1.0)
[ok] decodes (4096x3072)
```

Also verified against `djpeg` (no warnings) and `exiv2` (tag-for-tag identical to
the template except the intended fields).

### 5.7 Identity experiments

`--serial random` / `--uid random` mint fresh identifiers in the camera's format
(`secrets`, exact length) so you can test whether the app compares them with the
paired camera:

```bash
python3 insta360_clone.py photo.jpg --serial random --no-install   # serial only
python3 insta360_clone.py photo.jpg --uid random    --no-install   # uid only
```

The serial is 14 bytes at `0x1b1` — a bare `IB[A-Z0-9]{12}` scan also matches
three decoys in the compressed data, so the tool addresses it by value or by its
`0a 0e` tag. Field 24 remains unexplained (per-file, 10–43 M, not a hash we could
match) and is left untouched.

### 5.8 Offline (no camera)

```bash
python3 insta360_clone.py photo.jpg --offline            # samples/ used as templates
python3 insta360_clone.py photo.jpg --template /path/to/a/camera/photo.jpg
```

Without a camera the timestamp is "now" (`--timestamp` overrides) and nothing is
installed.

### 5.9 When the camera mounts read-only

The usual reason a clone "does not appear" is that it was never written: udisks mounts
the exFAT volume `ro` after an unclean unplug, and the tool then only produces
`out/IMG_….jpg`. It says so up front:

```
camera     : /media/CAMERA  Insta360 GO Ultra  IBEXAMPLE00001  …  [ro]
  ! read-only mount: the file can only be written to out/
```

Fix it once, then mount it yourself:

```bash
sudo umount /media/CAMERA
sudo fsck.exfat -y /dev/sda1          # exfatprogs; /usr/bin/fsck.exfat
sudo mkdir -p /mnt/cam
sudo mount -o rw /dev/sda1 /mnt/cam
python3 insta360_clone.py photo.jpg --camera /mnt/cam --yes
```

Always **eject/unmount before unplugging** — pulling the cable while it is mounted is
what sets the dirty flag that leads to the next read-only mount.

## 6. Known (deliberate) deviations

1. **No `DRI` marker.** The camera writes `DRI = 65535` on the 8192-wide modes and
   puts `RSTn` markers in the scan. Reproducing the `DRI` without the matching `RSTn`
   markers makes libjpeg — and therefore most printer firmware — stop decoding at the
   first expected restart and fill the rest of the frame with grey. Restart markers are
   a streaming artefact, not metadata, so they are dropped. If it ever turns out the
   app insists on them, encode with
   `cjpeg -qtables tables.txt -restart 65535B -sample 2x2,1x1,1x1` instead of Pillow.
2. **Thumbnail and preview are re-encoded**, so their byte size differs from the
   camera's. Dimensions, tables and JPEG structure are identical.
3. **The 400–580 kB trailer is copied from the template**, i.e. its bulk per-frame data
   belongs to the template's photo. Only the embedded file name (and, on request, the
   serial/UID) is patched. It is
   almost certainly calibration/AI data that the printer never reads, but if the app
   ever shows the *template's* picture in a preview, use `--no-trailer`.
4. **`Make`/`Model`/`Software` are taken from the template**, not hard-coded — swap in
   a newer firmware sample and the clone follows automatically.

---

## 7. If the printer still refuses the file

Work down this list; each step is a diagnostic, not a guess.

0. **Does the camera's thumbnail show *your* picture?** If the gallery shows the
   photo you used as a template, the raw NV21 buffer was not rewritten (see §3.4b) —
   re-run the build with a current version; `analyze` now reports the bulk image too.
0b. **Did the file actually land on the camera?**
   `python3 insta360_clone.py camera` prints every connected camera, its `[ro]`/`[rw]`
   state and an audit of `fileinfo_list.list`. `[ro]` means nothing was written; every
   record must say `ok`, `STALE` means the record and the file disagree.
1. `python3 insta360_clone.py analyze out/IMG_….jpg` — `camera fingerprints` 8/8?
   Does it print `field 9 … ok`?
2. Use a photo **from that camera** as the template (`--camera /mnt/cam`, the
   default whenever one is plugged in) rather than an offline sample — the serial,
   UID and calibration then belong to the camera being asked to print.
3. Try `--preset 37mp` / `--preset 12.5mp` explicitly — maybe only one mode is
   allowed.
4. Try `--no-trailer`: if the file is accepted, the trailer was *hurting*; if it is
   rejected, the trailer is required.
5. Try `--no-preview`: isolates the hidden-`APP2` hypothesis.
6. Try `--serial random` / `--uid random` (see 5.7): tells you whether the app
   compares the file's identity with the camera it is paired to.
7. If everything fails, the check is content-based (a signature over the JPEG data,
   e.g. a hash inside the trailer). That would require the app's binary to confirm —
   check the boundaries in `AGENTS.md` first.

---

## 8. Layout

```
insta360_clone.py      entry point (also `python3 -m insta360_print`)
insta360_print/
├── jpeg.py            markers, quantisation tables, encoder, re-assembly
├── exif.py            byte-level TIFF editing
├── meta.py            trailer protobuf + camera index reader/writer
├── template.py        a real camera photo, decomposed
├── build.py           render → assemble → verify
├── camera.py          mount discovery, install, audit
└── cli.py             the four commands
out/                   generated files (git-ignored)
samples/               put your own real camera JPEGs here for --offline (ignored)
```

`AGENTS.md` contains the short operational brief for coding agents.
