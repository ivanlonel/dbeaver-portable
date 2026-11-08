"""The end-user installer shipped in every package (instalar.cmd -> setup.cmd -> setup.ps1)."""

import json
import shutil
from importlib.resources import files
from typing import Any
lazy from collections.abc import Iterable, Mapping
lazy from pathlib import Path

PORTABLE_DIR = "portable"


def stage(
    package: Path,
    *,
    defaults: Path,
    drivers_xml: str,
    driver_files: Iterable[str],
    data_sources: Mapping[str, Any] | None,
) -> Path:
    """
    Write the installer and everything it installs besides the app and drivers into package.

    package is the folder that becomes the archive's top ``dbeaver`` folder: ``instalar.cmd``
    goes there and the rest into ``dbeaver/portable``, which travels with the installed app so
    that reset and uninstall keep working after the package folder is gone.
    """
    here = files(__package__)
    portable = package / "dbeaver" / PORTABLE_DIR
    portable.mkdir(parents=True, exist_ok=True)
    # Windows PowerShell 5.1 reads BOM-less scripts as ANSI, which would garble its messages.
    script = here.joinpath("setup.ps1").read_text("utf-8")
    (portable / "setup.ps1").write_text(script, encoding="utf-8-sig", newline="\r\n")
    # cmd.exe misparses LF-only batch files in corner cases, whatever git checked out.
    for source, target in (("setup.cmd", portable), ("instalar.cmd", package)):
        text = here.joinpath(source).read_text("utf-8")
        (target / source).write_text(text, "ascii", newline="\r\n")
    shutil.copytree(defaults, portable / "defaults", dirs_exist_ok=True)
    (portable / "drivers.xml").write_text(drivers_xml, "utf-8", newline="\n")
    manifest = "".join(f"{path.replace('/', '\\')}\n" for path in driver_files)
    (portable / "drivers-manifest.txt").write_text(manifest, "utf-8", newline="\n")
    if data_sources:
        text = json.dumps(data_sources, indent=2, ensure_ascii=False) + "\n"
        (portable / "data-sources.json").write_text(text, "utf-8", newline="\n")
    return portable
