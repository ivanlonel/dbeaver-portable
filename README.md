# dbeaver-portable

Build a self-contained, no-admin [DBeaver Community](https://dbeaver.io) package for Windows: the
latest release with its extensions, ready-to-use JDBC drivers, the PostgreSQL client tools, curated
default settings, optional data sources and a one-step installer, all in a single `.7z`.

## Why

Setting DBeaver up on locked-down machines is tedious. Every user installs the extensions by hand,
downloads drivers through the GUI (often through a proxy that blocks it) and recreates the same
preferences. Hand-made bundles go stale instead: driver jars drift away from `drivers.xml`, old
versions pile up, settings snapshots carry machine paths, and DBeaver's own layout keeps changing.

This tool rebuilds the package from scratch on every run:

- **Plugins**: installs every extension the DBeaver update site lists (Git, Office, Debugger…)
  with DBeaver's own p2 director, without opening the GUI.
- **Drivers**: reads each driver's library list from the new DBeaver version itself, takes the
  latest stable version from Maven Central (no previews; same flavor as DBeaver's tested default,
  such as `.jre11`), resolves dependencies the way Maven does, and *generates* `drivers.xml` from
  exactly what it downloaded. A self-check refuses to package if the two ever disagree.
- **PostgreSQL client**: `psql`, `pg_dump` and `pg_restore` from DBeaver's own repository, so
  backups and restores work offline.
- **Defaults**: a small, reviewable overlay of deliberate settings (theme, formats, language,
  connection types…) instead of a profile snapshot. DBeaver regenerates everything else.
- **Data sources**: choose which of your connections to ship on each build. Credentials are
  never included.
- **No leaks**: runtime state is dropped, and the build fails if the package mentions your user
  profile, computer name or build paths.

## Requirements

- Windows 10 or 11, x64
- [uv](https://docs.astral.sh/uv/), which installs Python 3.15 for the project
- [7-Zip](https://www.7-zip.org/) on `PATH` (`winget install 7zip.7zip`)
- [just](https://just.systems), optional, for the recipes below

## Quick start

```powershell
just bootstrap   # create .venv and install the git hooks
just build       # build DBeaver-<version>.7z in the current folder
```

`just build --help` lists the options:

| Option                           | Meaning                                                                        |
| -------------------------------- | ------------------------------------------------------------------------------ |
| `--datasources all\|none\|NAME…` | Data sources to include, by name or id; asks when omitted                      |
| `--profile DIR`                  | DBeaverData folder to take data sources from (default `%APPDATA%\DBeaverData`) |
| `--output-dir DIR`               | Folder for the archive (default: the current folder)                           |
| `--force`                        | Overwrite an existing archive                                                  |
| `--keep-work`                    | Keep the work folder for inspection                                            |

Downloads are cached in `%LOCALAPPDATA%\dbeaver-portable\cache`.

## The package

```text
dbeaver\
├─ instalar.cmd          ← what users run
├─ dbeaver\              ← DBeaver, plus portable\ (installer, defaults, drivers.xml, data sources)
└─ DBeaverData\drivers\  ← driver jars, licenses and the PostgreSQL client
```

`instalar.cmd` needs no admin rights and talks to the user in Brazilian Portuguese. It:

- installs DBeaver to `%LOCALAPPDATA%\Programs\DBeaver` and the drivers to
  `%APPDATA%\DBeaverData\drivers`;
- on a first install, creates the workspace from the curated defaults;
- on an upgrade, keeps the user's settings (adding only default files they lack), refreshes the
  packaged drivers' entries in `drivers.xml`, adds packaged connections the user was never
  offered, and removes driver files that a previous package installed but this one no longer
  ships;
- creates Desktop and Start Menu shortcuts and an entry in Windows' *Installed apps*, whose
  *Modify* and *Uninstall* buttons offer resetting the preferences (keeping connections, scripts
  and drivers) and uninstalling.

## Curated defaults

`src/dbeaver_portable/defaults` holds the settings every new installation starts with, laid out
like `DBeaverData`. To change them, configure DBeaver the way you want and capture its settings:

```powershell
just capture-settings                            # from %APPDATA%\DBeaverData
just capture-settings --from D:\other\DBeaverData
git diff                                         # review before committing
```

Only allowlisted files are captured (DBeaver and Eclipse preference files, connection types and
the UI language), and machine-specific values are dropped.

## Development

```powershell
just check                       # format, lint (ruff, pylint, deptry), type-check (ty, mypy), test
just test                        # tests only; installer tests run setup.ps1 in Windows PowerShell 5.1
just sandbox DBeaver-26.2.4.7z   # install a built package into a throwaway sandbox and start it
just sandbox-clean
```

## License

[MIT](LICENSE). DBeaver is a trademark of DBeaver Corp; this project is not affiliated with it.
