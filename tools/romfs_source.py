"""The game's RomFS read straight from the player's dump files, without a merged romfs.bin.

The 1.0.1 RomFS is the update's patch RomFS section: a virtual section whose bytes come either from the base game's
RomFS section or from the update's own section (BKTR). Its relocation table maps each virtual range to one of the two,
and its subsection table gives the AES-CTR counter generation of each range of the update's section. Both NCAs are
plain AES-CTR otherwise. This module resolves both tables once into a list of extents, each a range of the merged
image that is one contiguous AES-CTR run in one dump file, and writes them to romfs-map.bin (no key material: the
file names the keys, the reader takes them from the keys file). The runtime (runtime/romfs_source.c) and the tools
read the image through that map.

romfs-map.bin, little-endian:

    header   <8sIIQQQQ  magic b'ACRMAP1\\0', version 1, source count, image size, extent count, strings offset,
                        strings size
    sources  count x <QQQ32sIIIIIIIIIIQQQ32s  file size, NCA offset in the file, NCA size, SHA-256 of the NCA's 0xc00
                        header bytes as stored, path (string offset, length), kind (1 key area, 2 title key), key name
                        (string offset, length), key slot (key area index), keys file (string offset, length), two zero
                        words, title key offset in the file (kind 2), check: image offset and length of the source's
                        first extent's first bytes (at most 4 KiB) and their SHA-256, which tell a wrong key or another
                        file at start without hashing the image
    extents  count x <QQQQ8sII image offset, length, file offset, NCA-relative offset (the AES-CTR counter is that
                        offset / 16), upper 8 bytes of the counter, source index, 0; sorted, back to back, covering the
                        image
    strings  UTF-8, not NUL-terminated

The BKTR layout follows hactool (ISC, Copyright (c) 2018 SciresM), bktr.h and nca.c nca_process_bktr_section,
read for behaviour; this is our own code.
"""
import bisect
import hashlib
import io
import json
import shutil
import struct
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import extract
from extract import require

MAGIC = b'ACRMAP1\0'
VERSION = 1
HEADER = struct.Struct('<8sIIQQQQ')
SOURCE = struct.Struct('<QQQ32sIIIIIIIIIIQQQ32s')
CHECK_BYTES = 4096
EXTENT = struct.Struct('<QQQQ8sII')
MAP_NAME = 'romfs-map.bin'

KIND_KEY_AREA, KIND_TITLE_KEY = 1, 2
TICKET_BODIES = {0x10000: 0x240, 0x10001: 0x140, 0x10002: 0x80, 0x10003: 0x240, 0x10004: 0x140, 0x10005: 0x80}


def ticket_key_offsets(f, entries):
    """{rights id: file offset of its encrypted title key} of the container's common tickets."""
    out = {}
    for name, offset, size in entries:
        if not name.lower().endswith('.tik'):
            continue
        sig = struct.unpack('<I', extract.read_at(f, offset, 4))[0]
        require(sig in TICKET_BODIES and size >= TICKET_BODIES[sig] + 0x180, 'Unsupported ticket')
        body = offset + TICKET_BODIES[sig]
        data = extract.read_at(f, body, 0x180)
        out[data[0x160:0x170]] = body + 0x40
    return out


def key_recipe(hdr, ticket_offsets):
    """(kind, key name, key slot, title key offset) naming how the NCA's AES-CTR key is derived (extract.nca_ctr_key)."""
    generation = max(max(hdr[0x206], hdr[0x220]) - 1, 0)
    rights = hdr[0x230:0x240]
    if rights != bytes(16):
        require(rights in ticket_offsets, 'Missing common ticket for rights-managed NCA')
        return KIND_TITLE_KEY, f'titlekek_{generation:02x}', 0, ticket_offsets[rights]
    require(hdr[0x207] < 3, 'Unsupported NCA key area')
    area = ('application', 'ocean', 'system')[hdr[0x207]]
    return KIND_KEY_AREA, f'key_area_key_{area}_{generation:02x}', 2, 0


