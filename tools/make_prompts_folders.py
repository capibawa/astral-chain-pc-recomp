"""Build the Xbox and PlayStation button-prompts RomFS overlays from the game's own UI archive.

The game draws every button prompt from one atlas: texture 0x79c096fa of core.wtp inside each ui_core*.dtt of
ui/ui.pkz, 256 x 512 RGBA8 in Tegra block-linear layout (block height 16 GOBs), the same bytes in every language.
Its icons sit in cells of 56 x 44 pixels (row, column below). This tool reads that atlas from the extracted RomFS,
redraws the cells of the buttons another controller names differently with tools/prompt_glyphs.py, and writes four
folders next to romfs-index.bin, each a repacked ui/ui.pkz plus its manifest.tsv (runtime/romfs.c overlay_init,
runtime/input_map.c input_prompts_folder):

    romfs-patches-xbox                 Xbox prompts for pad_layout = nintendo
    romfs-patches-xbox-label           Xbox prompts for pad_layout = xbox or auto
    romfs-patches-playstation          PlayStation prompts for pad_layout = nintendo
    romfs-patches-playstation-label    PlayStation prompts for pad_layout = xbox or auto

As in TotK Recomp (tools/make_romfs_patches.py --prompts), each prompt names the button the pad layout presses.
The Nintendo layout plays a pad by position: Switch A is the east button, labelled B on an Xbox pad and circle on a
PlayStation pad, so the face icons follow the positions. The Xbox layout puts each Switch letter on the button
labelled with it (cross for A, circle for B, square for X, triangle for Y on a PlayStation pad), and the position
icons (four dots with one lit) move the lit dot to that button. Both variants turn L, R, ZL, ZR into LB, RB, LT, RT
(L1, R1, L2, R2) and Plus, Minus into Menu, View (Options, Create). Everything else in the archive is copied as is.

Nothing of the game is stored with the port: the setup runs this on the player's own extracted RomFS
(installer/steps.py, Compile), and a source build runs it by hand:

    .venv\\Scripts\\python tools\\make_prompts_folders.py --out build\\v101

--preview DIR also writes each variant's atlas as a PNG there (keep DIR under build/: it is game-derived).
"""

import argparse
import hashlib
import json
import shutil
import struct
import sys
import zlib
from pathlib import Path

import numpy as np

try:
    import zstandard
except ImportError:
    sys.exit("make_prompts_folders.py needs the zstandard package (.venv has it)")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prompt_glyphs as G  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PKZ_PATH = "ui/ui.pkz"

ATLAS_ID = 0x79c096fa
ATLAS_SHA256 = "f5d91738e94427d1b4db0204d8aa7a37f1a5dbdc49207c328c178409e510dc2d"   # v1.0.1, every language
ATLAS_W, ATLAS_H, ATLAS_FMT, ATLAS_BLOCK_LOG2 = 256, 512, 0x25, 4
CELL_W, CELL_H = 56, 44

# The atlas cells (row, column) this tool changes.
CELL = {
    "minus": (2, 1), "B": (2, 2), "A": (2, 3), "Y": (3, 0), "X": (3, 1), "R": (3, 2), "ZL": (3, 3),
    "ZR": (4, 0), "plus": (4, 1), "L": (5, 1),
    # the face buttons' position icons: four dots, the lit one north, south, west or east
    "dot_n": (8, 1), "dot_s": (8, 2), "dot_w": (8, 3), "dot_e": (9, 0),
}

FOLDERS = {   # folder: (controller, label layout)
    "romfs-patches-xbox": ("xbox", False),
    "romfs-patches-xbox-label": ("xbox", True),
    "romfs-patches-playstation": ("playstation", False),
    "romfs-patches-playstation-label": ("playstation", True),
}


# ---- archives
def pkz_entries(data):
    """(name, size, offset, zsize) of every entry of a pkz archive (magic b"pkzl"), in order."""
    if data[:4] != b"pkzl":
        raise ValueError("not a pkz archive")
    items = struct.unpack_from("<I", data, 0x10)[0]
    entry_off = struct.unpack_from("<I", data, 0x14)[0]
    name_table = entry_off + items * 0x20
    for i in range(items):
        p = entry_off + i * 0x20
        name_off, _, size, _, offset, _, zsize, _ = struct.unpack_from("<8I", data, p)
        end = data.index(0, name_table + name_off)
        yield data[name_table + name_off:end].decode("utf-8", "replace"), size, offset, zsize


