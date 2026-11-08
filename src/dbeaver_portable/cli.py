"""Command line: ``dbeaver-portable build`` and ``dbeaver-portable capture-settings``."""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
lazy from collections.abc import Sequence

from dbeaver_portable import PortableError, distribution, drivers, installer, maven, profile, scrub

log = logging.getLogger("dbeaver_portable")

# The settings Create-Portable-QGIS.ps1 uses: LZMA2 with a 256 MB dictionary, solid archive.
SEVEN_ZIP_ARGS = ("a", "-t7z", "-mx=9", "-m0=lzma2:d=256m:fb=273", "-ms=on", "-mmt=2", "-bsp1")
PACKAGE_DIR = "dbeaver"  # the archive's top folder
EXIT_INTERRUPTED = 130


def _live_profile() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "DBeaverData"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbeaver-portable",
        description="Build a no-admin DBeaver Community package for Windows.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log every step")
    commands = parser.add_subparsers(dest="command", required=True)

    build = commands.add_parser(
        "build",
        help="build DBeaver-<version>.7z from the latest DBeaver Community release",
        description="Download the latest DBeaver Community release, install its extensions and"
        " drivers, add the curated defaults, the chosen data sources and the installer, and"
        " compress it all into DBeaver-<version>.7z.",
    )
    build.add_argument(
        "--datasources",
        nargs="+",
        metavar="NAME",
        help="data sources to include: 'all', 'none' or names/ids (default: ask)",
    )
    build.add_argument(
        "--profile",
        type=Path,
        default=_live_profile(),
        help="DBeaverData folder to take data sources from (default: %(default)s)",
    )
    build.add_argument(
        "--output-dir",
        type=Path,
        default=Path(),
        help="folder for the archive (default: the current folder)",
    )
    build.add_argument("--force", action="store_true", help="overwrite an existing archive")
    build.add_argument(
        "--keep-work", action="store_true", help="keep the work folder for inspection"
    )
    build.set_defaults(handler=_build)

    capture = commands.add_parser(
        "capture-settings",
        help="refresh the curated defaults from a DBeaverData folder",
        description="Replace the curated defaults with the allowlisted settings of a DBeaverData"
        " folder, dropping machine-specific values. Review the result with git diff.",
    )
    capture.add_argument(
        "--from",
        dest="source",
        type=Path,
        default=_live_profile(),
        help="DBeaverData folder to read (default: %(default)s)",
    )
    capture.set_defaults(handler=_capture)
    return parser


def _prompt(sources: Sequence[profile.DataSource], location: Path) -> list[str]:
    private = [v.lower() for v in scrub.path_variants(Path.home())]
    width = max(len(s.name) for s in sources)
    print(f"\nData sources in {location}:")
    for number, source in enumerate(sources, 1):
        flag = (
            "  <- file inside your user profile"
            if any(p in source.target.lower() for p in private)
            else ""
        )
        print(f"  {number:>2}  {source.name:<{width}}  {source.driver:<28}  {source.target}{flag}")
    while True:
        answer = input("Data sources to include (e.g. 1,3-4, all, none) [none]: ")
        try:
            return [sources[i].source_id for i in profile.parse_selection(answer, len(sources))]
        except ValueError as error:
            print(f"  {error}")


def _choose_data_sources(names: list[str] | None, profile_dir: Path) -> dict[str, Any] | None:
    location = profile_dir / profile.DATA_SOURCES
    document, sources = profile.load_data_sources(location)
    if names is None:
        if not sources or not sys.stdin.isatty():
            return None
        ids = _prompt(sources, location)
    elif [n.lower() for n in names] == ["none"]:
        return None
    elif [n.lower() for n in names] == ["all"]:
        ids = [s.source_id for s in sources]
    else:
        ids = profile.select_named(sources, names)
    return profile.filter_data_sources(document, ids) if ids else None


