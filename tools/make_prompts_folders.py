"""Build the button-prompts RomFS overlays from emulator controller-UI mods.

Reads Switch emulator mods (folders with a romfs/ tree such as romfs/ui/ui_core.dat,
e.g. the Xbox Controller UI and PlayStation Controller UI mods for Astral Chain) and
writes romfs-patches-xbox and romfs-patches-playstation next to romfs-index.bin
(runtime/romfs.c overlay_init). Each folder holds a repacked ui/ui.pkz (the game's UI
archive with the mod's entries swapped in) plus its manifest.tsv, served in place of
the game's own archive while AC_PROMPTS is xbox or playstation (or auto with such a
controller). The game reads ui.pkz, not loose ui_core files, so a copy of the mods'
loose files would never load. The mods draw the Switch positions, so each folder is
layout-independent (input_prompts_folder).

The mod sources are vendored under controller-ui/ (xbox and playstation, each an
emulator mod root or a romfs/ tree), which the defaults read, or pass their paths.
The original ui.pkz comes from the extracted RomFS (--romfs, default
build/extracted/v101/romfs): its entries are copied verbatim except the ones the mod
replaces, and its SHA-256 goes into the manifest as the original. The game reads its
UI archive once at boot, so new prompts apply after a restart (the Controls tab says
so and offers one).

    .venv\\Scripts\\python tools\\make_prompts_folders.py --out build\\v101

The PlayStation mod only ships the base and US UI files: the other languages keep
the game's own entries (the tool lists the entries each folder swaps).
"""

import argparse
import hashlib
import json
import shutil
import struct
import sys
from pathlib import Path

try:
    import zstandard
except ImportError:
    sys.exit("make_prompts_folders.py needs the zstandard package (.venv has it)")
ROOT = Path(__file__).resolve().parent.parent

FOLDERS = (("xbox", "romfs-patches-xbox"), ("playstation", "romfs-patches-playstation"))

PKZ_PATH = "ui/ui.pkz"


def romfs_root(mod):
    """The romfs/ tree of an emulator mod root (any case) or the tree itself."""
    p = Path(mod)
    if not p.is_dir():
        sys.exit(f"{mod}: no such directory")
    for child in sorted(p.iterdir()):
        if child.is_dir() and child.name.lower() == "romfs":
            return child
    if (p / "ui").is_dir():
        return p
    sys.exit(f"{mod}: neither a romfs/ subfolder nor a romfs tree (expected romfs/ui/ inside)")


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
    out = bytearray()
    out += orig[:entry_off]   # header (total size fixed up below)
    table_at = len(out)
    out += b"\0" * (items * 0x20)
    out += orig[name_table:name_table + name_size]
    assert len(out) == data_start, "pkz layout: the data starts right after the name table"
    blobs = []
    for i, (name, size, offset, zsize) in enumerate(pkz_entries(orig)):
        if name in swaps:
            raw, z = swaps[name], zstandard.ZstdCompressor().compress(swaps[name])
        else:
            raw, z = None, orig[offset:offset + zsize]
            size = size  # untouched entries keep their sizes
        blobs.append((name, raw, z, size if raw is None else len(raw)))
    for i, (name, raw, z, size) in enumerate(blobs):
        if raw is not None:
            chk = zstandard.ZstdDecompressor().decompress(z)
            if chk != raw:
                sys.exit(f"repacked {name} does not round-trip")
        struct.pack_into("<8I", out, table_at + i * 0x20, *struct.unpack_from("<8I", orig, entry_off + i * 0x20)[:1],
                         0, size, 0, data_start, 0, len(z), 0)
        out += z
        data_start += len(z)
    struct.pack_into("<I", out, 0x08, len(out))   # header's total size
    return bytes(out)


