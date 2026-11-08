// The RomFS image's backing (romfs_source.h): romfs.bin, or the dump files romfs-map.bin names.
//
// romfs-map.bin (written by tools/romfs_source.py, whose docstring has the layout) lists extents: back-to-back ranges of
// the merged image, each one AES-CTR run inside one dump file's Program NCA, with the counter's upper half (the
// section counter, or a BKTR subsection's generation) and the NCA-relative offset its block number counts from. A read
// finds its first extent by binary search, reads the ciphertext straight into the caller's buffer and decrypts it in
// place: no copy, no buffer, any thread, as fast as a plain read with AES-NI.
//
// The keys come from the keys file the map names (AC_KEYS overrides it) and stay in this process: a title key
// (titlekek_XX over the ticket's encrypted key) or the NCA key area (header_key decrypts the header with XTS,
// key_area_key_<area>_XX the area). At open each file's size and NCA header hash, then a recorded hash of each file's
// first decrypted bytes, are checked, so a replaced file or another keys file stops the start with a named fatal
// instead of feeding the game garbage.
#include "romfs_source.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "aes.h"
#include "cfg.h"
#include "dispatch.h"
#include "host.h"
#include "platform/platform.h"
#include "sha256.h"

#define MAP_NAME "romfs-map.bin"
#define HDR_SIZE 48u    // <8sIIQQQQ
#define SRC_SIZE 152u   // <QQQ32sIIIIIIIIIIQQQ32s
#define EXT_SIZE 48u    // <QQQQ8sII
#define MAX_SOURCES 4

typedef struct { uint64_t off, len, file_off, rel; uint8_t high[8]; uint32_t src, pad; } Extent;
_Static_assert(sizeof(Extent) == EXT_SIZE, "extent layout");

typedef struct {
    PlatFile *f;
    Aes128 key;
    char path[PLAT_PATH_MAX * 2];
} Source;

static int mode;
static PlatFile *image;
static uint64_t image_size;
static char where[PLAT_PATH_MAX * 2];
static Source sources[MAX_SOURCES];
static uint32_t nsources;
static Extent *extents;
static uint64_t nextents;

static uint64_t u64_at(const uint8_t *p) {
    uint64_t v;
    memcpy(&v, p, 8);
    return v;
}
static uint32_t u32_at(const uint8_t *p) {
    uint32_t v;
    memcpy(&v, p, 4);
    return v;
}

// ---- the keys file: "name = hex" lines, '#' starts a comment (as tools/extract.py load_keys reads it)
static int hexval(int c) {
    return c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : c >= 'A' && c <= 'F' ? c - 'A' + 10 : -1;
}

// The value of key name (exactly n bytes) into out; 1 when found with that length.
static int key_lookup(const char *text, const char *name, uint8_t *out, size_t n) {
    size_t len = strlen(name);
    for (const char *line = text; line && *line;) {
        const char *end = strchr(line, '\n');
        const char *stop = end ? end : line + strlen(line);
        const char *hash = memchr(line, '#', (size_t)(stop - line));
        if (hash) stop = hash;
        const char *p = line;
        while (p < stop && (*p == ' ' || *p == '\t')) p++;
        const char *eq = memchr(p, '=', (size_t)(stop - p));
        if (eq) {
            const char *ne = eq;
            while (ne > p && (ne[-1] == ' ' || ne[-1] == '\t')) ne--;
            int match = (size_t)(ne - p) == len;
            for (size_t i = 0; match && i < len; i++)
                if ((p[i] | 0x20) != (name[i] | 0x20)) match = 0;
            if (match) {
                const char *v = eq + 1;
                while (v < stop && (*v == ' ' || *v == '\t')) v++;
                size_t got = 0;
                while (v + 1 < stop && hexval(v[0]) >= 0 && hexval(v[1]) >= 0 && got < n) {
                    out[got++] = (uint8_t)(hexval(v[0]) << 4 | hexval(v[1]));
                    v += 2;
                }
                while (v < stop && (*v == ' ' || *v == '\t' || *v == '\r')) v++;
                return got == n && v == stop;
            }
        }
        line = end ? end + 1 : NULL;
    }
    return 0;
}

