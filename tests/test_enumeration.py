"""Unit tests for core.enumeration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.discovery import DiscoveryResult, Library, RootSource, SteamRoot
from core.enumeration import enumerate_from_discovery, enumerate_prefixes
from core.models import PrefixType
from tests.test_vdf_binary import build_shortcuts_vdf


def _make_library(
    base: Path,
    name: str = "SteamLibrary",
    app_ids: tuple[int, ...] = (),
    manifests: dict[int, str] | None = None,
) -> Library:
    library_path = base / name
    (library_path / "steamapps" / "compatdata").mkdir(parents=True)
    for app_id in app_ids:
        (library_path / "steamapps" / "compatdata" / str(app_id)).mkdir()
    for manifest_app_id, manifest_name in (manifests or {}).items():
        _make_manifest(library_path, manifest_app_id, manifest_name)
    return Library(path=library_path.resolve(), root=base.resolve())


def _make_manifest(library_path: Path, app_id: int, name: str) -> None:
    text = f'"AppState"\n{{\n    "appid" "{app_id}"\n    "name" "{name}"\n}}\n'
    (library_path / "steamapps" / f"appmanifest_{app_id}.acf").write_text(text, encoding="utf-8")


def _make_shortcut_root(base: Path, users: dict[str, list[tuple[int, str]]]) -> SteamRoot:
    for user_id, entries in users.items():
        target = base / "userdata" / user_id / "config"
        target.mkdir(parents=True)
        (target / "shortcuts.vdf").write_bytes(build_shortcuts_vdf(entries))
    return SteamRoot(path=base.resolve(), source=RootSource.CUSTOM)


def test_steam_game_classified_with_manifest_name(tmp_path: Path) -> None:
    lib = _make_library(tmp_path, app_ids=(480,), manifests={480: "Half-Life 2"})
    prefixes = enumerate_prefixes([lib])
    assert len(prefixes) == 1
    assert prefixes[0].prefix_type is PrefixType.STEAM
    assert prefixes[0].name == "Half-Life 2"


def test_non_steam_classified_with_shortcut_name(tmp_path: Path) -> None:
    lib = _make_library(tmp_path, app_ids=(111400000,))
    root = _make_shortcut_root(tmp_path / "root", {"1234": [(111400000, "Epic Game")]})
    prefixes = enumerate_prefixes([lib], [root])
    assert prefixes[0].prefix_type is PrefixType.NON_STEAM
    assert prefixes[0].name == "Epic Game"


def test_orphaned_without_any_source(tmp_path: Path) -> None:
    lib = _make_library(tmp_path, app_ids=(999,))
    prefixes = enumerate_prefixes([lib])
    assert prefixes[0].prefix_type is PrefixType.ORPHANED
    assert prefixes[0].name == "Unknown (AppID: 999)"


def test_removed_shortcut_becomes_orphaned_but_listed(tmp_path: Path) -> None:
    lib = _make_library(tmp_path, app_ids=(111400000,))
    root = _make_shortcut_root(tmp_path / "root", {"1234": []})
    prefixes = enumerate_prefixes([lib], [root])
    assert len(prefixes) == 1
    assert prefixes[0].prefix_type is PrefixType.ORPHANED


def test_large_appid_handled(tmp_path: Path) -> None:
    lib = _make_library(tmp_path, app_ids=(3_000_000_000,))
    prefixes = enumerate_prefixes([lib])
    assert prefixes[0].app_id == 3_000_000_000
    assert prefixes[0].prefix_type is PrefixType.ORPHANED


def test_leading_zero_dir_normalizes_appid(tmp_path: Path) -> None:
    lib = _make_library(tmp_path)
    (lib.path / "steamapps" / "compatdata" / "007").mkdir()
    prefixes = enumerate_prefixes([lib])
    assert prefixes[0].app_id == 7


def test_malformed_manifest_falls_back_not_hidden(tmp_path: Path) -> None:
    lib = _make_library(tmp_path, app_ids=(500,))
    manifest = lib.path / "steamapps" / "appmanifest_500.acf"
    manifest.write_text('"AppState"\n{\n    "name" ', encoding="utf-8")
    root = _make_shortcut_root(tmp_path / "root", {"1": [(500, "Shortcut Name")]})
    prefixes = enumerate_prefixes([lib], [root])
    assert len(prefixes) == 1
    assert prefixes[0].prefix_type is PrefixType.NON_STEAM
    assert prefixes[0].name == "Shortcut Name"


def test_malformed_manifest_without_shortcut_yields_orphan(tmp_path: Path) -> None:
    lib = _make_library(tmp_path, app_ids=(501,))
    manifest = lib.path / "steamapps" / "appmanifest_501.acf"
    manifest.write_text("garbage\x00binary", encoding="utf-8")
    prefixes = enumerate_prefixes([lib])
    assert prefixes[0].prefix_type is PrefixType.ORPHANED


def test_corrupt_shortcuts_file_skipped_valid_one_used(tmp_path: Path) -> None:
    lib = _make_library(tmp_path, app_ids=(700,))
    corrupt = tmp_path / "root" / "userdata" / "1" / "config"
    corrupt.mkdir(parents=True)
    (corrupt / "shortcuts.vdf").write_bytes(b"\x00broken")
    valid = tmp_path / "root" / "userdata" / "2" / "config"
    valid.mkdir(parents=True)
    (valid / "shortcuts.vdf").write_bytes(build_shortcuts_vdf([(700, "Good Name")]))
    root = SteamRoot(path=(tmp_path / "root").resolve(), source=RootSource.CUSTOM)
    prefixes = enumerate_prefixes([lib], [root])
    assert len(prefixes) == 1
    assert prefixes[0].name == "Good Name"


def test_duplicate_same_appid_and_path_deduped(tmp_path: Path) -> None:
    lib_a = _make_library(tmp_path / "a", app_ids=(800,))
    lib_b = Library(path=lib_a.path, root=tmp_path.resolve())
    prefixes = enumerate_prefixes([lib_a, lib_b])
    assert len(prefixes) == 1


def test_same_appid_different_libraries_both_kept(tmp_path: Path) -> None:
    lib_a = _make_library(tmp_path / "a", name="LibA", app_ids=(801,))
    lib_b = _make_library(tmp_path / "b", name="LibB", app_ids=(801,))
    prefixes = enumerate_prefixes([lib_a, lib_b])
    assert len(prefixes) == 2
    assert {str(lib.path) for lib in (lib_a, lib_b)} == {p.library for p in prefixes}


def test_non_numeric_and_file_entries_ignored(tmp_path: Path) -> None:
    lib = _make_library(tmp_path, app_ids=(900,))
    compatdata = lib.path / "steamapps" / "compatdata"
    (compatdata / "pfx").mkdir()
    (compatdata / ".DS_Store").write_text("", encoding="utf-8")
    (compatdata / "not-a-prefix.txt").write_text("file", encoding="utf-8")
    prefixes = enumerate_prefixes([lib])
    assert len(prefixes) == 1
    assert prefixes[0].app_id == 900


@pytest.mark.skipif(os.geteuid() == 0, reason="permission checks do not apply to root")
def test_unreadable_compatdata_subdir_does_not_crash(tmp_path: Path) -> None:
    lib = _make_library(tmp_path, app_ids=(901,))
    locked = lib.path / "steamapps" / "compatdata" / "901"
    (locked / "pfx").mkdir(parents=True)
    locked.chmod(0o000)
    try:
        prefixes = enumerate_prefixes([lib])
    finally:
        locked.chmod(0o755)
    assert len(prefixes) == 1
    assert prefixes[0].prefix_type is PrefixType.ORPHANED


def test_empty_compatdata_yields_no_prefixes(tmp_path: Path) -> None:
    lib = _make_library(tmp_path)
    assert enumerate_prefixes([lib]) == []


def test_missing_compatdata_skips_library(tmp_path: Path) -> None:
    bare = tmp_path / "Bare"
    bare.mkdir()
    lib = Library(path=bare.resolve(), root=tmp_path.resolve())
    assert enumerate_prefixes([lib]) == []


def test_library_association_recorded(tmp_path: Path) -> None:
    lib = _make_library(tmp_path, app_ids=(950,))
    prefixes = enumerate_prefixes([lib])
    assert prefixes[0].library == str(lib.path)


def test_manifest_priority_over_shortcut(tmp_path: Path) -> None:
    lib = _make_library(tmp_path, app_ids=(960,), manifests={960: "Manifest Wins"})
    root = _make_shortcut_root(tmp_path / "root", {"1": [(960, "Shortcut Loses")]})
    prefixes = enumerate_prefixes([lib], [root])
    assert prefixes[0].prefix_type is PrefixType.STEAM
    assert prefixes[0].name == "Manifest Wins"


def test_shortcuts_map_override_used_without_roots(tmp_path: Path) -> None:
    lib = _make_library(tmp_path, app_ids=(970,))
    prefixes = enumerate_prefixes([lib], shortcuts_map={970: "Injected"})
    assert prefixes[0].prefix_type is PrefixType.NON_STEAM
    assert prefixes[0].name == "Injected"


def test_symlinked_entry_skipped(tmp_path: Path) -> None:
    lib = _make_library(tmp_path, app_ids=(980,))
    outside = tmp_path / "outside"
    outside.mkdir()
    (lib.path / "steamapps" / "compatdata" / "981").symlink_to(outside)
    prefixes = enumerate_prefixes([lib])
    assert [prefix.app_id for prefix in prefixes] == [980]


def test_enumerate_from_discovery_wires_result(tmp_path: Path) -> None:
    lib = _make_library(tmp_path, app_ids=(990,), manifests={990: "Wired"})
    result = DiscoveryResult(libraries=[lib])
    prefixes = enumerate_from_discovery(result)
    assert len(prefixes) == 1
    assert prefixes[0].name == "Wired"
