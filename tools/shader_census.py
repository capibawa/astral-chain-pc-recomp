"""Census of every Maxwell shader in the extracted Astral Chain RomFS.

Astral Chain keeps its shaders in root.pkz, in six "shader .dat" entries: shader.dat, shader2.dat,
autoshadereff.dat, shadereff.dat, shadereffcs.dat and shaderefflayout.dat. Each is a PlatinumGames "DAT"
container (magic b"DAT\\0") whose entry table names VSO/PSO/CSO objects (vertex / pixel / compute shaders; the
engine's HSO/DSO tessellation objects do not occur in this game). An object is a 0x18-byte header (magic, a
name hash, the control-section size, the control offset 0x18, a code field, the code offset) followed by the
NVN control section and the code. The control section the game hands NVN is 0x880 bytes; the object stores its
first part and the code starts inside that span, so the census reads 0x880 bytes from the object's control
offset and the code separately from the object's code offset (the two overlap, as the game's own buffers do).

Reads build/extracted/<version>/ (RomFS image, or the romfs-map.bin that reads it from the dump files, plus
index.json as tools/romfs_index.py reads them). Writes under
build/<version>/shaders/ only:

    code/<key>.code     translator input (graphics: SPH + instructions, compute: instructions)
    jobs.json           key -> job, source path, origin (vso|pso|cso)
    spv/<key>.spv       with --translate
    failures.txt        with --translate, when something failed: groups by message

The key is sha256(control[:0x880] + code[:length]); the runtime's own code hash (sha256 over stage, the 0x880
control section, code size, code and c1) is what tools/prepare_shaders.py joins the seed answers on, so
inventory() returns shaders keyed by it. Needs Python 3.14 (compression.zstd), so run in .venv-py314:

    .venv-py314\\Scripts\\python tools\\shader_census.py --version v101 --translate
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

import compression.zstd as zstd

ROOT = Path(__file__).resolve().parent.parent
XLATE = ROOT / 'build' / 'xlate' / ('ac_xlate.exe' if os.name == 'nt' else 'ac_xlate')
SPIRV_VAL = Path(r'C:\msys64\ucrt64\bin\spirv-val.exe') if os.name == 'nt' else Path(shutil.which('spirv-val') or '/usr/bin/spirv-val')
CONTROL = 0x880                                     # runtime/gfx_shader.c CONTROL_SIZE
CONTROL_MAGIC = b'\x34\x12\x76\x98'                 # NVN control section magic, u32 0x98761234
STAGE = {b'VSO\x00': 0, b'PSO\x00': 4, b'CSO\x00': 5}   # GFX_STAGE_* (runtime/gfx.h): VS, FS, CS
STAGE_IDS = {0: 'vs', 4: 'ps', 5: 'cs'}
SHADER_DATS = ('shader.dat', 'shader2.dat', 'autoshadereff.dat', 'shadereff.dat', 'shadereffcs.dat',
               'shaderefflayout.dat')
MAX_WORKERS = 20


def digest(data):
    return hashlib.sha256(data).hexdigest()


def code_hash(stage, control, length, code, c1):
    """runtime/gfx_shader.c build_code_hash: the hash seed answers reference a program by."""
    return hashlib.sha256(struct.pack('<I', stage) + control + struct.pack('<Q', length) + code[:length]
                          + struct.pack('<I', len(c1)) + c1).digest()


def pkz_entries(data):
    """(name, decompressed bytes) of every entry of a pkz archive (PlatinumGames, magic b"pkzl")."""
    if data[:4] != b'pkzl':
        raise ValueError('not a pkz archive')
    items = struct.unpack_from('<I', data, 0x10)[0]
    entry_off = struct.unpack_from('<I', data, 0x14)[0]
    name_table = entry_off + items * 0x20
    for i in range(items):
        p = entry_off + i * 0x20
        name_off, _, size, _, offset, _, zsize, _ = struct.unpack_from('<8I', data, p)
        end = data.index(0, name_table + name_off)
        name = data[name_table + name_off:end].decode('utf-8', 'replace')
        yield name, zstd.decompress(data[offset:offset + zsize])


def dat_objects(data):
    """(offset, magic, control bytes, code bytes) of every VSO/PSO/CSO object in a DAT container."""
    if data[:4] != b'DAT\x00':
        return
    count = struct.unpack_from('<I', data, 4)[0]
    table = struct.unpack_from('<I', data, 8)[0]
    for i in range(count):
        obj = struct.unpack_from('<I', data, table + i * 4)[0]
        magic = data[obj:obj + 4]
        if magic not in STAGE:
            continue
        ctrl_off = struct.unpack_from('<I', data, obj + 0x0c)[0]
        code_off = struct.unpack_from('<I', data, obj + 0x14)[0]
        control = data[obj + ctrl_off:obj + ctrl_off + CONTROL]
        length = struct.unpack_from('<I', control, 0x704)[0]
        c1_off = struct.unpack_from('<I', control, 0x700)[0]
        code = data[obj + code_off:obj + code_off + length]
        c1 = code[c1_off:length] if c1_off <= length else b''
        yield magic, control, code, length, c1


def open_image(romfs):
    """The RomFS image: romfs.bin, else romfs-map.bin reading it from the dump files. tools/romfs_source.py (and its
    cryptography package) is imported only for the map, so a romfs.bin build runs without that package."""
    image = Path(romfs) / 'romfs.bin'
    if image.is_file():
        return image.open('rb')
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import romfs_source
    return romfs_source.open_image(romfs)


def inventory(romfs, exefs=None):
    """Every unique shader, keyed by the runtime code hash: {code_hash: dict(stage, control, code, length, c1, src)}."""
    index = json.loads((Path(romfs) / 'index.json').read_text(encoding='utf-8'))
    if index.get('status') != 'pass':
        raise ValueError('RomFS has not passed integrity verification')
    entries = {r['path']: r for r in index['files']}
    shaders = {}
    with open_image(romfs) as stream:
        row = entries.get('root.pkz')
        if not row:
            return shaders
        stream.seek(row['offset'])
        raw = stream.read(row['size'])
        for name, dat in pkz_entries(raw):
            if name not in SHADER_DATS:
                continue
            if dat[:4] != b'DAT\x00' or CONTROL_MAGIC not in dat:
                continue
            for magic, control, code, length, c1 in dat_objects(dat):
                h = code_hash(STAGE[magic], control, length, code, c1)
                if h not in shaders:
                    shaders[h] = dict(stage=STAGE_IDS[STAGE[magic]], control=control, code=code,
                                      length=length, c1=c1, src=name)
    return shaders


def census_key(s):
    """The jobs.json key: sha256(control[:0x880] + code[:length])."""
    return hashlib.sha256(s['control'] + s['code'][:s['length']]).hexdigest()


def write_shaders(shaders, out):
    """Write code files and jobs.json; returns {census key: dict(job, source, origin)}."""
    (out / 'code').mkdir(parents=True, exist_ok=True)
    jobs = {}
    for h, s in sorted(shaders.items(), key=lambda kv: census_key(kv[1])):
        key = census_key(s)
        skip = 0 if s['stage'] == 'cs' else 0x30
        body = s['code'][skip:s['length']]
        path = out / 'code' / f'{key}.code'
        if not path.is_file() or path.read_bytes() != body:
            path.write_bytes(body)
        job = dict(key=key, stage=s['stage'], code=str(path), c1=s['c1'].hex(), debug=False)
        if s['stage'] == 'cs':
            job['local'] = list(struct.unpack_from('<III', s['control'], 0x748))
        jobs[key] = dict(job=job, source=s['src'],
                           origin={'vs': 'vso', 'ps': 'pso', 'cs': 'cso'}[s['stage']])
    (out / 'jobs.json').write_text(json.dumps(jobs, indent=0), encoding='utf-8')
    return jobs


def worker(keys, jobs, spv_dir):
    """One --serve process for a slice of keys. Returns (translated, failures {key: message}, errors {key: message})."""
    failures, errors, ok = {}, {}, 0
    process = subprocess.Popen([str(XLATE), '--serve'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True)
    try:
        for key in keys:
            process.stdin.write(json.dumps({'job': jobs[key]['job'], 'out': str(spv_dir)}) + '\n')
            process.stdin.flush()
            reply = process.stdout.readline().rstrip('\n')
            if not reply:
                failures[key] = 'translator service exited'
                break
            if not reply.startswith('ok '):
                failures[key] = reply.split(' ', 2)[2] if reply.count(' ') >= 2 else reply
                continue
            v = subprocess.run([str(SPIRV_VAL), '--target-env', 'vulkan1.3', str(spv_dir / f'{key}.spv')],
                               capture_output=True, text=True)
            if v.returncode:
                errors[key] = (v.stderr or v.stdout).strip().splitlines()[0] if (v.stderr or v.stdout) else 'spirv-val failed'
            else:
                ok += 1
    finally:
        process.stdin.close()
        process.wait()
    return ok, failures, errors


def normalize(message):
    return re.sub(r'\b\d+\b|0x[0-9a-fA-F]+', 'N', message)[:300]


def translate(jobs, out):
    spv_dir = out / 'spv'
    spv_dir.mkdir(exist_ok=True)
    keys = sorted(jobs)
    count = max(1, min(MAX_WORKERS, len(keys)))
    slices = [keys[i::count] for i in range(count)]
    ok, failures, errors = 0, {}, {}
    with concurrent.futures.ThreadPoolExecutor(count) as pool:
        for n, f, e in pool.map(lambda s: worker(s, jobs, spv_dir), slices):
            ok += n
            failures.update(f)
            errors.update(e)
    if failures or errors:
        lines = []
        for title, table in (('translate failures', failures), ('spirv-val errors', errors)):
            groups = defaultdict(list)
            for key, message in table.items():
                groups[normalize(message)].append((key, message))
            lines.append(f'== {title}: {len(table)} in {len(groups)} groups')
            for _, members in sorted(groups.items(), key=lambda g: -len(g[1])):
                lines.append(f'{len(members)}\t{members[0][1][:300]}')
                lines.extend(f'\texample {key}' for key, _ in sorted(members)[:2])
        (out / 'failures.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    else:
        (out / 'failures.txt').unlink(missing_ok=True)
    return ok, len(failures), len(errors)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--version', required=True, help='v101 (build/extracted/<version>)')
    ap.add_argument('--romfs', type=Path, help='RomFS directory (default build/extracted/<version>/romfs)')
    ap.add_argument('--exefs', type=Path, help='ExeFS directory (unused: no shaders are embedded there)')
    ap.add_argument('--out', type=Path, help='output directory (default build/<version>/shaders)')
    ap.add_argument('--translate', action='store_true', help='translate every shader and run spirv-val')
    args = ap.parse_args()
    t0 = time.monotonic()
    romfs = args.romfs or ROOT / 'build' / 'extracted' / args.version / 'romfs'
    out = args.out or ROOT / 'build' / args.version / 'shaders'
    out.mkdir(parents=True, exist_ok=True)
    shaders = inventory(romfs)
    jobs = write_shaders(shaders, out)
    print(f'{len(shaders)} unique shaders found ({sum(1 for s in shaders.values() if s["stage"] == "vs")} VS, '
          f'{sum(1 for s in shaders.values() if s["stage"] == "ps")} PS, '
          f'{sum(1 for s in shaders.values() if s["stage"] == "cs")} CS); '
          f'{len(jobs)} written, {time.monotonic() - t0:.1f} s', flush=True)
    if not args.translate:
        return 0
    if not XLATE.is_file() or not SPIRV_VAL.is_file():
        sys.exit(f'missing {XLATE} or {SPIRV_VAL}')
    ok, failed, bad = translate(jobs, out)
    print(f'translated {ok}, failed {failed}, spirv-val errors {bad}, total {time.monotonic() - t0:.1f} s')
    return 0


if __name__ == '__main__':
    sys.exit(main())