// ---- reads
static size_t extent_at(uint64_t off) {   // the extent holding image offset off (off < image_size)
    size_t lo = 0, hi = (size_t)nextents;
    while (hi - lo > 1) {
        size_t m = lo + (hi - lo) / 2;
        if (extents[m].off <= off) lo = m;
        else hi = m;
    }
    return lo;
}

int64_t romfs_src_read(void *buf, uint32_t n, uint64_t off) {
    if (mode == ROMFS_SRC_IMAGE) {
        int64_t got = plat_file_pread(image, buf, n, off);
        return got < 0 ? (got == -2 ? -2 : -1) : got;
    }
    if (off >= image_size) return 0;
    if (n > image_size - off) n = (uint32_t)(image_size - off);
    uint8_t *p = buf;
    uint32_t done = 0;
    for (size_t k = extent_at(off); done < n; k++) {
        const Extent *e = &extents[k];
        uint64_t skip = off - e->off, rel = e->rel + skip;
        uint32_t take = n - done < e->len - skip ? n - done : (uint32_t)(e->len - skip);
        int64_t got = plat_file_pread(sources[e->src].f, p, take, e->file_off + skip);
        if (got != (int64_t)take) return got == -2 ? -2 : -1;   // short: the file ends early
        aes128_ctr(&sources[e->src].key, e->high, rel >> 4, (unsigned)(rel & 15), p, take);
        p += take;
        done += take;
        off += take;
    }
    return n;
}

uint64_t romfs_src_size(void) { return image_size; }
const char *romfs_src_path(void) { return where; }
const void *romfs_src_map(void) { return mode == ROMFS_SRC_IMAGE ? plat_file_map(image) : NULL; }

int romfs_src_id(uint64_t out[5]) {
    PlatFileId id;
    if (mode == ROMFS_SRC_IMAGE) {
        if (!plat_file_id(image, &id)) return 0;
        uint64_t v[5] = {id.volume, id.file_id, id.size, id.write_time, id.create_time};
        memcpy(out, v, sizeof v);
        return 1;
    }
    Sha256 h;
    sha256_init(&h);
    PlatFile *m = plat_file_open(where);
    int ok = m && plat_file_id(m, &id);
    if (ok) sha256_update(&h, &id, sizeof id);
    if (m) plat_file_close(m);
    for (uint32_t i = 0; ok && i < nsources; i++) {
        ok = plat_file_id(sources[i].f, &id);
        sha256_update(&h, &id, sizeof id);
    }
    uint8_t d[32];
    sha256_final(&h, d);
    memcpy(&out[0], d, 8);
    memcpy(&out[1], d + 8, 8);
    out[2] = image_size;
    memcpy(&out[3], d + 16, 8);
    memcpy(&out[4], d + 24, 8);
    return ok;
}

// ---- the map
static void wipe(void *p, size_t n) {
    volatile uint8_t *v = p;
    while (n--) *v++ = 0;
}

static const char *what(uint32_t i) { return i == 0 ? "base game" : i == 1 ? "update" : "game"; }

