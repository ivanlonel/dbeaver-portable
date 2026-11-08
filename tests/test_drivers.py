"""Tests for driver definitions, drivers.xml rendering and the drivers-folder self-check."""

lazy from pathlib import Path
lazy from xml.etree.ElementTree import Element

import pytest
from defusedxml import ElementTree

from dbeaver_portable import drivers

LICENSE_URL = "https://raw.githubusercontent.com/duckdb/duckdb/master/LICENSE"
PLUGIN_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<plugin>
  <extension point="org.jkiss.dbeaver.dataSourceProvider">
    <datasource id="duckdb">
      <drivers>
        <driver id="duckdb_jdbc">
          <file type="jar" path="maven:/org.duckdb:duckdb_jdbc:windows_arm64:RELEASE"
                os="win32" arch="aarch64" bundle="!drivers.duckdb"/>
          <file type="jar" path="maven:/org.duckdb:duckdb_jdbc:RELEASE"
                os="win32" arch="aarch64" exclude="true" bundle="!drivers.duckdb"/>
          <file type="license" path="{LICENSE_URL}"/>
          <file type="jar" path="drivers/duckdb" bundle="drivers.duckdb"/>
        </driver>
      </drivers>
    </datasource>
  </extension>
  <extension point="org.jkiss.dbeaver.dataSourceProvider">
    <datasource id="postgresql">
      <drivers>
        <driver id="postgres-jdbc">
          <file type="jar" path="maven:/org.postgresql:postgresql:RELEASE[42.7.13]"
                bundle="!drivers.postgresql"/>
          <file type="jar" path="maven:/net.postgis:postgis-jdbc:RELEASE[2.5.0]"
                ignore-dependencies="true" optional="true" bundle="!drivers.postgresql"/>
          <file type="license" path="licenses/external/pg.txt"/>
        </driver>
      </drivers>
      <nativeClients>
        <client id="postgresql_client">
          <dist os="win32" targetPath="clients/postgresql/win/15"
                remotePath="repo:/drivers/postgresql/client/win/15">
            <file type="exec" name="psql.exe"/>
          </dist>
          <dist os="win32" targetPath="clients/postgresql/win/17"
                remotePath="repo:/drivers/postgresql/client/win/17">
            <file type="exec" name="psql.exe"/>
            <file type="lib" name="libpq.dll"/>
          </dist>
          <dist os="macosx" targetPath="clients/postgresql/mac/18" remotePath="repo:/mac"/>
        </client>
      </nativeClients>
    </datasource>
  </extension>
