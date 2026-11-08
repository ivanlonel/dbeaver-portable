"""Tests for the end-user installer, run by Windows PowerShell 5.1 against a sandbox."""

import json
import shutil
import subprocess
import uuid
import winreg
from dataclasses import dataclass
from pathlib import Path
from typing import Any
lazy from collections.abc import Iterator, Mapping

import pytest
from defusedxml import ElementTree

from dbeaver_portable import installer, profile

POWERSHELL = shutil.which("powershell.exe")
pytestmark = [
    pytest.mark.windows,
    pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell 5.1"),
]

REGISTRY_PARENT = r"Software\dbeaver-portable-tests"
PREFS = profile.PREFS.replace("/", "\\")
NEW_JAR = r"maven\maven-central\g\a-2.jar"
OLD_JAR = r"maven\maven-central\g\a-1.jar"
PACKAGED_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<drivers>\n"
    '\t<provider id="postgresql">\n'
    '\t\t<driver id="postgres-jdbc">\n'
    '\t\t\t<library type="jar" path="maven:/g:a:RELEASE" custom="false" version="2">\n'
    '\t\t\t\t<file id="g:a:RELEASE" version="2"'
    ' path="${drivers_home}\\maven\\maven-central\\g\\a-2.jar" crc="1"/>\n'
    "\t\t\t</library>\n"
    "\t\t</driver>\n"
    "\t</provider>\n"
    "</drivers>\n"
)


def _connection(name: str, folder: str | None = None) -> dict[str, Any]:
    connection: dict[str, Any] = {"provider": "sqlite", "driver": "sqlite_jdbc", "name": name}
    connection["configuration"] = {"database": f"D:/{name}.db", "type": "dev"}
    if folder:
        connection["folder"] = folder
    return connection


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")