def section(hdr, index):
    """(start, size, fs header) of NCA section index, offsets relative to the NCA."""
    start, end = struct.unpack_from('<II', hdr, 0x240 + index * 16)
    fs = hdr[0x400 + 512 * index:0x600 + 512 * index]
    require(hashlib.sha256(fs).digest() == hdr[0x280 + 32 * index:0x2a0 + 32 * index], 'FS header hash mismatch')
    return start * 512, (end - start) * 512, fs


def counter_high(fs, generation=None):
    """The upper 8 bytes of a section's AES-CTR counter: the section counter, big-endian; a BKTR range replaces its
    low word with the range's generation."""
    high = fs[0x140:0x148][::-1]
    return high if generation is None else high[:4] + struct.pack('>I', generation)


class Source:
    """One dump file's Program NCA: the container entry, its header and the key that decrypts it."""

    def __init__(self, path, record, hdr, recipe, keys):
        self.path, self.record, self.hdr, self.recipe = Path(path), record, hdr, recipe
        self.key = None
        self.keys = keys

    def derive(self, tickets):
        self.key = extract.nca_ctr_key(self.hdr, self.keys, tickets)


def open_sources(base, update, keys_path):
    """[base, update] Sources with their keys."""
    keys = extract.load_keys(Path(keys_path))
    tickets, out = {}, []
    for path, is_update in ((Path(base), False), (Path(update), True)):
        with path.open('rb') as f:
            kind, entries = extract.container_entries(f, path.stat().st_size)
            for rights, key in extract.common_tickets(f, entries).items():
                tickets[rights] = key
            records = extract.nca_inventory(f, entries, keys)
            if hasattr(extract, 'game_records'):   # the XCI reader leaves out a card's bundled update
                records = extract.game_records(f, kind, entries, keys)
            record, hdr = extract.program_nca(records, update=is_update)
            out.append(Source(path, record, hdr, key_recipe(hdr, ticket_key_offsets(f, entries)), keys))
    for source in out:
        source.derive(tickets)
    return out


