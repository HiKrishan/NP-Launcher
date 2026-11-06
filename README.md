# NP Launcher

A full, offline-capable Minecraft launcher written in Python with a Flask backend and a
modern dark web UI (served locally — no Electron, no Node).

## Features

- **Vanilla launching** — any official Minecraft version (release / snapshot / old), downloaded and
  cached from Mojang's piston-meta manifest.
- **Modded support**
  - **Fabric** — via the official launcher-profile endpoint (clean, reliable).
  - **Forge / NeoForge** — official headless installers (`--installClient`), output profile auto-detected.
- **Modpacks** — browse & install [Modrinth](https://modrinth.com) modpacks (`.mrpack`), or import a
  local `.mrpack`. Mods are installed into the instance's `mods/` folder.
- **Modrinth mod browser** — search & install compatible mod versions per instance (loader + MC version aware).
- **Multiple instances** — each with its own folder: saves, mods, configs, resource packs, screenshots.
- **Java handled for you** — detects installed JVMs; if none is compatible it downloads a
  [Temurin](https://adoptium.net) JRE automatically (per version, cached).
- **Non-premium (offline) mode** — play with any username, no account required.
- **Skin preview** — renders your skin by username (via mc-heads.net).
- **RAM & JVM settings** — per-instance min/max memory and JVM args, plus global defaults.
- **News feed** — latest Minecraft news from minecraft.net.
- **Auto-update** — checks a GitHub Releases feed on startup (configurable repo slug).
- **Console drawer** — live download progress + game output log.

## Requirements

- Python **3.11+**
- Java: auto-handled (auto-detect, or auto-download Temurin if missing)
- Internet access for first-time downloads (~200–400 MB per Minecraft version)

## Run from source

```bash
pip install -r requirements.txt
python run.py             # opens http://127.0.0.1:5340 in your browser
python run.py --port 8080 # custom port
python run.py --no-browser
```

## Build a Windows executable

```bat
build_exe.bat
```

Produces `dist\NP Launcher\NP Launcher.exe`. The exe is self-contained (templates + static assets
are bundled); game data still lives in `~/.np-launcher`.

## Where is my data?

`~/.np-launcher` (override with the `NP_LAUNCHER_DATA` environment variable):

```
.np-launcher/
├── libraries/        # shared Maven libraries
├── assets/           # sounds, textures, indexes
├── versions/         # version JSONs + client jars
├── natives/          # extracted LWJGL natives
├── runtimes/         # Temurin JREs downloaded automatically
├── instances/        # one folder per instance (saves, mods, config…)
├── logs/             # per-instance game logs
├── cache/            # version manifest cache
└── config.json       # launcher settings
```

## Notes & honest caveats

- **Offline mode is not a Microsoft account.** UUIDs are derived with the standard
  `MD5("OfflinePlayer:<name>")` algorithm. Servers with online-mode enforcement won't accept you,
  and online play isn't possible without a premium account. This launcher is for local/offline play,
  modded instances and testing.
- **Forge / NeoForge** installers run headless with `--installClient`. Very old Forge builds
  (≈1.7–1.12, and some exotic setups) may refuse and fall back to a GUI — just close it and retry,
  or use a different loader. Watch the console drawer for installer output.
- **Skin previews** are fetched from mc-heads.net at render time; offline-only machines won't
  show a preview (the launcher still works fine).
- **Auto-update** needs a public GitHub repo with releases. Set it in
  *Settings → Auto-Update → GitHub repo* (e.g. `yourname/np-launcher`). Without it, the check is skipped.
- Modpack installs verify SHA-1/SHA-512 from the pack manifest; corrupt or blocked downloads are
  reported per-file in the console rather than failing silently.

## Project layout

```
NP Launcher/
├── run.py                # dev entry point
├── requirements.txt
├── np-launcher.spec      # PyInstaller spec
├── build_exe.bat / run.bat
└── launcher/
    ├── app.py            # Flask app + JSON API + background job runner
    ├── mojang.py         # version manifest, inheritance, rules, natives, assets
    ├── java.py           # JVM detection + Temurin auto-download
    ├── loaders.py        # Fabric profile / Forge / NeoForge installers
    ├── modrinth.py       # Modrinth search, mod install, .mrpack import
    ├── launch.py         # command building + game process management
    ├── instances.py      # instance CRUD
    ├── news.py / updater.py / config.py / download.py / paths.py
    ├── templates/index.html
    └── static/{css,js}/  # UI
```

## License

MIT — do what you like, but don't blame me when you flood your world with TNT creepers.
