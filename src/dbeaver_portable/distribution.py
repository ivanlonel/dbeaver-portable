"""The DBeaver Community distribution: release lookup, zip, update-site features, p2 install."""

import gzip
import io
import logging
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import override
lazy from collections.abc import Iterable, Sequence
lazy from xml.etree.ElementTree import Element

from defusedxml import ElementTree

from dbeaver_portable import PortableError, net

log = logging.getLogger(__name__)

VERSION_URL = "https://dbeaver.io/product/version.xml"
FILES_URL = "https://dbeaver.io/files"
UPDATE_SITE = "https://dbeaver.io/update/ce/latest/"
# Integrates DBeaver into an Eclipse IDE; pointless inside DBeaver itself.
IDE_FEATURE = "org.jkiss.dbeaver.ide.feature.feature.group"
ZIP_ROOT = "dbeaver"
PROFILE_DIR = "p2/org.eclipse.equinox.p2.engine/profileRegistry/DefaultProfile.profile"
# The update sites DBeaver's own profile knows, which its GUI also consults for dependencies.
PROFILE_REPOSITORIES = (
    f"{PROFILE_DIR}/.data/.settings/org.eclipse.equinox.p2.metadata.repository.prefs"
)
PROPERTIES_ESCAPE = re.compile(r"\\(.)")
REPOSITORY_KEY = re.compile(r"repositories/([^/]+)/([^/]+)")
CATEGORY_PROPERTY = "org.eclipse.equinox.p2.type.category"
ROOT_PROPERTY = "org.eclipse.equinox.p2.type.root"
INSTALL_FOLDER_PROPERTY = "org.eclipse.equinox.p2.installFolder"
EXACT_RANGE = re.compile(r"\[([^,\]]+),\1\]")
DIRECTOR_TIMEOUT_SECONDS = 1800
OUTPUT_TAIL_LINES = 40


class DistributionError(PortableError):
    """The DBeaver distribution or its update site is not in the expected shape."""


@dataclass(frozen=True)
class Product:
    """Identity of an extracted DBeaver installation, from its .eclipseproduct file."""

    version: str
    qualifier: str


@dataclass(frozen=True, order=True)
class Feature:
    """An installable p2 feature group pinned to an exact version."""

    unit_id: str
    version: str

    @override
    def __str__(self) -> str:
        """Return the id/version form the p2 director's -installIU expects."""
        return f"{self.unit_id}/{self.version}"


def latest_version() -> str:
    """Return the version number of the latest DBeaver Community release."""
    number = ElementTree.fromstring(net.fetch(VERSION_URL)).findtext("number")
    if not number:
        msg = f"{VERSION_URL} has no <number> element"
        raise DistributionError(msg)
    return number.strip()


def zip_name(version: str) -> str:
    """Return the file name of the Windows x86-64 zip of a release."""
    return f"dbeaver-ce-{version}-windows-x86_64.zip"


def download_zip(version: str) -> Path:
    """Return the cached zip of a release, downloading it when missing or not matching its MD5."""
    name = zip_name(version)
    expected = net.fetch(f"{FILES_URL}/{version}/checksum/{name}.md5").decode().split()[0].lower()
    path = net.cache_dir() / "dbeaver" / name
    if path.is_file() and net.file_digest(path, "md5") == expected:
        log.info("Using cached %s", name)
        return path
    log.info("Downloading %s", name)
    url = f"{FILES_URL}/{version}/{name}"
    return net.download(url, path, digest=("md5", expected), show_progress=True)


def extract(zip_path: Path, dest: Path) -> Path:
    """
    Extract the zip into dest and return the app folder (dest/dbeaver).

    Members are written one at a time, after checking that each stays inside the zip's single top
    folder, so a malformed archive cannot write anywhere else.
    """
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            target = root.joinpath(*PurePosixPath(member.filename).parts).resolve()
            if not target.is_relative_to(root / ZIP_ROOT) or "\\" in member.filename:
                msg = f"unexpected entry in {zip_path.name}: {member.filename}"
                raise DistributionError(msg)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as out:
                shutil.copyfileobj(source, out)
    return root / ZIP_ROOT


def pristine_files(zip_path: Path) -> frozenset[str]:
    """Return the app-relative POSIX paths of every file the zip ships."""
    with zipfile.ZipFile(zip_path) as archive:
        return frozenset(
            PurePosixPath(name).relative_to(ZIP_ROOT).as_posix()
            for name in archive.namelist()
            if not name.endswith("/")
        )


def _profile_timestamp(name: str) -> int:
    return int(PurePosixPath(name).name.split(".", 1)[0])


def original_location(zip_path: Path) -> str:
    """Return the install folder recorded in the zip's own p2 profile (DBeaver's build path)."""
    prefix = f"{ZIP_ROOT}/{PROFILE_DIR}/"
    with zipfile.ZipFile(zip_path) as archive:
        names = [n for n in archive.namelist() if n.startswith(prefix) and n.endswith(".gz")]
        if not names:
            msg = f"{zip_path.name} has no p2 profile"
            raise DistributionError(msg)
        profile = ElementTree.fromstring(
            gzip.decompress(archive.read(max(names, key=_profile_timestamp)))
        )
    for prop in profile.iter("property"):
        if prop.get("name") == INSTALL_FOLDER_PROPERTY:
            return prop.get("value", "")
    msg = f"the p2 profile in {zip_path.name} has no {INSTALL_FOLDER_PROPERTY}"
    raise DistributionError(msg)