// The AES-CTR key of source i from the keys file text (the map's recipe).
static void derive_key(uint32_t i, const uint8_t *row, const char *strings, uint64_t ssize, const char *keys,
                       const char *keys_path) {
    Source *s = &sources[i];
    uint32_t kind = u32_at(row + 64), kn_off = u32_at(row + 68), kn_len = u32_at(row + 72), slot = u32_at(row + 76);
    uint64_t nca_off = u64_at(row + 8), ticket = u64_at(row + 96);
    if ((uint64_t)kn_off + kn_len > ssize || kn_len >= 64 || slot > 3)
        rt_fatal("RomFS map %s: malformed key recipe (run the setup again)", where);
    char name[64];
    memcpy(name, strings + kn_off, kn_len);
    name[kn_len] = 0;
    uint8_t kek[16], key[16];
    if (!key_lookup(keys, name, kek, 16))
        rt_fatal("RomFS: the keys file %s has no %s, which the %s file %s needs (choose the keys file the setup used, "
                 "or set AC_KEYS)", keys_path, name, what(i), s->path);
    Aes128 k;
    aes128_init(&k, kek);
    if (kind == 2) {   // title key: titlekek over the ticket's encrypted key
        if (plat_file_pread(s->f, key, 16, ticket) != 16) rt_fatal("RomFS: %s ends early (ticket)", s->path);
        aes128_decrypt(&k, key, key);
    } else if (kind == 1) {   // key area: header_key decrypts the header's second XTS unit, which holds the area
        uint8_t hk[32], sector[0x200];
        if (!key_lookup(keys, "header_key", hk, 32))
            rt_fatal("RomFS: the keys file %s has no header_key, which the %s file %s needs", keys_path, what(i),
                     s->path);
        if (plat_file_pread(s->f, sector, sizeof sector, nca_off + 0x200) != (int64_t)sizeof sector)
            rt_fatal("RomFS: %s ends early (NCA header)", s->path);
        Aes128 k1, k2;
        aes128_init(&k1, hk);
        aes128_init(&k2, hk + 16);
        aes128_xts_decrypt(&k1, &k2, 1, sector, sizeof sector);
        if (memcmp(sector, "NCA3", 4))
            rt_fatal("RomFS: the header_key of %s does not open the %s file %s", keys_path, what(i), s->path);
        aes128_decrypt(&k, sector + 0x100 + 16 * slot, key);
        wipe(hk, sizeof hk);
        wipe(sector, sizeof sector);
        wipe(&k1, sizeof k1);
        wipe(&k2, sizeof k2);
    } else {
        rt_fatal("RomFS map %s: unknown key kind %u (run the setup again)", where, kind);
    }
    aes128_init(&s->key, key);
    wipe(kek, sizeof kek);
    wipe(key, sizeof key);
    wipe(&k, sizeof k);
}

