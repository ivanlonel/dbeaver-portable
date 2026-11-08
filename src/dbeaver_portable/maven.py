"""Maven Central for DBeaver drivers: version policy, effective POMs and dependency resolution."""

import re
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
lazy from pathlib import Path
lazy from xml.etree.ElementTree import Element

from defusedxml import ElementTree

from dbeaver_portable import PortableError, net

CENTRAL = "https://repo1.maven.org/maven2/"
RUNTIME_SCOPES = frozenset({"compile", "runtime"})
PROPERTY = re.compile(r"\$\{([^}]+)\}")
NUMERIC_PREFIX = re.compile(r"(\d+(?:\.\d+)*)(.*)", re.DOTALL)
MAX_INTERPOLATION_PASSES = 10
MAX_RELOCATIONS = 5
FILE_EXTENSIONS = {"bundle": "jar", "jar": "jar", "pom": "pom"}  # Maven types/packagings

type Fetcher = Callable[[str], bytes]
"""Return the bytes at a repository-relative path; raise FileNotFoundError when absent."""

type DependencyKey = tuple[str, str, str, str]
"""Maven's dependency identity: group, artifact, type, classifier."""


class MavenError(PortableError):
    """An artifact, version or POM could not be resolved."""


@dataclass(frozen=True, order=True)
class Artifact:
    """A concrete file in a Maven repository."""

    group: str
    name: str
    version: str
    classifier: str = ""
    extension: str = "jar"

    @property
    def file_name(self) -> str:
        """Return the file name Maven (and DBeaver's driver cache) use for this artifact."""
        suffix = f"-{self.classifier}" if self.classifier else ""
        return f"{self.name}-{self.version}{suffix}.{self.extension}"

    @property
    def repository_path(self) -> str:
        """Return the artifact's path relative to the repository root."""
        return f"{self.group.replace('.', '/')}/{self.name}/{self.version}/{self.file_name}"


@dataclass(frozen=True)
class Dependency:
    """A <dependency> element after property interpolation."""

    group: str
    name: str
    version: str | None
    scope: str | None
    classifier: str = ""
    kind: str = "jar"
    optional: bool = False
    exclusions: frozenset[tuple[str, str]] = frozenset()

    @property
    def key(self) -> DependencyKey:
        """Return the identity Maven merges declarations by."""
        return (self.group, self.name, self.kind, self.classifier)


@dataclass(frozen=True)
class Pom:
    """The effective POM of one artifact version: parents, properties and BOM imports applied."""

    group: str
    name: str
    version: str
    packaging: str
    dependencies: tuple[Dependency, ...]
    managed: Mapping[DependencyKey, Dependency]
    properties: Mapping[str, str]
    relocation: tuple[str, str, str] | None = None


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _child(element: Element | None, name: str) -> Element | None:
    if element is None:
        return None
    return next((child for child in element if _local(child.tag) == name), None)


def _children(element: Element | None, name: str) -> list[Element]:
    return [] if element is None else [child for child in element if _local(child.tag) == name]


def _text(element: Element | None, name: str) -> str:
    found = _child(element, name)
    return "" if found is None else (found.text or "").strip()


def _interpolate(value: str, properties: Mapping[str, str]) -> str:
    for _ in range(MAX_INTERPOLATION_PASSES):
        expanded = PROPERTY.sub(lambda m: properties.get(m.group(1), m.group(0)), value)
        if expanded == value:
            break
        value = expanded
    return value


def numeric_key(version: str) -> tuple[int, ...]:
    """Return the leading dotted number of a version as a comparable tuple."""
    match = NUMERIC_PREFIX.match(version)
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def latest_stable(versions: Iterable[str], default: str | None = None) -> str:
    """
    Return the newest release-grade version, keeping the suffix of DBeaver's tested default.

    A candidate is a dotted number followed by exactly the default's suffix (``.jre11``, ``.x64``)
    or by nothing when there is no default, so preview/rc/beta/alpha/SNAPSHOT builds and sibling
    variants (``.jre8``) never qualify. The default wins when nothing newer exists.
    """
    match = NUMERIC_PREFIX.match(default) if default else None
    pattern = re.compile(r"\d+(?:\.\d+)*" + re.escape(match.group(2) if match else ""))
    candidates = [v for v in versions if pattern.fullmatch(v)]
    if default:
        candidates.append(default)
    if not candidates:
        msg = "no release version available"
        raise MavenError(msg)
    return max(candidates, key=numeric_key)