def ctr_read(f, source, nca_rel, size, high):
    """size bytes of source's NCA at NCA-relative offset nca_rel, decrypted with counter high || nca_rel / 16."""
    aligned = nca_rel & ~15
    data = extract.read_at(f, source.record['nca_offset'] + aligned, size + nca_rel - aligned)
    iv = high + (aligned // 16).to_bytes(8, 'big')
    return extract.decrypt(source.key, modes.CTR(iv), data)[nca_rel - aligned:]


def romfs_section(hdr):
    """The index of the base Program NCA's plain RomFS section (AES-CTR)."""
    for i in range(4):
        start, _ = struct.unpack_from('<II', hdr, 0x240 + i * 16)
        if start and hdr[0x400 + 512 * i + 2:0x400 + 512 * i + 5] == bytes([0, 3, 3]):
            return i
    raise ValueError('No RomFS section in the base Program NCA')


def patch_section(hdr):
    """The index of the update Program NCA's BKTR section."""
    found = [i for i in range(4) if struct.unpack_from('<II', hdr, 0x240 + i * 16)[0]
             and hdr[0x400 + 512 * i + 4] == 4]
    require(len(found) == 1, 'Expected one BKTR section')
    return found[0]


def buckets(table, entry_size, parse):
    """The entries of a BKTR bucket tree: a 0x4000-byte header (bucket count, total size, each bucket's first offset),
    then 0x4000-byte buckets (entry count, end offset, entries). Returns (total size, [entry tuples], [bucket starts])."""
    require(len(table) >= 0x4000, 'BKTR table too small')
    count, total = struct.unpack_from('<IQ', table, 4)
    require(0 < count <= (len(table) - 0x4000) // 0x4000 and count <= 0x7fe, 'BKTR bucket count')
    starts = list(struct.unpack_from(f'<{count}Q', table, 0x10))
    entries = []
    for b in range(count):
        base = 0x4000 * (b + 1)
        n = struct.unpack_from('<I', table, base + 4)[0]
        require(0 < n <= (0x3ff0 // entry_size), 'BKTR bucket entry count')
        entries += [parse(table, base + 0x10 + j * entry_size) for j in range(n)]
    return total, entries, starts


def extents(base, update):
    """(image size, [(image offset, length, source index, NCA-relative offset, counter high)]) of the merged RomFS."""
    b_index, p_index = romfs_section(base.hdr), patch_section(update.hdr)
    b_start, b_size, b_fs = section(base.hdr, b_index)
    p_start, p_size, p_fs = section(update.hdr, p_index)
    require(p_fs[8:12] == b'IVFC' and b_fs[8:12] == b'IVFC', 'Unsupported IVFC layout')
    l5_offset, l5_size = struct.unpack_from('<QQ', p_fs, 0x18 + 5 * 24)
    reloc_off, reloc_size, reloc_magic = struct.unpack_from('<QQI', p_fs, 0x100)
    sub_off, sub_size, sub_magic = struct.unpack_from('<QQI', p_fs, 0x120)
    require(reloc_magic == sub_magic == 0x52544B42, 'BKTR magic')
    require(reloc_off + reloc_size == sub_off and sub_off + sub_size <= p_size, 'Invalid BKTR layout')
    ctr_low = struct.unpack_from('<I', p_fs, 0x140)[0]

    with update.path.open('rb') as f:
        plain = counter_high(p_fs)
        relocs = ctr_read(f, update, p_start + reloc_off, reloc_size, plain)
        subs = ctr_read(f, update, p_start + sub_off, sub_size, plain)

    virt_total, rel, rel_starts = buckets(relocs, 20, lambda t, o: struct.unpack_from('<QQI', t, o))
    _, sub, _ = buckets(subs, 16, lambda t, o: struct.unpack_from('<QII', t, o)[::2])
    require(l5_offset + l5_size <= virt_total, 'RomFS outside the BKTR section')

    # Subsection ranges of the update's section: [start, next start) with a generation; the tables themselves and the
    # rest of the section use the section's own counter.
    sub_ranges = [(s, sub[j + 1][0] if j + 1 < len(sub) else reloc_off, g) for j, (s, g) in enumerate(sub)]
    sub_ranges.append((reloc_off, p_size, ctr_low))
    require(all(a < b for a, b, _ in sub_ranges) and
            all(sub_ranges[j][1] == sub_ranges[j + 1][0] for j in range(len(sub_ranges) - 1)), 'BKTR subsection order')
    sub_starts = [s for s, _, _ in sub_ranges]

    out = []

    def add(image_off, length, src, nca_rel, high):
        if out:
            o, n, s, r, h = out[-1]
            if s == src and h == high and o + n == image_off and r + n == nca_rel:
                out[-1] = (o, n + length, s, r, h)
                return
        out.append((image_off, length, src, nca_rel, high))

    lo, hi = l5_offset, l5_offset + l5_size
    for j, (virt, phys, is_patch) in enumerate(rel):
        end = rel[j + 1][0] if j + 1 < len(rel) else virt_total
        a, b = max(virt, lo), min(end, hi)
        if a >= b:
            continue
        p = phys + (a - virt)
        if not is_patch:
            require(p + (b - a) <= b_size, 'BKTR base range outside the base RomFS')
            add(a - lo, b - a, 0, b_start + p, counter_high(b_fs))
            continue
        while a < b:
            k = bisect.bisect_right(sub_starts, p) - 1
            require(k >= 0, 'BKTR patch range before the first subsection')
            s_start, s_end, gen = sub_ranges[k]
            n = min(b - a, s_end - p)
            require(n > 0, 'BKTR patch range outside its section')
            add(a - lo, n, 1, p_start + p, counter_high(p_fs, gen))
            a, p = a + n, p + n
    require(sum(e[1] for e in out) == l5_size and out[0][0] == 0, 'RomFS extents do not cover the image')
    return l5_size, out


def write_map(base, update, keys_path, out_path):
    """Resolve the merged RomFS of base + update into out_path (romfs-map.bin). Returns (image size, extent count)."""
    sources = open_sources(base, update, keys_path)
    size, exts = extents(*sources)
    strings = bytearray()

    def string(s):
        data = str(s).encode('utf-8')
        strings.extend(data)
        return len(strings) - len(data), len(data)

    keys_ref = string(Path(keys_path).resolve())
    rows = bytearray()
    for index, s in enumerate(sources):
        first = next((e for e in exts if e[2] == index), None)
        require(first is not None, f'The RomFS reads nothing from {s.path}')
        check_off, check_len = first[0], min(CHECK_BYTES, first[1])
        with s.path.open('rb') as f:
            head = extract.read_at(f, s.record['nca_offset'], 0xc00)
            check = ctr_read(f, s, first[3], check_len, first[4])
        kind, key_name, slot, ticket = s.recipe
        rows += SOURCE.pack(s.path.stat().st_size, s.record['nca_offset'], s.record['size'],
                            hashlib.sha256(head).digest(), *string(s.path.resolve()), kind, *string(key_name), slot,
                            *keys_ref, 0, 0, ticket, check_off, check_len, hashlib.sha256(check).digest())
    ext = b''.join(EXTENT.pack(o, n, sources[src].record['nca_offset'] + r, r, h, src, 0)
                   for o, n, src, r, h in exts)
    strings_offset = HEADER.size + len(rows) + len(ext)
    data = HEADER.pack(MAGIC, VERSION, len(sources), size, len(exts), strings_offset, len(strings)) + rows + ext + strings
    tmp = Path(out_path).with_suffix('.tmp')
    tmp.write_bytes(data)
    tmp.replace(out_path)
    return size, len(exts)


def read_map(path):
    """(image size, [source dicts], [extent tuples]) of a romfs-map.bin; ValueError on a malformed one."""
    data = Path(path).read_bytes()
    require(len(data) >= HEADER.size, 'RomFS map too small')
    magic, version, nsrc, size, nexts, soff, ssize = HEADER.unpack_from(data)
    require(magic == MAGIC and version == VERSION, 'Not a RomFS map of this version')
    require(soff == HEADER.size + nsrc * SOURCE.size + nexts * EXTENT.size and soff + ssize == len(data),
            'RomFS map layout')
    strings = data[soff:]

    def text(off, n):
        require(off + n <= len(strings), 'RomFS map string bounds')
        return strings[off:off + n].decode('utf-8')

    sources = []
    for i in range(nsrc):
        (fsize, nca_off, nca_size, head_sha, p_off, p_len, kind, k_off, k_len, slot, kf_off, kf_len, _, _,
         ticket, check_off, check_len, check_sha) = SOURCE.unpack_from(data, HEADER.size + i * SOURCE.size)
        sources.append(dict(path=text(p_off, p_len), size=fsize, nca_offset=nca_off, nca_size=nca_size,
                            header_sha256=head_sha, kind=kind, key_name=text(k_off, k_len), slot=slot,
                            keys=text(kf_off, kf_len), ticket_key_offset=ticket, check_offset=check_off,
                            check_size=check_len, check_sha256=check_sha))
    exts = [EXTENT.unpack_from(data, HEADER.size + nsrc * SOURCE.size + j * EXTENT.size) for j in range(nexts)]
    pos = 0
    for o, n, _, _, _, src, _ in exts:
        require(o == pos and n > 0 and src < nsrc, 'RomFS map extents')
        pos += n
    require(pos == size, 'RomFS map extents do not cover the image')
    return size, sources, exts


def map_key(source, keys):
    """The AES-CTR key of a map source, derived from the keys file as the runtime does (runtime/romfs_source.c)."""
    with open(source['path'], 'rb') as f:
        if source['kind'] == KIND_TITLE_KEY:
            require(source['key_name'] in keys, f'Missing {source["key_name"]} in the keys file')
            return extract.decrypt(keys[source['key_name']], modes.ECB(),
                                   extract.read_at(f, source['ticket_key_offset'], 16))
        require('header_key' in keys and source['key_name'] in keys, f'Missing {source["key_name"]} in the keys file')
        hdr = extract.read_nca_header(f, source['nca_offset'], source['nca_size'], keys)
        area = extract.decrypt(keys[source['key_name']], modes.ECB(), hdr[0x300:0x340])
        return area[16 * source['slot']:16 * source['slot'] + 16]


class MappedImage(io.RawIOBase):
    """A read-only file object over the merged RomFS described by a romfs-map.bin."""

    def __init__(self, map_path, keys_path=None):
        super().__init__()
        self.size, self.sources, self.extents = read_map(map_path)
        keys = extract.load_keys(Path(keys_path or self.sources[0]['keys']))
        self.files = []
        for s in self.sources:
            path = Path(s['path'])
            require(path.is_file() and path.stat().st_size == s['size'],
                    f'{path} is missing or changed since the RomFS map was written')
            f = path.open('rb')
            require(hashlib.sha256(extract.read_at(f, s['nca_offset'], 0xc00)).digest() == s['header_sha256'],
                    f'{path} holds another NCA than the RomFS map was written for')
            self.files.append(f)
        self.keys = [map_key(s, keys) for s in self.sources]
        self.starts = [e[0] for e in self.extents]
        self.pos = 0
        for s in self.sources:
            require(hashlib.sha256(self.pread(s['check_offset'], s['check_size'])).digest() == s['check_sha256'],
                    f'{s["path"]} does not decrypt to the game data the RomFS map was written for (another file, or '
                    f'another keys file)')

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, offset, whence=0):
        self.pos = (offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset)
        return self.pos

    def pread(self, offset, size):
        size = max(0, min(size, self.size - offset))
        out = bytearray()
        k = bisect.bisect_right(self.starts, offset) - 1
        while size:
            o, n, file_off, rel, high, src, _ = self.extents[k]
            skip = offset - o
            take = min(size, n - skip)
            r = rel + skip
            aligned = r & ~15
            f = self.files[src]
            f.seek(file_off + skip - (r - aligned))
            data = f.read(take + r - aligned)
            require(len(data) == take + r - aligned, 'Truncated dump file')
            ctx = Cipher(algorithms.AES(self.keys[src]), modes.CTR(high + (aligned // 16).to_bytes(8, 'big'))).decryptor()
            out += (ctx.update(data) + ctx.finalize())[r - aligned:]
            offset, size, k = offset + take, size - take, k + 1
        return bytes(out)

    def read(self, size=-1):
        if size is None or size < 0:
            size = self.size - self.pos
        data = self.pread(self.pos, size)
        self.pos += len(data)
        return data

    def readinto(self, buffer):
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def close(self):
        for f in getattr(self, 'files', []):
            f.close()
        super().close()


def open_image(romfs_dir):
    """The merged RomFS of an extraction folder as a binary file object: romfs.bin, else romfs-map.bin."""
    romfs_dir = Path(romfs_dir)
    image = romfs_dir / 'romfs.bin'
    if image.is_file():
        return image.open('rb')
    return MappedImage(romfs_dir / MAP_NAME)


def image_size(romfs_dir):
    """The size of an extraction folder's RomFS image (romfs.bin, else romfs-map.bin), None without either."""
    romfs_dir = Path(romfs_dir)
    if (romfs_dir / 'romfs.bin').is_file():
        return (romfs_dir / 'romfs.bin').stat().st_size
    if (romfs_dir / MAP_NAME).is_file():
        return read_map(romfs_dir / MAP_NAME)[0]
    return None


def extract_map(base, update, keys_path, output, progress=lambda done, total: None):
    """The map counterpart of extract.merge_update_romfs: write output/ (romfs-map.bin and index.json) for base +
    update without a merged image. The image is read once through the map, its SHA-256 recorded in index.json and its
    files indexed, so the caller checks it against the expected hashes as it does a merged romfs.bin. Returns the
    index.json report."""
    output = Path(output)
    work = output.with_name(output.name + '.new')
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    size, count = write_map(base, update, keys_path, work / MAP_NAME)
    with MappedImage(work / MAP_NAME, keys_path) as image:
        digest = hash_image(image, size, progress)
        files = extract.index_romfs_file(image, size)
    report = dict(status='pass', source='dumps', romfs_size=size, romfs_sha256=digest, files=files, extents=count,
                  integrity='read through the BKTR map from the dump files; SHA-256 of the whole image')
    (work / 'index.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    if output.exists():
        shutil.rmtree(output)
    work.rename(output)
    return report


def hash_image(f, size, progress=lambda done, total: None, chunk=16 << 20):
    """SHA-256 of a RomFS image file object."""
    digest = hashlib.sha256()
    for pos in range(0, size, chunk):
        f.seek(pos)
        data = f.read(min(chunk, size - pos))
        require(len(data) == min(chunk, size - pos), 'RomFS image read short')
        digest.update(data)
        progress(pos + len(data), size)
    return digest.hexdigest()
