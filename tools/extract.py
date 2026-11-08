"""Extract Astral Chain 1.0.1 (title 01007300020FA000, update 01007300020FA800) into build/ and verify it.

Adapted from the vendored MacNativeGames extractor, tools/vendor/macnativegames/extract_exefs.py,
extract_romfs.py and extract_update_romfs.py @ b75be8c (MIT, Copyright (c) 2026 MacNativeGames contributors),
and from the TotK Recomp tools/extract.py at its baseline commit. The tool decrypts in place (NCA3, AES-CTR
key-area or common ticket, hierarchical PFS0, IVFC) and never writes key material.

Steps, each skipped when its verified output already exists:

1. ExeFS of the base game (1.0.0), verified against tools/data/expected-v1.0.0.json.
2. ExeFS of the update NSP. Its version is identified by the SHA-256 of `main`, not by its file name, and
   checked against tools/data/expected-v<version>.json. Only the code modules (main, sdk, rtld) decide: a
   dump of the same build may carry another main.npdm.
3. RomFS of the update, merged from the base Program NCA and the update's BKTR section by hactool, verified
   through all six IVFC levels, with its file index.

The base NSP is expected to be ticketless or key-area encrypted (the usual game-card dump); the update NSP
carries a common ticket. A file whose data fails its hashes or ends early stops the extraction.

--base, --update, --keys and --work name the files and the output directory instead of the config (the
installer imports the functions instead). Key values are read by the tools from the keys file and never
printed.
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'tools' / 'data'

GAME_TITLE_ID = '01007300020FA000'
UPDATE_TITLE_ID = '01007300020FA800'   # the update NSP: the base id with bit 0x800 set


def require(condition, message):
    if not condition:
        raise ValueError(message)


def decrypt(key, mode, data):
    ctx = Cipher(algorithms.AES(key), mode).decryptor()
    return ctx.update(data) + ctx.finalize()


def read_at(f, offset, length):
    require(0 <= offset and 0 <= length <= 512 * 1024 * 1024, 'Invalid/big read')
    f.seek(offset)
    data = f.read(length)
    require(len(data) == length, 'Truncated input')
    return data


def partition_entries(f, offset, bound, magic=b'PFS0', stride=24):
    """Read a bounded PFS0/HFS0 table without loading its file payloads."""
    require(bound >= 16, 'Truncated partition header')
    hdr = read_at(f, offset, 16)
    require(hdr[:4] == magic, f'Expected {magic.decode()}')
    count, strings_size = struct.unpack_from('<II', hdr, 4)
    require(count <= 4096 and strings_size <= 1024 * 1024, 'Invalid partition table')
    size = 16 + count * stride + strings_size
    require(size <= bound, 'Partition table out of bounds')
    raw = read_at(f, offset, size)
    strings = raw[16 + count * stride:]
    entries = []
    names = set()
    for i in range(count):
        off, length, nameoff = struct.unpack_from('<QQI', raw, 16 + i * stride)
        require(nameoff < len(strings), 'Invalid partition filename offset')
        end = strings.find(b'\0', nameoff)
        require(end >= 0, 'Unterminated partition filename')
        name = strings[nameoff:end].decode('utf-8')
        require(name not in ('', '.', '..') and '/' not in name and '\\' not in name, 'Unsafe partition filename')
        require(name not in names, 'Duplicate partition filename')
        names.add(name)
        require(size + off + length <= bound, 'Partition entry out of bounds')
        entries.append((name, offset + size + off, length))
    end = offset + size
    for _, start, length in sorted(entries, key=lambda e: e[1]):
        require(start >= end, 'Overlapping partition entries')
        end = start + length
    return entries


def container_entries(f, size):
    if read_at(f, 0, 4) == b'PFS0':
        return 'nsp', partition_entries(f, 0, size)
    raise ValueError('Expected an NSP (PFS0) game file; XCI is not supported')


def load_keys(path):
    keys = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        line = line.split('#', 1)[0].strip()
        if '=' in line:
            name, value = line.split('=', 1)
            try:
                keys[name.strip().lower()] = bytes.fromhex(value.strip())
            except ValueError:
                raise ValueError(f'Invalid key encoding at line {number}') from None
    return keys


def common_tickets(f, entries):
    """Keep encrypted title keys in memory only; a personalized ticket never provides a common key."""
    tickets = {}
    bodies = {0x10000: 0x240, 0x10001: 0x140, 0x10002: 0x80,
              0x10003: 0x240, 0x10004: 0x140, 0x10005: 0x80}
    for name, offset, size in entries:
        if not name.lower().endswith('.tik'):
            continue
        require(size >= 4, 'Truncated NSP ticket')
        sig = struct.unpack('<I', read_at(f, offset, 4))[0]
        require(sig in bodies, 'Unsupported NSP ticket signature type')
        body = bodies[sig]
        require(size >= body + 0x180, 'Truncated NSP ticket body')
        data = read_at(f, offset + body, 0x180)
        require(data[0x141] == 0, 'Personalized NSP tickets are unsupported; a common ticket is required')
        rights, key = data[0x160:0x170], data[0x40:0x50]
        require(rights not in tickets or tickets[rights] == key, 'Conflicting NSP tickets')
        tickets[rights] = key
    return tickets


def read_nca_header(f, offset, size, keys):
    require(size >= 0xc00, 'Truncated NCA header')
    require('header_key' in keys, 'Missing header_key in prod.keys')
    raw = read_at(f, offset, 0xc00)
    hdr = b''.join(decrypt(keys['header_key'], modes.XTS(i.to_bytes(16, 'big')),
                           raw[i * 512:(i + 1) * 512]) for i in range(6))
    require(hdr[0x200:0x204] == b'NCA3', 'Expected NCA3 (check prod.keys and the game file)')
    require(struct.unpack_from('<Q', hdr, 0x208)[0] == size, 'NCA size does not match container entry')
    for i in range(4):
        start, end = struct.unpack_from('<II', hdr, 0x240 + i * 16)
        require((start == end == 0) or 0xc00 <= start * 512 < end * 512 <= size, 'NCA section out of bounds')
    return hdr


def nca_ctr_key(hdr, keys, tickets):
    generation = max(max(hdr[0x206], hdr[0x220]) - 1, 0)
    rights = hdr[0x230:0x240]
    if rights != bytes(16):
        require(rights in tickets, 'Missing common ticket for rights-managed NCA in the NSP')
        keyname = f'titlekek_{generation:02x}'
        require(keyname in keys, f'Missing {keyname} in prod.keys')
        return decrypt(keys[keyname], modes.ECB(), tickets[rights])
    require(hdr[0x207] < 3, 'Unsupported NCA key area')
    area = ('application', 'ocean', 'system')[hdr[0x207]]
    keyname = f'key_area_key_{area}_{generation:02x}'
    require(keyname in keys, f'Missing {keyname} in prod.keys')
    return decrypt(keys[keyname], modes.ECB(), hdr[0x300:0x340])[32:48]


def nca_inventory(f, entries, keys):
    records = []
    for name, offset, length in entries:
        if not name.lower().endswith('.nca'):
            continue
        hdr = read_nca_header(f, offset, length, keys)
        record = dict(name=name, nca_offset=offset, size=length, content_type=hdr[0x205],
                      title_id=f'{struct.unpack_from("<Q", hdr, 0x210)[0]:016X}')
        records.append((record, hdr))
    return records


def program_nca(records, update=False):
    """The one Program NCA of Astral Chain; update: the file must be the update NSP.

    Both packages carry the base title id on their Program NCA; the Meta NCA (content type 1) carries the
    package's own id, so that is what tells an update from a base game.
    """
    programs = [(r, h) for r, h in records if r['content_type'] == 0]
    require(len(programs) == 1, 'Expected one Program NCA; updates, DLC and combined packages are unsupported')
    record, hdr = programs[0]
    require(record['title_id'] == GAME_TITLE_ID,
            f'Expected Astral Chain ({GAME_TITLE_ID}); this file contains another title')
    metadata = [r['title_id'] for r, _ in records if r['content_type'] == 1]
    if update:
        require(UPDATE_TITLE_ID in metadata, 'The file is not an Astral Chain update')
    return record, hdr


def load_expected():
    """{display version: data} from every tools/data/expected-v*.json."""
    versions = {}
    for path in sorted(DATA.glob('expected-v*.json')):
        data = json.loads(path.read_text(encoding='utf-8'))
        versions[data['display_version']] = data
    return versions


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def extract_exefs(game, keys, output, update=False):
    """Extract the ExeFS of the base or update Program NCA into output/; write the extraction manifest next to it."""
    require(not (output.parent / 'extraction-manifest.json').exists(), 'ExeFS already extracted')
    keys = load_keys(keys)
    with game.open('rb') as f:
        kind, entries = container_entries(f, game.stat().st_size)
        tickets = common_tickets(f, entries)
        record, hdr = program_nca(nca_inventory(f, entries, keys), update=update)
        ctr_key = nca_ctr_key(hdr, keys, tickets)
        for i in range(4):
            start, end = struct.unpack_from('<II', hdr, 0x240 + 16 * i)
            if not start:
                continue
            fs = hdr[0x400 + 512 * i:0x600 + 512 * i]
            require(hashlib.sha256(fs).digest() == hdr[0x280 + 32 * i:0x2A0 + 32 * i], 'FS header hash mismatch')
            if fs[2:4] != b'\x01\x02':
                continue
            require(fs[4] == 3, 'Only AES-CTR ExeFS is supported')
            blocksize, levels = struct.unpack_from('<II', fs, 0x28)
            hashoff, hashsize, pfsoff, pfssize = struct.unpack_from('<QQQQ', fs, 0x30)
            secstart, secsize = start * 512, (end - start) * 512
            require(levels == 2 and blocksize > 0 and pfssize <= 512 * 1024 * 1024, 'Unsupported ExeFS hash layout')

            def section_read(relative, size):
                require(relative >= 0 and relative + size <= secsize, 'Section read out of bounds')
                absolute = secstart + relative
                aligned = absolute & ~15
                iv = fs[0x140:0x148][::-1] + (aligned // 16).to_bytes(8, 'big')
                data = read_at(f, record['nca_offset'] + aligned, size + absolute - aligned)
                return decrypt(ctr_key, modes.CTR(iv), data)[absolute - aligned:]

            hashes = section_read(hashoff, hashsize)
            require(hashlib.sha256(hashes).digest() == fs[8:40], 'ExeFS hash-table mismatch')
            pfs = section_read(pfsoff, pfssize)
            for index, pos in enumerate(range(0, pfssize, blocksize)):
                expected = hashes[index * 32:(index + 1) * 32]
                require(hashlib.sha256(pfs[pos:pos + blocksize]).digest() == expected,
                        f'ExeFS block hash mismatch: {index}')
            files = [(name, pfs[off:off + size]) for name, off, size in
                     partition_entries(io.BytesIO(pfs), 0, len(pfs))]
            require(any(name == 'main.npdm' for name, _ in files), 'PFS0 is not ExeFS')
            output.mkdir(parents=True, exist_ok=True)
            extracted = []
            for name, data in files:
                (output / name).write_bytes(data)
                extracted.append({'name': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
            manifest = {'source': str(game.resolve()), 'source_size': game.stat().st_size,
                        'source_mtime_ns': game.stat().st_mtime_ns, 'container': kind,
                        'update': update,
                        'nca_inventory': [r for r, _ in nca_inventory(f, entries, keys)], 'program': record,
                        'section_index': i,
                        'integrity': 'FS header, ExeFS hash table, and every ExeFS data block verified; RSA signatures not checked',
                        'files': extracted}
            (output.parent / 'extraction-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
            return manifest
    raise ValueError('No ExeFS section found')


def extract_romfs(game, keys, manifest, output):
    """Verify all six IVFC levels, stream the RomFS image, and index its files."""
    require(not output.exists(), 'RomFS output already exists')
    keys = load_keys(keys)
    record = manifest['program']
    with game.open('rb') as f:
        _, entries = container_entries(f, game.stat().st_size)
        actual, hdr = program_nca(nca_inventory(f, entries, keys))
        require(actual['name'] == record['name'] and actual['size'] == record['size'] and
                actual['nca_offset'] == record['nca_offset'], 'Source Program NCA changed; extract ExeFS again')
        ctr_key = nca_ctr_key(hdr, keys, common_tickets(f, entries))
        for i in range(4):
            start, end = struct.unpack_from('<II', hdr, 0x240 + i * 16)
            if not start:
                continue
            fs = hdr[0x400 + 512 * i:0x600 + 512 * i]
            require(hashlib.sha256(fs).digest() == hdr[0x280 + 32 * i:0x2A0 + 32 * i], 'FS header hash')
            if fs[2:5] != bytes([0, 3, 3]):
                continue
            require(fs[8:12] == b'IVFC' and struct.unpack_from('<III', fs, 12) == (0x20000, 32, 7),
                    'Unsupported IVFC layout')
            levels = [struct.unpack_from('<QQII', fs, 0x18 + j * 24) for j in range(6)]

            def read(relative, size):
                require(0 <= relative <= relative + size <= (end - start) * 512, 'Section bounds')
                absolute = start * 512 + relative
                aligned = absolute & ~15
                skip = absolute - aligned
                iv = fs[0x140:0x148][::-1] + (aligned // 16).to_bytes(8, 'big')
                return decrypt(ctr_key, modes.CTR(iv),
                               read_at(f, record['nca_offset'] + aligned, size + skip))[skip:]

            output.mkdir(parents=True)
            image_path = output / 'romfs.bin'
            digest = hashlib.sha256()
            counts = []
            parent = fs[0xc8:0xe8]
            with image_path.open('wb') as image:
                for level, (offset, size, power, reserved) in enumerate(levels):
                    require(12 <= power <= 24 and reserved == 0, 'IVFC block size')
                    block = 1 << power
                    require(len(parent) >= (size + block - 1) // block * 32, 'IVFC parent size')
                    captured = bytearray() if level < 5 else None
                    index = 0
                    for pos in range(0, size, 8 * 1024 * 1024):
                        chunk = read(offset + pos, min(8 * 1024 * 1024, size - pos))
                        for j in range(0, len(chunk), block):
                            payload = chunk[j:j + block]
                            require(hashlib.sha256(payload + bytes(block - len(payload))).digest()
                                    == parent[index * 32:index * 32 + 32], f'IVFC level {level} block {index} failed')
                            index += 1
                        if captured is not None:
                            captured.extend(chunk)
                        else:
                            image.write(chunk)
                            digest.update(chunk)
                    counts.append(index)
                    if captured is not None:
                        parent = bytes(captured)
                    print(f'IVFC level {level}: {index} blocks verified', flush=True)
            files = index_romfs(image_path)
            report = dict(status='pass', source_size=game.stat().st_size, program=record['name'], section=i,
                          ivfc_blocks=counts, romfs_size=levels[-1][1], romfs_sha256=digest.hexdigest(),
                          files=files,
                          integrity='FS header and all six IVFC levels verified; RSA signature not checked')
            (output / 'index.json').write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(dict(status='pass', file_count=len(files), size=levels[-1][1],
                                  sha256=digest.hexdigest())))
            return report
    raise ValueError('No supported RomFS found')


def index_romfs(path):
    with path.open('rb') as f:
        h = struct.unpack('<10Q', read_at(f, 0, 80))
        require(h[0] == 80, 'RomFS header')
        length = path.stat().st_size
        for off, size in [(h[1], h[2]), (h[3], h[4]), (h[5], h[6]), (h[7], h[8])]:
            require(off + size <= length, 'RomFS table bounds')
        dirs = read_at(f, h[3], h[4])
        entries = read_at(f, h[7], h[8])
        files = []
        seen_dirs = set()
        seen_files = set()
        paths = set()

        def name(table, off, n):
            require(off + n <= len(table), 'Name bounds')
            s = table[off:off + n].decode('utf-8')
            require(s not in ['.', '..'] and '/' not in s and '\\' not in s and '\0' not in s, 'Unsafe path')
            return s

        todo = [(0, '')]
        while todo:
            off, parent = todo.pop()
            require(off not in seen_dirs and off + 24 <= len(dirs), 'Invalid/cyclic directory')
            seen_dirs.add(off)
            p, sibling, child, file, hash_next, n = struct.unpack_from('<6I', dirs, off)
            part = name(dirs, off + 24, n)
            prefix = parent + part + '/' if part else parent
            if sibling != 0xffffffff:
                todo.append((sibling, parent))
            if child != 0xffffffff:
                todo.append((child, prefix))
            while file != 0xffffffff:
                require(file not in seen_files and file + 32 <= len(entries), 'Invalid/cyclic file')
                seen_files.add(file)
                p, next_file, data, size, hash_next, n = struct.unpack_from('<IIQQII', entries, file)
                part = name(entries, file + 32, n)
                require(part != '', 'Empty file name')
                filename = prefix + part
                require(filename not in paths and h[9] + data + size <= length, 'Duplicate/out-of-bounds file')
                paths.add(filename)
                files.append(dict(path=filename, offset=h[9] + data, size=size))
                file = next_file
        return sorted(files, key=lambda item: item['path'])


# The ExeFS files that identify the game build. main.npdm is the process's metadata (thread priority, stack size,
# service access), not code: dumps of the same build carry different ones (repacked NSPs, different dumpers), and the
# runtime checks the fields it uses at start (runtime/boot.c check_npdm). Comparing it would refuse a good dump.
VERSION_FILES = ('main', 'sdk', 'rtld')


# ---- update RomFS: the base Program NCA and the update's BKTR section merged by hactool
#
# The BKTR reader is MacNativeGames' hactool patch (tools/hactool/portable.patch). Its base-range hook is macOS-only
# and left out of the Windows build, so the base Program NCA is copied out of its NSP first (about 10 GB), as the
# TotK Recomp installer did.

class DamagedFile(ValueError):
    """A game file whose data fails its hash checks or ends early. path: that file; detail: the check that failed."""

    def __init__(self, path, detail):
        self.path, self.detail = Path(path), detail
        super().__init__(f'{self.path} is damaged or incomplete ({detail}). Copy that file again from its source '
                         f'(or dump it again) and retry.')


# hactool checks three RSA signatures: the NCA header's fixed-key signature, the NPDM signature and the ACID
# signature inside main.npdm (printed indented as `Signature`, under ACID). Any of them can read (FAIL) on a good dump:
# hactool lacks some of Nintendo's public keys, and repacked or converted dumps carry another NCA header or main.npdm
# (the ExeFS check tolerates that main.npdm, see VERSION_FILES). The data itself is verified by the IVFC hash levels
# and the merged image's SHA-256, so these lines are not errors.
UNSIGNED = re.compile(r'\s*(?:Fixed-Key Signature|NPDM Signature|Signature) \(FAIL\)')


def hactool_checked(log):
    """hactool's output without the signature lines UNSIGNED names: what the error and hash checks read."""
    return '\n'.join(line for line in log.splitlines() if not UNSIGNED.match(line))