def repack_pkz(orig, swaps):
    """orig (bytes of ui.pkz) with the entries named in swaps (name -> bytes) replaced. Returns the new archive."""
    items = struct.unpack_from("<I", orig, 0x10)[0]
    entry_off = struct.unpack_from("<I", orig, 0x14)[0]
    name_size = struct.unpack_from("<I", orig, 0x18)[0]
    name_table = entry_off + items * 0x20
    data_start = name_table + name_size
    out = bytearray(orig[:entry_off])
    table_at = len(out)
    out += b"\0" * (items * 0x20)
    out += orig[name_table:name_table + name_size]
    assert len(out) == data_start, "pkz layout: the data starts right after the name table"
    comp = zstandard.ZstdCompressor()
    for i, (name, size, offset, zsize) in enumerate(pkz_entries(orig)):
        if name in swaps:
            raw = swaps[name]
            z = comp.compress(raw)
            if zstandard.ZstdDecompressor().decompress(z) != raw:
                sys.exit(f"repacked {name} does not round-trip")
            size = len(raw)
        else:
            z = orig[offset:offset + zsize]
        first = struct.unpack_from("<I", orig, entry_off + i * 0x20)[0]
        struct.pack_into("<8I", out, table_at + i * 0x20, first, 0, size, 0, data_start, 0, len(z), 0)
        out += z
        data_start += len(z)
    struct.pack_into("<I", out, 0x08, len(out))   # header's total size
    return bytes(out)


def pkz_read(pkz, name, size, offset, zsize):
    return zstandard.ZstdDecompressor().decompress(pkz[offset:offset + zsize], max_output_size=size)


def dat_files(data):
    """{name: (offset, size)} of a PlatinumGames DAT / DTT archive."""
    magic, n, files_at, _, names_at, sizes_at = struct.unpack_from("<4sIIIII", data, 0)
    if magic != b"DAT\0":
        raise ValueError("not a DAT archive")
    offs = struct.unpack_from(f"<{n}I", data, files_at)
    sizes = struct.unpack_from(f"<{n}I", data, sizes_at)
    stride = struct.unpack_from("<I", data, names_at)[0]
    names = [data[names_at + 4 + i * stride:names_at + 4 + (i + 1) * stride].split(b"\0")[0].decode()
             for i in range(n)]
    return {names[i]: (offs[i], sizes[i]) for i in range(n)}


def atlas_place(wta):
    """(offset in core.wtp, size) of the prompts atlas from core.wta, after checking its format."""
    magic, _, n, offs_at, sizes_at, _, ids_at, info_at = struct.unpack_from("<4sIIIIIII", wta, 0)
    if magic != b"WTB\0":
        raise ValueError("core.wta is not a WTB header")
    ids = struct.unpack_from(f"<{n}I", wta, ids_at)
    if ATLAS_ID not in ids:
        raise ValueError(f"core.wta has no texture {ATLAS_ID:#x}")
    i = ids.index(ATLAS_ID)
    off = struct.unpack_from("<I", wta, offs_at + 4 * i)[0]
    size = struct.unpack_from("<I", wta, sizes_at + 4 * i)[0]
    _, _, _, _, mips, _, fmt, w, h, depth, _, block, _ = struct.unpack_from("<4sIQIIIIIIIIII", wta, info_at + i * 0x38)
    if (fmt, w, h, depth, mips, block) != (ATLAS_FMT, ATLAS_W, ATLAS_H, 1, 1, ATLAS_BLOCK_LOG2) or size != w * h * 4:
        raise ValueError(f"texture {ATLAS_ID:#x} is format {fmt:#x} {w}x{h}x{depth}, {mips} levels, block {block}")
    return off, size