def read_product(app_dir: Path) -> Product:
    """Return the version and build qualifier of an extracted DBeaver."""
    properties = dict(
        line.split("=", 1)
        for line in (app_dir / ".eclipseproduct").read_text("utf-8").splitlines()
        if "=" in line and not line.startswith("#")
    )
    version = properties.get("version", "").strip()
    qualifier = properties.get("snapshot", "").strip().rpartition("-")[2]
    if not version or not qualifier:
        msg = f"{app_dir / '.eclipseproduct'} lacks version or snapshot"
        raise DistributionError(msg)
    return Product(version, qualifier)


def category_features(content: Element) -> list[Feature]:
    """Return every feature group a p2 category requires, minus the Eclipse-IDE integration."""
    features: set[Feature] = set()
    for unit in content.iter("unit"):
        properties = {p.get("name"): p.get("value") for p in unit.findall("properties/property")}
        if properties.get(CATEGORY_PROPERTY) != "true":
            continue
        for required in unit.findall("requires/required"):
            name = required.get("name", "")
            exact = EXACT_RANGE.fullmatch(required.get("range", ""))
            if name.endswith(".feature.group") and name != IDE_FEATURE and exact:
                features.add(Feature(name, exact.group(1)))
    return sorted(features)


def update_site_features(site: str = UPDATE_SITE) -> list[Feature]:
    """Return the installable features offered by the update site's categories."""
    with zipfile.ZipFile(io.BytesIO(net.fetch(site + "content.jar"))) as jar:
        return category_features(ElementTree.fromstring(jar.read("content.xml")))


def check_matches(features: Iterable[Feature], product: Product) -> None:
    """Fail when the update site belongs to a different build than the extracted DBeaver."""
    stale = [str(f) for f in features if not f.version.endswith(product.qualifier)]
    if stale:
        msg = (
            f"the update site does not match DBeaver {product.version}"
            f" (build {product.qualifier}), probably because a newer release is being"
            f" published; run the build again: {stale}"
        )
        raise DistributionError(msg)


def latest_profile(app_dir: Path) -> Path:
    """Return the newest p2 profile snapshot of an installation."""
    snapshots = list((app_dir / PROFILE_DIR).glob("*.profile.gz"))
    if not snapshots:
        msg = f"no p2 profile under {app_dir}"
        raise DistributionError(msg)
    return max(snapshots, key=lambda p: _profile_timestamp(p.name))


def profile_roots(app_dir: Path) -> set[str]:
    """Return the ids of the root installable units of an installation's p2 profile."""
    profile = ElementTree.fromstring(gzip.decompress(latest_profile(app_dir).read_bytes()))
    return {
        units.get("id", "")
        for units in profile.iter("iuProperties")
        for prop in units.iter("property")
        if prop.get("name") == ROOT_PROPERTY and prop.get("value") == "true"
    }


def known_repositories(app_dir: Path) -> list[str]:
    """
    Return the enabled remote update sites preconfigured in an installation's p2 profile.

    Extensions depend on Eclipse bundles that only these sites provide (the Debugger needs
    org.eclipse.debug.ui from the Eclipse release train); the GUI installer finds them this way.
    """
    path = app_dir / PROFILE_REPOSITORIES
    sites: dict[str, dict[str, str]] = {}
    for line in path.read_text("utf-8").splitlines() if path.is_file() else []:
        key, _, value = line.partition("=")
        if match := REPOSITORY_KEY.fullmatch(key):
            name, field = match.groups()
            sites.setdefault(name, {})[field] = PROPERTIES_ESCAPE.sub(r"\1", value.strip())
    return [
        site["uri"]
        for site in sites.values()
        if site.get("enabled") == "true" and site.get("uri", "").startswith("https://")
    ]


def install_features(app_dir: Path, features: Sequence[Feature], work_dir: Path) -> None:
    """
    Install features into app_dir with DBeaver's bundled p2 director.

    APPDATA and the workspace point inside work_dir, so the run never touches the user's own
    DBeaver profile.
    """
    if not features:
        return
    appdata = work_dir / "appdata"
    appdata.mkdir(parents=True, exist_ok=True)
    command = [
        str(app_dir / "dbeaverc.exe"),
        "-nosplash",
        "-application",
        "org.eclipse.equinox.p2.director",
        "-repository",
        ",".join(dict.fromkeys([UPDATE_SITE, *known_repositories(app_dir)])),
        "-installIU",
        ",".join(map(str, features)),
        "-data",
        str(work_dir / "p2-workspace"),
    ]
    log.info("Installing plugins: %s", ", ".join(f.unit_id for f in features))
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        command,
        env=os.environ | {"APPDATA": str(appdata)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=DIRECTOR_TIMEOUT_SECONDS,
        check=False,
    )
    missing = {f.unit_id for f in features} - profile_roots(app_dir)
    if result.returncode or missing:
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-OUTPUT_TAIL_LINES:])
        msg = (
            f"the p2 director failed (exit {result.returncode}, missing {sorted(missing)}):"
            f"\n{tail}"
        )
        raise DistributionError(msg)
