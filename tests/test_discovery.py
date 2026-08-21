"""Unit tests for core.discovery."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.discovery import (
    DiscoveryErrorKind,
    RootSource,
    default_candidates,
    discover,
    validate_root,
)


def _make_root(
    base: Path,
    name: str = "Steam",
    *,
    compatdata: bool = True,
    vdf_paths: list[str] | None = None,
) -> Path:
    root = base / name
    steamapps = root / "steamapps"
    steamapps.mkdir(parents=True)
    if compatdata:
        (steamapps / "compatdata").mkdir()
    if vdf_paths is not None:
        lines = ['"libraryfolders"', "{"]
        for index, entry in enumerate(vdf_paths):
            lines += [f'    "{index}"', "    {", f'        "path" "{entry}"', "    }"]
        lines.append("}")
        (steamapps / "libraryfolders.vdf").write_text("\n".join(lines), encoding="utf-8")
    return root


def test_default_candidates_order(tmp_path: Path) -> None:
    candidates = default_candidates(home=tmp_path)
    assert [entry.source for entry in candidates] == [
        RootSource.NATIVE,
        RootSource.NATIVE,
        RootSource.NATIVE,
        RootSource.FLATPAK,
        RootSource.FLATPAK,
        RootSource.SNAP,
    ]
    assert candidates[0].path == tmp_path / ".local" / "share" / "Steam"
    assert candidates[3].path == (
        tmp_path / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"
    )
    assert candidates[5].path == (
        tmp_path / "snap" / "steam" / "common" / ".local" / "share" / "Steam"
    )


def test_validate_root_accepts_compatdata_only(tmp_path: Path) -> None:
    root = _make_root(tmp_path, vdf_paths=None)
    assert validate_root(root) is True


def test_validate_root_accepts_vdf_only(tmp_path: Path) -> None:
    root = _make_root(tmp_path, compatdata=False, vdf_paths=[])
    assert validate_root(root) is True


def test_validate_root_rejects_plain_directory(tmp_path: Path) -> None:
    plain = tmp_path / "not-steam"
    plain.mkdir()
    assert validate_root(plain) is False


def test_validate_root_rejects_compatdata_file_without_vdf(tmp_path: Path) -> None:
    root = _make_root(tmp_path, compatdata=False)
    (root / "steamapps" / "compatdata").write_text("not a directory", encoding="utf-8")
    assert validate_root(root) is False


def test_validate_root_accepts_vdf_despite_compatdata_file(tmp_path: Path) -> None:
    root = _make_root(tmp_path, compatdata=False, vdf_paths=[])
    (root / "steamapps" / "compatdata").write_text("not a directory", encoding="utf-8")
    assert validate_root(root) is True


def test_native_root_discovered(tmp_path: Path) -> None:
    root = _make_root(tmp_path / ".local" / "share")
    result = discover(home=tmp_path)
    assert result.found_roots is True
    assert [entry.path for entry in result.roots] == [root.resolve()]
    assert result.roots[0].source is RootSource.NATIVE
    assert [lib.path for lib in result.libraries] == [root.resolve()]


def test_flatpak_root_discovered(tmp_path: Path) -> None:
    base = tmp_path / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share"
    root = _make_root(base)
    result = discover(home=tmp_path)
    assert [entry.source for entry in result.roots] == [RootSource.FLATPAK]
    assert result.roots[0].path == root.resolve()


def test_snap_root_discovered(tmp_path: Path) -> None:
    base = tmp_path / "snap" / "steam" / "common" / ".local" / "share"
    _make_root(base)
    result = discover(home=tmp_path)
    assert [entry.source for entry in result.roots] == [RootSource.SNAP]


def test_custom_root_discovered(tmp_path: Path) -> None:
    root = _make_root(tmp_path / "games")
    result = discover(custom_roots=[str(root)], home=tmp_path)
    assert [entry.source for entry in result.roots] == [RootSource.CUSTOM]
    assert result.roots[0].path == root.resolve()


def test_custom_root_tilde_expanded(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    root = _make_root(tmp_path / "steam-custom")
    result = discover(custom_roots=["~/steam-custom/Steam"], home=tmp_path / "elsewhere")
    assert [entry.path for entry in result.roots] == [root.resolve()]


def test_multi_library_merge(tmp_path: Path) -> None:
    main = _make_root(tmp_path / ".local" / "share")
    extra = _make_root(tmp_path / "mnt" / "games", name="SteamLibrary")
    (main / "steamapps" / "libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n    "1"\n    {{\n        "path" "{extra}"\n    }}\n}}\n',
        encoding="utf-8",
    )
    result = discover(home=tmp_path)
    assert [lib.path for lib in result.libraries] == [main.resolve(), extra.resolve()]
    assert all(lib.root == main.resolve() for lib in result.libraries)


def test_symlinked_duplicate_roots_merge(tmp_path: Path) -> None:
    real = _make_root(tmp_path / ".local" / "share")
    link = tmp_path / ".steam"
    link.mkdir()
    (link / "steam").symlink_to(real)
    result = discover(home=tmp_path)
    assert len(result.roots) == 1
    assert result.roots[0].path == real.resolve()


def test_invalid_candidate_rejected(tmp_path: Path) -> None:
    plain = tmp_path / ".local" / "share" / "NotSteam"
    plain.mkdir(parents=True)
    result = discover(home=tmp_path)
    assert result.found_roots is False
    assert result.libraries == []
    assert result.errors == []


def test_no_roots_reports_clean_empty_result(tmp_path: Path) -> None:
    result = discover(home=tmp_path)
    assert result.found_roots is False
    assert result.roots == []
    assert result.libraries == []
    assert result.errors == []


def test_malformed_vdf_isolated_from_other_roots(tmp_path: Path) -> None:
    broken = _make_root(tmp_path / ".local" / "share")
    (broken / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n    "0"\n', encoding="utf-8"
    )
    healthy = _make_root(tmp_path / "games", name="SteamLibrary")
    result = discover(custom_roots=[healthy], home=tmp_path)
    assert [root.path for root in result.roots] == [broken.resolve(), healthy.resolve()]
    assert [lib.path for lib in result.libraries] == [broken.resolve(), healthy.resolve()]
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.kind is DiscoveryErrorKind.PARSE
    assert error.path == broken / "steamapps" / "libraryfolders.vdf"


@pytest.mark.skipif(os.geteuid() == 0, reason="permission checks do not apply to root")
def test_unreadable_vdf_reports_permission_error(tmp_path: Path) -> None:
    root = _make_root(tmp_path / ".local" / "share", vdf_paths=[])
    vdf_path = root / "steamapps" / "libraryfolders.vdf"
    vdf_path.write_text('""\n{\n}\n', encoding="utf-8")
    vdf_path.chmod(0o000)
    try:
        result = discover(home=tmp_path)
    finally:
        vdf_path.chmod(0o644)
    assert result.found_roots is True
    assert len(result.errors) == 1
    assert result.errors[0].kind is DiscoveryErrorKind.PERMISSION
    assert result.errors[0].path == vdf_path


def test_missing_library_path_recorded_and_excluded(tmp_path: Path) -> None:
    root = _make_root(tmp_path / ".local" / "share")
    ghost = tmp_path / "vanished" / "SteamLibrary"
    (root / "steamapps" / "libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n    "1"\n    {{\n        "path" "{ghost}"\n    }}\n}}\n',
        encoding="utf-8",
    )
    result = discover(home=tmp_path)
    assert [lib.path for lib in result.libraries] == [root.resolve()]
    assert len(result.errors) == 1
    error = result.errors[0]
    assert error.kind is DiscoveryErrorKind.MISSING
    assert error.path == ghost


def test_duplicate_custom_root_not_duplicated(tmp_path: Path) -> None:
    root = _make_root(tmp_path / ".local" / "share")
    result = discover(custom_roots=[str(root)], home=tmp_path)
    assert len(result.roots) == 1
    assert result.roots[0].source is RootSource.NATIVE


def test_multiple_installs_merge(tmp_path: Path) -> None:
    native = _make_root(tmp_path / ".local" / "share")
    flatpak_base = tmp_path / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share"
    flatpak = _make_root(flatpak_base)
    custom = _make_root(tmp_path / "opt", name="SteamCustom")
    result = discover(custom_roots=[custom], home=tmp_path)
    assert [root.path for root in result.roots] == [
        native.resolve(),
        flatpak.resolve(),
        custom.resolve(),
    ]
    assert [root.source for root in result.roots] == [
        RootSource.NATIVE,
        RootSource.FLATPAK,
        RootSource.CUSTOM,
    ]
