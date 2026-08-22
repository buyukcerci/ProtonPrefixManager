"""JSON configuration persistence."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core.models import PrefixType

CONFIG_DIR_NAME = "proton-prefix-manager"
CONFIG_FILE_NAME = "config.json"
CONFIG_VERSION = 1

DEFAULT_TYPE_FILTER = [PrefixType.STEAM, PrefixType.NON_STEAM, PrefixType.ORPHANED]
VALID_SORT_COLUMNS = ("size", "name", "app_id", "path")


def config_path() -> Path:
    """Resolve the config file location, honoring $XDG_CONFIG_HOME."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / CONFIG_DIR_NAME / CONFIG_FILE_NAME


def default_config() -> AppConfig:
    return AppConfig()


@dataclass
class AppConfig:
    """Application settings persisted as JSON under ~/.config/proton-prefix-manager/."""

    steam_roots: list[str] = field(default_factory=list)
    custom_roots: list[str] = field(default_factory=list)
    font_family: str | None = None
    font_size: int = 10
    type_filter: list[PrefixType] = field(default_factory=lambda: list(DEFAULT_TYPE_FILTER))
    sort_column: str = "size"
    sort_ascending: bool = False
    size_cache: dict[str, dict] = field(default_factory=dict)
    version: int = CONFIG_VERSION

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["type_filter"] = [entry.value for entry in self.type_filter]
        return data

    @classmethod
    def from_dict(cls, data: object) -> AppConfig:
        defaults = cls()
        if not isinstance(data, dict):
            return defaults
        version = data.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version != CONFIG_VERSION:
            return defaults
        steam_roots = _string_list(data.get("steam_roots"))
        custom_roots = _string_list(data.get("custom_roots"))
        font_family = data.get("font_family")
        if not isinstance(font_family, str):
            font_family = defaults.font_family
        font_size = data.get("font_size")
        if not isinstance(font_size, int) or isinstance(font_size, bool):
            font_size = defaults.font_size
        type_filter = _type_filter(data.get("type_filter"))
        sort_column = data.get("sort_column")
        if sort_column not in VALID_SORT_COLUMNS:
            sort_column = defaults.sort_column
        sort_ascending = data.get("sort_ascending")
        if not isinstance(sort_ascending, bool):
            sort_ascending = defaults.sort_ascending
        size_cache = _size_cache(data.get("size_cache"))
        return cls(
            steam_roots=steam_roots,
            custom_roots=custom_roots,
            font_family=font_family,
            font_size=font_size,
            type_filter=type_filter,
            sort_column=sort_column,
            sort_ascending=sort_ascending,
            size_cache=size_cache,
            version=CONFIG_VERSION,
        )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _type_filter(value: object) -> list[PrefixType]:
    if not isinstance(value, list):
        return list(DEFAULT_TYPE_FILTER)
    if not value:
        return []
    result: list[PrefixType] = []
    for item in value:
        if isinstance(item, str):
            try:
                result.append(PrefixType(item))
            except ValueError:
                pass
    return result if result else list(DEFAULT_TYPE_FILTER)


def _size_cache(value: object) -> dict[str, dict]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): entry
        for key, entry in value.items()
        if isinstance(key, str) and isinstance(entry, dict)
    }


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration, always returning a valid AppConfig without raising."""
    target = path or config_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return default_config()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return default_config()
    return AppConfig.from_dict(data)


def save_config(config: AppConfig, path: Path | None = None) -> None:
    """Write configuration atomically via a temp file and os.replace."""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{CONFIG_FILE_NAME}.", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config.to_dict(), handle, indent=2, sort_keys=True)
        os.replace(tmp_name, target)
    finally:
        if tmp_name:
            tmp_path = Path(tmp_name)
            if tmp_path.exists():
                tmp_path.unlink()
