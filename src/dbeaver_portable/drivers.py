"""DBeaver driver definitions (plugin.xml), their files, the PostgreSQL client and drivers.xml."""

import logging
import shutil
import urllib.parse
import xml.etree.ElementTree as ET  # building only; every parse goes through defusedxml
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from xml.etree.ElementTree import Element
lazy from collections.abc import Iterable, Iterator, Sequence

from defusedxml import ElementTree

from dbeaver_portable import PortableError, maven, net

log = logging.getLogger(__name__)

# (provider id, driver id) of every driver the package ships, in drivers.xml order.
TARGETS: tuple[tuple[str, str], ...] = (
    ("postgresql", "postgres-jdbc"),
    ("oracle", "oracle_thin"),
    ("sqlserver", "microsoft"),
    ("sqlite", "sqlite_jdbc"),
    ("mysql", "mysql8"),
    ("duckdb", "duckdb_jdbc"),
    ("generic", "msaccess_ucanaccess"),
    ("generic", "csvjdbc"),
)
CLIENT_ID = "postgresql_client"
PLATFORM_OS, PLATFORM_ARCH = "win32", "x86_64"
DATA_SOURCE_POINT = "org.jkiss.dbeaver.dataSourceProvider"
MAVEN_PREFIX = "maven:/"
REPO_PREFIX = "repo:/"
REPO_URL = "https://dbeaver.io/files/jdbc/"  # what DBeaver's repo:/ prefix points to
MAVEN_DIR = "maven/maven-central"
REMOTE_DIR = "remote"
DRIVERS_HOME = "${drivers_home}"
DOWNLOAD_WORKERS = 8
COORDINATES_WITH_CLASSIFIER = 4


class DriverError(PortableError):
    """A driver definition or its files are not what the package needs."""


@dataclass(frozen=True)
class Library:
    """A downloadable <file> of a driver definition."""

    kind: str
    path: str
    ignore_dependencies: bool = False


@dataclass(frozen=True)
class Driver:
    """A driver definition from a DBeaver plugin.xml."""

    provider: str
    driver_id: str
    libraries: tuple[Library, ...]


@dataclass(frozen=True)
class ClientDist:
    """A native client distribution (e.g. pg_dump and friends) DBeaver can download."""

    target_path: str
    remote_path: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class MavenReference:
    """A ``maven:/group:artifact[:classifier]:spec[default]`` library path."""

    group: str
    name: str
    classifier: str
    spec: str
    default: str | None

    @classmethod
    def parse(cls, path: str) -> MavenReference:
        """Parse a plugin.xml library path."""
        body, default = path.removeprefix(MAVEN_PREFIX), None
        if body.endswith("]") and "[" in body:
            body, _, bracket = body.rpartition("[")
            default = bracket[:-1]
        parts = body.split(":")
        if len(parts) == COORDINATES_WITH_CLASSIFIER:
            group, name, classifier, spec = parts
        elif len(parts) == COORDINATES_WITH_CLASSIFIER - 1:
            (group, name, spec), classifier = parts, ""
        else:
            msg = f"unexpected Maven library path: {path}"
            raise DriverError(msg)
        return cls(group, name, classifier, spec, default)

    @property
    def library_id(self) -> str:
        """Return the id DBeaver gives the library's own file (e.g. ``g:a:RELEASE``)."""
        return ":".join(filter(None, (self.group, self.name, self.classifier, self.spec)))

    @property
    def path(self) -> str:
        """Return the library path as drivers.xml stores it (without the default version)."""
        return MAVEN_PREFIX + self.library_id


@dataclass(frozen=True)
class DriverFile:
    """A file recorded under a <library> of drivers.xml."""

    file_id: str
    version: str | None
    relative_path: str
    crc: int


@dataclass(frozen=True)
class ResolvedLibrary:
    """A library with the version chosen for it and the files that implement it."""

    kind: str
    path: str
    version: str | None
    forced: bool
    ignore_dependencies: bool
    files: tuple[DriverFile, ...]


def crc32(path: Path) -> int:
    """Return the CRC-32 DBeaver records for a driver file."""
    crc = 0
    with path.open("rb") as file:
        while chunk := file.read(net.CHUNK_SIZE):
            crc = zlib.crc32(chunk, crc)
    return crc


def plugin_documents(plugins_dir: Path) -> list[Element]:
    """Return the parsed plugin.xml of every DBeaver bundle in plugins_dir."""
    documents = []
    for bundle in sorted(plugins_dir.glob("org.jkiss.dbeaver.*")):
        if bundle.is_dir():
            data = (
                (bundle / "plugin.xml").read_bytes() if (bundle / "plugin.xml").is_file() else b""
            )
        else:
            with zipfile.ZipFile(bundle) as jar:
                data = jar.read("plugin.xml") if "plugin.xml" in jar.namelist() else b""
        if data:
            documents.append(ElementTree.fromstring(data))
    return documents


