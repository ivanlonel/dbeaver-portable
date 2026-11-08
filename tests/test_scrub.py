"""Tests for removing runtime state, rewriting build paths and finding leaks."""

import gzip
from pathlib import Path

from dbeaver_portable import scrub

BUILD = Path(r"C:\Users\Someone\Temp\work dir\dbeaver")
ORIGINAL = "/var/build/dbeaver"
PROFILE_DIR = "p2/org.eclipse.equinox.p2.engine/profileRegistry/DefaultProfile.profile"


def test_path_variants_cover_windows_and_java_spellings() -> None:
    """Spell a path natively, with slashes, JSON and .properties escapes, and as a URL."""
    assert set(scrub.path_variants(BUILD)) == {
        r"C:\Users\Someone\Temp\work dir\dbeaver",
        "C:/Users/Someone/Temp/work dir/dbeaver",
        r"C:\\Users\\Someone\\Temp\\work dir\\dbeaver",
        r"C\:\\Users\\Someone\\Temp\\work dir\\dbeaver",
        "C:/Users/Someone/Temp/work%20dir/dbeaver",
    }


def test_rewrite_paths_ignores_case() -> None:
    """Rewrite every spelling of a path, whatever its case, as Windows paths are."""
    text = (
        "a=file:/c:/users/someone/temp/work%20dir/dbeaver/x\n"
        r"b=C\:\\Users\\Someone\\Temp\\work dir\\dbeaver"
    )
    assert scrub.rewrite_paths(text, BUILD, ORIGINAL) == f"a=file:/{ORIGINAL}/x\nb={ORIGINAL}"


def _profile(path: Path, location: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        gzip.compress(
            "<profile><property name='org.eclipse.equinox.p2.installFolder'"
            f" value='{location}'/></profile>".encode()
        )
    )


def test_scrub_app_keeps_shipped_files_and_the_latest_profile(tmp_path: Path) -> None:
    """Delete runtime state and intermediate snapshots; rewrite the build path in the rest."""
    app = tmp_path / "dbeaver"
    pool = "configuration/org.eclipse.osgi/110/data/42"
    files = {
        "configuration/config.ini": f"osgi.install.area=file:/{app.as_posix()}/\n",
        scrub.BUNDLES_INFO: f"#version=1\nx,1.0,{pool}/plugins/x_1.0.jar,4,false\n",
        f"{pool}/plugins/x_1.0.jar": "jar",
        f"{pool}/features/x_1.0/feature.xml": "<feature/>",
        f"{pool}/artifacts.xml": f"<repository name='{app}'/>",
        "configuration/org.eclipse.osgi/cache.bin": "x",
        "configuration/.settings/org.eclipse.ui.ide.prefs": "RECENT_WORKSPACES=x\n",
        "configuration/1234.log": "",
        "p2/org.eclipse.equinox.p2.core/cache/artifacts.xml": "x",
    }
    for relative, text in files.items():
        (app / relative).parent.mkdir(parents=True, exist_ok=True)
        (app / relative).write_text(text, "utf-8")
    _profile(app / PROFILE_DIR / "100.profile.gz", ORIGINAL)  # shipped by DBeaver
    _profile(app / PROFILE_DIR / "150.profile.gz", str(app))  # intermediate p2 snapshot
    _profile(app / PROFILE_DIR / "200.profile.gz", str(app))  # the install's result
    pristine = frozenset(
        {
            "configuration/config.ini",
            scrub.BUNDLES_INFO,
            f"{PROFILE_DIR}/100.profile.gz",
        }
    )

    scrub.scrub_app(app, pristine, ORIGINAL)

    remaining = sorted(p.relative_to(app).as_posix() for p in app.rglob("*") if p.is_file())
    assert remaining == sorted(
        [
            "configuration/config.ini",
            scrub.BUNDLES_INFO,
            f"{pool}/artifacts.xml",
            f"{pool}/features/x_1.0/feature.xml",
            f"{pool}/plugins/x_1.0.jar",
            f"{PROFILE_DIR}/100.profile.gz",
            f"{PROFILE_DIR}/200.profile.gz",
        ]
    )
    assert not (app / "configuration" / "org.eclipse.osgi" / "cache.bin").exists()
    assert scrub.missing_bundles(app) == []
    (app / pool / "plugins" / "x_1.0.jar").unlink()
    assert scrub.missing_bundles(app) == [f"{pool}/plugins/x_1.0.jar"]
    assert (app / pool / "artifacts.xml").read_text("utf-8") == f"<repository name='{ORIGINAL}'/>"
    latest = gzip.decompress((app / PROFILE_DIR / "200.profile.gz").read_bytes()).decode()
    assert f"value='{ORIGINAL}'" in latest
    config = (app / "configuration" / "config.ini").read_text("utf-8")
    assert config == f"osgi.install.area=file:/{ORIGINAL}/\n"
    assert scrub.find_leaks(app, scrub.path_variants(app)) == []


def test_find_leaks_reads_utf16_and_gzip_but_skips_binaries(tmp_path: Path) -> None:
    """Find a path in UTF-8, UTF-16 and gzipped files, ignoring jars and other binaries."""
    home = r"C:\Users\Someone"
    (tmp_path / "plain.txt").write_text(f"path={home}\\x", "utf-8")
    (tmp_path / "wide.xml").write_bytes(f"<p>{home.lower()}</p>".encode("utf-16-le"))
    (tmp_path / "profile.gz").write_bytes(gzip.compress(home.encode()))
    (tmp_path / "driver.jar").write_bytes(home.encode())
    (tmp_path / "clean.txt").write_text("nothing here", "utf-8")
    leaks = scrub.find_leaks(tmp_path, [home])
    assert sorted(leak.path.name for leak in leaks) == ["plain.txt", "profile.gz", "wide.xml"]
