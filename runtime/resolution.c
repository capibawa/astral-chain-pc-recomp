// Game resolution: the size the game composes its frame in (its output: canvas, UI and swapchain) and the size of its
// 3D scene, above the shipped 1920x1080 and 1600x900. The game does the rendering itself, as with the community
// resolution mods (UltraCam, Visual Fixes): the patch sets output and scene (recomp/patches-v121.txt,
// patches-v143.txt; MacNativeGames games/totk/runtime/game_patches.c and game_patches_v143.c, MIT, Copyright (c) 2026
// MacNativeGames contributors) make the game read both sizes from the patch page, and the texture heap that holds the
// output- and scene-sized render targets grows with them.
//
// Settings, read at start: AC_OUTPUT_RES = <w>x<h> (default 1920x1080, the game's own), AC_RENDER_SCALE = the
// scene's size relative to the output: "game" (default, 5/6, the game's 1600x900 at 1080p) or a percentage; above 100
// the game supersamples its scene, which smooths edges (anti-aliasing) at the cost of GPU time. The game's own final
// pass (FSR 1 EASU) scales the scene to the output; the present scales the output to the window (vk_fsr.c).
//
// Patch page: +16 the scene size {w, h} as floats, +32 the output size and +48 the swapchain size as {w, h} in one
// 64-bit word, +40 the renderer (1.2.1) or display configuration (1.4.3) the patched code stores there.
// +56 the camera's aspect ratio as a float when the output is not 16:9 (set aspect), +60 the field-of-view factor
// (set fov, res_fov_live).
// 1.4.3 scales the scene it reads by output width / 1920 (a 2560x1440 output renders its 1600x900 scene at 2128x1200;
// MacNativeGames measured the same), so its page holds the wanted size divided by that factor and the game's 5/6 needs
// no scene patch. 1.2.1 renders the size it reads (1600x900 on a 2560x1440 output): its page holds the wanted size, and
// the scene set is on whenever the output differs from 1920x1080.
//
// Heap: agl's DynamicTextureAllocator is one fixed heap, "DynamicTextureAllocatorMem" in
// System/HeapSize/Product.Nin_NX_NVN.HeapDef.byml.zs (113664 KiB, carved from "Graphics", 201728 KiB). At 2560x1440
// the shipped size runs out at the title screen. Both heaps grow by the same amount: the shipped size times the output's
// pixel ratio above 1920x1080 plus the scene's above 1600x900 (MacNativeGames' measurement: a 3440x1440 canvas with a
// 4620x1935 scene crashed at +258 MiB and loaded at +400 MiB; this formula gives +750 MiB there). romfs.c serves the
// rewritten table (res_heapdef_build) in place of the original; memory.c adds the amount to the application pool, so
// no other heap shrinks. Without the rewritten table (a RomFS that lacks it, a format this code does not know) the
// patch sets stay off and the game keeps its own sizes.
//
// Astral Chain 1.0.1 has no output set: there the res set reads AC_OUTPUT_RES from the patch page at the game's render
// size sites and grows the game's render-target budget with it (res_pick below); none of the HeapDef growth above
// applies.
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zstd.h>
#ifndef _WIN32
#include <dirent.h>
#endif

#include "cfg.h"
#include "cpu.h"
#include "gfx.h"
#include "host.h"
#include "layout.h"
#include "platform/platform.h"
#include "simd.h"

#define BASE_W 1600
#define BASE_H 900
#define SCENE_W 1600
#define SCENE_H 900
#define DTA_KIB 113664u      // DynamicTextureAllocatorMem, shipped
#define SCENE_MAX 4096       // per side, as MacNativeGames' AC_RENDER_RES
#define SHADOW_BASE 960      // the sun shadow map's base size (its cascades: a D16 array of 4 layers)

static struct {
    int decided;
    unsigned ow, oh, sw, sh;   // output and scene
    int scene_set;             // the scene differs from the game's 5/6 of the output
    int heapdef_ok;            // romfs.c serves the grown HeapDef
    int on;                    // res_init switched the patch sets on
    unsigned shadow;           // fixed sun shadow map size (AC_SHADOW_RES), 0: the game's own
    double dlss_k;             // the upscaling quality's share of the output (quality .. ultra_performance), else 0
    int live;                  // the mode can change while the game runs (1.0.1 with DLSS able to run: res_dlss_mode)
} R;

// The scene size is decided before the device exists, so NGX cannot be asked yet. NGX loads DLSS from nvngx_dlss.dll
// next to astralchain.exe on Windows, libnvidia-ngx-dlss.so.<version> next to the executable on Linux (vk_dlaa.c
// vk_dlaa_init): without that file DLSS never runs, and a DLSS mode would leave the game's plain scaling at 33..67 % of
// the output. After the TotK Recomp's resolution.c (same author, MIT). Once NGX has answered (vk_dlaa.c
// vk_dlaa_ruled_out) the file no longer counts where DLSS cannot run: the menu then shows what output_res = auto will take
// at the next start as it will really end up.
int vk_dlaa_ruled_out(void);   // vk_dlaa.c, whose header pulls in Vulkan

// The DLSS model's file is next to the executable (vk_dlaa.c vk_dlaa_wanted loads NGX at start where it is).
int res_dlss_model(void) {
#ifdef _WIN32
    char exe[1024], path[1024 + 32];
    if (!plat_exe_path(exe, sizeof exe)) return 1;
    char *last = plat_last_sep(exe);
    if (!last) return 1;
    snprintf(path, sizeof path, "%.*s/nvngx_dlss.dll", (int)(last - exe), exe);
    return plat_file_exists(path);
#else
    char exe[1024];
    if (!plat_exe_path(exe, sizeof exe)) return 1;
    char *last = plat_last_sep(exe);
    if (!last) return 1;
    *last = 0;
    DIR *d = opendir(exe);
    if (!d) return 1;
    int found = 0;
    for (struct dirent *e; !found && (e = readdir(d));)
        found = !strncmp(e->d_name, "libnvidia-ngx-dlss.so.", 22);
    closedir(d);
    return found;
#endif
}