def extension_for(kind: str) -> str:
    """Return the file extension of a Maven type or packaging (``bundle`` ships as a jar)."""
    return FILE_EXTENSIONS.get(kind, kind)


class Repository:
    """Read-only view of a Maven repository, memoizing effective POMs for one build."""

    def __init__(self, fetch: Fetcher) -> None:
        """Wrap fetch, which returns repository-relative paths' bytes."""
        self._fetch = fetch
        self._poms: dict[tuple[str, str, str], Pom] = {}

    def versions(self, group: str, name: str) -> list[str]:
        """Return the versions maven-metadata.xml lists for an artifact."""
        path = f"{group.replace('.', '/')}/{name}/maven-metadata.xml"
        try:
            metadata = ElementTree.fromstring(self._fetch(path))
        except FileNotFoundError as error:
            msg = f"{group}:{name} is not in the repository"
            raise MavenError(msg) from error
        versions = _child(_child(metadata, "versioning"), "versions")
        return [(v.text or "").strip() for v in _children(versions, "version")]

    def pom(self, group: str, name: str, version: str) -> Pom:
        """Return the effective POM of group:name:version."""
        key = (group, name, version)
        if key not in self._poms:
            self._poms[key] = self._effective_pom(group, name, version)
        return self._poms[key]

    def _effective_pom(self, group: str, name: str, version: str) -> Pom:
        path = Artifact(group, name, version, extension="pom").repository_path
        try:
            root = ElementTree.fromstring(self._fetch(path))
        except FileNotFoundError as error:
            msg = f"no POM for {group}:{name}:{version}"
            raise MavenError(msg) from error
        parent_element = _child(root, "parent")
        parent = (
            None
            if parent_element is None
            else self.pom(
                _text(parent_element, "groupId"),
                _text(parent_element, "artifactId"),
                _text(parent_element, "version"),
            )
        )
        sections = _active_sections(root)
        properties = _properties(sections, parent, (group, name, version))
        managed = dict(parent.managed) if parent else {}
        for dependency in _declarations(sections, "dependencyManagement", properties):
            self._manage(dependency, managed)
        declared = {d.key: d for d in parent.dependencies} if parent else {}
        declared |= {d.key: d for d in _declarations(sections, None, properties)}
        relocation = _child(_child(root, "distributionManagement"), "relocation")
        return Pom(
            group=group,
            name=name,
            version=version,
            packaging=_text(root, "packaging") or "jar",
            dependencies=tuple(_apply_management(d, managed) for d in declared.values()),
            managed=managed,
            properties=properties,
            relocation=None
            if relocation is None
            else (
                _text(relocation, "groupId") or group,
                _text(relocation, "artifactId") or name,
                _text(relocation, "version") or version,
            ),
        )

    def _manage(self, dependency: Dependency, managed: dict[DependencyKey, Dependency]) -> None:
        if dependency.scope == "import" and dependency.kind == "pom" and dependency.version:
            bom = self.pom(dependency.group, dependency.name, dependency.version)
            for key, entry in bom.managed.items():
                managed.setdefault(key, entry)  # explicit declarations beat imported ones
        else:
            managed[dependency.key] = dependency

    def locate(self, group: str, name: str, version: str) -> Pom:
        """Return the effective POM of an artifact after following relocations."""
        pom = self.pom(group, name, version)
        for _ in range(MAX_RELOCATIONS):
            if pom.relocation is None or pom.relocation == (pom.group, pom.name, pom.version):
                return pom
            pom = self.pom(*pom.relocation)
        msg = f"relocation loop starting at {group}:{name}:{version}"
        raise MavenError(msg)


def _active_sections(root: Element) -> list[Element]:
    """Return the POM itself plus its profiles that are active without any condition."""
    # ponytail: only activeByDefault profiles count; jdk/os/property activation is ignored.
    return [root] + [
        profile
        for profile in _children(_child(root, "profiles"), "profile")
        if _text(_child(profile, "activation"), "activeByDefault") == "true"
    ]


def _properties(
    sections: Iterable[Element], parent: Pom | None, coordinates: tuple[str, str, str]
) -> dict[str, str]:
    """Return the inherited, declared and built-in properties a POM interpolates with."""
    properties = dict(parent.properties) if parent else {}
    for section in sections:
        declared = _child(section, "properties")
        for prop in [] if declared is None else list(declared):
            properties[_local(prop.tag)] = (prop.text or "").strip()
    group, name, version = coordinates
    properties |= {
        "project.groupId": group,
        "project.artifactId": name,
        "project.version": version,
        "pom.version": version,
    }
    if parent:
        properties |= {
            "project.parent.groupId": parent.group,
            "project.parent.version": parent.version,
        }
    return properties


