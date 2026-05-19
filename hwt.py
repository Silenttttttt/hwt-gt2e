#!/usr/bin/env python3
"""
hwt.py — Huawei GT2e (.hwt) watchface toolkit

  python hwt.py info   FILE.hwt              Print metadata and image table
  python hwt.py view   FILE.hwt [--out DIR]  Extract images + contact-sheet PNG
  python hwt.py build  IMAGE    [options]    Build a new watchface from any image
  python hwt.py push   FILE.hwt [--via ...]  Push to phone via ADB + open installer

Build options:
  --name  NAME   Display name on watch  (default: image stem)
  --out   PATH   Output .hwt            (default: watchfaces/<NAME>.hwt)
  --push         Also push after build

Template used for build: A.323.hwt  (must be in same directory as this script)
"""

import argparse, io, re, struct, subprocess, sys, zipfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# ── Paths ────────────────────────────────────────────────────────────────────
HERE         = Path(__file__).parent
TEMPLATE     = HERE / "templates" / "Carrera.hwt"   # community face — no ROM skin refs
OUT_DIR      = HERE / "watchfaces"
GB_PKG       = "nodomain.freeyourgadget.gadgetbridge"
GB_FILES     = f"/sdcard/Android/data/{GB_PKG}/files"
PROVIDER     = f"{GB_PKG}.screenshot_provider"

HH_PKG       = "com.huawei.health"
HH_DIR       = "/sdcard/Huawei/Themes"

# Orange-box regions in 250×250 image space (where hour/minute digits are displayed)
_HOUR_BOX   = (8,   15, 124, 212)   # (x1, y1, x2, y2)
_MINUTE_BOX = (125, 15, 239, 212)

# Static icon positions in 454×454 display space (decoded from A.323 protobuf)
# image index → (x, y) top-left placement
_ICON_POS = {
    11: (212, 176),
    12: (205, 394),
    13: (155,  52),
}

# ── Binary codec ─────────────────────────────────────────────────────────────