static int dlss_dll(void) { return !vk_dlaa_ruled_out() && res_dlss_model(); }

// The scene size at start, before NGX can be asked, as vk_dlaa.c taa_pick will decide it (it corrects this at its first
// present through res_dlss_mode when the GPU says otherwise): "dlaa" for the game's size (no upscaler, or the native
// mode: DLAA or FSR 3's Native AA), else upscaling_quality. DLSS needs the DLSS model beside the executable; without it
// upscaling = dlss runs nothing and auto runs FSR 3, which needs no file.
static const char *start_mode(void) {
    int dll = dlss_dll();
    const char *up = cfg_value("AC_UPSCALING"), *q = cfg_value("AC_UPSCALING_QUALITY");
    if (!strcmp(up, "off") || !strcmp(q, "native")) return "dlaa";
    if (!strcmp(up, "dlss") && !dll) {
#ifdef _WIN32
        rt_log("resolution: DLSS upscaling needs nvngx_dlss.dll next to astralchain.exe, which is missing: the scene "
               "keeps the game's size");
#else
        rt_log("resolution: DLSS upscaling needs libnvidia-ngx-dlss.so.<version> next to the executable, which is "
               "missing: the scene keeps the game's size");
#endif
        return "dlaa";
    }
    return q;
}

// A temporal mode runs from the start (start_mode, quietly): an upscaler able to run, in its native mode or with the
// scene patch set there to shrink the scene. output_res = auto (res_auto) raises the game's resolution to
// the screen then, so DLAA anti-aliases at the screen's size, an upscaler writes it, and the window only copies. A GPU
// without DLSS learns that only once NGX is asked (vk_dlaa.c vk_dlaa_init), after this answer: where nothing temporal
// is left then, vk_dlaa_init takes the size back to 1920x1080 (res_auto_drop), so DLAA's fallback to SMAA does not run
// at the screen's size.
int res_temporal_at_start(void) {
    const char *up = cfg_value("AC_UPSCALING");
    if (!strcmp(up, "off") || (!strcmp(up, "dlss") && !dlss_dll())) return 0;
    return !strcmp(cfg_value("AC_UPSCALING_QUALITY"), "native") || rt_patch_set("scene") >= 0;
}

// The scene's share of the output for a mode: 0 for dlaa (the scene keeps the game's size), else the quality's.
static double dlss_share(const char *m) {
    static const struct { const char *name; double k; } dlss[] = {
        {"quality", 2.0 / 3.0}, {"balanced", 0.58}, {"performance", 0.5}, {"ultra_performance", 1.0 / 3.0}};
    for (int i = 0; i < (int)(sizeof dlss / sizeof dlss[0]); i++)
        if (!strcmp(m, dlss[i].name)) return dlss[i].k;
    return 0;
}

static void res_size(unsigned *w, unsigned *h);   // below: output_res as the res set renders it, auto resolved

static void decide(void) {
    if (R.decided) return;
    R.decided = 1;
    R.ow = BASE_W;
    R.oh = BASE_H;
    const char *o = cfg_value("AC_OUTPUT_RES");
    unsigned w = 0, h = 0;
    char tail;
    if (!strcmp(o, "auto")) {   // the size auto took at start (res_size)
        res_size(&w, &h);
        R.ow = w, R.oh = h;
    } else if (sscanf(o, "%ux%u%c", &w, &h, &tail) == 2 && w >= 1280 && h >= 720 && w <= 7680 && h <= 4320) {
        R.ow = w & ~1u;
        R.oh = h & ~1u;
    } else {
        rt_log("resolution: AC_OUTPUT_RES=%s is not <width>x<height> in 1280x720..7680x4320; 1920x1080", o);
    }
    const char *s = cfg_value("AC_RENDER_SCALE");
    double k = 5.0 / 6.0;
    const char *m = start_mode();   // an upscaling quality sets the scene size
    R.dlss_k = dlss_share(m);
    // Without the scene patch set (1.0.1 has none) the scene keeps the game's size: a DLSS mode would only bias the
    // textures toward an output that is never upscaled, sharpening them into shimmer. The upscaler then runs at the
    // game's size.
    if (R.dlss_k && rt_patch_set("scene") < 0) {
        rt_log("resolution: upscaling (%s) needs the scene patch set, which this game version lacks: it runs at the "
               "game's size", m);
        R.dlss_k = 0;
    }
    if (R.dlss_k) {
        k = R.dlss_k;
        R.scene_set = 1;
        rt_log("resolution: upscaling %s: the scene at %.0f %% of the output (render_scale ignored)", m, k * 100);
    } else if (strcmp(s, "game")) {
        char *end;
        double pct = strtod(s, &end);
        if (end != s && !*end && pct >= 25 && pct <= 400) {
            k = pct / 100.0;
            R.scene_set = 1;
        } else {
            rt_log("resolution: AC_RENDER_SCALE=%s is not game or a percentage 25..400; game", s);
        }
    }
    double sw = R.ow * k, sh = R.oh * k;
    double fit = fmin(1.0, fmin(SCENE_MAX / sw, SCENE_MAX / sh));
    if (fit < 1.0) R.scene_set = 1;
    R.sw = (unsigned)(sw * fit + 0.5);
    R.sh = (unsigned)(sh * fit + 0.5);
    const char *sr = cfg_value("AC_SHADOW_RES");
    if (strcmp(sr, "game")) {
        unsigned n = (unsigned)atoi(sr);
        if (n >= 256 && n <= 4096 && !(n & 7)) R.shadow = n;
        else rt_log("resolution: AC_SHADOW_RES=%s is not game or a size 256..4096 (a multiple of 8); game", sr);
    }
}

