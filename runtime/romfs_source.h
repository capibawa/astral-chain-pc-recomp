// Where the game's RomFS image comes from (romfs.c reads it only through here): romfs.bin, the merged image the
// extraction wrote, or romfs-map.bin, which maps the image onto the player's base game and update dump files
// (tools/romfs_source.py) so no merged copy is kept. Any thread may read once romfs_src_open has returned.
#pragma once
#include <stddef.h>
#include <stdint.h>

enum { ROMFS_SRC_IMAGE = 1, ROMFS_SRC_DUMPS = 2 };

// Opens dir/romfs.bin, else dir/romfs-map.bin and the files it names, deriving their keys from the keys file it names
// (AC_KEYS overrides that path). Returns ROMFS_SRC_*, 0 when dir holds neither. A map whose files are missing, changed,
// or do not decrypt to the recorded data stops the game with a fatal that names the file.
int romfs_src_open(const char *dir);
uint64_t romfs_src_size(void);
// What the image is read from, for messages: the romfs.bin path, or the map path.
const char *romfs_src_path(void);
// Bytes read into buf at off (0 at or past the end), -1 on an I/O error, -2 when the OS refused to write into buf.
int64_t romfs_src_read(void *buf, uint32_t n, uint64_t off);
// A read-only view of romfs.bin (AC_ROMFS_MMAP); NULL for a map (its bytes are encrypted on disk) or when mapping fails.
const void *romfs_src_map(void);
// What changes when a file behind the image is replaced or written (the full-hash proof's key): romfs.bin's own
// identity, or for a map one derived from the map's and every dump file's (size: the image size). 0 on failure.
int romfs_src_id(uint64_t out[5]);   // volume, file id, size, write time, create time (PlatFileId's fields)
