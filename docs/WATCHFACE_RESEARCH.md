# Huawei GT2e Watchface Research Notes

Empirical reverse-engineering of the `.hwt` format used by Huawei/Honor smartwatches
(GT2e, GT3, Band 7, etc.) as supported by Gadgetbridge.

---

## 1. File Format (.hwt)

A `.hwt` is a ZIP archive containing:

```
com.huawei.watchface   — raw binary (proto header + image data)
description.xml        — metadata (title, author, screen size code)
preview/cover.jpg      — 960×960 JPEG preview shown in GB app
preview/aod.jpg        — 960×960 JPEG desaturated AOD preview
preview/icon_small.jpg — 390×390 JPEG thumbnail
```

---

## 2. `com.huawei.watchface` Binary Layout

```
Offset  Size  Type    Field
------  ----  ------  -----
0       2     u16le   version      (1 = older analog, 2 = newer digital)
2       2     u16le   xmllen       (bytes of protobuf section that follow header)
4       4     u32le   maplen       (bytes of FAT table: num_images × 8)
8       4     u32le   binlen       (body byte count + 8; the +8 accounts for skip8)
12      4     u32le   unknown      (observed 0x00000000)

16      xmllen bytes  protobuf     (Huawei-specific layout definition)
16+xmllen  maplen bytes  FAT table (image directory; see §3)
after FAT  8 bytes   skip8        (marker: 8× 0x55)
after skip8  binlen-8 bytes  body (concatenated images; see §4)
```

`binlen` = actual body byte count + 8 (the +8 is a constant offset, not a mistake).
When rewriting: `new_binlen = old_binlen + (new_body_size - old_body_size)`.

---

## 3. FAT Table

Each entry is 8 bytes:

```
Offset  Size  Field
------  ----  -----
0       4     u32le  off   pixel-data start within body = img_start_in_body + 8
4       4     u32le  sz    total image size including its 8-byte header
```

**Sentinel entries** have `sz == 0`; they must be left untouched (Carrera/v1 has one).

To rebuild FAT after replacing images:

```python
pos = 0
for each image blob:
    FAT[k].off = pos + 8          # pixel data starts 8 bytes in
    FAT[k].sz  = len(blob)        # total size including 8-byte header
    pos += len(blob)
```

Walk non-sentinel FAT entries in order and map them 1:1 to the scanned images.

---

## 4. Image Encoding (BGRA + RLE)

Each image starts with an 8-byte header:

```
Bytes 0-3:  magic = 0x45 0x23 0x88 0x88   (little-endian: 88882345)
Bytes 4-5:  u16le  width
Bytes 6-7:  u16le  height
Bytes 8+:   pixel data (BGRA order, RLE-compressed)
```

Pixel data RLE scheme:
- **Literal pixel**: 4 bytes `[B, G, R, A]`
- **RLE run**:
  - 4-byte escape `[0x89, 0x67, 0x45, 0x23]`
  - 4 bytes `[B, G, R, A]` color
  - 4 bytes `u32le` repeat count

---

## 5. Background Image Detection (Opacity Rule)

The key empirical finding: watch backgrounds fall into two distinct opacity profiles.

| Profile | Opacity | Raw size (454×454) | Usage |
|---------|---------|-------------------|-------|
| **Main background** | ~78% | 49–616 KB | Normal clock display |
| **AOD / overlay** | <10% | 20–62 KB | Always-on display |

The ~78% opaque value comes from the circular mask applied to all watch faces
(a 454×454 circle inscribed in a square — `π/4 ≈ 78.5%` of pixels are inside the circle).

**Rule**: images with `opaque_fraction > 0.60` are display backgrounds safe to replace with
custom photos. Images below this threshold are AOD overlays, sprite sheets, or decorative
elements — replacing them with large photos will crash or corrupt the watch display.

Verified on all surveyed templates:

| Template | Img | Size | Opacity | Safe? | Notes |
|----------|-----|------|---------|-------|-------|
| Carrera | 0 | 454×454 | 78.6% | ✓ | Works with photos |
| PixelWhite | 0 | 250×250 | 78.5% | ✓ | Thumbnail bg |
| PixelWhite | 1 | 454×454 | 78.4% | ✓ | Main bg |
| Termoe | 0 | 250×250 | 78.5% | ✓ | Thumbnail bg |
| Termoe | 50 | 454×454 | 78.3% | ✓ | Main bg |
| BlackFace | 0 | 250×250 | 78.4% | ✓ | Thumbnail bg |
| BlackFace | 13 | 454×454 | 5.7% | ✗ | AOD overlay |
| BlackFace | 23 | 210×210 | 7.1% | ✗ | Decorative |
| BlackFace | 24 | 224×224 | 9.4% | ✗ | Decorative |
| Carrera | 5 | 104×307 | 5.9% | ✗ | Analog hand mask |
| MD3 | 4 | 454×454 | 78.3% | ✓ | Main bg |