// Texture heap for a sun shadow map above the base size: the four layers of 2 bytes, three maps in flight (the engine
// makes the array anew each frame).
static uint64_t shadow_kib(void) {
    return R.shadow > SHADOW_BASE ? ((uint64_t)R.shadow * R.shadow - SHADOW_BASE * SHADOW_BASE) * 8 * 3 / 1024 : 0;
}

static int bigger(void) {
    decide();
    return R.ow != BASE_W || R.oh != BASE_H || R.scene_set || shadow_kib();
}

static int output_set(void);   // below: TotK's output patch set is in the generated C

// TotK's HeapDef growth for its output, scene and shadow sizes; 0 without the output set (1.0.1), whose size comes from
// the res set and grows the pool through res_output_extra_bytes instead.
uint64_t res_heap_extra_kib(void) {
    if (!output_set() || !bigger()) return 0;
    double grow = fmax(0.0, (double)R.ow * R.oh / (BASE_W * BASE_H) - 1.0) +
                  fmax(0.0, (double)R.sw * R.sh / (SCENE_W * SCENE_H) - 1.0);
    uint64_t kib = grow > 0 ? (uint64_t)ceil(DTA_KIB * (1.0 + grow) / 1024.0) * 1024 - DTA_KIB : 0;
    return kib + ((shadow_kib() + 1023) & ~1023ull);   // whole MiB
}

uint64_t res_output_extra_bytes(void);   // below: the res set's render-target budget growth

uint64_t res_heap_extra_bytes(void) {
    return ((res_heap_extra_kib() << 10) + res_output_extra_bytes() + 0x1fffff) & ~0x1fffffull;
}

// ---- the HeapDef table. BYML version 2..7, little endian ("YB"): a hash node is {u8 0xC1, u24 count} and count
// entries {u24 key index, u8 type, u32 value}; an int (type 0xD1) is stored in the entry, so a size changes in place.
typedef struct { uint8_t *p; size_t n; uint32_t keys; } Byml;

static int u32_at(const Byml *b, size_t off, uint32_t *v) {
    if (off + 4 > b->n) return 0;
    memcpy(v, b->p + off, 4);
    return 1;
}

static const char *key_name(const Byml *b, uint32_t k) {
    uint32_t head, at;
    if (!u32_at(b, b->keys, &head) || (head & 0xff) != 0xc2 || k >= head >> 8 || !u32_at(b, b->keys + 4 + 4 * k, &at) ||
        b->keys + at >= b->n || !memchr(b->p + b->keys + at, 0, b->n - b->keys - at))
        return NULL;
    return (const char *)b->p + b->keys + at;
}

// The offset of the value of hash node `node`'s entry `name`, which must have type `type`; 0 when there is none.
static size_t field(const Byml *b, uint32_t node, const char *name, uint8_t type) {
    uint32_t head;
    if (!u32_at(b, node, &head) || (head & 0xff) != 0xc1) return 0;
    for (uint32_t i = 0; i < head >> 8; i++) {
        uint32_t e;
        if (!u32_at(b, node + 4 + 8 * (size_t)i, &e)) return 0;
        const char *k = key_name(b, e & 0xffffff);
        if (k && !strcmp(k, name)) return e >> 24 == type ? node + 8 + 8 * (size_t)i : 0;
    }
    return 0;
}

static size_t heap_size_field(const Byml *b, uint32_t heaps, const char *heap) {
    size_t at = field(b, heaps, heap, 0xc1);
    uint32_t node;
    return at && u32_at(b, at, &node) ? field(b, node, "Size", 0xd1) : 0;
}

int res_heapdef_build(const void *in, size_t n, void **out, size_t *out_n) {
    uint64_t extra = res_heap_extra_kib();
    *out = NULL;
    if (!extra) return 0;
    if (ZSTD_getDictID_fromFrame(in, n)) {
        rt_log("resolution: the HeapDef table needs a zstd dictionary; the game keeps its own resolution");
        return 0;
    }
    unsigned long long raw = ZSTD_getFrameContentSize(in, n);
    if (raw == ZSTD_CONTENTSIZE_UNKNOWN || raw == ZSTD_CONTENTSIZE_ERROR || raw > (64u << 20)) {
        rt_log("resolution: the HeapDef table is not a zstd frame with a size; the game keeps its own resolution");
        return 0;
    }
    Byml b = {malloc((size_t)raw), (size_t)raw, 0};
    if (!b.p) return 0;
    size_t got = ZSTD_decompress(b.p, b.n, in, n);
    uint32_t root, heaps = 0;
    uint16_t ver = 0;
    if (!ZSTD_isError(got) && got == b.n && b.n >= 16) memcpy(&ver, b.p + 2, 2);
    size_t heaps_at = 0, dta_at = 0, gfx_at = 0;
    if (ver >= 2 && ver <= 7 && !memcmp(b.p, "YB", 2) && u32_at(&b, 4, &b.keys) && u32_at(&b, 12, &root) &&
        (heaps_at = field(&b, root, "Heaps", 0xc1)) && u32_at(&b, heaps_at, &heaps)) {
        dta_at = heap_size_field(&b, heaps, "DynamicTextureAllocatorMem");
        gfx_at = heap_size_field(&b, heaps, "Graphics");
    }
    if (!dta_at || !gfx_at) {
        rt_log("resolution: the HeapDef table has no DynamicTextureAllocatorMem / Graphics sizes; the game keeps its own "
               "resolution");
        free(b.p);
        return 0;
    }
    int32_t dta, gfx;
    memcpy(&dta, b.p + dta_at, 4);
    memcpy(&gfx, b.p + gfx_at, 4);
    int32_t dta2 = dta + (int32_t)extra, gfx2 = gfx + (int32_t)extra;
    memcpy(b.p + dta_at, &dta2, 4);
    memcpy(b.p + gfx_at, &gfx2, 4);
    size_t cap = ZSTD_compressBound(b.n);
    void *z = malloc(cap);
    size_t zn = z ? ZSTD_compress(z, cap, b.p, b.n, 9) : 0;
    free(b.p);
    if (!z || ZSTD_isError(zn)) {
        free(z);
        return 0;
    }
    *out = z;
    *out_n = zn;
    rt_log("resolution: HeapDef DynamicTextureAllocatorMem %d -> %d KiB, Graphics %d -> %d KiB", dta, dta2, gfx, gfx2);
    return 1;
}