HACTOOL_ERROR = re.compile(r'\(FAIL\)|\(INVALID\)|Failed to|Error:')
HACTOOL_HASH_FAIL = re.compile(r'^\s*(?:Level \d|Superblock Hash|Hash Table) \(FAIL\)', re.M)
LONG_HEX = re.compile(r'[0-9A-Fa-f]{32,}')
# Lockpick_RCM writes these as 17 bytes: the 16-byte source plus a trailing NUL. hactool exits unless the value is
# exactly 16 bytes, before any BKTR check.
LOCKPICK_KEK_SOURCE = re.compile(r'(?:mariko_)?master_kek_source_[0-9a-f]{2}\Z')


def copy_range(source, offset, size, target):
    """Copy size bytes at offset into target (from MacNativeGames extract_update_romfs.py)."""
    with source.open('rb') as src, target.open('wb') as dst:
        src.seek(offset)
        while size:
            data = src.read(min(size, 8 << 20))
            require(bool(data), 'Truncated NCA')
            dst.write(data)
            size -= len(data)


def hactool_prod_keys(source, target):
    """Copy prod.keys for hactool (from MacNativeGames extract_update_romfs.py).

    hactool knows no comments: it reads the rest of a line as the value, so a trailing `# note` makes it exit. The copy
    keeps only the `name = value` lines. A Lockpick trailing NUL on master_kek_source / mariko_master_kek_source is
    dropped. Any other length is rejected without recording the key bytes.
    """
    lines = []
    for number, line in enumerate(Path(source).read_text(encoding='utf-8').splitlines(), 1):
        body = line.split('#', 1)[0]
        if '=' not in body:
            continue
        name, value = (part.strip() for part in body.split('=', 1))
        if LOCKPICK_KEK_SOURCE.fullmatch(name.lower()) is not None:
            require(re.fullmatch(r'[0-9a-fA-F]+', value) is not None, f'Invalid key encoding at line {number}')
            if len(value) == 34 and value[-2:].lower() == '00':
                value = value[:32]
            require(len(value) == 32, f'{name.lower()} must be 16 bytes')
        lines.append(f'{name} = {value}')
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write('\n'.join(lines) + ('\n' if lines else ''))


