# Installer

A public release that turns the player's own game files into a playable Astral Chain folder. The release carries no game
data and no code derived from the game: the installer builds the game on the player's machine. The installed folder
holds only the game; the installer and its tools stay outside it.

The port builds the 1.0.1 update: the player chooses the base game (1.0.0, XCI or NSP) and the 1.0.1 update NSP. The
update supplies the new code and a delta RomFS section, which hactool merges with the base RomFS. There is no firmware
folder to read: the game's own data carries everything.

## What the player does

1. Downloads `Astral Chain-Setup-<version>-windows.zip` from GitHub Releases, unpacks it anywhere and starts
   `Astral Chain-Setup.exe`.
2. Picks:
   - the base game (XCI or NSP, version 1.0.0);
   - the update (NSP, version 1.0.1);
   - their `prod.keys`;
   - the game language and the install folder.
3. Waits for the setup, one time. The window shows each step and its progress.
4. Plays `astralchain.exe` from the install folder. The installer and the source files can be deleted, except with
   **Read the game data from these files instead of copying it** (`--romfs-source dumps`, see below).

### Reading the game data from the player's files

By default the extract step writes the merged 1.0.1 RomFS into `romfs/romfs.bin` (about 10 GB, next to the player's
own copy of the same data). With `--romfs-source dumps` (the window's checkbox) it writes `romfs/romfs-map.bin`
instead: `tools/romfs_source.py` resolves the update's BKTR relocation and subsection tables once into about 360
extents, each a range of the merged image that is one AES-CTR run in the base game or the update file. The map names
both files and the keys file by path and holds no key material. At every start `runtime/romfs_source.c` opens them,
derives the two keys from the keys file, checks each file's size, its NCA header hash and a hash of its first
decrypted bytes, and from then on reads the image straight out of the two files, decrypting in place (AES-NI; the
game's full-image SHA-256 runs at the disk's speed). The extract step reads the whole image through the map once and
compares its SHA-256 with `tools/data/expected-v1.0.1.json`, as for `romfs.bin`. Nothing else changes: the RomFS index,
the button-prompt archives and the shader census read the image through the same map.

The choice is recorded in `astralchain-install.json` and kept by later runs; `--romfs-source image` goes back to the
copy. Moving, replacing or deleting the base game, the update or the keys file stops the game at start with a message
naming the file; running the setup again with the files at their new place fixes it.

The install folder (`main.c` `portable_paths()` reads this layout; it stays portable):

```
<install folder>/
  astralchain.exe, SDL3.dll, libopus-0.dll, ac_xlate.exe
  astralchain.ini                       language; the other keys as commented defaults (astralchain.ini.example)
  exefs/, romfs/                        extracted from the player's files (romfs/: romfs.bin, or romfs-map.bin)
  shader-cache/                         the translated shaders, packed into one shader-cache.pack
  shader-prepared.bin                   the variants prepared ahead of play (read at boot to warm the driver)
  profile/                              saves
  mods/                                 the player's mods, one folder per mod (created by the menu's "Open mods folder")
  logs/                                 astralchain.log of the last run, astralchain.1.log to astralchain.4.log before it
  build-cache/                          what the setup keeps to rebuild the game for the mods: generated C, objects,
                                        logs and status files
  astralchain-install.json              what built this folder: release version, input hashes, finished steps
```

## Updating the port

The player runs a newer installer on the same folder. It reads `astralchain-install.json`, keeps `romfs/`, `exefs/`,
`profile/` and `astralchain.ini` after checking their hashes and rebuilds `astralchain.exe`. A newer release that
changes nothing game-derived skips the rebuild. An installation made before update support (1.0.0) needs the game files
again: it is rebuilt from the 1.0.1 update.

## Setup steps

1. **Check** before writing anything: both files open with the keys, the base game's code modules (`main`, `sdk`, `rtld`)
   match the 1.0.0 hashes, the update's `main` matches 1.0.1, enough space is free on the target disk (about 25 GiB
   while the update is merged, 14 GiB once it is cleaned up, 4 GiB more for the generated C and the objects in
   `build-cache/`), the CPU is at least x86-64-v2 (SSE4.2; with AVX2/FMA the faster `v3` build is used, otherwise the
   `v2` one), a Vulkan 1.3 GPU exists. Each failure is one clear message.
   `main.npdm` is not compared: it is the process's metadata, dumps of the same build carry different ones (repacked
   NSPs, different dumpers), and the game checks the fields it uses at start (`runtime/boot.c` `check_npdm`); a
   differing one is a warning.
   A version mismatch is not a dead end: the window offers **Continue anyway**, which runs the same setup with the
   check reported as a warning (`--accept-unknown-version` on the command line). The game may then not run or crash;
   the player decides.