void res_heapdef_served(void) { R.heapdef_ok = 1; }

// ---- sun shadow map size: each frame the engine sizes its shadow cascades to the area they cover, down to about 400 near
// the ground, which draws foliage shadows blocky, and each change of size moves the shadow texels, so leaf shadows jump.
// A fixed size (the shadow set, recomp/patches-v121.txt) keeps them still and sharper. Page +92.
static void shadow_init(uint8_t *page) {
    if (!R.shadow) {
        rt_log("resolution: sun shadow map sized by the game (AC_SHADOW_RES=game)");
        return;
    }
    int set = rt_patch_set("shadow");
    if (set < 0) {
        rt_log("resolution: the generated C has no shadow patch set: the game sizes its shadow map");
        return;
    }
    int32_t n = (int32_t)R.shadow;
    memcpy(page + 92, &n, 4);
    rt_patch_switch(set, 1);
    rt_log("resolution: sun shadow map %ux%u (AC_SHADOW_RES)", R.shadow, R.shadow);
}

// ---- start: the patch page and the sets
static uint64_t pack(unsigned w, unsigned h) { return (uint64_t)h << 32 | w; }

// AC_SHADOW_RANGE (percent, 50 to 400; 100 = the game's): the sun's shadow cascades end at 20, 40, 80 and 160 m.
// Other values multiply those distances through the cascade patch set (recomp/patches-v143.txt), so the sharp first
// cascade reaches further and the visible steps between cascades move away from the player, at the cost of shadow
// texels per metre (AC_SHADOW_SCALE pays that back).
static void res_shadow_range(void) {
    int pct = cfg_int("AC_SHADOW_RANGE"), set = rt_patch_set("cascade");
    if (pct == 100) return;
    if (set < 0) {
        rt_log("resolution: the generated C has no cascade patch set; shadow_range stays 100");
        return;
    }
    float k = (float)(pct < 50 ? 50 : pct > 400 ? 400 : pct) / 100.0f, d[4] = {20 * k, 40 * k, 80 * k, 160 * k};
    memcpy((uint8_t *)rt_patch_page() + 64, d, sizeof d);
    rt_patch_switch(set, 1);
    rt_log("resolution: shadow cascades end at %g, %g, %g and %g m", d[0], d[1], d[2], d[3]);
}

// ---- start: the patch page and the sets
// Game versions without TotK's output patch set (1.0.1): the res set (recomp/patches-v101.txt) reads the output size
// from the patch page (+32 width, +36 height, u32; rt_res_* below) at the sites that hold the game's render size, so
// output_res takes a size as in the TotK Recomp, here any 16:9 one of 32n x 18n: 1920x1080 by default, 1280x720 for
// GPUs that cannot hold 1080p, up to 3840x2160 (res_size). Above 1080p's pixel count the game's render-target budget
// (main+0x40e0b0, 310 MiB, which 1080p fills to 304) grows with the pixels (res_budget), and the process heap by the
// same amount, as the budget is carved from it (res_output_extra_bytes). AC_RES1062=1 still asks for the community
// 1062p set instead.
#define RES_PAGE 32
#define RES_BUDGET_GAME (310ull << 20)   // the game's own render-target budget, 0x13600000
#define RES_PIXELS_MAX (3840ull * 2160)  // the most the process heap's 4 GiB (memory.c HEAP_CAP) leaves room for

// TotK's output and scene sets (patches-v121.txt, patches-v143.txt) make the game read any output size from the patch
// page, with the grown HeapDef table (res_init below); without them the res set does.
static int output_set(void) { return rt_patch_set("output") >= 0; }

// The output size output_res asks for on 1.0.1, decided once (mem_init sizes the pool before the sets are switched):
// 32n x 18n from 1280x720 (n = 40) to 3840x2160 (n = 120); else 1920x1080. Other shapes and sizes off that grid fail:
// 1280x800 lost the device in the first gameplay frames (a device fault in a shader, as scene sizes off the grid did
// in the temporal-upscaling work), and in handheld, where the scene targets are 1280x720, a 1280-wide output makes the
// ladder's cmp take the docked entries for them, so a level's target is never made and loading a save reads through
// its null texture in main+0x9d0df0 (as issues #25 and #38). On the grid the only 1280-wide size is 1280x720 itself.
// res_output_grid is that rule alone, also for cfg.c's Steam Deck migration (cfg_migrate_deck_res), which has to know
// which stored values end up as 1920x1080.
int res_output_grid(const char *o, unsigned *w, unsigned *h) {
    unsigned a = 0, b = 0;
    char tail;
    if (!o || sscanf(o, "%ux%u%c", &a, &b, &tail) != 2 || a % 32 || b % 18 || a / 32 != b / 18 || a < 1280 ||
        (uint64_t)a * b > RES_PIXELS_MAX)
        return 0;
    *w = a, *h = b;
    return 1;
}

int gfx_display_height(void);   // vk.h: the screen the game shows on, 0 when unknown