</plugin>
"""
JAR = "maven/maven-central/org.postgresql/postgresql-42.7.14.jar"
EXPECTED_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<drivers>\n"
    '\t<provider id="postgresql">\n'
    '\t\t<driver id="postgres-jdbc">\n'
    '\t\t\t<library type="jar" path="maven:/org.postgresql:postgresql:RELEASE" custom="false"'
    ' version="42.7.14" forced-version="true">\n'
    '\t\t\t\t<file id="org.postgresql:postgresql:RELEASE" version="42.7.14"'
    ' path="${drivers_home}\\maven\\maven-central\\org.postgresql\\postgresql-42.7.14.jar"'
    ' crc="5099f19d" />\n'
    "\t\t\t</library>\n"
    '\t\t\t<library type="license" path="https://example.org/LICENSE" custom="false">\n'
    # DBeaver writes CRCs with Long.toHexString: lowercase, no zero padding.
    '\t\t\t\t<file id="https://example.org/LICENSE"'
    ' path="${drivers_home}\\remote\\LICENSE" crc="f7b54a9" />\n'
    "\t\t\t</library>\n"
    "\t\t</driver>\n"
    "\t</provider>\n"
    "</drivers>\n"
)


def _documents() -> list[Element]:
    return [ElementTree.fromstring(PLUGIN_XML)]


def _library(*files: drivers.DriverFile, version: str | None = None) -> drivers.ResolvedLibrary:
    return drivers.ResolvedLibrary(
        kind="jar",
        path="maven:/g:a:RELEASE",
        version=version,
        forced=False,
        ignore_dependencies=False,
        files=files,
    )


def test_find_drivers_applies_bundle_and_platform_filters() -> None:
    """Keep only files for CE on win32/x86_64; the arm64 jar and bundled folders drop out."""
    duckdb, postgres = drivers.find_drivers(
        _documents(), [("duckdb", "duckdb_jdbc"), ("postgresql", "postgres-jdbc")]
    )
    assert [library.path for library in duckdb.libraries] == [
        "maven:/org.duckdb:duckdb_jdbc:RELEASE",
        LICENSE_URL,
    ]
    assert [(lib.path, lib.ignore_dependencies) for lib in postgres.libraries] == [
        ("maven:/org.postgresql:postgresql:RELEASE[42.7.13]", False),
        ("maven:/net.postgis:postgis-jdbc:RELEASE[2.5.0]", True),
    ]


def test_find_drivers_reports_missing_definitions() -> None:
    """Name the drivers a DBeaver version no longer defines."""
    with pytest.raises(drivers.DriverError, match="mysql/mysql8"):
        drivers.find_drivers(_documents(), [("mysql", "mysql8")])


def test_find_client_picks_the_newest_windows_dist() -> None:
    """Choose the highest client version DBeaver offers for Windows."""
    assert drivers.find_client(_documents()) == drivers.ClientDist(
        "clients/postgresql/win/17",
        "repo:/drivers/postgresql/client/win/17",
        ("psql.exe", "libpq.dll"),
    )


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "maven:/org.postgresql:postgresql:RELEASE[42.7.13]",
            ("org.postgresql", "postgresql", "", "RELEASE", "42.7.13"),
        ),
        (
            "maven:/org.duckdb:duckdb_jdbc:windows_arm64:RELEASE",
            ("org.duckdb", "duckdb_jdbc", "windows_arm64", "RELEASE", None),
        ),
    ],
)
def test_maven_reference(path: str, expected: tuple[str | None, ...]) -> None:
    """Split library paths like DBeaver, which drops the default version in drivers.xml."""
    reference = drivers.MavenReference.parse(path)
    parsed = (reference.group, reference.name, reference.classifier, reference.spec)
    assert (*parsed, reference.default) == expected
    assert reference.path == path.partition("[")[0]


def test_remote_files_land_where_dbeaver_looks_for_them() -> None:
    """Store remote files under remote/ with the URL path, as DBeaver does."""
    assert drivers._remote_relative(LICENSE_URL) == "remote/duckdb/duckdb/master/LICENSE"


def test_render_drivers_xml_uses_dbeaver_format() -> None:
    """Write bare <driver> elements with DBeaver's library and file attributes."""
    driver = drivers.Driver("postgresql", "postgres-jdbc", ())
    libraries = [
        drivers.ResolvedLibrary(
            kind="jar",
            path="maven:/org.postgresql:postgresql:RELEASE",
            version="42.7.14",
            forced=True,
            ignore_dependencies=False,
            files=(
                drivers.DriverFile(
                    "org.postgresql:postgresql:RELEASE", "42.7.14", JAR, 0x5099F19D
                ),
            ),
        ),
        drivers.ResolvedLibrary(
            kind="license",
            path="https://example.org/LICENSE",
            version=None,
            forced=False,
            ignore_dependencies=False,
            files=(
                drivers.DriverFile(
                    "https://example.org/LICENSE", None, "remote/LICENSE", 0xF7B54A9
                ),
            ),
        ),
    ]
    assert drivers.render_drivers_xml([(driver, libraries)]) == EXPECTED_XML


def test_verify_accepts_an_exact_match(tmp_path: Path) -> None:
    """Return the folder's files when drivers.xml records each one with its CRC."""
    (tmp_path / "maven").mkdir()
    (tmp_path / "clients").mkdir()
    jar = tmp_path / "maven" / "a-1.jar"
    jar.write_bytes(b"jar")
    (tmp_path / "clients" / "psql.exe").write_bytes(b"exe")
    library = _library(drivers.DriverFile("g:a:RELEASE", "1", "maven/a-1.jar", drivers.crc32(jar)))
    assert drivers.verify(tmp_path, [library], ["clients/psql.exe"]) == [
        "clients/psql.exe",
        "maven/a-1.jar",
    ]


def test_verify_reports_drift(tmp_path: Path) -> None:
    """Report wrong CRCs, missing files and files drivers.xml does not record."""
    (tmp_path / "maven").mkdir()
    jar = tmp_path / "maven" / "a-1.jar"
    jar.write_bytes(b"jar")
    (tmp_path / "maven" / "stale-0.jar").write_bytes(b"old")
    library = _library(
        drivers.DriverFile("g:a:RELEASE", "1", "maven/a-1.jar", drivers.crc32(jar) + 1),
        drivers.DriverFile("g:b:1", "1", "maven/b-1.jar", 0),
    )
    with pytest.raises(drivers.DriverError, match="disagree") as caught:
        drivers.verify(tmp_path, [library])
    message = str(caught.value)
    assert "CRC mismatch for maven/a-1.jar" in message
    assert "missing maven/b-1.jar" in message
    assert "not recorded in drivers.xml: maven/stale-0.jar" in message