def _matches_platform(file: Element) -> bool:
    family = file.get("os")
    if family is None:
        return True
    matches = family == PLATFORM_OS and file.get("arch") in {None, PLATFORM_ARCH}
    return matches != (file.get("exclude") == "true")


def _libraries(driver: Element) -> Iterator[Library]:
    for file in driver.findall("file"):
        path = file.get("path", "")
        bundle = file.get("bundle") or ""
        # CE ships no product bundles: `bundle="x"` files never apply, `bundle="!x"` always do.
        if bundle and not bundle.startswith("!"):
            continue
        if path.startswith((MAVEN_PREFIX, "https://", "http://")) and _matches_platform(file):
            yield Library(file.get("type", "jar"), path, file.get("ignore-dependencies") == "true")


def find_drivers(
    documents: Iterable[Element], targets: Sequence[tuple[str, str]] = TARGETS
) -> list[Driver]:
    """Return the definitions of the target drivers, in targets order."""
    found: dict[tuple[str, str], Driver] = {}
    for document in documents:
        for extension in document.iter("extension"):
            if extension.get("point") != DATA_SOURCE_POINT:
                continue
            for source in extension.findall("datasource"):
                provider = source.get("id", "")
                for element in source.findall("drivers/driver"):
                    key = (provider, element.get("id", ""))
                    if key in targets:
                        found[key] = Driver(*key, tuple(_libraries(element)))
    missing = [f"{p}/{d}" for p, d in targets if (p, d) not in found]
    if missing:
        msg = f"this DBeaver version does not define the drivers {missing}"
        raise DriverError(msg)
    return [found[key] for key in targets]


def find_client(documents: Iterable[Element], client_id: str = CLIENT_ID) -> ClientDist:
    """Return the newest Windows x86-64 distribution of a native client."""
    dists = [
        ClientDist(
            dist.get("targetPath", ""),
            dist.get("remotePath", ""),
            tuple(f.get("name", "") for f in dist.findall("file")),
        )
        for document in documents
        for client in document.iter("client")
        if client.get("id") == client_id
        for dist in client.findall("dist")
        if dist.get("os") == PLATFORM_OS and dist.get("arch") in {None, PLATFORM_ARCH}
    ]
    if not dists:
        msg = f"no Windows distribution of the {client_id} native client"
        raise DriverError(msg)
    return max(dists, key=lambda d: maven.numeric_key(PurePosixPath(d.target_path).name))


@dataclass(frozen=True)
class _Plan:
    """A library with its artifacts resolved but not yet downloaded."""

    library: Library
    reference: MavenReference | None
    version: str | None
    artifacts: tuple[maven.Artifact, ...]


def _plan(library: Library, repo: maven.Repository) -> _Plan:
    if not library.path.startswith(MAVEN_PREFIX):
        return _Plan(library, None, None, ())
    ref = MavenReference.parse(library.path)
    if ref.spec in {"RELEASE", "LATEST"}:
        version = maven.latest_stable(repo.versions(ref.group, ref.name), ref.default)
    else:
        version = ref.spec
    pom = repo.pom(ref.group, ref.name, version)
    root = maven.Artifact(
        ref.group, ref.name, version, ref.classifier, maven.extension_for(pom.packaging)
    )
    artifacts = maven.resolve(repo, root, transitive=not library.ignore_dependencies)
    return _Plan(library, ref, version, tuple(artifacts))


def _remote_relative(url: str) -> str:
    parts = PurePosixPath(urllib.parse.urlsplit(url).path).parts[1:]
    if not parts or ".." in parts:
        msg = f"unexpected remote driver file URL: {url}"
        raise DriverError(msg)
    return PurePosixPath(REMOTE_DIR, *parts).as_posix()


def _place(plan: _Plan, home: Path) -> ResolvedLibrary:
    """Copy a planned library's files into home (artifacts are already cached) and record them."""
    files: list[DriverFile] = []
    if plan.reference is None:  # a remote file, such as a license
        relative = _remote_relative(plan.library.path)
        net.download(plan.library.path, home / relative)
        files.append(DriverFile(plan.library.path, None, relative, crc32(home / relative)))
        path = plan.library.path
    else:
        for index, artifact in enumerate(plan.artifacts):
            relative = f"{MAVEN_DIR}/{artifact.group}/{artifact.file_name}"
            target = home / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(maven.download(artifact.repository_path), target)
            file_id = (
                plan.reference.library_id
                if index == 0
                else ":".join(
                    filter(
                        None,
                        (artifact.group, artifact.name, artifact.classifier, artifact.version),
                    )
                )
            )
            files.append(DriverFile(file_id, artifact.version, relative, crc32(target)))
        path = plan.reference.path
    default = plan.reference.default if plan.reference else None
    return ResolvedLibrary(
        kind=plan.library.kind,
        path=path,
        version=plan.version if default else None,
        forced=default is not None and plan.version != default,
        ignore_dependencies=plan.library.ignore_dependencies,
        files=tuple(files),
    )