// output_res = auto: 1920x1080 unless DLAA or temporal upscaling runs from the start (res_temporal_at_start); then the
// largest of 2560x1440 and 3840x2160 the screen holds (gfx_display_height, which also answers before the window
// exists), so DLAA anti-aliases at the screen's size and DLSS or FSR 3 writes it straight from the small scene, the HUD
// is drawn at that size, and the window copies the picture 1:1 instead of scaling it a second time. SMAA and Off keep
// 1920x1080: raising them would be plain supersampling. Only those two sizes are taken: a 1600-line screen gets
// 2560x1440, which the window then scales up the rest of the way.
static const char *res_auto(void) {
    if (!res_temporal_at_start()) return "1920x1080";
    int screen = gfx_display_height();
    const char *o = screen >= 2160 ? "3840x2160" : screen >= 1440 ? "2560x1440" : "1920x1080";
    rt_log("resolution: output_res auto: DLAA or upscaling on a %d-line screen: %s", screen, o);
    return o;
}

// For the menu, each frame: the size auto would take at the next start, read again only when a setting changed (the
// DLSS model check behind res_temporal_at_start looks at the file system). Quiet, unlike res_auto.
void res_auto_next(unsigned out[2]) {
    static unsigned seen = ~0u, next[2];
    if (cfg_version() != seen) {
        seen = cfg_version();
        int screen = res_temporal_at_start() ? gfx_display_height() : 0;
        next[0] = screen >= 2160 ? 3840 : screen >= 1440 ? 2560 : 1920;
        next[1] = screen >= 2160 ? 2160 : screen >= 1440 ? 1440 : 1080;
    }
    out[0] = next[0], out[1] = next[1];
}

static char asked[16];        // output_res as res_size read it
static unsigned size_w, size_h; // the size res_size decided (0 until then)
static int auto_raised;         // output_res = auto took the screen's size above 1920x1080

const char *res_output_asked(void) { return asked; }

static void res_size(unsigned *w, unsigned *h) {
    if (!size_w) {
        const char *o = cfg_value("AC_OUTPUT_RES");
        snprintf(asked, sizeof asked, "%s", o);
        if (!strcmp(o, "auto")) o = res_auto(), auto_raised = strcmp(o, "1920x1080") != 0;
        unsigned sw = 1920, sh = 1080;
        if (!res_output_grid(o, &sw, &sh) && strcmp(o, "1920x1080"))
            rt_log("resolution: output_res = %s is not a 16:9 size of 32n x 18n from 1280x720 to 3840x2160; "
                   "1920x1080", o);
        size_h = sh, size_w = sw;
    }
    *w = size_w, *h = size_h;
}

// The render-target budget for w x h: the game's own up to 1080p's pixel count, which it fits; above, the measured use
// (87 MiB and 110 bytes a pixel: 304 MiB at 1920x1080, 482 at 2560x1440, 955 at 3840x2160, the same 81 allocations over
// 75 s into play) with 15 % to spare, in whole MiB: 545 MiB at 2560x1440, 1100 at 3840x2160.
static uint64_t res_budget(unsigned w, unsigned h) {
    uint64_t px = (uint64_t)w * h;
    if (px <= 1920ull * 1080) return RES_BUDGET_GAME;
    uint64_t use = (87ull << 20) + px * 110;
    return (use + use * 15 / 100 + 0xfffff) & ~0xfffffull;
}

// The res set is in the generated C and nothing turned it off: AC_RES=0, or AC_RES1062=1, whose sites are the same.
static int res_set_wanted(void) {
    if (output_set() || rt_patch_set("res") < 0) return 0;
    const char *r = cfg_get("AC_RES"), *r1062 = cfg_get("AC_RES1062");
    return !(r && !atoi(r)) && !(r1062 && atoi(r1062));
}

uint64_t res_output_extra_bytes(void) {
    if (!res_set_wanted()) return 0;
    unsigned w, h;
    res_size(&w, &h);
    return res_budget(w, h) - RES_BUDGET_GAME;
}

static unsigned res_page(int i) {
    uint32_t v;
    memcpy(&v, (const uint8_t *)rt_patch_page() + RES_PAGE + 4 * i, sizeof v);
    return v;
}

// The res set's sites: a mov of the game's render width or height, the ladder's cmp of the scene target's width
// against it (flags as cmp w0, width sets them), and the budget's mov.
void rt_res_w8(Cpu *c) { c->x[8] = res_page(0); }
void rt_res_h8(Cpu *c) { c->x[8] = res_page(1); }
void rt_res_w9(Cpu *c) { c->x[9] = res_page(0); }
void rt_res_h9(Cpu *c) { c->x[9] = res_page(1); }
void rt_res_h22(Cpu *c) { c->x[22] = res_page(1); }
uint64_t rt_res_cmp_w0(Cpu *c) {
    uint32_t a = (uint32_t)c->x[0], b = res_page(0), r = a - b;
    return MKFLAGS(r >> 31, r == 0, a >= b, ((a ^ b) & (a ^ r)) >> 31);
}
void rt_res_budget_w8(Cpu *c) { c->x[8] = res_budget(res_page(0), res_page(1)); }

static void res_pick(void) {
    int res = rt_patch_set("res"), r1062 = rt_patch_set("res1062");
    if (res < 0) return;
    if (rt_patch_enabled(r1062) && rt_patch_enabled(res)) {
        rt_patch_switch(res, 0);
        rt_log("resolution: AC_RES1062 asks for 1888x1062: the res set is off");
    }
    if (rt_patch_enabled(res)) {
        unsigned w, h;
        res_size(&w, &h);
        uint32_t page[2] = {w, h};
        memcpy((uint8_t *)rt_patch_page() + RES_PAGE, page, sizeof page);
        rt_log("resolution: the game renders %ux%u (render-target budget %llu MiB)", w, h,
               (unsigned long long)(res_budget(w, h) >> 20));
    } else if (rt_patch_enabled(r1062)) {
        rt_log("resolution: the game renders 1888x1062");
    }
}

