# Astral Chain Recomp

**Astral Chain Recomp** is a static recompilation of *Astral Chain* (Nintendo Switch, 2019) into a native x86-64
application for Windows, built on the pipeline of [TotK Recomp](https://github.com/valentderah/totk-pc-recomp)
([docs/architecture.md](docs/architecture.md)).

> [!IMPORTANT]
> **No game files are included.** You need your own legally obtained copy of *Astral Chain*, dumped from your own
> console: the base game (version 1.0.0) and the 1.0.1 update. Instructions for obtaining those files will not be
> provided.
>
> The game is built from your files on your machine, so the result contains the game. It is for your own use. Do not
> share it.

## Status

The game is playable from the title screen through the story, including its audio and its save data. The only known issue is the assault attack at 60FPS has bad input timers at 60FPS (long known unresolved issue in Emulation), compiled code is being analyzed to be patched. At 30FPS, the timer is accurate so players can temporarly switch to 30FPS during act 4 to pass the tutorial.

Report problems as issues, following [CONTRIBUTING.md](CONTRIBUTING.md).

## Features

- Native code, not emulation. It already runs on very old systems; it uses a small amount of RAM and VRAM to run.
- Externally tested (Reddit, YouTube, ...): playable from intro to credits with no known visual issues.
- Base game 1.0.0 with its 1.0.1 update
- 60 fps by default, with a 30 fps switch and a cutscene speed fix
- The game's own audio: music, effects and voices
- Custom resolutions, AMD FSR 1 upscaling up to 4K and emulator-style 3D render scaling
- Anisotropic texture filtering up to 16x, for sharp ground into the distance
- In-game settings menu (F1) and performance overlay (F10)
- Setup and in-game menu in English, Russian, Brazilian Portuguese and Simplified Chinese
- Keyboard, mouse and gamepad, with remapping, rumble and gyro
- Xbox and PlayStation button prompts (button mods drawn at the Switch positions; PlayStation mod from https://gamebanana.com/mods/604666)
- Mods and data cheats from the emulator formats
- Shaders prepared at install instead of during play

## Planned

- Optimizations for 4gb VRAM GPU and Steam Deck

## Installing

Download `Astral Chain-Setup-<version>-windows.zip` from [Releases](../../releases), unpack it anywhere and start
`Astral Chain-Setup.exe`. It asks for:

- your own copy of the base game (version 1.0.0, XCI or NSP) and of the 1.0.1 update (NSP);
- your `prod.keys`;
- the game language and the folder to install into.

It then extracts the game's data, merges the update into it, lifts the game's code to C, and builds `astralchain.exe` on
your machine: about a minute on a fast CPU, a few minutes on a slow one, about 14 GB for the installed folder and about
11 GB of temporary space while the update is merged. The setup carries no game data and no code derived from the game.

To save those 10 GB, tick **Read the game data from these files instead of copying it**: the install folder then holds
no copy of the game data (about 4 GB in all), and the game reads it from your base game, update and keys files at
every start, so keep those three where they are.

You need Windows 10 or 11 on x86-64 (SSE4.2 at least) and a Vulkan 1.3 graphics card. The exe is unsigned, so
SmartScreen warns on the first start.

## Credits

Astral Chain Recomp builds on the pipeline of [TotK Recomp](https://github.com/valentderah/totk-pc-recomp), which
supplied the lifter, the runtime, the graphics translation, the installer and most of the documentation this port
started from. What was taken from each project, with its license, is listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

- [TotK Recomp](https://github.com/valentderah/totk-pc-recomp)
- [LAS Recomp](https://github.com/peiikai/LASRecomp)
- [MacNativeGames](https://github.com/ly05010419/MacNativeGames)
- [nxnative](https://github.com/seedds/nxnative)
- [Ryujinx](https://github.com/Ryujinx/Ryujinx)
- [deko3d](https://github.com/devkitPro/deko3d)
- [SDL 3](https://libsdl.org), [Dear ImGui](https://github.com/ocornut/imgui), [AMD FSR 1](https://github.com/GPUOpen-Effects/FidelityFX-FSR), [Vulkan Memory Allocator](https://github.com/GPUOpen-LibrariesAndSDKs/VulkanMemoryAllocator), [libopus](https://opus-codec.org), [Zstandard](https://github.com/facebook/zstd)
- [Switchbrew](https://switchbrew.org) and the Switch homebrew community

## License

The project's own code is under the [MIT License](LICENSE); third-party code keeps its own license
([THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)). The license grants nothing for Nintendo's game.

Astral Chain Recomp is an independent, non-commercial fan project, not affiliated with or endorsed by Nintendo.
*Astral Chain*, *PlatinumGames* and *Nintendo Switch* are trademarks of their respective owners.

## Building from source

[docs/building.md](docs/building.md) lists the toolchain and the steps. Everything the build writes stays under
`build\` and `generated\`, which git ignores; run `tools/check_content.py` before a commit.
