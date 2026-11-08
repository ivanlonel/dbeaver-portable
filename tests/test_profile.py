"""Tests for capturing the curated defaults and selecting data sources."""

import json
lazy from pathlib import Path

import pytest

from dbeaver_portable import profile

PRIVATE = r"C\:\\Users\\someone"  # how a .properties file spells C:\Users\someone

DOCUMENT = {
    "folders": {"Prod": {}, "Prod/Census": {"parent": "Prod"}, "Unused": {}},
    "connections": {
        "pg-1": {
            "provider": "postgresql",
            "driver": "postgres-jdbc",
            "name": "Census",
            "save-password": True,
            "folder": "Prod/Census",
            "configuration": {
                "host": "db.example.org",
                "port": "5432",
                "database": "census",
                "user": "me",
                "password": "secret",
                "type": "prod",
                "handlers": {"ssh_tunnel": {"properties": {"host": "jump", "user": "me"}}},
            },
        },
        "sqlite-1": {
            "provider": "sqlite",
            "driver": "sqlite_jdbc",
            "name": "Local",
            "configuration": {"database": "D:/data/local.db", "type": "dev"},
        },
    },
    "connection-types": {"prod": {"name": "Production"}, "dev": {"name": "Development"}},
}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")


def test_capture_keeps_choices_and_drops_machine_state(tmp_path: Path) -> None:
    """Capture allowlisted settings minus comments, denied keys and private paths."""
    source = tmp_path / "DBeaverData"
    prefs = source / profile.PREFS
    _write(
        prefs / "org.jkiss.dbeaver.core.prefs",
        "#Wed Oct 07 12:32:02 GMT-03:00 2026\neclipse.preferences.version=1\n"
        f"platform.language=pt_BR\nqm.logDirectory={PRIVATE}\\\\x\nrecent.file={PRIVATE}\\\\y\n",
    )
    _write(
        prefs / "org.eclipse.ui.workbench.prefs", "editors=<xml/>\nACTIVE_TAB_BG_END=72,72,76\n"
    )
    _write(prefs / "org.eclipse.debug.ui.prefs", "auto.generated=1\n")  # not allowlisted
    _write(source / "settings" / "global-settings.ini", "#DBeaver configuration\nnl=pt_BR\n")
    _write(
        source / "settings" / "product-config.json",
        '{"features": {"ai": {"enabled": false}, "git": {"enabled": true}}}',
    )
    _write(
        source / "workspace6" / ".metadata" / "dbeaver-workspace.properties", "workspace-id=X\n"
    )
    dest = tmp_path / "defaults"
    _write(dest / "stale.prefs", "old=1\n")

    captured = profile.capture(source, dest, [PRIVATE])

    assert sorted(captured) == [
        "settings/global-settings.ini",
        "settings/product-config.json",
        f"{profile.PREFS}/org.eclipse.ui.workbench.prefs",
        f"{profile.PREFS}/org.jkiss.dbeaver.core.prefs",
    ]
    assert sorted(
        p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()
    ) == sorted(captured)
    assert (dest / profile.PREFS / "org.jkiss.dbeaver.core.prefs").read_text("utf-8") == (
        "eclipse.preferences.version=1\nplatform.language=pt_BR\n"
    )
    assert (dest / profile.PREFS / "org.eclipse.ui.workbench.prefs").read_text("utf-8") == (
        "ACTIVE_TAB_BG_END=72,72,76\n"
    )
    assert (dest / "settings" / "global-settings.ini").read_text("utf-8") == "nl=pt_BR\n"
    # Copied verbatim: its presence is what skips DBeaver's first-run product dialog.
    features = json.loads((dest / "settings" / "product-config.json").read_text("utf-8"))
    assert features == {"features": {"ai": {"enabled": False}, "git": {"enabled": True}}}


def test_capture_rejects_a_folder_that_is_not_dbeaver_data(tmp_path: Path) -> None:
    """Refuse to replace the defaults from a folder that is not a DBeaver profile."""
    with pytest.raises(profile.ProfileError, match="not a DBeaverData folder"):
        profile.capture(tmp_path, tmp_path / "defaults", [])


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("", []),
        ("none", []),
        ("ALL", [0, 1, 2, 3]),
        ("1,3", [0, 2]),
        (" 2-4 ", [1, 2, 3]),
        ("4, 1-2, 2", [0, 1, 3]),
    ],
)
def test_parse_selection(answer: str, expected: list[int]) -> None:
    """Accept all, none, numbers and ranges, returning sorted zero-based indexes."""
    assert profile.parse_selection(answer, 4) == expected


@pytest.mark.parametrize("answer", ["0", "5", "x", "3-1", "1,,2"])
def test_parse_selection_rejects_bad_answers(answer: str) -> None:
    """Reject answers that are not in-range numbers or ranges."""
    with pytest.raises(ValueError, match=r"range|number"):
        profile.parse_selection(answer, 4)


def test_load_and_select_data_sources(tmp_path: Path) -> None:
    """Summarize connections and match them by id or case-insensitive name."""
    path = tmp_path / "data-sources.json"
    path.write_text(json.dumps(DOCUMENT), "utf-8")
    document, sources = profile.load_data_sources(path)
    assert document == DOCUMENT
    assert [(s.source_id, s.name, s.driver, s.target) for s in sources] == [
        ("pg-1", "Census", "postgresql/postgres-jdbc", "db.example.org:5432/census"),
        ("sqlite-1", "Local", "sqlite/sqlite_jdbc", "D:/data/local.db"),
    ]
    assert profile.select_named(sources, ["local", "pg-1"]) == ["sqlite-1", "pg-1"]
    with pytest.raises(profile.ProfileError, match="no data source named 'nope'"):
        profile.select_named(sources, ["nope"])
    assert profile.load_data_sources(tmp_path / "absent.json") == ({}, [])


def test_filter_keeps_references_and_drops_credentials() -> None:
    """Keep the referenced folders and types; drop users, passwords and save-password."""
    assert profile.filter_data_sources(DOCUMENT, ["pg-1"]) == {
        "folders": {"Prod": {}, "Prod/Census": {"parent": "Prod"}},
        "connections": {
            "pg-1": {
                "provider": "postgresql",
                "driver": "postgres-jdbc",
                "name": "Census",
                "folder": "Prod/Census",
                "configuration": {
                    "host": "db.example.org",
                    "port": "5432",
                    "database": "census",
                    "type": "prod",
                    "handlers": {"ssh_tunnel": {"properties": {"host": "jump"}}},
                },
            }
        },
        "connection-types": {"prod": {"name": "Production"}},
    }