void res_game_size(unsigned out[2]) {
    if (rt_patch_enabled(rt_patch_set("res"))) {
        res_size(&out[0], &out[1]);
    } else if (rt_patch_enabled(rt_patch_set("res1062"))) {
        out[0] = 1888, out[1] = 1062;
    } else {
        unsigned scene[2];
        res_sizes(out, scene);
    }
}

// ---- 1.0.1: the scene set alone (recomp/patches-v101.txt). The output stays the game resolution (res, res1062 or
// the game's own 1600x900) and only the scene changes, to the DLSS mode's share of it: the game's dynamic-resolution
// levels all take the size on page +16 ({w, h} as floats, read by rt_res_scene_w1 / rt_res_scene_h2 below), and the
// game's own scaling pass draws the scene to the output, where vk_dlaa.c runs DLSS in its place. The scene is never
// larger than the targets the game already makes, so the texture heap needs nothing more.
// The scene_rebuild set is on from the start (scene_only_init), so the upscaler and its mode can change while the game
// runs (res_dlss_mode); the scene set's level sizes are on only while the scene is smaller (scene_switch).

// The scene for a share k of the game resolution g. The first 3D frame loses the device (RTX 5070 Ti) unless the scene
// is 32n x 18n, with or without DLSS evaluating (AC_DLAA_MV and the object pass off changed nothing either; the device
// fault names only shader instructions, so the cause is a pass of the game's or the runtime's, not found yet). Measured
// at 1920x1080: 1120x630 plays; 1114x626, 1120x626, 1114x630, 1136x640 and 1104x621 do not. The game resolutions
// output_res offers are all on that grid (resolution.c res_output_grid), so the nearest n below the game's own width
// is the scene: 1280x720, 1120x630, 960x540 and 640x360 at 1920x1080. None (a game resolution narrower than 64): the
// game resolution, and the upscaler runs at it. k = 0: the game resolution itself.
static void scene_for(double k, const unsigned g[2], unsigned *w, unsigned *h) {
    *w = g[0], *h = g[1];
    if (!k) return;
    int want = (int)lround(g[0] * k / 32), most = (int)(g[0] / 32);
    if (want >= most) want = most - 1;
    if (want < 1) {
        rt_log("resolution: no scene near %.0f %% of %ux%u on the 32x18 grid: it runs at the game's size", k * 100,
               g[0], g[1]);
        return;
    }
    *w = 32 * (unsigned)want, *h = 18 * (unsigned)want;
}

static void scene_page(unsigned w, unsigned h) {
    float sc[2] = {(float)w, (float)h};
    memcpy((uint8_t *)rt_patch_page() + 16, sc, sizeof sc);
}

static uint64_t size_asked;            // the latest size res_dlss_mode asked for (the present's thread, as pack())
static volatile uint64_t size_pending; // that size until the game thread takes it, 0: none

static int scene_idx = -1;   // the scene set (rt_patch_set), switched with the scene size (scene_switch)

// The scene set's level-size sites are on only while the scene is smaller than the game resolution: at the game's
// size they are off and every level keeps the game's own docked and handheld size (recomp/patches-v101.txt: the game
// resolution in handheld made a level without its target). The page is written before the switch, so a site that
// sees the set on reads the new size.
static void scene_switch(unsigned w, unsigned h) {
    scene_page(w, h);
    rt_patch_switch(scene_idx, w < R.ow);
}

static void scene_only_init(int set) {
    decide();   // R.dlss_k, the start mode's share (nothing on the 1.0.1 path has asked for it yet)
    unsigned g[2];
    res_game_size(g);
    // The rebuild site is always on, so the upscaler and its mode can be turned on and changed while the game runs, as
    // in other games; the level sizes follow through the scene set (scene_switch). AC_PATCH_SCENE=0 keeps both off (the
    // game's own levels; upscaling then runs only in its native mode).
    const char *force = cfg_get("AC_PATCH_SCENE");
    int rebuild = rt_patch_set("scene_rebuild");
    R.live = !(force && !strcmp(force, "0")) && rebuild >= 0;
    if (!R.live) {
        rt_log("resolution: %s: the scene set stays off, upscaling runs at the game's size",
               rebuild < 0 ? "the generated C has no scene_rebuild set" : "AC_PATCH_SCENE=0");
        rt_patch_switch(set, 0);
        rt_patch_switch(rebuild, 0);
        return;
    }
    scene_idx = set;
    R.ow = g[0], R.oh = g[1];
    scene_for(R.dlss_k, g, &R.sw, &R.sh);
    scene_switch(R.sw, R.sh);
    size_asked = pack(R.sw, R.sh);
    rt_patch_switch(rebuild, 1);
    R.on = 1;
    rt_log("resolution: scene %ux%u, output %ux%u (%s); upscaling can change while the game runs", R.sw, R.sh, R.ow,
           R.oh, start_mode());
}