def program_ncas(base, update, keys_path):
    """(tickets, [(source, record, header)] of the base and the update Program NCA)."""
    keys = load_keys(keys_path)
    tickets, inputs = {}, []
    for source, is_update in ((base, False), (update, True)):
        with source.open('rb') as f:
            kind, entries = container_entries(f, source.stat().st_size)
            require(not is_update or kind == 'nsp', 'Update must be an NSP')
            for rights, key in common_tickets(f, entries).items():
                require(tickets.get(rights, key) == key, 'Conflicting common tickets')
                tickets[rights] = key
            record, header = program_nca(nca_inventory(f, entries, keys), update=is_update)
            rights = header[0x230:0x240]
            require(rights == bytes(16) or rights in tickets, 'Missing common ticket')
            inputs.append((source, record, header))
    return tickets, inputs


def merge_space(base, update, keys_path):
    """Bytes merge_update_romfs copies next to its output beyond the merged image: both Program NCAs."""
    _, inputs = program_ncas(base, update, keys_path)
    return inputs[0][1]['size'] + inputs[1][1]['size']


def run_hactool(command, environment):
    """(exit code, stdout and stderr with long hex strings removed). hactool prints UTF-8."""
    completed = subprocess.run(command, env=environment, capture_output=True, encoding='utf-8', errors='replace')
    return completed.returncode, LONG_HEX.sub('<hex removed>', completed.stdout + completed.stderr)