---

## 6. Template Survey

### Carrera (version=1, 6 images, analog)
- `img[0]` 454×454, 78.6% opaque — full-resolution main background
- Analog hands (images 1-5 are hand sprites/masks)
- **Status**: works perfectly with photo replacement, but shows **analog** hands only

### PixelWhite (version=2, 50 images, **digital**)
- `img[0]` 250×250, 78.5% — thumbnail
- `img[1]` 454×454, 78.4% — main background (solid white design, low raw size = heavy RLE)
- `img[2–12]` 30×30 × 11 — digit sprites (0–9 + colon?)
- `img[13–23]` 24×43 × 11 — digit sprites (larger)
- `img[24]` 109×109 — icon
- `img[25–36]` 64×24 × 12 — digit/label sprites
- `img[37–46]` 19×23 × 10 — digit sprites (small)
- `img[47–49]` thin vertical bars — separators/progress
- **Status**: ✓ **Primary digital template** — same background profile as Carrera

### Termoe (version=2, 51 images, digital)
- `img[0]` 250×250, 78.5% — thumbnail
- `img[50]` 454×454, 78.3% — main background
- Mix of digit sprites, icons, hands
- **Status**: caused watch OS freeze in testing — root cause unknown (possibly corrupt
  binary at the time). Per user request: **do not use**.

### BlackFace (version=2, 27 images, mixed)
- `img[0]` 250×250, 78.4% — thumbnail only
- `img[13]` 454×454, **5.7% opaque** — AOD background — **NOT replaceable**
- `img[23]` 210×210 and `img[24]` 224×224 — both <10% opaque — skip
- **Status**: ✗ main 454×454 background is AOD type, cannot hold photos safely

### MD3 (version=?, 5 images)
- `img[0]` 250×250, 78.5% — thumbnail
- `img[4]` 454×454, 78.3% — main background
- **Status**: untested but background profile looks replaceable

### GreenV2 (version=?, 14 images)
- `img[0]` 250×250, 78.5% — thumbnail only
- No 454×454 background found — all other images are small sprites
- **Status**: no full-resolution background to replace

---

## 6b. Digital Templates Without a Full-Screen Background

Some digital templates (e.g. **A.323**, **GreenV2**) have no 454×454 background image.
The firmware renders them on a plain black display. The img[0] in these files is only a
250×250 preview thumbnail baked with the rendered watchface — it is NOT displayed on the
watch face itself. "001" is never referenced in the protobuf, confirming it is thumbnail-only.

**How to add a custom photo background to such templates:**

1. Append a new 454×454 photo image to the end of the body (new img[N]).
2. Add a FAT entry for it (8 bytes: `off = body_end + 8`, `sz = len(new_blob)`).
3. **Insert a type-0 placement element at the START of the protobuf section** so the
   firmware renders it before all other elements (behind the digits):
   ```
   0a LL        # outer field 1, LL bytes
     08 00      # type = 0 (static image at fixed position)
     12 NN      # field 2, NN bytes
       0a 03 NNN  # image index string e.g. "015" (1-based, zero-padded)
       10 00    # x = 0
       18 00    # y = 0
   12 05 A.323  # name field (copy from existing proto elements)
   1a 07 Unnamed  # author field
   ```
4. Update header: `xmllen += len(new_element)`, `maplen += 8`, `binlen += len(new_blob)`.

**Rendering order**: proto elements are rendered in order of appearance. Inserting the
background element first ensures it renders at z=0, behind all digit sprites.
Confirmed working on GT2e with A.323 template.

**Image index convention**: images are referenced as 3-digit zero-padded 1-based strings.
img[0] = "001", img[13] = "014", new img[14] = "015", etc.

`hwt.py` handles this automatically: if the template has no image ≥ 400px, it uses the
inject path instead of the replace path.

---

## 7. Protobuf Notes

The protobuf section encodes the watchface layout (positions of hands, digits, icons).

Known string fields:
- `\x12\x05A.323` — the internal version string in Carrera (5 bytes: "A.323")
- When the output name is longer (e.g. 9 chars), this field must be expanded and `xmllen`
  updated: `new_xmllen = old_xmllen + (new_len - old_len)`

`hwt.py` handles this automatically in `_patch_proto`.

---

## 8. description.xml

Standard XML:
```xml
<?xml version="1.0" encoding="utf-8"?>
<HwTheme>
  <title>WatchfaceName</title>
  <title-cn>Name</title-cn>
  <author>Author</author>
  <designer>Designer</designer>
  <screen>HWHD02</screen>    <!-- screen size code -->
  <version>1.0.0</version>
</HwTheme>
```