def resolve_drivers(
    drivers: Sequence[Driver], repo: maven.Repository, home: Path
) -> list[tuple[Driver, list[ResolvedLibrary]]]:
    """Choose versions, download every file into home and describe the result per driver."""
    plans = [
        (driver, [_plan(library, repo) for library in driver.libraries]) for driver in drivers
    ]
    artifacts = sorted({a for _, group in plans for plan in group for a in plan.artifacts})
    log.info("Downloading %d driver files", len(artifacts))
    with ThreadPoolExecutor(DOWNLOAD_WORKERS) as pool:
        list(pool.map(maven.download, [a.repository_path for a in artifacts]))
    resolved = []
    for driver, group in plans:
        libraries = [_place(plan, home) for plan in group]
        for library in libraries:
            log.info(
                "  %s/%s: %s %s",
                driver.provider,
                driver.driver_id,
                library.path,
                library.version or "",
            )
        resolved.append((driver, libraries))
    return resolved


def download_client(dist: ClientDist, home: Path) -> list[str]:
    """Download a native client into home and return its files' home-relative paths."""
    base = REPO_URL + dist.remote_path.removeprefix(REPO_PREFIX).strip("/") + "/"
    relatives = [PurePosixPath(dist.target_path, name).as_posix() for name in dist.files]
    log.info("Downloading the native client %s (%d files)", dist.target_path, len(relatives))
    with ThreadPoolExecutor(DOWNLOAD_WORKERS) as pool:
        list(pool.map(lambda r: net.download(base + PurePosixPath(r).name, home / r), relatives))
    return relatives


def render_drivers_xml(resolved: Iterable[tuple[Driver, Sequence[ResolvedLibrary]]]) -> str:
    """
    Return drivers.xml for the resolved drivers.

    Each <driver> carries only its id: any other attribute would override, and later go stale
    against, the definition of the DBeaver version that reads it.
    """
    root = Element("drivers")
    providers: dict[str, Element] = {}
    for driver, libraries in resolved:
        if driver.provider not in providers:
            providers[driver.provider] = ET.SubElement(root, "provider", id=driver.provider)
        driver_element = ET.SubElement(providers[driver.provider], "driver", id=driver.driver_id)
        for library in libraries:
            attributes = {"type": library.kind, "path": library.path, "custom": "false"}
            if library.version:
                attributes["version"] = library.version
            if library.ignore_dependencies:
                attributes["ignore-dependencies"] = "true"
            if library.forced:
                attributes["forced-version"] = "true"
            library_element = ET.SubElement(driver_element, "library", attributes)
            for file in library.files:
                file_attributes = {"id": file.file_id}
                if file.version:
                    file_attributes["version"] = file.version
                file_attributes["path"] = (
                    DRIVERS_HOME + "\\" + file.relative_path.replace("/", "\\")
                )
                file_attributes["crc"] = format(file.crc, "x")
                ET.SubElement(library_element, "file", file_attributes)
    ET.indent(root, space="\t")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
    )


def verify(
    home: Path, libraries: Iterable[ResolvedLibrary], extra: Iterable[str] = ()
) -> list[str]:
    """
    Check that drivers.xml and the drivers folder agree exactly, and return the folder's files.

    Every recorded file must exist with its CRC and every file on disk must be recorded (or be one
    of the extra, e.g. native client, files): drift between the two is what this tool removes.
    """
    recorded = set(extra)
    problems = []
    for library in libraries:
        if not library.files:
            problems.append(f"{library.path} has no files")
        for file in library.files:
            recorded.add(file.relative_path)
            path = home / file.relative_path
            if not path.is_file():
                problems.append(f"missing {file.relative_path}")
            elif crc32(path) != file.crc:
                problems.append(f"CRC mismatch for {file.relative_path}")
    on_disk = {p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file()}
    problems += [f"not recorded in drivers.xml: {p}" for p in sorted(on_disk - recorded)]
    if problems:
        msg = "drivers.xml and the drivers folder disagree:\n  " + "\n  ".join(problems)
        raise DriverError(msg)
    return sorted(on_disk)