def _assemble(version: str, work: Path, data_sources: dict[str, Any] | None) -> Path:
    package = work / "package" / PACKAGE_DIR
    zip_path = distribution.download_zip(version)
    log.info("Extracting %s", zip_path.name)
    app = distribution.extract(zip_path, package)
    product = distribution.read_product(app)
    if product.version != version:
        msg = f"{zip_path.name} contains DBeaver {product.version}, not {version}"
        raise PortableError(msg)
    features = distribution.update_site_features()
    distribution.check_matches(features, product)
    distribution.install_features(app, features, work)
    log.info("Removing runtime state and build paths from the app")
    scrub.scrub_app(
        app, distribution.pristine_files(zip_path), distribution.original_location(zip_path)
    )
    if missing := scrub.missing_bundles(app):
        msg = f"the installation lost {len(missing)} plugin(s), e.g. {missing[0]}"
        raise PortableError(msg)
    home = package / "DBeaverData" / "drivers"
    documents = drivers.plugin_documents(app / "plugins")
    resolved = drivers.resolve_drivers(
        drivers.find_drivers(documents), maven.Repository(maven.central_fetch), home
    )
    client_files = drivers.download_client(drivers.find_client(documents), home)
    driver_files = drivers.verify(
        home, [lib for _, libs in resolved for lib in libs], client_files
    )
    installer.stage(
        package,
        defaults=profile.DEFAULTS_DIR,
        drivers_xml=drivers.render_drivers_xml(resolved),
        driver_files=driver_files,
        data_sources=data_sources,
    )
    return package


def _archive(seven_zip: str, package: Path, archive: Path) -> None:
    """Compress package into archive, replacing an older archive only once the new one is whole."""
    archive.parent.mkdir(parents=True, exist_ok=True)
    partial = archive.with_name(archive.name + ".part")
    partial.unlink(missing_ok=True)
    log.info("Compressing into %s", archive)
    subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [seven_zip, *SEVEN_ZIP_ARGS, str(partial), package.name], cwd=package.parent, check=True
    )
    partial.replace(archive)


def _build(args: argparse.Namespace) -> int:
    if sys.platform != "win32":
        msg = "building needs Windows: it runs DBeaver's own p2 director"
        raise PortableError(msg)
    seven_zip = shutil.which("7z")
    if not seven_zip:
        msg = "7-Zip (7z.exe) is not on PATH; install it with `winget install 7zip.7zip`"
        raise PortableError(msg)
    output_dir = args.output_dir.resolve()
    log.info("Looking up the latest DBeaver Community release")
    version = distribution.latest_version()
    archive = output_dir / f"DBeaver-{version}.7z"
    if archive.exists() and not args.force:
        msg = f"{archive} already exists; pass --force to overwrite it"
        raise PortableError(msg)
    data_sources = _choose_data_sources(args.datasources, args.profile)
    work = Path(tempfile.mkdtemp(prefix="dbeaver-portable-")).resolve()
    tokens = scrub.leak_tokens(work, Path.cwd(), output_dir)
    try:
        selected = json.dumps(data_sources or {}).lower()
        if found := [t for t in tokens if t.lower() in selected]:
            msg = f"the selected data sources contain machine-specific paths: {found}"
            raise PortableError(msg)
        package = _assemble(version, work, data_sources)
        if leaks := scrub.find_leaks(package, tokens):
            lines = "\n".join(
                f"  {leak.path.relative_to(package)}: {leak.token}" for leak in leaks
            )
            msg = f"machine-specific paths found in the package:\n{lines}"
            raise PortableError(msg)
        _archive(seven_zip, package, archive)
    finally:
        if args.keep_work:
            log.info("Work folder kept at %s", work)
        else:
            shutil.rmtree(work, ignore_errors=True)
    log.info("Done: %s (%.0f MB)", archive, archive.stat().st_size / 1e6)
    return 0


def _capture(args: argparse.Namespace) -> int:
    captured = profile.capture(args.source, profile.DEFAULTS_DIR, scrub.path_variants(Path.home()))
    for path in captured:
        log.info("  %s", path)
    log.info(
        "Captured %d file(s) into %s; review them with `git diff` before committing",
        len(captured),
        profile.DEFAULTS_DIR,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line and return its exit code."""
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s"
    )
    try:
        return int(args.handler(args))
    except (PortableError, OSError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