static void open_map(const char *dir) {
    uint64_t size = 0;
    uint8_t *m = plat_file_read(where, &size);
    if (!m || size < HDR_SIZE || memcmp(m, "ACRMAP1\0", 8) || u32_at(m + 8) != 1)
        rt_fatal("RomFS map %s is unreadable or of another version (run the setup again)", where);
    uint32_t n = u32_at(m + 12);
    image_size = u64_at(m + 16);
    nextents = u64_at(m + 24);
    uint64_t soff = u64_at(m + 32), ssize = u64_at(m + 40);
    if (n == 0 || n > MAX_SOURCES || nextents == 0 || nextents > (1u << 24) ||
        soff != HDR_SIZE + (uint64_t)n * SRC_SIZE + nextents * EXT_SIZE || soff + ssize != size)
        rt_fatal("RomFS map %s: malformed layout (run the setup again)", where);
    nsources = n;
    const char *strings = (const char *)m + soff;
    extents = malloc((size_t)nextents * sizeof *extents);
    if (!extents) rt_fatal("RomFS map: out of memory");
    memcpy(extents, m + HDR_SIZE + (uint64_t)n * SRC_SIZE, (size_t)nextents * EXT_SIZE);
    uint64_t pos = 0;
    for (uint64_t k = 0; k < nextents; k++) {
        const Extent *e = &extents[k];
        if (e->off != pos || !e->len || e->len > image_size - pos || e->src >= n)
            rt_fatal("RomFS map %s: malformed extents (run the setup again)", where);
        pos += e->len;
    }
    if (pos != image_size) rt_fatal("RomFS map %s: the extents do not cover the image", where);

    // The keys file: AC_KEYS, else the one the setup recorded (the same for every source).
    const uint8_t *row0 = m + HDR_SIZE;
    char keys_path[PLAT_PATH_MAX * 2];
    const char *env = cfg_get("AC_KEYS");
    uint32_t kf_off = u32_at(row0 + 80), kf_len = u32_at(row0 + 84);
    if (env && *env) snprintf(keys_path, sizeof keys_path, "%s", env);
    else if ((uint64_t)kf_off + kf_len <= ssize && kf_len < sizeof keys_path) {
        memcpy(keys_path, strings + kf_off, kf_len);
        keys_path[kf_len] = 0;
    } else
        rt_fatal("RomFS map %s: malformed keys path", where);
    uint64_t ksize = 0;
    char *keys = (char *)plat_file_read(keys_path, &ksize);
    if (!keys)
        rt_fatal("RomFS: the keys file %s is missing. This install reads the game data from your dump files and "
                 "needs the keys file the setup used: put it back, set AC_KEYS to its path, or run the setup again",
                 keys_path);

    for (uint32_t i = 0; i < n; i++) {
        const uint8_t *row = m + HDR_SIZE + (uint64_t)i * SRC_SIZE;
        Source *s = &sources[i];
        uint32_t p_off = u32_at(row + 56), p_len = u32_at(row + 60);
        if ((uint64_t)p_off + p_len > ssize || p_len >= sizeof s->path)
            rt_fatal("RomFS map %s: malformed path", where);
        memcpy(s->path, strings + p_off, p_len);
        s->path[p_len] = 0;
        s->f = plat_file_open(s->path);
        if (!s->f)
            rt_fatal("RomFS: the %s file %s is missing. This install reads the game data from it: put it back at "
                     "that path or run the setup again",
                     what(i), s->path);
        PlatFileId id;
        uint8_t head[0xc00], digest[32];
        if (!plat_file_id(s->f, &id) || id.size != u64_at(row))
            rt_fatal("RomFS: the %s file %s changed size since the setup read it (run the setup again)", what(i),
                     s->path);
        if (plat_file_pread(s->f, head, sizeof head, u64_at(row + 8)) != (int64_t)sizeof head)
            rt_fatal("RomFS: %s ends early", s->path);
        sha256(head, sizeof head, digest);
        if (memcmp(digest, row + 24, 32))
            rt_fatal("RomFS: the %s file %s is not the one the setup read (run the setup again)", what(i), s->path);
        derive_key(i, row, strings, ssize, keys, keys_path);
    }
    wipe(keys, (size_t)ksize);
    free(keys);

    mode = ROMFS_SRC_DUMPS;
    for (uint32_t i = 0; i < n; i++) {   // the recorded hash of each file's first decrypted bytes
        const uint8_t *row = m + HDR_SIZE + (uint64_t)i * SRC_SIZE;
        uint64_t off = u64_at(row + 104), len = u64_at(row + 112);
        uint8_t buf[4096], digest[32];
        if (len > sizeof buf || romfs_src_read(buf, (uint32_t)len, off) != (int64_t)len)
            rt_fatal("RomFS: reading the %s file %s failed", what(i), sources[i].path);
        sha256(buf, (size_t)len, digest);
        if (memcmp(digest, row + 120, 32))
            rt_fatal("RomFS: the %s file %s does not decrypt to the game data the setup recorded: another keys file "
                     "(%s) than the setup used, or another file (run the setup again)",
                     what(i), sources[i].path, keys_path);
    }
    free(m);
    rt_log("RomFS: reading the game data from %u dump files through %s (%llu extents, %llu bytes, AES-%s)", n, where,
           (unsigned long long)nextents, (unsigned long long)image_size, "CTR");
    (void)dir;
}

int romfs_src_open(const char *dir) {
    snprintf(where, sizeof where, "%s/romfs.bin", dir);
    image = plat_file_open(where);
    if (image) {
        PlatFileId id;
        if (!plat_file_id(image, &id)) rt_fatal("RomFS image %s: no file information", where);
        image_size = id.size;
        mode = ROMFS_SRC_IMAGE;
        return mode;
    }
    snprintf(where, sizeof where, "%s/" MAP_NAME, dir);
    if (!plat_file_exists(where)) {
        snprintf(where, sizeof where, "%s/romfs.bin", dir);
        return 0;
    }
    open_map(dir);
    return mode;
}
