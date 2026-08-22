"""Unit tests for core.config."""

from __future__ import annotations

import json
from pathlib import Path

from core.config import (
    AppConfig,
    config_path,
    default_config,
    load_config,
    save_config,
)
from core.models import PrefixType


def test_default_config_values() -> None:
    config = default_config()
    assert config.steam_roots == []
    assert config.custom_roots == []
    assert config.font_family is None
    assert config.font_size == 10
    assert config.type_filter == [PrefixType.STEAM, PrefixType.NON_STEAM]
    assert config.sort_column == "size"
    assert config.sort_ascending is False
    assert config.size_cache == {}
    assert config.version == 1


def test_round_trip_preserves_values(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = AppConfig(
        steam_roots=["/a", "/b"],
        custom_roots=["/c"],
        font_family="Noto Sans",
        font_size=13,
        type_filter=[PrefixType.STEAM, PrefixType.ORPHANED],
        sort_column="name",
        sort_ascending=True,
        size_cache={"/x": {"size_bytes": 123, "scanned_at": "2026-01-01T00:00:00"}},
    )
    save_config(config, path)
    loaded = load_config(path)
    assert loaded == config


def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "does-not-exist.json")
    assert config == default_config()


def test_malformed_json_returns_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert load_config(path) == default_config()


def test_invalid_utf8_returns_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(b"\xff\xfe\x00invalid")
    assert load_config(path) == default_config()


def test_boolean_version_returns_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": True, "font_size": 20}), encoding="utf-8")
    assert load_config(path) == default_config()


def test_wrong_types_fall_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "steam_roots": "not-a-list",
                "custom_roots": [1, 2],
                "font_family": 42,
                "font_size": "big",
                "type_filter": ["bogus", "steam"],
                "sort_column": "bogus",
                "sort_ascending": "yes",
                "size_cache": "not-a-dict",
            }
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.steam_roots == []
    assert config.custom_roots == []
    assert config.font_family is None
    assert config.font_size == 10
    assert config.type_filter == [PrefixType.STEAM]
    assert config.sort_column == "size"
    assert config.sort_ascending is False
    assert config.size_cache == {}


def test_invalid_type_filter_items_revert_to_default(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "type_filter": ["bogus"]}), encoding="utf-8")
    config = load_config(path)
    assert config.type_filter == [PrefixType.STEAM, PrefixType.NON_STEAM]


def test_unknown_keys_ignored(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "future_key": "anything"}), encoding="utf-8")
    assert load_config(path) == default_config()


def test_unsupported_version_returns_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 99, "font_size": 20}), encoding="utf-8")
    assert load_config(path) == default_config()


def test_non_dict_root_returns_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_config(path) == default_config()


def test_config_path_uses_xdg(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert config_path() == tmp_path / "cfg" / "proton-prefix-manager" / "config.json"


def test_config_path_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert config_path() == tmp_path / ".config" / "proton-prefix-manager" / "config.json"


def test_save_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "config.json"
    save_config(default_config(), path)
    assert path.is_file()


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_config(default_config(), path)
    save_config(AppConfig(font_size=14), path)
    assert load_config(path).font_size == 14
    assert list(tmp_path.iterdir()) == [path]
