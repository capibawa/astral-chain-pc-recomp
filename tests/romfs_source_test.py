"""tools/romfs_source.py on a synthetic dump: a title-key encrypted "NCA" with a ticket, a keys file and a map whose
extents switch file, counter half and alignment, the way a BKTR map does. Checks that MappedImage reads back the
plaintext at any offset and length, that the start checks catch another keys file or a changed file, and that the
installer accepts and refuses romfs/ folders in the dumps form. No game files; needs the cryptography package.

Run: python tests/romfs_source_test.py
"""
import hashlib
import io
import json
import os
import random
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tools'))
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402

import romfs_source as rs  # noqa: E402
from installer import steps  # noqa: E402
from installer.events import Reporter  # noqa: E402

NCA_OFFSET = 0x1000   # where the fake NCA starts in the fake dump file
TICKET_KEY = 0x400    # where the ticket's encrypted title key sits


def ctr(key, high, rel, data):
    """data encrypted at NCA-relative offset rel with counter high || rel / 16 (CTR is its own inverse)."""
    aligned = rel & ~15
    stream = Cipher(algorithms.AES(key), modes.CTR(high + (aligned // 16).to_bytes(8, 'big'))).encryptor()
    return stream.update(bytes(rel - aligned) + data)[rel - aligned:]


class Fixture:
    """A dump file of two "sources" (the same file twice, different key halves), its keys file and a map."""

    def __init__(self, folder, image_size=300_000, seed=7):
        rng = random.Random(seed)
        self.folder = Path(folder)
        self.image = bytes(rng.getrandbits(8) for _ in range(image_size))
        kek, title = os.urandom(16), os.urandom(16)
        self.keys = self.folder / 'prod.keys'
        self.keys.write_text(f'titlekek_00 = {kek.hex()}\n# a comment\nheader_key = {"00" * 32}\n')
        enc_title = Cipher(algorithms.AES(kek), modes.ECB()).encryptor().update(title)
        # Cut the image into extents of odd sizes; each goes to an NCA-relative offset with its own counter half.
        dump = bytearray(NCA_OFFSET + 0xc00 + image_size + 64 * 1024)
        dump[TICKET_KEY:TICKET_KEY + 16] = enc_title
        dump[NCA_OFFSET:NCA_OFFSET + 0xc00] = os.urandom(0xc00)
        self.extents, pos, rel = [], 0, 0xc00 + 5
        while pos < image_size:
            n = min(image_size - pos, rng.choice([1, 15, 16, 17, 4095, 40000, 70001]))
            src = rng.randrange(2)
            high = os.urandom(8)
            enc = ctr(title, high, rel, self.image[pos:pos + n])
            dump[NCA_OFFSET + rel:NCA_OFFSET + rel + n] = enc
            self.extents.append((pos, n, NCA_OFFSET + rel, rel, high, src, 0))
            pos, rel = pos + n, rel + n + rng.randrange(0, 40)
        self.dump = self.folder / 'game.nsp'
        self.dump.write_bytes(bytes(dump))
        self.map = self.folder / 'romfs' / rs.MAP_NAME
        self.map.parent.mkdir(parents=True, exist_ok=True)
        self.write_map()

    def write_map(self):
        strings = bytearray()

        def string(s):
            data = str(s).encode()
            strings.extend(data)
            return len(strings) - len(data), len(data)

        keys_ref = string(self.keys)
        head = hashlib.sha256(self.dump.read_bytes()[NCA_OFFSET:NCA_OFFSET + 0xc00]).digest()
        rows = b''
        for src in range(2):
            first = next(e for e in self.extents if e[5] == src)
            n = min(rs.CHECK_BYTES, first[1])
            check = hashlib.sha256(self.image[first[0]:first[0] + n]).digest()
            rows += rs.SOURCE.pack(self.dump.stat().st_size, NCA_OFFSET, 0xc00 + len(self.image), head,
                                   *string(self.dump), rs.KIND_TITLE_KEY, *string('titlekek_00'), 0, *keys_ref, 0, 0,
                                   TICKET_KEY, first[0], n, check)
        ext = b''.join(rs.EXTENT.pack(*e) for e in self.extents)
        soff = rs.HEADER.size + len(rows) + len(ext)
        self.map.write_bytes(rs.HEADER.pack(rs.MAGIC, rs.VERSION, 2, len(self.image), len(self.extents), soff,
                                            len(strings)) + rows + ext + bytes(strings))


class MappedImageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_reads_back_the_image(self):
        with rs.MappedImage(self.fx.map) as image:
            self.assertEqual(image.size, len(self.fx.image))
            self.assertEqual(image.read(), self.fx.image)
            rng = random.Random(3)
            for _ in range(500):
                off = rng.randrange(len(self.fx.image))
                n = rng.choice([1, 3, 16, 33, 5000, 100000])
                self.assertEqual(image.pread(off, n), self.fx.image[off:off + n])
            for o, *_ in self.fx.extents[1:]:   # across every extent boundary
                self.assertEqual(image.pread(o - 3, 7), self.fx.image[o - 3:o + 4])
            self.assertEqual(rs.hash_image(image, image.size, chunk=4096), hashlib.sha256(self.fx.image).hexdigest())

    def test_open_image_prefers_romfs_bin(self):
        (self.fx.map.parent / 'romfs.bin').write_bytes(b'merged')
        with rs.open_image(self.fx.map.parent) as f:
            self.assertEqual(f.read(), b'merged')

    def test_another_keys_file_is_refused(self):
        self.fx.keys.write_text(f'titlekek_00 = {os.urandom(16).hex()}\n')
        with self.assertRaisesRegex(ValueError, 'does not decrypt'):
            rs.MappedImage(self.fx.map)

    def test_a_changed_file_is_refused(self):
        with self.fx.dump.open('ab') as f:
            f.write(b'x')
        with self.assertRaisesRegex(ValueError, 'missing or changed'):
            rs.MappedImage(self.fx.map)

    def test_malformed_maps_are_refused(self):
        data = bytearray(self.fx.map.read_bytes())
        start = rs.HEADER.size + 2 * rs.SOURCE.size
        struct.pack_into('<Q', data, start + rs.EXTENT.size, 1)   # second extent no longer follows the first
        self.fx.map.write_bytes(bytes(data))
        with self.assertRaisesRegex(ValueError, 'extents'):
            rs.read_map(self.fx.map)


class InstallFolderTest(unittest.TestCase):
    """installer/steps.py romfs_verified and the cleanup check for an install in the dumps form."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fx = Fixture(self.tmp.name, image_size=5000)
        self.romfs = self.fx.map.parent
        digest = hashlib.sha256(self.fx.image).hexdigest()
        (self.romfs / 'index.json').write_text(json.dumps({'status': 'pass', 'source': 'dumps', 'romfs_size': 5000,
                                                           'romfs_sha256': digest}))
        self.expected = {'romfs': {'size': 5000, 'sha256': digest}}

    def tearDown(self):
        self.tmp.cleanup()

    def test_map_install_is_verified(self):
        self.assertTrue(steps.romfs_verified(self.romfs, self.expected, 'dumps'))
        steps.romfs_problem(self.romfs, self.expected, 'dumps')

    def test_mode_must_match(self):
        self.assertFalse(steps.romfs_verified(self.romfs, self.expected, 'image'))

    def test_moved_dump_fails(self):
        self.fx.dump.rename(self.fx.dump.with_name('moved.nsp'))
        self.assertFalse(steps.romfs_verified(self.romfs, self.expected, 'dumps'))
        with self.assertRaises(steps.InstallError):
            steps.romfs_problem(self.romfs, self.expected, 'dumps')

    def test_mode_follows_the_option_then_the_record(self):
        ctx = SimpleNamespace(options=SimpleNamespace(romfs_source=None), state={'romfs_source': 'dumps'})
        self.assertEqual(steps.romfs_mode(ctx), 'dumps')
        ctx.options.romfs_source = 'image'
        self.assertEqual(steps.romfs_mode(ctx), 'image')
        self.assertEqual(steps.romfs_mode(SimpleNamespace(state={})), 'image')


if __name__ == '__main__':
    unittest.main()