def damaged_merge_input(hactool, hactool_keys, environment, base, update, base_nca, log_path):
    """The DamagedFile for a merge whose hash checks failed. The merged RomFS mixes data of both files, so hactool
    verifies the base Program NCA alone: if it fails, the base game file is damaged, else the update file is."""
    code, log = run_hactool([str(hactool), '-y', '--disablekeywarns', '--suppresskeys', '-k', str(hactool_keys),
                             str(base_nca)], environment)
    with log_path.open('a', encoding='utf-8') as f:
        f.write(f'\n---- base Program NCA alone (exit code {code}) ----\n{log}')
    checked = hactool_checked(log)
    if HACTOOL_HASH_FAIL.search(checked):
        return DamagedFile(base, f'its game data fails the hash check; see {log_path}')
    if all(re.search(rf'Level {i} \(GOOD\)', log) for i in range(6)):
        return DamagedFile(update, f'its game data fails the hash check; see {log_path}')
    return ValueError(f'the game data fails the hash check: the base game file {base} or the update file {update} '
                      f'is damaged or incomplete. Copy them again from their source and retry; see {log_path}')


def merge_update_romfs(base, update, keys_path, hactool, output, logs, expected_size, phase=lambda name: None):
    """Merge the base Program NCA with the update's BKTR section and write output/ (romfs.bin, index.json).

    Needs room for both Program NCAs and the merged image next to output: base and update are copied into
    update-merge-* (hactool's Windows build cannot read the base NCA inside its NSP). phase(name) is called as each
    part starts. Raises DamagedFile when one of the two files fails its hashes.
    """
    if output.exists():
        shutil.rmtree(output)
    tickets, inputs = program_ncas(base, update, keys_path)
    sections = [i for i in range(4) if inputs[1][2][0x400 + i * 512 + 4] == 4]
    require(len(sections) == 1, 'Expected one BKTR section')
    need = inputs[0][1]['size'] + inputs[1][1]['size'] + expected_size
    require(shutil.disk_usage(output.parent).free > need,
            f'Need {need / (1 << 30):.0f} GiB free in {output.parent}')
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f'merge_romfs-{output.parent.name}.log'
    with tempfile.TemporaryDirectory(prefix='update-merge-', dir=output.parent) as tmp:
        tmp = Path(tmp)
        title_keys = tmp / 'title.keys'
        fd = os.open(title_keys, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as f:
            for rights, key in tickets.items():
                f.write(f'{rights.hex()} = {key.hex()}\n')
        hactool_keys = tmp / 'hactool.keys'
        hactool_prod_keys(keys_path, hactool_keys)
        phase('copy')
        base_nca, update_nca = tmp / 'base.nca', tmp / 'update.nca'
        for (source, record, _), target in zip(inputs, (base_nca, update_nca)):
            try:
                copy_range(source, record['nca_offset'], record['size'], target)
            except ValueError as e:   # the file ends inside its Program NCA
                raise DamagedFile(source, str(e)) from None
        result = tmp / 'result'
        result.mkdir()
        phase('merge')
        environment = dict(os.environ, MNG_TITLE_KEYS=str(title_keys))
        command = [str(hactool), '-x', '-y', '--disablekeywarns', '--suppresskeys', '-k', str(hactool_keys),
                   f'--basenca={base_nca}', f'--section{sections[0]}={result / "romfs.bin"}', str(update_nca)]
        code, log = run_hactool(command, environment)
        log_path.write_text(log, encoding='utf-8')
        checked = hactool_checked(log)
        if HACTOOL_HASH_FAIL.search(checked):
            phase('diagnose')
            raise damaged_merge_input(hactool, hactool_keys, environment, base, update, base_nca, log_path)
        require(code == 0 and not HACTOOL_ERROR.search(checked), f'hactool merge failed; see {log_path}')
        require(all(re.search(rf'Level {i} \(GOOD\)', log) for i in range(6)),
                'hactool did not verify all six IVFC levels')
        image = result / 'romfs.bin'
        phase('index')
        files = index_romfs(image)
        report = dict(status='pass', romfs_size=image.stat().st_size, romfs_sha256=sha256(image), files=files,
                      integrity='BKTR merge; all six IVFC levels verified')
        (result / 'index.json').write_text(json.dumps(report, indent=2) + '\n')
        result.rename(output)
        return report


def identify_update(exefs, versions):
    """The expected data of the update whose `main` hash this ExeFS has."""
    digest = sha256(exefs / 'main')
    for expected in versions.values():
        if expected.get('version') and expected['exefs_sha256'].get('main') == digest:
            return expected
    raise ValueError(f'Update main {digest[:16]}... matches no supported version '
                     f'({", ".join(v for v, e in versions.items() if e.get("version"))})')


def exefs_ok(exefs_dir, expected):
    """Whether the extracted ExeFS matches the expected hashes of the code modules; (ok, problems) where problems is the
    list of differing names (or missing names when the expected file is absent)."""
    if not (exefs_dir / 'main.npdm').exists():
        return False, ['missing ExeFS']
    problems = []
    for name, want in expected.get('exefs_sha256', {}).items():
        if name not in VERSION_FILES:
            continue
        path = exefs_dir / name
        if not path.exists() or sha256(path) != want:
            problems.append(name)
    return not problems, problems


def update_expected(display_version, version, manifest, romfs_report):
    """Write the measured hashes into tools/data/expected-v<display_version>.json (first run on a new dump)."""
    path = DATA / f'expected-v{display_version}.json'
    exefs = {f['name']: f['sha256'] for f in manifest['files']}
    expected = {
        '_comment': f'Hashes of what the build produces from a genuine Astral Chain {display_version} (no game data). '
                    'tools/extract.py checks every step against them; a mismatch stops the build.',
        'title_id': GAME_TITLE_ID,
        'version': version,
        'display_version': display_version,
        'program_nca': manifest['program']['name'],
        'exefs_sha256': exefs,
        'romfs': {'size': romfs_report['romfs_size'], 'sha256': romfs_report['romfs_sha256']},
    }
    path.write_text(json.dumps(expected, indent=2) + '\n')


def check_expected(display_version, manifest, romfs_report):
    expected = load_expected().get(display_version)
    if not expected:
        version = 0 if display_version == '1.0.0' else int(display_version.replace('.', ''))
        update_expected(display_version, version, manifest, romfs_report)
        print(f'expected-v{display_version}.json: written from this dump (ExeFS: {len(manifest["files"])} files, '
              f'RomFS {romfs_report["romfs_size"]} bytes)')
        return
    problems = []
    for name, want in expected.get('exefs_sha256', {}).items():
        if name not in VERSION_FILES:
            continue
        got = next((f['sha256'] for f in manifest['files'] if f['name'] == name), None)
        if got != want:
            problems.append(name)
    want_romfs = expected.get('romfs', {})
    if want_romfs.get('size') != romfs_report['romfs_size'] or \
            want_romfs.get('sha256') != romfs_report['romfs_sha256']:
        problems.append('romfs')
    require(not problems, f'Extracted files differ from expected-v{display_version}.json: {", ".join(problems)}. '
                          f'Copy the game files again from their source and retry.')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', type=Path, default=ROOT / 'config.local.toml')
    parser.add_argument('--base', type=Path, help='base game NSP (default: input.base of the config)')
    parser.add_argument('--update', type=Path, help='update NSP, 1.0.1 (default: input.update of the config)')
    parser.add_argument('--keys', type=Path, help='prod.keys (default: input.keys of the config)')
    parser.add_argument('--hactool', type=Path,
                        default=ROOT / 'toolchain' / 'hactool' / ('hactool.exe' if os.name == 'nt' else 'hactool'),
                        help='the hactool build with the BKTR reader (tools/build_hactool.py)')
    parser.add_argument('--work', type=Path, help='output directory with extracted/ (default: work.dir of the '
                                                  'config, else build)')
    args = parser.parse_args()

    config = {}
    if not (args.base and args.update and args.keys and args.work):
        config = tomllib.loads(args.config.read_text(encoding='utf-8'))
    given = config.get('input', {})
    base = args.base or Path(given['base'])
    update = args.update or Path(given.get('update', ''))
    keys = args.keys or Path(given['keys'])
    work = args.work or ROOT / config.get('work', {}).get('dir', 'build')
    require(base.is_file(), f'Base game not found: {base}')
    require(update.is_file(), f'Update not found: {update} (set input.update in the config or pass --update)')
    require(keys.is_file(), f'Keys file not found: {keys}')
    require(args.hactool.exists(), f'hactool not found: {args.hactool} (run tools/build_hactool.py)')

    versions = load_expected()
    base_expected = versions['1.0.0']
    logs = work / 'logs'
    extracted = work / 'extracted'

    print(f'Base game: {base.name}')
    base_out = extracted / 'v0'
    if base_out.exists() and not exefs_ok(base_out / 'exefs', base_expected)[0]:
        shutil.rmtree(base_out)
    if not (base_out / 'exefs').is_dir():
        manifest = extract_exefs(base, keys, base_out / 'exefs')
        print('ExeFS extracted:', ', '.join(f['name'] for f in manifest['files']))
    ok, problems = exefs_ok(base_out / 'exefs', base_expected)
    require(ok, f'Base game is not Astral Chain 1.0.0: {", ".join(problems)} differ')
    if sha256(base_out / 'exefs' / 'main.npdm') != base_expected['exefs_sha256']['main.npdm']:
        print('  main.npdm differs from the reference dump; only the code modules are used')

    print(f'Update: {update.name}')
    update_out = extracted / 'v101'
    if (update_out / 'exefs').is_dir():
        try:
            known = identify_update(update_out / 'exefs', versions)
        except ValueError:
            known = None
        if known is not None and not exefs_ok(update_out / 'exefs', known)[0]:
            shutil.rmtree(update_out)
    manifest_path = update_out / 'extraction-manifest.json'
    if not (update_out / 'exefs').is_dir() or not manifest_path.is_file():
        shutil.rmtree(update_out, ignore_errors=True)
        manifest = extract_exefs(update, keys, update_out / 'exefs', update=True)
        print('ExeFS extracted:', ', '.join(f['name'] for f in manifest['files']))
    else:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    try:
        expected = identify_update(update_out / 'exefs', versions)
    except ValueError:
        expected = None
    bootstrap = expected is None
    if bootstrap:
        # No tools/data/expected-v*.json for this update yet: measure this dump, and check_expected writes the file
        # after the merge. Later runs identify the update by its main hash and stop on any difference.
        display = '1.0.1'
        expected = {'display_version': display, 'version': int(display.replace('.', '')),
                    'exefs_sha256': {f['name']: f['sha256'] for f in manifest['files']},
                    'romfs': {'size': 11 << 30, 'sha256': None}}
        print('  no expected hashes for this update yet; measuring this dump')
    else:
        ok, problems = exefs_ok(update_out / 'exefs', expected)
        require(ok, f'Update {expected["display_version"]}: {", ".join(problems)} differ')
        print(f'  ExeFS {expected["display_version"]}: main, sdk, rtld match')
        if sha256(update_out / 'exefs' / 'main.npdm') != expected['exefs_sha256']['main.npdm']:
            print('  main.npdm differs from the reference dump; only the code modules are used')

    romfs = update_out / 'romfs'
    report = None
    if not bootstrap and (romfs / 'romfs.bin').is_file() and (romfs / 'index.json').is_file():
        report = json.loads((romfs / 'index.json').read_text(encoding='utf-8'))
        if (report.get('romfs_size') != expected['romfs']['size']
                or report.get('romfs_sha256') != expected['romfs']['sha256']):
            report = None
    if report is None:
        print('Merging RomFS with the update (hactool; about 21 GiB free next to it)')
        report = merge_update_romfs(base, update, keys, args.hactool, romfs, logs,
                                    expected['romfs']['size'],
                                    phase=lambda name: print(f'  {name}'))
    if not bootstrap:
        require(report['romfs_size'] == expected['romfs']['size'] and
                report['romfs_sha256'] == expected['romfs']['sha256'],
                f'Merged RomFS does not match {expected["display_version"]}: size {report["romfs_size"]}, '
                f'sha256 {report["romfs_sha256"][:16]}...')

    check_expected(expected['display_version'], manifest, report)
    print(f'OK: ExeFS and RomFS verified for Astral Chain {expected["display_version"]} ({GAME_TITLE_ID})')


if __name__ == '__main__':
    try:
        main()
    except ValueError as error:
        sys.exit(f'error: {error}')
