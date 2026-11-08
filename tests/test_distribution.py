"""Tests for reading the DBeaver distribution and its update site."""

import gzip
import zipfile
lazy from pathlib import Path

import pytest
from defusedxml import ElementTree

from dbeaver_portable import distribution

QUALIFIER = "202611012052"
CONTENT_XML = f"""<repository>
  <units>
    <unit id="{QUALIFIER}.org.jkiss.dbeaver.additions" version="1.0.0">
      <properties>
        <property name="org.eclipse.equinox.p2.type.category" value="true"/>
      </properties>
      <requires>
        <required name="org.jkiss.dbeaver.git.feature.feature.group"
                  range="[1.0.178.{QUALIFIER},1.0.178.{QUALIFIER}]"/>
        <required name="org.jkiss.dbeaver.ide.feature.feature.group"
                  range="[26.2.4.{QUALIFIER},26.2.4.{QUALIFIER}]"/>
        <required name="some.bundle" range="[1.0,1.0]"/>
        <required name="loose.feature.group" range="[1.0,2.0)"/>
      </requires>
    </unit>
    <unit id="org.jkiss.dbeaver.office.feature.feature.group" version="1.1.227.{QUALIFIER}">
      <requires>
        <required name="x.feature.group" range="[1,1]"/>
      </requires>
    </unit>
  </units>
</repository>"""


def test_category_features_skip_the_ide_integration_and_loose_ranges() -> None:
    """Install categorized features pinned exactly, minus the Eclipse-IDE integration."""
    features = distribution.category_features(ElementTree.fromstring(CONTENT_XML))
    assert [str(f) for f in features] == [
        f"org.jkiss.dbeaver.git.feature.feature.group/1.0.178.{QUALIFIER}"
    ]


def test_check_matches_rejects_features_of_another_build() -> None:
    """Refuse an update site that belongs to a different build than the zip."""
    product = distribution.Product("26.2.4", QUALIFIER)
    distribution.check_matches(
        [distribution.Feature("f.feature.group", f"1.0.{QUALIFIER}")], product
    )
    with pytest.raises(distribution.DistributionError, match="run the build again"):
        distribution.check_matches(
            [distribution.Feature("f.feature.group", "1.0.209912312359")], product
        )


def _zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def test_extract_writes_the_app_folder_and_reads_the_product(tmp_path: Path) -> None:
    """Extract the zip's dbeaver folder and read its version and build qualifier."""
    eclipseproduct = f"name=DBeaver\nversion=26.2.4\nsnapshot=26.2.4-{QUALIFIER}\n".encode()
    zip_path = _zip(
        tmp_path / "dbeaver.zip",
        {"dbeaver/.eclipseproduct": eclipseproduct, "dbeaver/plugins/a.jar": b"jar"},
    )
    app = distribution.extract(zip_path, tmp_path / "out")
    assert app == (tmp_path / "out" / "dbeaver").resolve()
    assert (app / "plugins" / "a.jar").read_bytes() == b"jar"
    assert distribution.read_product(app) == distribution.Product("26.2.4", QUALIFIER)
    assert distribution.pristine_files(zip_path) == {".eclipseproduct", "plugins/a.jar"}


@pytest.mark.parametrize(
    "name", ["dbeaver/../evil.txt", "other/file.txt", "dbeaver\\..\\evil.txt"]
)
def test_extract_refuses_entries_outside_the_app_folder(tmp_path: Path, name: str) -> None:
    """Never write a zip entry outside the dbeaver folder (zip slip)."""
    zip_path = _zip(tmp_path / "bad.zip", {name: b"x"})
    with pytest.raises(distribution.DistributionError, match="unexpected entry"):
        distribution.extract(zip_path, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


def test_known_repositories_lists_enabled_remote_sites(tmp_path: Path) -> None:
    """Pass the director the same enabled update sites the DBeaver GUI would consult."""
    prefs = tmp_path / distribution.PROFILE_REPOSITORIES
    prefs.parent.mkdir(parents=True)
    prefs.write_text(
        "eclipse.preferences.version=1\n"
        "repositories/https\\:__a_x/enabled=true\n"
        "repositories/https\\:__a_x/uri=https\\://a/x/\n"
        "repositories/https\\:__b_y/enabled=false\n"
        "repositories/https\\:__b_y/uri=https\\://b/y/\n"
        "repositories/file\\:_c/enabled=true\n"
        "repositories/file\\:_c/uri=file\\:/c/\n",
        "utf-8",
    )
    assert distribution.known_repositories(tmp_path) == ["https://a/x/"]
    assert distribution.known_repositories(tmp_path / "elsewhere") == []


def test_original_location_reads_the_newest_shipped_profile(tmp_path: Path) -> None:
    """Read DBeaver's own install folder from the newest p2 profile in the zip."""

    def profile(location: str) -> bytes:
        return gzip.compress(
            f"<profile><properties><property name='org.eclipse.equinox.p2.installFolder'"
            f" value='{location}'/></properties></profile>".encode()
        )

    prefix = f"dbeaver/{distribution.PROFILE_DIR}"
    zip_path = _zip(
        tmp_path / "dbeaver.zip",
        {
            f"{prefix}/9.profile.gz": profile("/old"),
            f"{prefix}/10.profile.gz": profile("/build/dbeaver"),
        },
    )
    assert distribution.original_location(zip_path) == "/build/dbeaver"
