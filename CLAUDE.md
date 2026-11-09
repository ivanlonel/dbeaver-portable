# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

`dbeaver-portable` builds `DBeaver-<version>.7z`, a no-admin DBeaver Community package for
Windows: the latest release with every update-site extension, the drivers in `drivers.TARGETS`
with a generated `drivers.xml`, DBeaver's PostgreSQL client, the curated `defaults/`, optional
data sources and the `installer/` scripts. Python 3.15 with uv, tasks in the `justfile` (pwsh),
hooks run by prek. The README covers usage and the package layout.

## Rules

- The repository is **public**. Keep employer names, usernames, computer names and local paths
  out of code, docs, `defaults/`, test fixtures and commit messages. Fixtures are synthetic, never
  copied from a real profile. Everything is in English except the installer's pt-BR messages.
- Never write to the live `%APPDATA%\DBeaverData`, the real Desktop or Start Menu, or the real
  `HKCU` uninstall key. Try packages with `just sandbox <archive>`, then `just sandbox-clean`.
- Packages never carry credentials (`profile.filter_data_sources`) or files DBeaver regenerates
  offline: caches, POMs, layout state, locks, logs.
- `installer/setup.ps1` must run on Windows PowerShell 5.1. The repo copy has no BOM (the
  `fix-byte-order-marker` hook strips it); `installer.stage` adds it, and CRLF, at build time.
  Values returned through function output come back PSObject-wrapped, which `JavaScriptSerializer`
  rejects as circular, so store `.psobject.BaseObject` (`Set-Entry`, `ConvertTo-JsonText`).
- Annotation-only imports use PEP 810 `lazy from … import`, not `if TYPE_CHECKING:`.
- Commit messages follow Conventional Commits (`feat: …`, `fix(installer): …`).

## DBeaver facts the code relies on

Verified on DBeaver 26.2.4. When a new release breaks the build, suspect these first.

- **Bundle pool.** p2 installs extensions under `configuration/org.eclipse.osgi/<id>/data/<hash>/`
  (`plugins/`, `features/`, `artifacts.xml`, `content.xml`), not in the app's `plugins/`, and
  `bundles.info` points there. It looks like OSGi cache but must ship: `scrub.scrub_app` keeps
  every pool `bundles.info` references, and the build fails on `scrub.missing_bundles`.
- **p2 repositories.** The update site alone is not enough: the Debugger needs
  `org.eclipse.debug.ui` from the Eclipse release train (the director exits 13 without it).
  `distribution.install_features` also passes the enabled sites that the zip's p2 profile
  preconfigures (`known_repositories`).
- **Roaming profile.** The zip's p2 profile has `roaming=true` and DBeaver's CI path as its
  install folder. The build rewrites its own paths back to that value
  (`distribution.original_location`, `scrub.rewrite_paths`).
- **First-run dialog.** DBeaver skips "Product Configuration" only when
  `DBeaverData/settings/product-config.json` exists. `capture-settings` copies it verbatim, and
  upgrades add the default files a user lacks (robocopy `/XC /XN /XO`) without overwriting theirs.
- **Data sources.** A lone `workspace6/General/.dbeaver/data-sources.json` is enough: DBeaver
  creates `General/.project` itself.
- **`drivers.xml`**:
  - In plugin.xml's `maven:/group:artifact:RELEASE[x]`, `x` is DBeaver's tested version. A
    library `version` above `x` overrides it, and `forced-version` keeps DBeaver from resetting
    it.
  - Libraries match plugin.xml by path with the bracket stripped. DBeaver downloads a library
    when it has no `<file>` entries or a listed file is missing, and never checks CRCs, so POMs
    are pure cache.
  - `crc` is CRC32 in lowercase hex without padding (Java's `Long.toHexString`).
  - Files live at `${drivers_home}\maven\maven-central\<group>\<artifact>-<version>[-<classifier>].<ext>`;
    remote license files at `${drivers_home}\remote\<url path>`.
  - Write a bare `<driver id>`: a `class`, `url` or `description` attribute there overrides the
    plugin's definition.

## Commands

```text
just bootstrap                              # once after cloning: .venv and git hooks
just check                                  # format, lint, type-check, then pytest
just prek                                   # every pre-commit hook, as CI runs them
just test -k maven                          # pytest; installer tests use Windows PowerShell 5.1
just build --datasources none --keep-work   # a real build (network; takes minutes)
just capture-settings                       # refresh defaults/, then review git diff
just sandbox DBeaver-<version>.7z           # install and start a package in .sandbox/
```