// An upscaling change while the game runs (vk_dlaa.c, at a present): the new scene size waits in size_pending for the
// framework's per-frame window update (0x9db520, run by the main loop after each frame's draw), which writes it to the
// patch page and is told the display changed (rt_res_rebuild_w8). The game then runs its own display-change sequence
// there: it remakes its screen-sized resources and calls the callback its renderer setup registered for a docked /
// handheld change (0x40fa50, registered at 0x40e138), which releases the levels' scene targets and the other
// screen-sized ones and makes them again through 0x40e180 and 0x40e2e0, whose ladder reads the page through the scene
// set. (Calling 0x40fa50 alone, from the present, left that sequence half done: the game crashed a frame after the
// second change.) The page is written on the game's thread, right before that sequence, because the ladder reads width
// and height in two steps: a page written from the present during a rebuild could make targets of one mode's width and
// another's height. 1 when another size was asked for.
//
// ldur w8, [x29, #-0x34] at 0x9db5b0: the display-changed flag, set as well when a scene size is pending.
void rt_res_rebuild_w8(Cpu *c) {
    uint32_t changed = *(volatile const uint32_t *)(uintptr_t)(c->x[29] - 0x34);
    uint64_t want = __atomic_load_n(&size_pending, __ATOMIC_ACQUIRE);
    if (want && (want = __atomic_exchange_n(&size_pending, 0, __ATOMIC_ACQ_REL))) {
        unsigned w = (unsigned)want, h = (unsigned)(want >> 32);
        if (w != R.sw || h != R.sh) {   // the mode went away and back before the game got here: nothing to rebuild
            scene_switch(w, h);   // the level sizes the rebuild below reads
            R.sw = w, R.sh = h;   // vk_dlaa.c sees the new size from the frames drawn with it
            changed = 1;
            rt_log("resolution: the game rebuilds its scene targets at %ux%u", w, h);
        }
    }
    c->x[8] = changed;
}

int res_dlss_mode(const char *m) {
    decide();
    double k = dlss_share(m);
    if (k == R.dlss_k || !R.on) return 0;   // R.on: the scene set is on (scene_only_init)
    R.dlss_k = k;
    unsigned g[2] = {R.ow, R.oh}, w, h;
    scene_for(k, g, &w, &h);
    // Against the last size asked for, not the one built: going back to the built size while another still waits has
    // to replace that one, or the game would build the mode the player just left.
    uint64_t want = pack(w, h);
    if (want == size_asked) return 0;
    size_asked = want;
    __atomic_store_n(&size_pending, want, __ATOMIC_RELEASE);
    rt_log("resolution: upscaling %s: scene %ux%u from the next frame", m, w, h);
    return 1;
}

int res_scene_live(void) { return R.live; }

// For the menu's mode row: the scene size a mode ("native", "quality" ... "ultra_performance") gives at the game
// resolution, as res_dlss_mode would build it. The game resolution where the scene set is off.
void res_scene_for(const char *mode, unsigned out[2]) {
    unsigned g[2];
    res_game_size(g);
    scene_for(R.live ? dlss_share(mode) : 0, g, &out[0], &out[1]);
}

static PlatMutex init_lock = PLAT_MUTEX_INIT;   // res_init (boot) against res_auto_drop (vk_init, on the prewarm thread)

// output_res = auto took the screen's size because the settings asked for DLAA or DLSS, decided from the DLSS model's
// file before the device existed; NGX has now said DLSS cannot run on this GPU and nothing else temporal will
// (vk_dlaa.c vk_dlaa_init). Back to 1920x1080, or the run would draw SMAA at the screen's size, plain supersampling at
// several times the cost. The device is made before the game makes its render targets, and the res set's sites read
// the patch page each time they run, so rewriting the page is enough; the render-target budget and the application
// pool keep the larger size, unused. With the scene set on, the scene follows for the current mode.
void res_auto_drop(void) {
    plat_mutex_lock(&init_lock);
    if (auto_raised) {
        auto_raised = 0;
        rt_log("resolution: output_res auto: DLSS cannot run on this GPU and nothing else temporal is set: 1920x1080, "
               "not %ux%u",
               size_w, size_h);
        size_w = 1920, size_h = 1080;
        if (rt_patch_enabled(rt_patch_set("res"))) {
            uint32_t page[2] = {size_w, size_h};
            memcpy((uint8_t *)rt_patch_page() + RES_PAGE, page, sizeof page);
        }
        R.ow = size_w, R.oh = size_h;   // decide's output, if it already ran
        if (R.on) {
            unsigned g[2] = {size_w, size_h};
            scene_for(R.dlss_k, g, &R.sw, &R.sh);
            scene_switch(R.sw, R.sh);
            size_asked = pack(R.sw, R.sh);
        }
    }
    plat_mutex_unlock(&init_lock);
}

static unsigned page_size(int i) {
    float v;
    memcpy(&v, (const uint8_t *)rt_patch_page() + 16 + 4 * i, sizeof v);
    return v >= 64.0f && v <= SCENE_MAX ? (unsigned)v : 64u;
}

// The scene set's sites (each a csel of a level's width into w1 or height into w2).
void rt_res_scene_w1(Cpu *c) { c->x[1] = page_size(0); }
void rt_res_scene_h2(Cpu *c) { c->x[2] = page_size(1); }