def read_pkz(romfs):
    """(offset, size, bytes) of ui/ui.pkz in the extracted RomFS."""
    romfs = Path(romfs)
    index = json.loads((romfs / "index.json").read_text(encoding="utf-8"))
    row = next((f for f in index["files"] if f["path"] == PKZ_PATH), None)
    if row is None:
        sys.exit(f"{romfs}: no {PKZ_PATH} in index.json")
    if (romfs / "romfs.bin").is_file():
        stream = (romfs / "romfs.bin").open("rb")
    else:   # romfs-map.bin: the image read from the dump files (needs the cryptography package)
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import romfs_source
        stream = romfs_source.open_image(romfs)
    with stream:
        stream.seek(row["offset"])
        data = stream.read(row["size"])
    if len(data) != row["size"]:
        sys.exit(f"{romfs}: short read of {PKZ_PATH}")
    return data


def build(mod, folder, out, orig):
    """Write out/folder with a repacked ui.pkz (the mod's entries swapped in) and its manifest."""
    src = romfs_root(mod)
    files = sorted(f for f in src.rglob("*") if f.is_file())
    if not files:
        sys.exit(f"{mod}: no files under {src}")
    swaps = {}
    for f in files:
        rel = f.relative_to(src).as_posix()
        if rel.startswith(".") or "\\" in rel or f.name.lower() == "manifest.tsv":
            sys.exit(f"{mod}: refusing to overlay {rel}")
        swaps.setdefault(f.name, f.read_bytes())
    names = [n for n, _, _, _ in pkz_entries(orig)]
    missing = sorted(set(swaps) - set(names))
    if missing:
        sys.exit(f"{mod}: {', '.join(missing)} match no entry of ui.pkz")
    swapped = sorted(set(swaps) & set(names))
    new = repack_pkz(orig, {n: swaps[n] for n in swapped})
    dst = Path(out) / folder
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "ui").mkdir(parents=True)
    (dst / PKZ_PATH).write_bytes(new)
    line = f"{PKZ_PATH}\t{len(new)}\t{hashlib.sha256(new).hexdigest()}\t{hashlib.sha256(orig).hexdigest()}"
    (dst / "manifest.tsv").write_text(line + "\n", encoding="utf-8", newline="\n")
    print(f"{folder}: repacked {PKZ_PATH} ({len(new)} bytes) with {len(swapped)} swapped entries: "
          f"{', '.join(swapped)}")
    return set(swapped)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--xbox-mod", type=Path, default=ROOT / "controller-ui" / "xbox",
                    help="emulator mod with the Xbox button UI (default: controller-ui/xbox)")
    ap.add_argument("--playstation-mod", type=Path, default=ROOT / "controller-ui" / "playstation",
                    help="emulator mod with the PlayStation button UI (default: controller-ui/playstation)")
    ap.add_argument("--romfs", type=Path, default=ROOT / "build" / "extracted" / "v101" / "romfs",
                    help="extracted RomFS with romfs.bin and index.json (default: build/extracted/v101/romfs)")
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "v101",
                    help="folder holding romfs-index.bin (default: build/v101)")
    args = ap.parse_args()
    mods = []
    for opt, folder in FOLDERS:
        mod = getattr(args, opt + "_mod")
        if not Path(mod).is_dir():
            if f"--{opt}-mod" in sys.argv:
                sys.exit(f"{mod}: no such directory")
            continue   # flag omitted and no conventional folder: skip
        mods.append((mod, folder))
    if not mods:
        ap.error("nothing to do: drop the emulator mods into controller-ui/ or pass --xbox-mod/--playstation-mod")
    if not (Path(args.out) / "romfs-index.bin").is_file():
        sys.exit(f"{args.out}/romfs-index.bin missing: --out is the folder holding romfs-index.bin")
    orig = read_pkz(args.romfs)
    print(f"original {PKZ_PATH}: {len(orig)} bytes, {hashlib.sha256(orig).hexdigest()[:16]}...")
    got = {}
    for mod, folder in mods:
        got[folder] = build(mod, folder, args.out, orig)
    names = [f for _, f in FOLDERS if f in got]
    if len(names) == 2:
        only_first = sorted(got[names[0]] - got[names[1]])
        only_second = sorted(got[names[1]] - got[names[0]])
        if only_first or only_second:
            print(f"note: {names[1]} keeps the game's own {', '.join(only_first) or 'nothing'}; "
                  f"{names[0]} keeps the game's own {', '.join(only_second) or 'nothing'}")


if __name__ == "__main__":
    main()