def _open_hwt(path: Path):
    """Return (proto: bytes, skip8: bytes, body: bytes, desc_xml: bytes, extras: dict)."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        raw   = z.read("com.huawei.watchface")
        desc  = z.read("description.xml") if "description.xml" in names else b""
        extras = {n: z.read(n) for n in names
                  if n not in ("com.huawei.watchface", "description.xml")}
    # Skip8 position is deterministic from the header — different templates use different
    # marker bytes (0x55×8 for Carrera/A.323, 0x2e×4+... for SimpleCmd, etc.)
    xmllen   = struct.unpack_from('<H', raw, 2)[0]
    maplen   = struct.unpack_from('<I', raw, 4)[0]
    sign_pos = 16 + xmllen + maplen
    return raw[:sign_pos], raw[sign_pos:sign_pos+8], raw[sign_pos+8:], desc, extras


def _map_images(body: bytes):
    """Return list of (hdr4, w, h, start, end) for each image in the body."""
    imgs, pos = [], 0
    while pos + 8 <= len(body):
        hdr = body[pos:pos+4]
        w   = body[pos+4] | (body[pos+5] << 8)
        h   = body[pos+6] | (body[pos+7] << 8)
        if w == 0 or h == 0 or w > 4000 or h > 4000:
            break
        start = pos
        pos  += 8
        px, needed = 0, w * h
        while px < needed:
            if pos + 4 > len(body): break
            b, g, r, a = body[pos], body[pos+1], body[pos+2], body[pos+3]
            pos += 4
            if (b, g, r, a) == (0x89, 0x67, 0x45, 0x23):
                count = struct.unpack_from('<I', body, pos+4)[0]
                pos  += 8; px += count
            else:
                px += 1
        imgs.append((hdr, w, h, start, pos))
    return imgs


def _decode_img(body: bytes, start: int, end: int, w: int, h: int) -> Image.Image:
    """Decode one BGRA+RLE image from body bytes into PIL RGBA."""
    pos, pixels, needed = start + 8, [], w * h
    while len(pixels) < needed:
        if pos + 4 > end: break
        b, g, r, a = body[pos], body[pos+1], body[pos+2], body[pos+3]
        pos += 4
        if (b, g, r, a) == (0x89, 0x67, 0x45, 0x23):
            rb, rg, rr, ra = body[pos], body[pos+1], body[pos+2], body[pos+3]
            count = struct.unpack_from('<I', body, pos+4)[0]
            pos  += 8
            pixels.extend([(rr, rg, rb, ra)] * count)
        else:
            pixels.append((r, g, b, a))
    while len(pixels) < needed:
        pixels.append((0, 0, 0, 0))
    img = Image.new("RGBA", (w, h))
    img.putdata(pixels[:needed])
    return img


def _encode_img(hdr4: bytes, img: Image.Image) -> bytes:
    """Encode PIL RGBA image to BGRA+RLE binary with 8-byte header."""
    w, h = img.size
    out  = bytearray(hdr4) + bytes([w & 0xFF, w >> 8, h & 0xFF, h >> 8])
    pxs  = list(img.getdata())
    i    = 0
    while i < len(pxs):
        r, g, b, a = pxs[i]
        run = 1
        while run < 0xFFFF and i + run < len(pxs) and pxs[i+run] == (r, g, b, a):
            run += 1
        if run >= 2:
            out += bytes([0x89, 0x67, 0x45, 0x23, b, g, r, a]) + struct.pack('<I', run)
        else:
            out += bytes([b, g, r, a])
        i += run
    return bytes(out)


def _patch_proto(proto: bytes, old_imgs: list, new_blobs: list) -> bytes:
    """
    Rebuild FAT entries for all images.
    old_imgs : list of (hdr, w, h, start, end) from _map_images on original body
    new_blobs: list of byte strings — same length as old_imgs, new encoded images
               (unchanged images = original body[start:end] slice, replaced ones = new data)
    FAT entry convention (empirically verified):
      - FAT[k].off  = image k's pixel-data start = image_body_start + 8
      - FAT[k].sz   = total image size including its 8-byte header = end - start
      - Sentinel entries (sz == 0) are left untouched
    """
    PLACEHOLDER = b'000000000'
    old_field2  = b'\x12\x05A.323'
    new_field2  = b'\x12\x09' + PLACEHOLDER

    count = proto.count(old_field2)
    proto = proto.replace(old_field2, new_field2)

    p = bytearray(proto)

    if count:
        old_xmllen = struct.unpack_from('<H', p, 2)[0]
        struct.pack_into('<H', p, 2, old_xmllen + count * 4)

    # Update total body size (binlen = body_bytes + 8 convention in all known templates)
    old_total = sum(e - s for _, _, _, s, e in old_imgs)
    new_total = sum(len(b) for b in new_blobs)
    old_binlen = struct.unpack_from('<I', p, 8)[0]
    struct.pack_into('<I', p, 8, old_binlen + (new_total - old_total))

    xmllen    = struct.unpack_from('<H', p, 2)[0]
    maplen    = struct.unpack_from('<I', p, 4)[0]
    num_fat   = maplen // 8
    table_off = 16 + xmllen

    # Compute new start position for each scanned image
    new_starts = []
    pos = 0
    for blob in new_blobs:
        new_starts.append(pos)
        pos += len(blob)

    # Walk FAT entries; map non-sentinel entries to scanned images in order
    scan_idx = 0
    for k in range(num_fat):
        base = table_off + k * 8
        sz   = struct.unpack_from('<I', p, base + 4)[0]
        if sz == 0:
            continue  # sentinel — leave untouched
        if scan_idx < len(new_blobs):
            struct.pack_into('<I', p, base,     new_starts[scan_idx] + 8)
            struct.pack_into('<I', p, base + 4, len(new_blobs[scan_idx]))
            scan_idx += 1

    return bytes(p)


def _inject_proto_bg(proto: bytes, new_img_blob: bytes, n_existing_imgs: int) -> bytes:
    """
    Append a new full-screen background image to a watchface that has no 454×454 bg.
    Inserts a type-0 placement element at the FRONT of the protobuf section so the
    firmware renders it first (behind all other elements), then appends a FAT entry.
    Returns the updated proto bytes (header + proto section + FAT) — does NOT include
    the body; caller must append (original_body + new_img_blob) after skip8.
    """
    p = bytearray(proto)
    xmllen    = struct.unpack_from('<H', p, 2)[0]
    maplen    = struct.unpack_from('<I', p, 4)[0]
    binlen    = struct.unpack_from('<I', p, 8)[0]
    table_off = 16 + xmllen

    # Image ID in proto is 1-based, zero-padded to 3 digits
    img_id = f'{n_existing_imgs + 1:03d}'.encode('ascii')

    # Build proto element: outer field-1 message containing type=0 static-image-at-(0,0)
    field2_inner = bytes([0x0a, len(img_id)]) + img_id + bytes([0x10, 0x00, 0x18, 0x00])
    inner        = bytes([0x08, 0x00, 0x12, len(field2_inner)]) + field2_inner
    element_body = bytes([0x0a, len(inner)]) + inner

    # Grab name/author from existing proto (look for first occurrence)
    proto_sect  = bytes(p[16:16 + xmllen])
    name_pos    = proto_sect.find(b'\x12\x05A.323')
    name_tag    = b'\x12\x05A.323'    if name_pos >= 0 else b'\x12\x05A.323'
    author_tag  = b'\x1a\x07Unnamed'

    new_element = element_body + name_tag + author_tag

    # Compute current body end from FAT entries (to place new image immediately after)
    body_end = 0
    for k in range(maplen // 8):
        base = table_off + k * 8
        sz   = struct.unpack_from('<I', p, base + 4)[0]
        off  = struct.unpack_from('<I', p, base)[0]
        if sz > 0:
            body_end = max(body_end, off - 8 + sz)

    new_fat_entry = struct.pack('<II', body_end + 8, len(new_img_blob))

    # Update header fields
    struct.pack_into('<H', p, 2, xmllen + len(new_element))   # xmllen
    struct.pack_into('<I', p, 4, maplen + 8)                  # maplen
    struct.pack_into('<I', p, 8, binlen + len(new_img_blob))  # binlen

    # Reconstruct: header | new_element | old_proto_section | old_FAT | new_FAT_entry
    return (bytes(p[:16]) +
            new_element +
            proto_sect +
            bytes(p[table_off:table_off + maplen]) +
            new_fat_entry)


# Firmware memory limit for encoded background blobs (empirically: 686KB works,
# 852KB crashes). Use 690KB as safe ceiling.
MAX_BG_ENCODED_BYTES = 706_560   # 690 * 1024


def _snap_colors(img: Image.Image, step: int) -> Image.Image:
    """Round each RGB channel to the nearest multiple of step (preserves alpha)."""
    half = step // 2
    pxs = list(img.getdata())
    snapped = [
        (min(255, ((r + half) // step) * step),
         min(255, ((g + half) // step) * step),
         min(255, ((b + half) // step) * step),
         a)
        for r, g, b, a in pxs
    ]
    out = Image.new("RGBA", img.size)
    out.putdata(snapped)
    return out


def _encode_img_capped(hdr4: bytes, img: Image.Image, max_bytes: int = MAX_BG_ENCODED_BYTES) -> bytes:
    """
    Encode image; if result exceeds max_bytes, apply progressively stronger compression
    while preserving sharpness as long as possible:
      1. Color snapping only (rounds channels to nearest multiple of step — no blur)
      2. JPEG de-noise q=75 + color snapping
      3. JPEG q=50 + color snapping
      4. Light blur (last resort, r=2..8)
    """
    from io import BytesIO
    from PIL import ImageFilter

    blob = _encode_img(hdr4, img)
    if len(blob) <= max_bytes:
        return blob

    # Phase 1: color snap only — sharpness fully preserved
    for step in (2, 4, 6, 8, 12, 16):
        snapped = _snap_colors(img, step)
        snapped = _apply_circle_mask(snapped, img.size[0])
        blob = _encode_img(hdr4, snapped)
        if len(blob) <= max_bytes:
            print(f"  Applied snap step={step} → {len(blob):,}B")
            return blob

    # Phase 2: JPEG q=75 de-noise + color snap (kills photo noise, keeps edges)
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=75)
    buf.seek(0)
    from PIL import Image as _Image
    jbase75 = _Image.open(buf).convert("RGBA")
    jbase75 = _apply_circle_mask(jbase75, img.size[0])
    for step in (4, 6, 8, 12, 16, 24):
        snapped = _snap_colors(jbase75, step)
        snapped = _apply_circle_mask(snapped, img.size[0])
        blob = _encode_img(hdr4, snapped)
        if len(blob) <= max_bytes:
            print(f"  Applied JPEG q=75 + snap step={step} → {len(blob):,}B")
            return blob

    # Phase 3: JPEG q=50 + color snap
    buf = BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=50)
    buf.seek(0)
    jbase50 = _Image.open(buf).convert("RGBA")
    jbase50 = _apply_circle_mask(jbase50, img.size[0])
    for step in (4, 8, 12, 16, 24):
        snapped = _snap_colors(jbase50, step)
        snapped = _apply_circle_mask(snapped, img.size[0])
        blob = _encode_img(hdr4, snapped)
        if len(blob) <= max_bytes:
            print(f"  Applied JPEG q=50 + snap step={step} → {len(blob):,}B")
            return blob

    # Phase 4: light blur (last resort)
    for radius in range(2, 9, 2):
        blurred = jbase50.filter(ImageFilter.GaussianBlur(radius=radius))
        blurred = _apply_circle_mask(blurred.convert("RGBA"), img.size[0])
        blob = _encode_img(hdr4, blurred)
        if len(blob) <= max_bytes:
            print(f"  Applied JPEG q=50 + blur r={radius} → {len(blob):,}B")
            return blob

    print(f"  WARNING: still {len(blob):,}B after max compression — may crash")
    return blob


# ── Image helpers ─────────────────────────────────────────────────────────────

def _prepare_bg(src: Path, size: int) -> Image.Image:
    """Square-crop (top-biased), resize, apply anti-aliased circular alpha mask."""
    img  = Image.open(src).convert("RGBA")
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top  = max(0, (h - side) // 3)
    img  = img.crop((left, top, left+side, top+side)).resize((size, size), Image.LANCZOS)
    return _apply_circle_mask(img, size)


def _apply_circle_mask(img: Image.Image, size: int) -> Image.Image:
    big  = size * 4
    mask_big = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask_big).ellipse((0, 0, big-1, big-1), fill=255)
    img.putalpha(mask_big.resize((size, size), Image.LANCZOS))
    return img


def _posterize_bg(img: Image.Image, blur: int = 10, step: int = 64) -> Image.Image:
    """
    Blur then snap each RGB channel to multiples of step.
    Re-applies a clean circular mask after blurring (blur bleeds the alpha channel).
    Produces an image that RLE-compresses to ~38KB — within the watch firmware's
    ~40KB budget for image 0.
    """
    from PIL import ImageFilter
    blurred = img.filter(ImageFilter.GaussianBlur(radius=blur))
    px = list(blurred.getdata())
    snapped = [(min(255, (r // step) * step),
                min(255, (g // step) * step),
                min(255, (b // step) * step),
                a) for r, g, b, a in px]
    out = Image.new("RGBA", img.size)
    out.putdata(snapped)
    return _apply_circle_mask(out, img.size[0])


def _make_preview_images(src: Path, size: int = 250):
    """Return (cover_960, aod_960, icon_390) as JPEG bytes."""
    img  = Image.open(src).convert("RGB")
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top  = max(0, (h - side) // 3)
    sq   = img.crop((left, top, left+side, top+side))

    def _to_jpeg(pil, sz):
        pil = pil.resize((sz, sz), Image.LANCZOS)
        buf = io.BytesIO()
        pil.save(buf, "JPEG", quality=90)
        return buf.getvalue()

    # AOD: desaturate + darken
    gray = sq.convert("L").convert("RGB")
    aod  = Image.new("RGB", sq.size)
    for i, (a, b) in enumerate(zip(sq.getdata(), gray.getdata())):
        r = (a[0] * 30 + b[0] * 70) // 100
        g = (a[1] * 30 + b[1] * 70) // 100
        b_ = (a[2] * 30 + b[2] * 70) // 100
        aod.putpixel((i % sq.width, i // sq.width), (r, g, b_))

    return _to_jpeg(sq, 960), _to_jpeg(aod, 960), _to_jpeg(sq, 390)


# ── Watchface renderer ───────────────────────────────────────────────────────

def _render_watchface(imgs_pil: list, sample_time: str = "12:34") -> Image.Image:
    """
    Render a simulated 454×454 watchface showing sample_time.
    Uses the actual digit sprites from images 1-10 placed in the detected
    orange-box regions (hour box left, minute box right).
    """
    DISP  = 454
    IM_SZ = 250
    SC    = DISP / IM_SZ

    canvas = Image.new("RGBA", (DISP, DISP), (0, 0, 0, 255))

    # Scale background (image 0) to full display size
    bg = imgs_pil[0].convert("RGBA").resize((DISP, DISP), Image.LANCZOS)
    canvas.alpha_composite(bg)

    # Parse sample time
    t = sample_time.replace(":", "")
    if len(t) != 4 or not t.isdigit():
        t = "1234"
    d = [int(c) for c in t]   # [H_tens, H_units, M_tens, M_units]

    # Scale orange-box coords to display space
    def _sc(v): return int(v * SC)
    hx1, hy1, hx2, hy2 = _sc(_HOUR_BOX[0]),   _sc(_HOUR_BOX[1]),   _sc(_HOUR_BOX[2]),   _sc(_HOUR_BOX[3])
    mx1, my1, mx2, my2 = _sc(_MINUTE_BOX[0]), _sc(_MINUTE_BOX[1]), _sc(_MINUTE_BOX[2]), _sc(_MINUTE_BOX[3])

    def _place_pair(c, d1, d2, bx1, by1, bx2, by2):
        bw, bh = bx2 - bx1, by2 - by1
        dw = bw // 2
        for col, dig in enumerate((d1, d2)):
            sp   = imgs_pil[1 + dig].convert("RGBA") if 1 + dig < len(imgs_pil) else None
            if sp is None:
                continue
            sw, sh = sp.size
            scale  = min(dw / sw, bh / sh)
            nsw, nsh = max(1, int(sw * scale)), max(1, int(sh * scale))
            sp     = sp.resize((nsw, nsh), Image.LANCZOS)
            ox     = bx1 + col * dw + (dw - nsw) // 2
            oy     = by1 + (bh - nsh) // 2
            c.alpha_composite(sp, (ox, oy))

    _place_pair(canvas, d[0], d[1], hx1, hy1, hx2, hy2)
    _place_pair(canvas, d[2], d[3], mx1, my1, mx2, my2)

    # Static icons at known positions
    for idx, (px, py) in _ICON_POS.items():
        if idx < len(imgs_pil):
            canvas.alpha_composite(imgs_pil[idx].convert("RGBA"), (px, py))

    # Anti-aliased circular mask
    big = DISP * 4
    mask_big = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask_big).ellipse((0, 0, big - 1, big - 1), fill=255)
    canvas.putalpha(mask_big.resize((DISP, DISP), Image.LANCZOS))

    # Composite onto opaque black so PNG looks right
    out = Image.new("RGBA", (DISP, DISP), (0, 0, 0, 255))
    out.alpha_composite(canvas)
    return out


def _composite_bg(template_img0: Image.Image, src_bg: Image.Image) -> Image.Image:
    """
    Build a composite background: src_bg as the base, template's orange digit-box
    pixels overlaid on top.  This preserves the orange regions the firmware uses for
    digit display while showing src_bg everywhere else.
    """
    size = src_bg.size
    t    = template_img0.convert("RGBA")
    if t.size != size:
        t = t.resize(size, Image.NEAREST)
    result  = list(src_bg.convert("RGBA").getdata())
    t_data  = list(t.getdata())
    for i, (tr, tg, tb, ta) in enumerate(t_data):
        # Orange: high red, moderate green, low blue, well above transparent
        if ta > 128 and tr > 140 and tg > 50 and tb < 80 and tr > tg + 40:
            result[i] = (tr, tg, tb, ta)
    out = Image.new("RGBA", size)
    out.putdata(result)
    return out


# ── contact-sheet visualizer ──────────────────────────────────────────────────

def _contact_sheet(imgs_pil: list, labels: list, metadata: str) -> Image.Image:
    """
    Render a contact sheet PNG:
      Left column : image 0 (background) large, with metadata text
      Right grid  : all other images, scaled to thumb_h = 120px
    """
    THUMB    = 120
    MARGIN   = 8
    BG_COL   = (20, 20, 20)
    TEXT_COL = (220, 220, 220)
    LABEL_COL= (150, 220, 255)

    try:
        font  = ImageFont.truetype("/usr/share/fonts/TTF/DejaVuSansMono.ttf", 11)
        font_s= ImageFont.truetype("/usr/share/fonts/TTF/DejaVuSansMono.ttf", 9)
    except Exception:
        font  = ImageFont.load_default()
        font_s= font

    # Background image (img 0) preview
    bg_size = 250
    bg_img  = imgs_pil[0].copy()
    # Composite over black so transparent corners show
    bg_bg   = Image.new("RGBA", (bg_size, bg_size), (0, 0, 0, 255))
    bg_bg.alpha_composite(bg_img.resize((bg_size, bg_size), Image.LANCZOS))
    bg_rgb  = bg_bg.convert("RGB")

    # Metadata text block
    meta_lines = metadata.split("\n")
    meta_h     = len(meta_lines) * 14 + MARGIN * 2
    left_w     = bg_size + MARGIN * 2
    left_h     = max(bg_size + MARGIN * 2, meta_h + MARGIN)

    # Sprite thumbnails grid (images 1..N)
    sprites    = imgs_pil[1:]
    sprite_lbl = labels[1:]
    cols       = 5
    rows       = (len(sprites) + cols - 1) // cols
    cell_w     = THUMB + MARGIN
    cell_h     = THUMB + MARGIN + 12
    grid_w     = cols * cell_w + MARGIN
    grid_h     = rows * cell_h + MARGIN

    total_w    = left_w + grid_w
    total_h    = max(left_h, grid_h) + meta_h + MARGIN * 2

    sheet = Image.new("RGB", (total_w, total_h), BG_COL)
    draw  = ImageDraw.Draw(sheet)

    # Paste background image
    sheet.paste(bg_rgb, (MARGIN, MARGIN))
    draw.rectangle([MARGIN-1, MARGIN-1, MARGIN+bg_size, MARGIN+bg_size],
                   outline=(80, 80, 80))
    draw.text((MARGIN, MARGIN + bg_size + 2), labels[0], fill=LABEL_COL, font=font_s)

    # Metadata text below background image
    ty = MARGIN + bg_size + 16
    for line in meta_lines:
        draw.text((MARGIN, ty), line, fill=TEXT_COL, font=font_s)
        ty += 13

    # Sprite grid
    gx0 = left_w
    for idx, (sp, lbl) in enumerate(zip(sprites, sprite_lbl)):
        col = idx % cols
        row = idx // cols
        x   = gx0 + MARGIN + col * cell_w
        y   = MARGIN + row * cell_h

        # Scale sprite to fit THUMB, keeping aspect
        sw, sh = sp.size
        scale  = THUMB / max(sw, sh)
        tsz    = (max(1, int(sw * scale)), max(1, int(sh * scale)))
        sp_rgb = Image.new("RGB", (THUMB, THUMB), (30, 30, 30))
        thumb  = sp.convert("RGBA").resize(tsz, Image.LANCZOS)
        # composite
        tmp = Image.new("RGBA", (THUMB, THUMB), (30, 30, 30, 255))
        ox  = (THUMB - tsz[0]) // 2
        oy  = (THUMB - tsz[1]) // 2
        tmp.alpha_composite(thumb, (ox, oy))
        sp_rgb = tmp.convert("RGB")

        sheet.paste(sp_rgb, (x, y))
        draw.rectangle([x-1, y-1, x+THUMB, y+THUMB], outline=(60, 60, 60))
        draw.text((x, y + THUMB + 1), lbl[:14], fill=LABEL_COL, font=font_s)

    return sheet


# ── CLI commands ──────────────────────────────────────────────────────────────

def cmd_info(args):
    path = Path(args.file)
    proto, skip, body, desc_xml, extras = _open_hwt(path)
    imgs = _map_images(body)

    # Parse title from desc_xml
    title = re.search(rb"<title>([^<]+)</title>", desc_xml)
    title = title.group(1).decode() if title else "?"
    screen = re.search(rb"<screen>([^<]+)</screen>", desc_xml)
    screen = screen.group(1).decode() if screen else "?"
    author = re.search(rb"<author>([^<]+)</author>", desc_xml)
    author = author.group(1).decode() if author else "?"
    version = re.search(rb"<version>([^<]+)</version>", desc_xml)
    version = version.group(1).decode() if version else "?"

    # Proto title
    pm = proto.find(b'\x12\x05')
    proto_title = proto[pm+2:pm+7].decode(errors='replace') if pm >= 0 else "?"

    print(f"File    : {path}")
    print(f"Title   : {title}  (proto: '{proto_title}')")
    print(f"Author  : {author}  v{version}")
    print(f"Screen  : {screen}")
    print(f"Proto   : {len(proto)} bytes  (sign at {len(proto)})")
    print(f"Images  : {len(imgs)}")
    print(f"Extras  : {list(extras.keys()) if extras else 'none'}")
    print()
    print(f"{'#':>3}  {'W':>5}  {'H':>5}  {'Raw bytes':>10}  {'Uncomp':>10}  {'Ratio':>6}")
    for i, (hdr, w, h, s, e) in enumerate(imgs):
        raw  = e - s
        full = 8 + w * h * 4
        print(f"{i:>3}  {w:>5}  {h:>5}  {raw:>10,}  {full:>10,}  {raw/full:>6.2f}  hdr={hdr.hex()}")


def cmd_view(args):
    path    = Path(args.file)
    out_dir = Path(args.out) if args.out else path.parent / (path.stem + "_view")
    out_dir.mkdir(parents=True, exist_ok=True)

    proto, skip, body, desc_xml, extras = _open_hwt(path)
    imgs = _map_images(body)

    title = re.search(rb"<title>([^<]+)</title>", desc_xml)
    title = title.group(1).decode() if title else path.stem
    screen = re.search(rb"<screen>([^<]+)</screen>", desc_xml)
    screen = screen.group(1).decode() if screen else "?"

    print(f"Extracting {len(imgs)} images → {out_dir}/")
    decoded = []
    labels  = []
    for i, (hdr, w, h, s, e) in enumerate(imgs):
        img = _decode_img(body, s, e, w, h)
        out = out_dir / f"{i:02d}_{w}x{h}.png"
        img.save(out)
        decoded.append(img)
        labels.append(f"[{i}] {w}×{h}")
        print(f"  [{i:2d}] {w}×{h}  {e-s:,}B → {out.name}")

    # Also save preview images from extras
    for name, data in extras.items():
        if name.startswith("preview/") and name.endswith((".jpg", ".png")):
            dest = out_dir / name.replace("/", "_")
            dest.write_bytes(data)
            print(f"  preview → {dest.name}")

    # Build metadata string for contact sheet
    meta = (
        f"Title   : {title}\n"
        f"Screen  : {screen}\n"
        f"Images  : {len(imgs)}\n"
        f"Proto   : {len(proto)}B\n"
        f"Has AOD : {'preview/aod.jpg' in extras}"
    )

    sheet = _contact_sheet(decoded, labels, meta)
    sheet_path = out_dir / "_contact_sheet.png"
    sheet.save(sheet_path)
    print(f"Contact sheet → {sheet_path}")

    # Watchface render
    wf = _render_watchface(decoded, args.time if hasattr(args, "time") else "12:34")
    wf_path = out_dir / "_watchface.png"
    wf.save(wf_path)
    print(f"Watchface render → {wf_path}")


def _img_opacity(body: bytes, s: int, e: int, w: int, h: int) -> float:
    """Return fraction of pixels with alpha > 128 (0.0 – 1.0)."""
    pxs = list(_decode_img(body, s, e, w, h).getdata())
    if not pxs:
        return 0.0
    return sum(1 for _, _, _, a in pxs if a > 128) / len(pxs)


def cmd_build(args):
    src     = Path(args.image)
    if not src.exists():
        sys.exit(f"Image not found: {src}")
    name    = args.name or src.stem
    out_dir = OUT_DIR
    out_dir.mkdir(exist_ok=True)
    out_path = Path(args.out) if args.out else out_dir / f"{name}.hwt"

    # Load template
    tpl_path = Path(args.template) if getattr(args, "template", None) else TEMPLATE
    proto, skip, body, desc_xml, _ = _open_hwt(tpl_path)
    imgs    = _map_images(body)

    # Replace only genuine display backgrounds: images >= 100px wide whose pixels are
    # mostly opaque (>60%).  ~78% opaque = circular watch-face crop = main/thumbnail bg.
    # AOD overlays, digit sprites, decorative elements are all <15% opaque and are left
    # untouched.  This correctly handles:
    #   Carrera   — img0 (454×454, 78.6% opaque) → replace
    #   PixelWhite — img0 (250×250, 78.5%) + img1 (454×454, 78.4%) → both replace
    #   BlackFace  — img13 (454×454, 5.7%) + img23/24 (~8%) → skip
    BG_OPACITY_THRESHOLD = 0.60
    BG_MIN_SIZE          = 100

    # Detect whether this template already has a full-size (≥400px) display background.
    # Templates like A.323 have no such image — they render on a black framebuffer.
    # Templates like Carrera/PixelWhite have a 454×454 image that fills the display.
    has_fullsize_bg = any(w >= 400 and h >= 400 for _, w, h, _, _ in imgs)

    if has_fullsize_bg:
        # ── Standard path: replace high-opacity backgrounds in-place ────────────
        new_blobs = []
        for i, (hdr, w, h, s, e) in enumerate(imgs):
            if w >= BG_MIN_SIZE and h >= BG_MIN_SIZE:
                opacity = _img_opacity(body, s, e, w, h)
                is_bg   = opacity > BG_OPACITY_THRESHOLD
            else:
                opacity = None
                is_bg   = False

            if is_bg:
                print(f"Replacing bg[{i}] ({w}×{h}, {opacity*100:.0f}% opaque)…")
                bg = _prepare_bg(src, w)
                if getattr(args, "composite", False):
                    print(f"  Composite mode: overlaying template orange boxes…")
                    template_img = _decode_img(body, s, e, w, h)
                    bg = _composite_bg(template_img, bg)
                if getattr(args, "posterize", False):
                    print(f"  Posterizing…")
                    bg = _posterize_bg(bg)
                new_blob = _encode_img_capped(hdr, bg)
                print(f"  {e-s:,}B → {len(new_blob):,}B  (Δ{len(new_blob)-(e-s):+,})")
            else:
                new_blob = body[s:e]
            new_blobs.append(new_blob)

        new_proto = _patch_proto(proto, imgs, new_blobs)
        new_body  = b''.join(new_blobs)

    else:
        # ── Inject path: digital templates with no full-size background ──────────
        # Keep all existing images (digit sprites etc.) unchanged.
        # Inject a new 454×454 background image and a proto element to display it first.
        print(f"No full-size background in template — injecting 454×454 background…")
        hdr0 = imgs[0][0]  # reuse image magic header from img[0]
        new_bg = _prepare_bg(src, 454)
        if getattr(args, "posterize", False):
            new_bg = _posterize_bg(new_bg)
        new_bg_blob = _encode_img_capped(hdr0, new_bg)
        print(f"  Injected bg: {len(new_bg_blob):,}B")

        new_proto = _inject_proto_bg(proto, new_bg_blob, len(imgs))
        new_body  = body + new_bg_blob  # append new image to original body

    new_raw = new_proto + skip + new_body

    # Updated description.xml
    new_desc = re.sub(rb"<title>[^<]*</title>",
                      f"<title>{name}</title>".encode(), desc_xml)
    new_desc = re.sub(rb"<title-cn>[^<]*</title-cn>",
                      f"<title-cn>{name[:7]}</title-cn>".encode(), new_desc)

    # Generate preview images
    print("Generating preview images…")
    cover_jpg, aod_jpg, icon_jpg = _make_preview_images(src, args.size)

    # Write .hwt
    with zipfile.ZipFile(out_path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr("com.huawei.watchface", new_raw)
        z.writestr("description.xml",      new_desc)
        z.writestr("preview/cover.jpg",    cover_jpg)
        z.writestr("preview/aod.jpg",      aod_jpg)
        z.writestr("preview/icon_small.jpg", icon_jpg)

    print(f"Written : {out_path}  ({out_path.stat().st_size:,} bytes)")

    if args.push:
        if getattr(args, "via", "gadgetbridge") == "huawei-health":
            cmd_push_path_hh(out_path)
        else:
            cmd_push_path(out_path)

    return out_path


def cmd_push(args):
    via = getattr(args, "via", "gadgetbridge")
    if via == "huawei-health":
        cmd_push_path_hh(Path(args.file))
    else:
        cmd_push_path(Path(args.file))


def cmd_push_path_hh(hwt_path: Path):
    name   = hwt_path.name
    remote = f"{HH_DIR}/{name}"

    print(f"Pushing {name} → {remote}")
    subprocess.run(["adb", "push", str(hwt_path), remote], check=True)

    print("Opening Huawei Health…")
    subprocess.run([
        "adb", "shell", "monkey", "-p", HH_PKG, "-c",
        "android.intent.category.LAUNCHER", "1",
    ], check=True)

    print()
    print("On the phone: Watchfaces → Mine → ADD WATCH FACES → select the file from")
    print(f"  Huawei/Themes/{name}")


def cmd_push_path(hwt_path: Path):
    name    = hwt_path.name
    remote  = f"{GB_FILES}/{name}"
    content = (f"content://{PROVIDER}/external_files"
               f"/Android/data/{GB_PKG}/files/{name}")

    print(f"Pushing {name} → {remote}")
    subprocess.run(["adb", "push", str(hwt_path), remote], check=True)

    print("Opening Gadgetbridge installer…")
    subprocess.run([
        "adb", "shell", "am", "start",
        "-n", f"{GB_PKG}/.activities.install.FileInstallerActivity",
        "-a", "android.intent.action.VIEW",
        "-t", "application/zip",
        "-d", content,
        "--grant-read-uri-permission",
    ], check=True)
    print("Installer open — tap Install on the phone.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    p  = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)

    # info
    pi = sp.add_parser("info", help="Print metadata and image table")
    pi.add_argument("file")

    # view
    pv = sp.add_parser("view", help="Extract images + contact-sheet PNG")
    pv.add_argument("file")
    pv.add_argument("--out",  "-o", help="Output directory")
    pv.add_argument("--time", "-t", default="12:34",
                    help="Sample time to display in watchface render (default: 12:34)")

    # build
    pb = sp.add_parser("build", help="Build watchface from an image")
    pb.add_argument("image")
    pb.add_argument("--name",  "-n", help="Watchface name (default: image stem)")
    pb.add_argument("--template", "-T", help="Template .hwt file (default: Carrera.hwt)")
    pb.add_argument("--size",  "-s", type=int, default=454,
                    help="Background diameter px (default 454 for Carrera template)")
    pb.add_argument("--out",   "-o", help="Output .hwt path")
    pb.add_argument("--push",  "-p", action="store_true",
                    help="Push to phone after build")
    pb.add_argument("--via", choices=["gadgetbridge", "huawei-health"],
                    default="gadgetbridge",
                    help="Install target when using --push (default: gadgetbridge)")
    pb.add_argument("--posterize", action="store_true",
                    help="Posterize background (lossy flat colours, smaller file)")
    pb.add_argument("--composite", "-c", action="store_true",
                    help="Composite mode: overlay template orange digit-boxes on top of source image "
                         "(recommended when source image is large — keeps image 0 near original size)")

    # push
    pp = sp.add_parser("push", help="Push existing .hwt to phone via ADB")
    pp.add_argument("file")
    pp.add_argument("--via", choices=["gadgetbridge", "huawei-health"],
                    default="gadgetbridge",
                    help="Install target (default: gadgetbridge)")

    args = p.parse_args()
    {"info": cmd_info, "view": cmd_view,
     "build": cmd_build, "push": cmd_push}[args.cmd](args)


if __name__ == "__main__":
    main()