static void init_sets(void) {
    res_pick();
    res_shadow_range();
    if (!output_set()) {   // 1.0.1: the res set took output_res above, the scene set scales the scene for DLSS
        int only = rt_patch_set("scene");
        if (only >= 0) scene_only_init(only);
        shadow_init(rt_patch_page());
        return;
    }
    if (!bigger()) {
        rt_log("resolution: the game's own (output 1920x1080, scene 1600x900)");
        shadow_init(rt_patch_page());
        return;
    }
    int output = rt_patch_set("output"), scene = rt_patch_set("scene");
    if (output < 0 || scene < 0) {
        rt_log("resolution: the generated C has no output and scene patch sets; the game keeps its own resolution");
        return;
    }
    fs_init();   // romfs.c serves the grown HeapDef from here on (res_heapdef_served)
    if (res_heap_extra_kib() && !R.heapdef_ok) {   // sizes at or below the shipped ones fit the shipped heaps
        rt_log("resolution: no grown HeapDef table; the game keeps its own resolution");
        return;
    }
    uint8_t *page = rt_patch_page();
    double k = RT_V143 ? (double)R.ow / BASE_W : 1.0;   // 1.4.3's own scaling of the scene with the output width
    int scene_on = R.scene_set || (!RT_V143 && (R.ow != BASE_W || R.oh != BASE_H));
    float sc[2] = {(float)(R.sw / k), (float)(R.sh / k)};
    uint64_t out = pack(R.ow, R.oh), none = 0;
    memcpy(page + 16, sc, sizeof sc);
    memcpy(page + 32, &out, 8);
    memcpy(page + 40, &none, 8);
    memcpy(page + 48, &out, 8);
    rt_patch_switch(output, R.ow != BASE_W || R.oh != BASE_H);
    rt_patch_switch(scene, scene_on);
    R.on = 1;
    // Another shape than 16:9 (more than 1% off): the camera takes the output's ratio (recomp/patches-v121.txt and
    // patches-v143.txt, set aspect), so a 21:9 output shows more of the world at the sides instead of a stretched 16:9 view. The HUD's layouts
    // for that shape come from romfs-patches-<w>x<h> (romfs.c overlay_init, tools/make_romfs_patches.py --aspect).
    double ratio = (double)R.ow / R.oh, game = (double)BASE_W / BASE_H;
    int aspect = rt_patch_set("aspect");
    if (!res_is_16x9(R.ow, R.oh)) {
        if (aspect < 0) {
            rt_log("resolution: output %ux%u is not 16:9 and this game version has no camera aspect patch; the view "
                   "is stretched", R.ow, R.oh);
        } else {
            float r = (float)ratio;
            memcpy(page + 56, &r, sizeof r);
            rt_patch_switch(aspect, 1);
            rt_log("resolution: camera aspect ratio %.4f (16:9 is %.4f)", ratio, game);
        }
    } else {
        rt_patch_switch(aspect, 0);   // the page holds no ratio
    }
    shadow_init(page);
    rt_log("resolution: output %ux%u, scene %ux%u%s; texture heap +%llu KiB", R.ow, R.oh, R.sw, R.sh,
           scene_on ? "" : " (the game's own 5/6 of the output)", (unsigned long long)res_heap_extra_kib());
}

void res_init(void) {
    plat_mutex_lock(&init_lock);
    init_sets();
    plat_mutex_unlock(&init_lock);
}

// DLSS upscaling: textures are sampled for the output's resolution, not the scene's (NVIDIA's DLSS guide: a mip bias
// of log2(render / output)), so their detail survives the scaling. 0 when the scene is the output's size; vk_dlaa.c
// applies it only while DLSS runs (the game's own scaling of a biased scene only shimmers).
float res_dlss_lod_bias(void) {
    decide();
    return R.on && R.sw < R.ow ? (float)log2((double)R.sw / R.ow) : 0.0f;
}

// 16:9 within 1% (1366x768 counts): the shape the game's own layouts and camera are made for.
int res_is_16x9(unsigned w, unsigned h) {
    double rel = ((double)w / h) / ((double)BASE_W / BASE_H);
    return rel >= 0.99 && rel <= 1.01;
}

// The game resolution asks for a romfs-patches-<w>x<h> overlay: it is not 16:9 and the game version has the camera
// aspect patch (set aspect); the UI layouts of another shape only fit a camera that follows the same shape.
int res_wants_wide_overlay(const unsigned out[2]) {
    return !res_is_16x9(out[0], out[1]) && rt_patch_set("aspect") >= 0;
}

// Field of view (AC_FOV, percent of the game's own, 70..140, live): the camera lens's vertical view angle times the
// factor on page +60 where the projection matrix is built from it, and where the scene setup copies it for its
// screen-size work (recomp/patches-v101.txt, set fov: rt_fov_s0). The horizontal angle follows through the aspect
// ratio. Cutscenes keep their authored framing: while the game's cutscene flag is set (rt_fov_s0 reads it directly) the
// angle passes unscaled, and the lens it builds then is the cutscene camera's. 100 switches the set off, so the camera
// code is the game's own. Runs on a settings change (gfx_present.c window_settings_live), the first time at the first
// present.
#define FOV_MIN 70
#define FOV_MAX 140

void rt_fov_s0(Cpu *c) {
    // The game's own cutscene flag (bit 30 of the word at main+0xDB8398), not frame_step_cutscene, which only follows
    // it while the 60 fps patch is on.
    uint32_t volatile *flags = *(uint32_t volatile *volatile *)((uintptr_t)MOD_MAIN_BASE + 0xDB8398ull);
    if (flags && (*flags >> 30 & 1)) return;   // the shot's own framing
    float k;
    memcpy(&k, (const uint8_t *)rt_patch_page() + 60, sizeof k);
    FP_SETS(0, VSF32(0) * k);
}

void res_fov_live(void) {
    static int set = -2, shown = 100;
    if (set == -2) set = rt_patch_set("fov");
    int pct = cfg_int("AC_FOV");
    pct = pct < FOV_MIN ? FOV_MIN : pct > FOV_MAX ? FOV_MAX : pct;
    if (pct == shown) return;
    shown = pct;
    if (set < 0) {
        rt_log("resolution: this game version has no field-of-view patch; AC_FOV=%d does nothing", pct);
        return;
    }
    float k = pct / 100.0f;
    memcpy((uint8_t *)rt_patch_page() + 60, &k, sizeof k);   // before the switch: a site that sees it on reads k
    rt_patch_switch(set, pct != 100);
    rt_log("resolution: field of view %d%% of the game's", pct);
}

void res_wanted(unsigned out[2]) {
    decide();
    out[0] = R.ow, out[1] = R.oh;
}

void res_sizes(unsigned out[2], unsigned scene[2]) {
    decide();
    if (R.on) {
        out[0] = R.ow, out[1] = R.oh;
        scene[0] = R.sw, scene[1] = R.sh;
        return;
    }
    out[0] = BASE_W, out[1] = BASE_H;
    scene[0] = SCENE_W, scene[1] = SCENE_H;
}