2. **Extract** the update's ExeFS into the install folder and merge its RomFS BKTR section with the base game's Program
   NCA through hactool, verifying every IVFC level, and compare the result with `tools/data/expected-v1.0.1.json`.
   With `--romfs-source dumps` it writes `romfs/romfs-map.bin` instead and checks the image read through it.
3. **Lift** the ExeFS to C (`recomp/gen.py`, `recomp/patches-v101.txt`) into `build-cache/gen/` in the install folder.
   The code cheats of the mods' cheat files and the mods' ExeFS patch files become patch sets first
   (`tools/exefs_patches.py`, status in `build-cache/exefs.status`); a file the tool refuses costs those patches, not
   the install.
4. **Compile** the generated C, `modules.c`, `romfs.c`, `patches.c` and `ov_zstd.c` (they bake the per-install layout,
   RomFS hashes and the game version's patch page) and link them with the prebuilt runtime library for the host's level
   (`libacrt.a` or `libacrt-v2.a`) into `astralchain.exe` (`build_game.py --runtime-lib`; about 3 min on 24 cores).
5. **Prepare** the shaders: `tools/prepare_shaders.py` reads the variants players met (`data/shader-answers.bin`) and
   the shaders themselves from the RomFS (`tools/shader_census.py`), adds each shader's default variant, and writes the
   seed files; `astralchain.exe --prepare-shaders` then translates every variant and builds each stage library or compute
   pipeline once, so the GPU driver's own disk cache holds them. `tools/pack_shader_cache.py` folds the loose files of
   the cache into one `shader-cache.pack`. A driver whose compiler crashes on a variant skips it (the game translates it
   when first needed), reported as a warning.
6. **Clean up**: delete the temp folder (its run logs move to `build-cache/logs/`), remove the copied Program NCAs, and
   write `build-cache/rebuild.txt` in the install folder.

A cancelled or failed setup resumes from the last finished step (`astralchain-install.json`).

## Rebuilding for the mods

The generated C and the objects stay in the install folder's `build-cache/`, so the next lift rewrites only the chunks
whose C changed and Ninja recompiles only those: a new or edited ExeFS patch costs the lift (under a minute on 24 cores,
a few minutes on 4) and a few dozen chunks, not a full compile.

`build-cache/rebuild.txt` holds the command that opens the setup window in its rebuild mode on the install folder
(`installer/gui.py --rebuild <install folder>`, started through `python -c` so the release folder is on `sys.path`
whatever the working folder), one argument per line. When an enabled mod has ExeFS patch files the build lacks,
`astralchain.exe` runs it at start and exits (`runtime/cheats.c` `rebuild_if_needed`); the window runs the setup on the
folder at once and starts the game again when it succeeds. A failed or cancelled rebuild offers to start the game with
the code it has; `build-cache/rebuild.skip` then keeps the same files from starting it again, until "Rebuild the game
now" on the Mods tab. `[mods] mods_rebuild = off` turns it off. The rebuild needs the release folder the setup ran
from: a moved or deleted release folder means running a setup again.

## Command line

`installer/` (stdlib only; `installer/layout.py` maps the release folder and, in a repository, the dev toolchain):

```
python -m installer --base FILE --update FILE --keys FILE --lang CODE --dest DIR [--root DIR] [--temp DIR] [--verify]
```

- A first install needs the base game, the update NSP and the keys. On a folder with `astralchain-install.json` only
  `--dest` is needed; the game files only when the extraction must run again (an installation made before update
  support needs them).
- stdout: one JSON event per line, types `start`, `plan`, `step` (started, done, skipped), `progress` (step, phase,
  done/total/unit), `space` (bytes needed and free on the install disk and the temp disk), `warning`, `error`, `end`
  (`installer/events.py`). Messages are ids with parameters; `installer/text/en.json` holds their English text. The
  human-readable log goes to `<install>/build-cache/logs/` (a failed or cancelled run leaves it in `<temp>/logs/`).
- Exit codes: 0 done, 1 failed, 2 bad command line, 3 cancelled. The window cancels with Ctrl+Break to the installer's
  process group.
- Each step records a key over its inputs (runner.py). A step with the recorded key and intact output is skipped, so
  resume and update are one rule; output kept from another release is re-hashed first.
- Window (`python -m installer.gui`, `installer/gui.py` draws, `installer/guilogic.py` decides): form, step list with
  progress, space line, collapsible log, Cancel (Ctrl+Break to the CLI's process group, then it waits for the exit),
  finish screen with Open folder. English, Russian, Brazilian Portuguese or Simplified Chinese, chosen from the Windows
  display language (zh-CN and zh-SG give Chinese; the Traditional Chinese regions give English), switchable in the
  window; every sentence is an id in `installer/text/<en|ru|pt-BR|zh-Hans>.json`. A folder with
  `astralchain-install.json` needs only the folder. `--selftest [--shots DIR]` builds every screen in every language and
  closes itself.
- `tests/installer_gui_test.py`: event model, form, catalogs, cancel, the self-closing window and one end-to-end run of
  the CLI in `--verify` mode (needs `build/install-test`; see its docstring).
- `tests/installer_test.py`: bookkeeping with fake steps. `tests/installer_e2e.py`: a dev-mode run with the files of
  `config.local.toml`.

## Release layout

The unpacked release; `tools/make_release.py` assembles it and `--zip` packs it:

```
Astral Chain-Setup.exe        starts python/pythonw.exe -m installer.gui (installer/launcher/launcher.c, no console)
installer/            the installer: steps, CLI, window, en/ru/pt-BR/zh-Hans text catalog
python/               Python 3.14.9 embeddable + numpy, lz4, cryptography, cffi, pycparser (+ tkinter for the window)
recomp/, tools/       the lifter and the extraction, build and patch scripts the steps run (copied from the repo)
llvm/                 llvm-mingw, x86_64-only subset (Apache-2.0 with LLVM exception; mingw-w64 parts permissive)
runtime/              libacrt.a, libacrt-v2.a, the headers the generated code and modules.c/romfs.c/patches.c include,
                      and deps/ (Vulkan, SDL3, Opus, libvpx headers and import libraries)
bin/                  ac_xlate.exe (+ .id), hactool.exe, SDL3.dll, libopus-0.dll, ninja.exe
licenses/             the licence files of everything bundled
THIRD_PARTY_NOTICES.md
astralchain.ini.example, VERSION  the default ini; the release string (the repository's VERSION plus the commit)
```

- SDL3 comes from the upstream release, not MSYS2: MSYS2's DLL imports the LGPL libiconv.
- Every bundled third-party piece gets a row in `THIRD_PARTY_NOTICES.md`.
- The release contains nothing game-derived; `make_release.py` runs `tools/check_content.py` over the folder before it
  finishes, then starts the folder's own installer to see the check step reach the game files and builds every window
  screen in both languages.

## Constraints

- The keys file path is passed to the tools; key values never appear in the UI, logs or files.
- The window is a front end over the CLI (`python -m installer --base ... --keys ... --lang ... --dest ...`), which
  prints one JSON progress event per line. Tests and CI use the CLI.
- Paths may hold any Unicode characters (a Cyrillic user name puts the temp and install folders under it). Every exe
  the setup runs or builds reads its arguments and paths as UTF-8 through an `activeCodePage` manifest: hactool (our
  patch, [building.md](building.md)), astralchain.exe (`runtime/totk.rc`), ninja (upstream). The Python steps write
  their files and logs as UTF-8, and `build_game.py` quotes paths with spaces on its Ninja command lines.
- A game file whose data fails its hash checks or ends early stops the setup with `check.input_damaged`, which names
  the file and asks to copy it again.
- Unsigned exe: SmartScreen warns on first start.

## Later

- Linux release (`tools/make_release_linux.py` does not exist in this port yet).
- NSZ and XCZ input.