Screen size codes (`HuaweiWatchfaceManager.Resolution` in Gadgetbridge source):

| Code | Resolution (H×W) | Device |
|------|-----------------|--------|
| HWHD01 | 390×390 | GT3 46mm |
| HWHD02 | 454×454 | GT2e, GT3 Pro |
| HWHD09 | 466×466 | Watch 3 |
| HWHD13 | 480×408 | — |

GT2e uses **HWHD02** (454×454).

---

## 9. Gadgetbridge Install Flow

1. `adb push file.hwt /sdcard/Android/data/nodomain.freeyourgadget.gadgetbridge/files/`
2. `adb shell am start -n nodomain.freeyourgadget.gadgetbridge/.activities.install.FileInstallerActivity -a android.intent.action.VIEW -t application/zip -d content://... --grant-read-uri-permission`
3. Tap **Install** in the GB dialog on the phone
4. In GB → Device → Watchfaces → select face → **Set active**

`hwt.py push` automates steps 1–2. Use `build --push` to build and immediately install.

---

## 10. `hwt.py` Usage

```bash
# Inspect a template
python hwt.py info  watchfaces/PixelWhite.hwt

# Extract and view all images + contact sheet
python hwt.py view  watchfaces/PixelWhite.hwt

# Build a new face from a photo (digital template)
python hwt.py build photo.jpg --template watchfaces/PixelWhite.hwt --name "MyFace" --push

# Build using default (Carrera = analog)
python hwt.py build photo.jpg --name "MyFace" --push

# Push existing .hwt
python hwt.py push  watchfaces/MyFace.hwt
```

Background replacement is automatic: images with `opacity > 60%` and size >= 100px are
replaced with the photo cropped, scaled, and masked to a circle.

---

## 11. Protobuf Element Types (Observed)

| Type | Observed usage |
|------|---------------|
| 0 | Static image placement at (x, y) — icon, decoration, or injected background |
| 1 | Animated rotating element (analog hand) |
| 3 | Meter/arc widget (step ring, progress arc) |
| 6 | Digit-sequence display (hours, minutes) — lists image indices for 0–9 |

Type 6 element inner structure (field 8):
- field 1 = x position (varint)
- field 2 = y position (varint)  
- field 3 = unknown (varies per digit position)
- field 4 = 5 (constant observed)
- field 10 = repeated string fields listing image indices for digits 0–9

---

## 12. Known Issues / Lessons Learned

- **Black screen bug**: early builds only replaced image 0 (thumbnail) but not the actual
  454×454 display background (e.g. Termoe's img50). Fixed by opacity-based detection.

- **`>= 200px` threshold too broad**: BlackFace has 210×210 and 224×224 decorative elements
  (<10% opaque) that would be wrongly replaced. Opacity check (>60%) fixes this.

- **PixelWhite is analog**: despite having digit sprites (0–9, month names), it is an
  analog face — the digits are for sub-dials (weather, date, steps). Confirmed by preview.

- **A.323 / GreenV2 are digital**: 105×154 orange digit sprites, no 454×454 background.
  Use inject path. GreenV2 is structurally identical to A.323.

- **Termoe crash**: watch froze after installing Termoe-based faces. Root cause unclear.
  Termoe img50 is 78.3% opaque (same profile as Carrera) — crash was NOT due to AOD
  buffer overflow. Per user request, Termoe is not used.

- **Carrera is analog only**: version=1 with hand sprites. Use A.323/GreenV2 for digital.

- **`binlen` off-by-8**: every known template has `binlen = body_bytes + 8`. Constant
  convention, not a parsing error. When recalculating: `new_binlen = old_binlen + delta`.

- **Inject z-order**: background element must be PREPENDED (not appended) to the proto
  section so the firmware renders it first, behind the digit sprites. Confirmed on GT2e.

- **Firmware encoded size limit**: Body image blobs are loaded into RAM. Images above
  ~686 KB encoded size crash the watch silently (black screen + reboot). Images at ~334–452 KB
  are safe. Safe ceiling in `hwt.py`: **690 KB (706,560 bytes)**. Complex landscape/portrait
  photos with high pixel variance encode large; apply Gaussian blur (r=2–30) first, then
  blur r=20 + colour quantisation (step=8–64) if still oversized. Step=8 is usually sufficient.

- **skip8 marker is not always `0x55×8`**: Carrera/A.323 use `55 55 55 55 55 55 55 55`.
  SimpleCmd/SimpleItalic use `2e 2e 2e 2e 01 00 00 01`. Do NOT search for a fixed byte
  pattern to find the marker. Instead, compute its position from the header:
  `sign_pos = 16 + xmllen + maplen`. This is deterministic and works for all templates.
