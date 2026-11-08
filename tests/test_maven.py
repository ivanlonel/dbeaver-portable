"""Tests for the Maven version policy, effective POMs and dependency resolution."""

import pytest

from dbeaver_portable import maven

EXCLUDING_Y = (
    "<exclusions><exclusion>"  # keep the pieces: one literal would exceed the line limit
    "<groupId>g</groupId><artifactId>y</artifactId>"
    "</exclusion></exclusions>"
)


def _pom_path(group: str, name: str, version: str) -> str:
    return maven.Artifact(group, name, version, extension="pom").repository_path


def _pom(body: str) -> str:
    return f'<project xmlns="http://maven.apache.org/POM/4.0.0">{body}</project>'


def _dependency(name: str, version: str, extra: str = "") -> str:
    return (
        f"<dependency><groupId>g</groupId><artifactId>{name}</artifactId>"
        f"<version>{version}</version>{extra}</dependency>"
    )


def _dependencies(*dependencies: str) -> str:
    return f"<dependencies>{''.join(dependencies)}</dependencies>"


def _repository(files: dict[str, str]) -> maven.Repository:
    def fetch(path: str) -> bytes:
        try:
            return files[path].encode()
        except KeyError:
            raise FileNotFoundError(path) from None

    return maven.Repository(fetch)


@pytest.mark.parametrize(
    ("versions", "default", "expected"),
    [
        (
            ["12.8.1.jre11", "13.6.2.jre8", "13.6.2.jre11", "13.7.0.jre11-preview"],
            "13.4.0.jre11",
            "13.6.2.jre11",
        ),
        (["13.6.2.x64", "13.6.2.x86", "13.7.0.x64-preview"], "13.4.0.x64", "13.6.2.x64"),
        (["23.2.0.0", "23.9.0.25.07", "23.26.4.0.0", "23.26.5.0.0-M1"], "23.2.0.0", "23.26.4.0.0"),
        (["3.53.4.0", "3.54.0.0-SNAPSHOT", "3.54.0.0.rc1"], None, "3.53.4.0"),
        (["1.0.0-beta"], "0.9.0", "0.9.0"),
    ],
)
def test_latest_stable(versions: list[str], default: str | None, expected: str) -> None:
    """
    Pick the newest release with the default's suffix, by number, never a pre-release.

    The default itself wins when nothing stable is newer.
    """
    assert maven.latest_stable(versions, default) == expected


def test_latest_stable_without_candidates() -> None:
    """Fail clearly when an artifact has no release version at all."""
    with pytest.raises(maven.MavenError, match="no release version"):
        maven.latest_stable(["1.0-SNAPSHOT"])


def test_artifact_paths() -> None:
    """Name files like Maven and DBeaver's driver cache, classifier included."""
    artifact = maven.Artifact("org.duckdb", "duckdb_jdbc", "1.5.6.0", "windows_arm64")
    assert artifact.file_name == "duckdb_jdbc-1.5.6.0-windows_arm64.jar"
    assert artifact.repository_path == (
        "org/duckdb/duckdb_jdbc/1.5.6.0/duckdb_jdbc-1.5.6.0-windows_arm64.jar"
    )
    assert maven.extension_for("bundle") == "jar"
    assert maven.extension_for("dll") == "dll"


def test_effective_pom_applies_parent_properties_and_imported_boms() -> None:
    """Resolve versions through inherited properties, built-ins and imported BOMs."""
    repo = _repository(
        {
            _pom_path("g", "parent", "1"): _pom(
                "<groupId>g</groupId><artifactId>parent</artifactId><version>1</version>"
                "<properties><lib.version>2.0</lib.version></properties>"
                "<dependencyManagement>"
                + _dependencies(_dependency("bom", "3", "<type>pom</type><scope>import</scope>"))
                + "</dependencyManagement>"
            ),
            _pom_path("g", "bom", "3"): _pom(
                "<dependencyManagement>"
                + _dependencies(_dependency("managed", "4.1", "<scope>runtime</scope>"))
                + "</dependencyManagement>"
            ),
            _pom_path("g", "child", "5"): _pom(
                "<parent><groupId>g</groupId><artifactId>parent</artifactId><version>1</version>"
                "</parent><artifactId>child</artifactId><version>5</version>"
                + _dependencies(
                    _dependency("lib", "${lib.version}"),
                    "<dependency><groupId>g</groupId><artifactId>managed</artifactId></dependency>",
                    _dependency("sibling", "${project.version}"),
                )
            ),
        }
    )
    dependencies = repo.pom("g", "child", "5").dependencies
    assert {(d.name, d.version, d.scope) for d in dependencies} == {
        ("lib", "2.0", None),
        ("managed", "4.1", "runtime"),
        ("sibling", "5", None),
    }


def test_resolve_follows_maven_rules() -> None:
    """
    Resolve the runtime closure like Maven does.

    Test, optional and provided dependencies are skipped, b's exclusion of y holds, the
    shallowest-first shared:2 wins over b's shared:1, and old is relocated to new.
    """
    repo = _repository(
        {
            _pom_path("g", "root", "1"): _pom(
                _dependencies(
                    _dependency("a", "1"),
                    _dependency("b", "1", EXCLUDING_Y),
                    _dependency("t", "1", "<scope>test</scope>"),
                    _dependency("o", "1", "<optional>true</optional>"),
                    _dependency("p", "1", "<scope>provided</scope>"),
                )
            ),
            _pom_path("g", "a", "1"): _pom(
                _dependencies(_dependency("shared", "2"), _dependency("old", "1"))
            ),
            _pom_path("g", "b", "1"): _pom(
                _dependencies(_dependency("shared", "1"), _dependency("y", "1"))
            ),
            _pom_path("g", "old", "1"): _pom(
                "<distributionManagement><relocation><artifactId>new</artifactId></relocation>"
                "</distributionManagement>"
            ),
            _pom_path("g", "new", "1"): _pom(""),
            _pom_path("g", "shared", "2"): _pom(""),
        }
    )
    root = maven.Artifact("g", "root", "1")
    assert [a.file_name for a in maven.resolve(repo, root)] == [
        "root-1.jar",
        "a-1.jar",
        "b-1.jar",
        "shared-2.jar",
        "new-1.jar",
    ]
    assert maven.resolve(repo, root, transitive=False) == [root]


def test_missing_pom_is_reported() -> None:
    """Name the missing coordinates instead of failing with a bare 404."""
    with pytest.raises(maven.MavenError, match="no POM for g:absent:1"):
        _repository({}).pom("g", "absent", "1")