# ---- Tegra block-linear layout of a 4-byte-per-texel surface
def _swizzle_map(width_bytes, height, block_height):
    gobs_x = (width_bytes + 63) // 64
    y = np.arange(height)[:, None]
    x = np.arange(width_bytes)[None, :]
    base = ((y // (8 * block_height)) * gobs_x + x // 64) * (512 * block_height) + ((y % (8 * block_height)) // 8) * 512
    xg, yg = x % 64, y % 8
    return base + (xg // 32) * 256 + (yg // 2) * 64 + ((xg % 32) // 16) * 32 + (yg % 2) * 16 + xg % 16


SWZ = _swizzle_map(ATLAS_W * 4, ATLAS_H, 1 << ATLAS_BLOCK_LOG2)


def deswizzle(raw):
    return np.frombuffer(raw, np.uint8)[SWZ].reshape(ATLAS_H, ATLAS_W, 4).copy()


def swizzle(img):
    out = np.zeros(ATLAS_W * ATLAS_H * 4, np.uint8)
    out[SWZ.reshape(-1)] = img.reshape(-1)
    return out.tobytes()


# ---- cell edits
def cell_view(img, key):
    r, c = CELL[key]
    return img[r * CELL_H:(r + 1) * CELL_H, c * CELL_W:(c + 1) * CELL_W]


def glyph_box(cell):
    ys, xs = np.nonzero(cell[..., 3])
    return xs.min(), ys.min(), xs.max() + 1, ys.max() + 1


def draw(img, orig, key, body, cut, dy=0.0):
    """Clear the cell and draw body minus cut, centred where the game's own icon is (its UV rectangle)."""
    x0, y0, x1, y1 = glyph_box(cell_view(orig, key))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    a, c = G.render_icon(CELL_W, CELL_H, body, cut(cx, cy + dy), cx, cy)
    cell = cell_view(img, key)
    # As in the game's own icons (prompt_glyphs.render_icon): alpha is the body minus the symbol, the colour the
    # body grown by the rim, black beyond it. The UI blends the colour of transparent texels too: white over the
    # whole cell showed as a grey box, colour only under the alpha lost the rim.
    cell[..., :3] = np.round(c * 255).astype(np.uint8)[..., None]
    cell[..., 3] = np.round(a * 255).astype(np.uint8)


def copy_icon(img, orig, dst, src):
    """The game's own icon of cell src, moved into cell dst centred on dst's own icon. The centring uses the alpha
    boxes; the copy takes every texel with any channel set, so the colour layer's rim past the alpha moves too."""
    s = cell_view(orig, src)
    sx0, sy0, sx1, sy1 = glyph_box(s)
    dx0, dy0, dx1, dy1 = glyph_box(cell_view(orig, dst))
    ox = int(round((dx0 + dx1) / 2 - (sx0 + sx1) / 2))
    oy = int(round((dy0 + dy1) / 2 - (sy0 + sy1) / 2))
    ys, xs = np.nonzero(s.any(axis=2))
    cx0, cy0 = max(xs.min(), -ox, 0), max(ys.min(), -oy, 0)
    cx1, cy1 = min(xs.max() + 1, CELL_W - ox), min(ys.max() + 1, CELL_H - oy)
    cell = cell_view(img, dst)
    cell[:] = 0
    cell[cy0 + oy:cy1 + oy, cx0 + ox:cx1 + ox] = s[cy0:cy1, cx0:cx1]


def make_variant(orig, controller, label):
    img = orig.copy()
    if controller == "xbox":
        shoulders = {"L": "LB", "R": "RB", "ZL": "LT", "ZR": "RT"}
        plus, minus = G.lines, G.view
        # Nintendo layout: Switch A is the east button, labelled B on an Xbox pad (and B is A, X is Y, Y is X); the
        # game's own lettered icons swap. Xbox layout: the letters already match the labels.
        face = None if label else {"A": "B", "B": "A", "X": "Y", "Y": "X"}
    else:
        shoulders = {"L": "L1", "R": "R1", "ZL": "L2", "ZR": "R2"}
        plus, minus = G.lines, (lambda cx, cy: G.lines(cx, cy, vertical=True))
        face = None
        symbols = ({"A": "cross", "B": "circle", "X": "square", "Y": "triangle"} if label else
                   {"A": "circle", "B": "cross", "X": "triangle", "Y": "square"})
        for key, sym in symbols.items():
            draw(img, orig, key, G.circle, lambda cx, cy, s=sym: G.ps_symbol(s, cx, cy))
    for key, txt in shoulders.items():
        body = G.trigger if key.startswith("Z") else G.bumper
        draw(img, orig, key, body, lambda cx, cy, t=txt: G.text(t, cx, cy), dy=1.0 if key.startswith("Z") else 0.0)
    draw(img, orig, "plus", G.circle, plus)
    draw(img, orig, "minus", G.circle, minus)
    if face:
        for dst, src in face.items():
            copy_icon(img, orig, dst, src)
    if label:
        # The lit dot names the face button's position; with the Xbox layout Switch A is pressed with the south
        # button, B with the east one, X with the west one and Y with the north one.
        for dst, src in (("dot_e", "dot_s"), ("dot_s", "dot_e"), ("dot_n", "dot_w"), ("dot_w", "dot_n")):
            copy_icon(img, orig, dst, src)
    return img


# ---- output
def write_png(path, rgba):
    h, w, _ = rgba.shape
    raw = b"".join(b"\0" + rgba[y].tobytes() for y in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) +
                           chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def read_pkz(romfs):
    romfs = Path(romfs)
    index = json.loads((romfs / "index.json").read_text(encoding="utf-8"))
    row = next((f for f in index["files"] if f["path"] == PKZ_PATH), None)
    if row is None:
        sys.exit(f"{romfs}: no {PKZ_PATH} in index.json")
    with (romfs / "romfs.bin").open("rb") as stream:
        stream.seek(row["offset"])
        data = stream.read(row["size"])
    if len(data) != row["size"]:
        sys.exit(f"{romfs}: short read of {PKZ_PATH}")
    return data


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--romfs", type=Path, default=ROOT / "build" / "extracted" / "v101" / "romfs",
                    help="extracted RomFS with romfs.bin and index.json (default: build/extracted/v101/romfs)")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "v101",
                    help="folder holding romfs-index.bin (default: build/v101)")
    ap.add_argument("--preview", type=Path, help="also write each variant's atlas as a PNG into this folder")
    args = ap.parse_args()
    if not (args.out / "romfs-index.bin").is_file():
        sys.exit(f"{args.out}/romfs-index.bin missing: --out is the folder holding romfs-index.bin")
    pkz = read_pkz(args.romfs)
    entries = {name: (name, size, off, zsize) for name, size, off, zsize in pkz_entries(pkz)}
    dtts = sorted(n for n in entries if n.startswith("ui_core") and n.endswith(".dtt"))
    if not dtts:
        sys.exit(f"{PKZ_PATH}: no ui_core*.dtt entries")
    # Every language's archive pair: its DTT bytes and where the atlas lies in them, the atlas checked against the
    # one these cells were measured on.
    sources = {}
    orig_img = None
    for dtt_name in dtts:
        dat_name = dtt_name[:-4] + ".dat"
        if dat_name not in entries:
            sys.exit(f"{PKZ_PATH}: {dtt_name} has no {dat_name}")
        dat = pkz_read(pkz, *entries[dat_name])
        dtt = pkz_read(pkz, *entries[dtt_name])
        files = dat_files(dat)
        wta_off, wta_size = files["core.wta"]
        tex_off, tex_size = atlas_place(dat[wta_off:wta_off + wta_size])
        wtp_off, wtp_size = dat_files(dtt)["core.wtp"]
        at = wtp_off + tex_off
        if tex_off + tex_size > wtp_size:
            sys.exit(f"{dtt_name}: the atlas runs past core.wtp")
        raw = dtt[at:at + tex_size]
        if hashlib.sha256(raw).hexdigest() != ATLAS_SHA256:
            sys.exit(f"{dtt_name}: the prompts atlas differs from the one this tool knows (another game version?)")
        if orig_img is None:
            orig_img = deswizzle(raw)
        sources[dtt_name] = (dtt, at)
    orig_sha = hashlib.sha256(pkz).hexdigest()
    print(f"original {PKZ_PATH}: {len(pkz)} bytes; atlas in {len(dtts)} archives")
    if args.preview:
        args.preview.mkdir(parents=True, exist_ok=True)
        write_png(args.preview / "original.png", orig_img)
    for folder, (controller, label) in FOLDERS.items():
        img = make_variant(orig_img, controller, label)
        new_raw = swizzle(img)
        swaps = {}
        for name, (dtt, at) in sources.items():
            b = bytearray(dtt)
            b[at:at + len(new_raw)] = new_raw
            swaps[name] = bytes(b)
        new = repack_pkz(pkz, swaps)
        dst = args.out / folder
        if dst.exists():
            shutil.rmtree(dst)
        (dst / "ui").mkdir(parents=True)
        (dst / PKZ_PATH).write_bytes(new)
        line = f"{PKZ_PATH}\t{len(new)}\t{hashlib.sha256(new).hexdigest()}\t{orig_sha}"
        (dst / "manifest.tsv").write_text(line + "\n", encoding="utf-8", newline="\n")
        if args.preview:
            write_png(args.preview / f"{folder}.png", img)
        print(f"{folder}: {PKZ_PATH} repacked ({len(new)} bytes), atlas redrawn in {len(swaps)} archives")


if __name__ == "__main__":
    main()