def _declarations(
    sections: Iterable[Element], container: str | None, properties: Mapping[str, str]
) -> list[Dependency]:
    """Return the <dependencies> of every section, or of their container element if given."""
    return [
        _dependency(element, properties)
        for section in sections
        for element in _children(
            _child(_child(section, container) if container else section, "dependencies"),
            "dependency",
        )
    ]


def _dependency(element: Element, properties: Mapping[str, str]) -> Dependency:
    def field(name: str) -> str:
        return _interpolate(_text(element, name), properties)

    return Dependency(
        group=field("groupId"),
        name=field("artifactId"),
        version=field("version") or None,
        scope=field("scope") or None,
        classifier=field("classifier"),
        kind=field("type") or "jar",
        optional=field("optional") == "true",
        exclusions=frozenset(
            (_text(exclusion, "groupId"), _text(exclusion, "artifactId"))
            for exclusion in _children(_child(element, "exclusions"), "exclusion")
        ),
    )


def _apply_management(
    dependency: Dependency, managed: Mapping[DependencyKey, Dependency]
) -> Dependency:
    entry = managed.get(dependency.key)
    if entry is None:
        return dependency
    return Dependency(
        group=dependency.group,
        name=dependency.name,
        version=dependency.version or entry.version,
        scope=dependency.scope or entry.scope,
        classifier=dependency.classifier,
        kind=dependency.kind,
        optional=dependency.optional,
        exclusions=dependency.exclusions | entry.exclusions,
    )


def _is_excluded(dependency: Dependency, exclusions: frozenset[tuple[str, str]]) -> bool:
    return bool(
        {(dependency.group, dependency.name), (dependency.group, "*"), ("*", "*")} & exclusions
    )


def resolve(repo: Repository, root: Artifact, *, transitive: bool = True) -> list[Artifact]:
    """
    Return root followed by the files of its runtime dependency closure.

    Follows Maven: compile/runtime scopes only, optional dependencies skipped, exclusions
    inherited down the tree, relocations followed, and the shallowest declaration of each
    artifact wins.
    """
    artifacts = [root]
    if not transitive:
        return artifacts
    seen = {(root.group, root.name, root.classifier)}
    queue: deque[tuple[Artifact, frozenset[tuple[str, str]]]] = deque([(root, frozenset())])
    while queue:
        parent, excluded = queue.popleft()
        for dependency in repo.pom(parent.group, parent.name, parent.version).dependencies:
            identity = (dependency.group, dependency.name, dependency.classifier)
            if (
                dependency.optional
                or (dependency.scope or "compile") not in RUNTIME_SCOPES
                or _is_excluded(dependency, excluded)
                or identity in seen
                or not dependency.version
            ):
                continue
            if dependency.version[0] in "[(":
                msg = f"version ranges are not supported: {dependency} (needed by {parent})"
                raise MavenError(msg)
            seen.add(identity)
            pom = repo.locate(dependency.group, dependency.name, dependency.version)
            child = Artifact(
                pom.group,
                pom.name,
                pom.version,
                dependency.classifier,
                extension_for(dependency.kind),
            )
            if child.extension != "pom":
                artifacts.append(child)
            queue.append((child, excluded | dependency.exclusions))
    return artifacts


def _published_digest(url: str) -> tuple[str, str]:
    for algorithm in ("sha256", "sha1"):
        try:
            return algorithm, net.fetch(f"{url}.{algorithm}").decode().split()[0]
        except FileNotFoundError:
            continue
    msg = f"Maven Central publishes no checksum for {url}"
    raise MavenError(msg)


def download(path: str) -> Path:
    """Return the cached copy of a Maven Central file, fetching and verifying it when missing."""
    target = net.cache_dir() / "maven" / path
    if not target.is_file():
        url = CENTRAL + path
        net.download(url, target, digest=_published_digest(url))
    return target


def central_fetch(path: str) -> bytes:
    """Fetch a Maven Central path; everything but maven-metadata.xml is immutable and cached."""
    if path.endswith("maven-metadata.xml"):
        return net.fetch(CENTRAL + path)
    return download(path).read_bytes()