@dataclass
class Sandbox:
    """A package plus redirected install, data, shortcut and registry locations."""

    root: Path
    registry: str

    @property
    def package(self) -> Path:
        """Return the package's top folder (the archive's ``dbeaver``)."""
        return self.root / "package" / "dbeaver"

    @property
    def install(self) -> Path:
        """Return the redirected install folder."""
        return self.root / "Programs" / "DBeaver"

    @property
    def data(self) -> Path:
        """Return the redirected DBeaverData folder."""
        return self.root / "AppData" / "DBeaverData"

    @property
    def workspace(self) -> Path:
        """Return the redirected DBeaver workspace."""
        return self.data / "workspace6"

    def build_package(self, data_sources: Mapping[str, Any] | None) -> None:
        """Stage a minimal package the way the build does, with one driver file."""
        app = self.package / "dbeaver"
        _write(app / ".eclipseproduct", "name=DBeaver\nversion=26.2.4\nsnapshot=26.2.4-1\n")
        _write(app / "dbeaver.exe", "not really an exe")
        _write(self.package / "DBeaverData" / "drivers" / NEW_JAR, "jar")
        defaults = self.root / "defaults"
        _write(defaults / "settings" / "global-settings.ini", "nl=pt_BR\n")
        _write(defaults / PREFS / "org.jkiss.dbeaver.core.prefs", "resultset.maxrows=750\n")
        installer.stage(
            self.package,
            defaults=defaults,
            drivers_xml=PACKAGED_XML,
            driver_files=[NEW_JAR.replace("\\", "/")],
            data_sources=data_sources,
        )

    def run(self, action: str, *, installed: bool = False) -> subprocess.CompletedProcess[str]:
        """Run setup.ps1 from the package, or from the installed copy, non-interactively."""
        base = self.install if installed else self.package / "dbeaver"
        assert POWERSHELL
        return subprocess.run(  # noqa: S603 -- fixed argv
            [
                POWERSHELL,
                "-NoLogo",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(base / installer.PORTABLE_DIR / "setup.ps1"),
                action,
                "-InstallDir",
                str(self.install),
                "-DataDir",
                str(self.data),
                "-DesktopDir",
                str(self.root / "Desktop"),
                "-StartMenuDir",
                str(self.root / "StartMenu"),
                "-RegistryKey",
                f"HKCU:\\{self.registry}",
                "-NonInteractive",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )

    def state(self) -> dict[str, Any]:
        """Return the installer's state file."""
        state: dict[str, Any] = json.loads(
            (self.data / "portable-install.json").read_text("utf-8-sig")
        )
        return state

    def data_sources(self) -> dict[str, Any]:
        """Return the workspace's data-sources.json."""
        path = self.workspace / "General" / ".dbeaver" / "data-sources.json"
        document: dict[str, Any] = json.loads(path.read_text("utf-8-sig"))
        return document


def _delete_key(root: int, path: str) -> None:
    try:
        with winreg.OpenKey(root, path) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_key(root, f"{path}\\{child}")
    except FileNotFoundError:
        return
    winreg.DeleteKey(root, path)


@pytest.fixture(name="sandbox")
def fixture_sandbox(tmp_path: Path) -> Iterator[Sandbox]:
    """Yield a sandbox and delete its registry key afterwards."""
    box = Sandbox(tmp_path, f"{REGISTRY_PARENT}\\{uuid.uuid4().hex}")
    yield box
    _delete_key(winreg.HKEY_CURRENT_USER, box.registry)


def _succeeded(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def test_fresh_install(sandbox: Sandbox) -> None:
    """Install app, drivers, defaults, connections, shortcuts and the uninstall entry."""
    sandbox.build_package({"connections": {"pkg-1": _connection("Shared")}})

    _succeeded(sandbox.run("install"))

    assert (sandbox.install / "dbeaver.exe").is_file()
    assert (sandbox.install / installer.PORTABLE_DIR / "setup.cmd").is_file()
    assert (sandbox.data / "drivers" / NEW_JAR).read_text("utf-8") == "jar"
    assert (sandbox.workspace / ".metadata" / ".config" / "drivers.xml").read_text(
        "utf-8"
    ) == PACKAGED_XML
    prefs = sandbox.workspace.parent / PREFS / "org.jkiss.dbeaver.core.prefs"
    assert prefs.read_text("utf-8") == "resultset.maxrows=750\n"
    assert list(sandbox.data_sources()["connections"]) == ["pkg-1"]
    assert sandbox.state()["offeredDataSources"] == ["pkg-1"]
    assert sandbox.state()["driverFiles"] == [NEW_JAR]
    assert (sandbox.root / "Desktop" / "DBeaver.lnk").is_file()
    assert (sandbox.root / "StartMenu" / "DBeaver.lnk").is_file()
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sandbox.registry) as key:
        assert winreg.QueryValueEx(key, "DisplayVersion")[0] == "26.2.4"
        assert winreg.QueryValueEx(key, "UninstallString")[0].endswith('setup.cmd" uninstall')


def test_upgrade_merges_and_never_offers_a_connection_twice(sandbox: Sandbox) -> None:
    """
    Merge an upgrade into the user's state without clobbering it.

    Packaged driver libraries replace stale ones while user libraries, drivers, connections,
    settings and downloads stay; missing default files are added, superseded packaged files go
    and deleted connections are not offered again.
    """
    sandbox.build_package(
        {
            "folders": {"Team": {}},
            "connections": {
                "pkg-new": _connection("New", "Team"),
                "pkg-deleted": _connection("Gone"),
            },
            "connection-types": {"dev": {"name": "Development"}},
        }
    )
    _write(sandbox.install / "dbeaver.exe", "previous version")
    _write(sandbox.data / "drivers" / OLD_JAR, "old")
    _write(sandbox.data / "drivers" / r"maven\maven-central\other\z-9.jar", "user downloaded")
    _write(
        sandbox.data / "portable-install.json",
        json.dumps({"offeredDataSources": ["pkg-deleted"], "driverFiles": [OLD_JAR]}),
    )
    _write(
        sandbox.workspace / ".metadata" / ".config" / "drivers.xml",
        '<?xml version="1.0" encoding="UTF-8"?><drivers>'
        '<provider id="postgresql"><driver id="postgres-jdbc">'
        '<library type="jar" path="maven:/g:a:RELEASE" custom="false" version="1"/>'
        '<library type="jar" path="D:\\extra.jar" custom="true"/></driver></provider>'
        '<provider id="generic"><driver id="mine" custom="true" name="Mine"/></provider>'
        "</drivers>",
    )
    _write(
        sandbox.workspace / "General" / ".dbeaver" / "data-sources.json",
        json.dumps({"connections": {"user-1": _connection("Mine")}}),
    )
    user_prefs = sandbox.data / PREFS / "org.jkiss.dbeaver.core.prefs"
    _write(user_prefs, "resultset.maxrows=10\n")

    _succeeded(sandbox.run("install"))

    # Settings the user has stay; default files the user lacks are added.
    assert user_prefs.read_text("utf-8") == "resultset.maxrows=10\n"
    assert (sandbox.data / "settings" / "global-settings.ini").read_text("utf-8") == "nl=pt_BR\n"
    drivers_xml = ElementTree.parse(sandbox.workspace / ".metadata" / ".config" / "drivers.xml")
    libraries = drivers_xml.findall(
        "provider[@id='postgresql']/driver[@id='postgres-jdbc']/library"
    )
    assert [(lib.get("path"), lib.get("version")) for lib in libraries] == [
        ("D:\\extra.jar", None),
        ("maven:/g:a:RELEASE", "2"),
    ]
    assert drivers_xml.find("provider[@id='generic']/driver[@id='mine']") is not None
    sources = sandbox.data_sources()
    assert sorted(sources["connections"]) == ["pkg-new", "user-1"]
    assert sources["folders"] == {"Team": {}}
    assert sources["connection-types"] == {"dev": {"name": "Development"}}
    assert sorted(sandbox.state()["offeredDataSources"]) == ["pkg-deleted", "pkg-new"]
    assert not (sandbox.data / "drivers" / OLD_JAR).exists()
    assert (sandbox.data / "drivers" / r"maven\maven-central\other\z-9.jar").exists()
    assert (sandbox.data / "drivers" / NEW_JAR).exists()


def test_upgrade_from_the_old_kind_of_package_mirrors_drivers_once(sandbox: Sandbox) -> None:
    """Without installer state, mirror the drivers folder once, as the old scripts did."""
    sandbox.build_package(None)
    _write(sandbox.install / "dbeaver.exe", "installed by the old instalar.cmd")
    _write(sandbox.data / "drivers" / OLD_JAR, "old")

    _succeeded(sandbox.run("install"))

    files = [
        p.relative_to(sandbox.data / "drivers") for p in (sandbox.data / "drivers").rglob("*.jar")
    ]
    assert files == [Path(NEW_JAR)]


def test_reset_restores_defaults_and_keeps_connections(sandbox: Sandbox) -> None:
    """Reset preferences and layout from the installed copy; connections stay."""
    sandbox.build_package({"connections": {"pkg-1": _connection("Shared")}})
    _succeeded(sandbox.run("install"))
    prefs = sandbox.workspace.parent / PREFS
    _write(prefs / "org.jkiss.dbeaver.core.prefs", "resultset.maxrows=10\n")
    _write(prefs / "org.eclipse.debug.ui.prefs", "changed=1\n")
    layout = (
        sandbox.workspace / ".metadata" / ".plugins" / "org.eclipse.e4.workbench" / "workbench.xmi"
    )
    _write(layout, "<layout/>")

    _succeeded(sandbox.run("reset", installed=True))

    assert (prefs / "org.jkiss.dbeaver.core.prefs").read_text("utf-8") == "resultset.maxrows=750\n"
    assert not (prefs / "org.eclipse.debug.ui.prefs").exists()
    assert not layout.exists()
    assert list(sandbox.data_sources()["connections"]) == ["pkg-1"]


def test_uninstall_keeps_user_data_by_default(sandbox: Sandbox) -> None:
    """Remove app, drivers, shortcuts and registry entry but keep the user's workspace."""
    sandbox.build_package(None)
    _succeeded(sandbox.run("install"))

    _succeeded(sandbox.run("uninstall", installed=True))

    assert not sandbox.install.exists()
    assert not (sandbox.data / "drivers").exists()
    assert not (sandbox.root / "Desktop" / "DBeaver.lnk").exists()
    assert not (sandbox.root / "StartMenu" / "DBeaver.lnk").exists()
    with pytest.raises(FileNotFoundError):
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, sandbox.registry)
    assert (sandbox.workspace / ".metadata" / ".config" / "drivers.xml").exists()
    assert "driverFiles" not in sandbox.state()


def test_installing_needs_the_package(sandbox: Sandbox) -> None:
    """Point the user at instalar.cmd when install is run from the installed copy."""
    sandbox.build_package(None)
    _succeeded(sandbox.run("install"))

    result = sandbox.run("install", installed=True)

    assert result.returncode == 1
    assert "instalar.cmd" in result.stdout
