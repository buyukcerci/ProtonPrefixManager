"""Unit tests for core.tools and the tool deletion validator."""

from __future__ import annotations

import errno
import os
import shutil
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import deletion as deletion_module
from core import tools as tools_module
from core.deletion import (
    DeleteMode,
    DeletionStatus,
    RejectReason,
    delete_tools,
)
from core.discovery import Library, RootSource, SteamRoot
from core.toolmap import load_tool_mapping
from core.tools import (
    CompatToolParseError,
    Tool,
    ToolCategory,
    enumerate_tools,
    is_reclaimable,
    parse_compat_tool_text,
    tool_category,
    used_by,
)

VALID_TOOL_VDF = """
"compatibilitytools"
{
  "compat_tools"
  {
    "GE-Proton9-1"
    {
      "install_path" "."
      "display_name" "GE-Proton 9-1"
      "from_oslist" "windows"
      "to_oslist" "linux"
    }
  }
}
"""

MAPPING_SHELL = """
"InstallConfigStore"
{
  "Software"
  {
    "Valve"
    {
      "Steam"
      {
        "CompatToolMapping"
        {
%s
        }
      }
    }
  }
}
"""


def _mapping_entry(app_id: int, tool: str) -> str:
    return (
        f'          "{app_id}"\n          {{\n'
        f'            "name" "{tool}"\n'
        '            "config" ""\n'
        '            "priority" "250"\n'
        "          }\n"
    )


def _make_root(base: Path, name: str = "Steam") -> SteamRoot:
    return SteamRoot(path=base / name, source=RootSource.NATIVE)


def _make_library(base: Path, name: str = "lib") -> Library:
    return Library(path=(base / name).resolve(), root=base.resolve())


def _write_tool(
    toolsdir: Path,
    dirname: str,
    vdf_text: str | None = VALID_TOOL_VDF,
    size: int = 0,
) -> Path:
    target = toolsdir / dirname
    target.mkdir(parents=True, exist_ok=True)
    if vdf_text is not None:
        (target / "compatibilitytool.vdf").write_text(vdf_text, encoding="utf-8")
    if size:
        (target / "payload.bin").write_bytes(b"x" * size)
    return target


def _write_mapping(root: Path, entries: str) -> None:
    config_path = root / "config" / "config.vdf"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(MAPPING_SHELL % entries, encoding="utf-8")


def _enumerate(roots: Sequence[SteamRoot], libraries: Sequence[Library]) -> list[Tool]:
    """Enumerate tools and drop the warning channel for compact assertions."""
    tools, _ = enumerate_tools(roots, libraries)
    return tools


@pytest.fixture(autouse=True)
def _isolated_system_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    system = tmp_path / "system-tools"
    monkeypatch.setattr(tools_module, "SYSTEM_TOOLS_DIR", system)
    return system


def test_parse_valid_tool_file() -> None:
    assert parse_compat_tool_text(VALID_TOOL_VDF) == "GE-Proton 9-1"


def test_parse_spaced_block_name() -> None:
    text = VALID_TOOL_VDF.replace('"compat_tools"', '"compat tools"')
    assert parse_compat_tool_text(text) == "GE-Proton 9-1"


def test_parse_missing_block_returns_blank() -> None:
    assert parse_compat_tool_text('"other"\n{\n"a" "b"\n}\n') == ""


def test_parse_malformed_raises() -> None:
    with pytest.raises(CompatToolParseError):
        parse_compat_tool_text('"compatibilitytools"\n{\n"compat_tools" ')


def test_parse_recursion_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(text: str) -> dict[str, object]:
        raise RecursionError("nesting too deep")

    monkeypatch.setattr(tools_module.vdf, "loads", boom)
    with pytest.raises(CompatToolParseError):
        parse_compat_tool_text('"compatibilitytools"\n{\n}')


def test_parse_deep_nesting_finds_block_without_recursing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The block finder walks the parsed structure; a document nested past
    # the Python recursion limit must still resolve instead of raising
    # RecursionError outside the parse error handling.
    deep: dict[str, object] = {"compat_tools": {"GE-Proton9-1": {"display_name": "Deep"}}}
    for _ in range(6000):
        deep = {"level": deep}

    def fake_loads(text: str) -> dict[str, object]:
        return deep

    monkeypatch.setattr(tools_module.vdf, "loads", fake_loads)
    assert parse_compat_tool_text("{}") == "Deep"


def test_first_compattools_block_wins_over_later_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The block finder walks the parsed structure in preorder, so a
    # later sibling that carries a same-named nested block must not
    # shadow the first match found earlier in insertion order.
    data: dict[str, object] = {
        "CompatTools": {"GE-Proton9-1": {"display_name": "First"}},
        "Other": {"CompatTools": {"GE-Proton9-1": {"display_name": "Second"}}},
    }

    def fake_loads(text: str) -> dict[str, object]:
        return data

    monkeypatch.setattr(tools_module.vdf, "loads", fake_loads)
    assert parse_compat_tool_text("{}") == "First"


def test_deeper_earlier_compattools_block_wins_over_shallow_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data: dict[str, object] = {
        "Outer": {"CompatTools": {"GE-Proton9-1": {"display_name": "Deep"}}},
        "CompatTools": {"GE-Proton9-1": {"display_name": "Shallow"}},
    }

    def fake_loads(text: str) -> dict[str, object]:
        return data

    monkeypatch.setattr(tools_module.vdf, "loads", fake_loads)
    assert parse_compat_tool_text("{}") == "Deep"


def test_oversized_vdf_is_skipped_and_dirname_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "Huge", vdf_text=None)
    vdf_path = toolsdir / "Huge" / "compatibilitytool.vdf"
    vdf_path.write_bytes(b"x" * 200_000)
    cap = 4
    monkeypatch.setattr(tools_module, "MAX_VDF_BYTES", cap)
    read_bytes: list[bytes] = []
    original_read = os.read

    def tracking_read(fd: int, size: int) -> bytes:
        chunk = original_read(fd, size)
        read_bytes.append(chunk)
        return chunk

    monkeypatch.setattr("os.read", tracking_read)
    tools = _enumerate([root], [])
    assert [tool.name for tool in tools] == ["Huge"]
    # The bounded read stops at the cap instead of slurping the file.
    assert sum(len(chunk) for chunk in read_bytes) <= cap + 65_536


def test_vdf_within_cap_still_parses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "GE-Proton9-1", size=8)
    monkeypatch.setattr(tools_module, "MAX_VDF_BYTES", 1_048_576)
    tools = _enumerate([root], [])
    assert [tool.name for tool in tools] == ["GE-Proton 9-1"]


def test_vdf_exactly_at_cap_still_parses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "GE-Proton9-1", vdf_text=None)
    vdf_path = toolsdir / "GE-Proton9-1" / "compatibilitytool.vdf"
    content = VALID_TOOL_VDF.encode("utf-8")
    vdf_path.write_bytes(content)
    monkeypatch.setattr(tools_module, "MAX_VDF_BYTES", len(content))
    tools = _enumerate([root], [])
    assert [tool.name for tool in tools] == ["GE-Proton 9-1"]


def test_bom_vdf_parses_display_name(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "GE-Proton9-1", vdf_text=None)
    vdf_path = toolsdir / "GE-Proton9-1" / "compatibilitytool.vdf"
    vdf_path.write_text(VALID_TOOL_VDF, encoding="utf-8-sig")
    tools = _enumerate([root], [])
    # The BOM must be decoded away, or the exact-match used_by lookup
    # on the display name would never hit.
    assert [tool.name for tool in tools] == ["GE-Proton 9-1"]


def test_fifo_vdf_does_not_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "Piped", vdf_text=None)
    os.mkfifo(toolsdir / "Piped" / "compatibilitytool.vdf")
    read_calls: list[int] = []
    original_read = os.read

    def tracking_read(fd: int, size: int) -> bytes:
        read_calls.append(fd)
        return original_read(fd, size)

    monkeypatch.setattr("os.read", tracking_read)
    # Without a non-blocking open this call hangs forever on the FIFO.
    tools = _enumerate([root], [])
    assert [tool.name for tool in tools] == ["Piped"]
    # The FIFO must be rejected by the S_ISREG fstat check before any
    # read happens, not by an empty read on the open descriptor.
    assert read_calls == []


def test_symlinked_vdf_falls_back_to_dirname(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    target = tmp_path / "outside.vdf"
    target.write_text(VALID_TOOL_VDF, encoding="utf-8")
    tool_dir = _write_tool(toolsdir, "Linked", vdf_text=None)
    (tool_dir / "compatibilitytool.vdf").symlink_to(target)
    tools = _enumerate([root], [])
    assert [tool.name for tool in tools] == ["Linked"]


def test_overcap_vdf_content_falls_back_to_dirname(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "Overflow", vdf_text=None)
    vdf_path = toolsdir / "Overflow" / "compatibilitytool.vdf"
    vdf_path.write_bytes(VALID_TOOL_VDF.encode("utf-8") + b" " * 16)
    monkeypatch.setattr(tools_module, "MAX_VDF_BYTES", 4)
    tools = _enumerate([root], [])
    assert [tool.name for tool in tools] == ["Overflow"]


def test_parse_empty_raises() -> None:
    with pytest.raises(CompatToolParseError):
        parse_compat_tool_text("")


def test_enumerate_custom_tool(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "GE-Proton9-1")
    tools = _enumerate([root], [])
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "GE-Proton 9-1"
    assert tool.path == (toolsdir / "GE-Proton9-1").resolve(strict=False)
    assert tool.root == root.path
    assert tool.read_only is False


def test_enumerate_missing_dirs_is_empty(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    library = _make_library(tmp_path)
    assert _enumerate([root], [library]) == []


def test_enumerate_missing_vdf_falls_back_to_dirname(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "MyBuild", vdf_text=None)
    (toolsdir / "MyBuild" / "compatibilitytool.vdf").write_bytes(b"\xff\xfe broken")
    _write_tool(toolsdir, "PlainDir", vdf_text=None)
    tools = _enumerate([root], [])
    assert {tool.name for tool in tools} == {"MyBuild", "PlainDir"}


def test_enumerate_malformed_vdf_falls_back(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "Broken", vdf_text='"compatibilitytools"\n{\n')
    (toolsdir / "Broken" / "extra.txt").write_text("x", encoding="utf-8")
    tools = _enumerate([root], [])
    assert len(tools) == 1
    assert tools[0].name == "Broken"


def test_enumerate_skips_symlinks_and_files(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    real = _write_tool(toolsdir, "Real")
    (toolsdir / "Link").symlink_to(real, target_is_directory=True)
    (toolsdir / "notes.txt").write_text("hi", encoding="utf-8")
    tools = _enumerate([root], [])
    assert [tool.name for tool in tools] == ["GE-Proton 9-1"]


def test_enumerate_managed_tools_are_read_only(tmp_path: Path) -> None:
    library = _make_library(tmp_path)
    common = library.path / "steamapps" / "common"
    (common / "Proton 9.0").mkdir(parents=True)
    (common / "Proton - Experimental").mkdir(parents=True)
    (common / "SteamLinuxRuntime").mkdir(parents=True)
    (common / "SteamLinuxRuntime_sniper").mkdir(parents=True)
    (common / "ProtonNotes.txt").write_text("hi", encoding="utf-8")
    tools = _enumerate([], [library])
    assert {tool.name for tool in tools} == {
        "Proton 9.0",
        "Proton - Experimental",
        "SteamLinuxRuntime",
        "SteamLinuxRuntime_sniper",
    }
    assert all(tool.read_only for tool in tools)


def test_system_dir_tools_are_read_only(_isolated_system_dir: Path) -> None:
    target = _isolated_system_dir / "DistroBuild"
    target.mkdir(parents=True)
    (target / "compatibilitytool.vdf").write_text(VALID_TOOL_VDF, encoding="utf-8")
    (target / "payload.bin").write_bytes(b"x" * 16)
    tools = _enumerate([], [])
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "GE-Proton 9-1"
    assert tool.read_only is True
    assert tool.root == _isolated_system_dir.resolve(strict=False)


def test_missing_system_dir_is_empty(_isolated_system_dir: Path) -> None:
    assert not _isolated_system_dir.exists()
    assert _enumerate([], []) == []


def test_same_name_in_system_and_user_dirs_coexist(
    tmp_path: Path, _isolated_system_dir: Path
) -> None:
    system_target = _isolated_system_dir / "SharedBuild"
    system_target.mkdir(parents=True)
    (system_target / "compatibilitytool.vdf").write_text(VALID_TOOL_VDF, encoding="utf-8")
    root = _make_root(tmp_path)
    user_target = root.path / "compatibilitytools.d" / "SharedBuild"
    user_target.mkdir(parents=True)
    (user_target / "compatibilitytool.vdf").write_text(VALID_TOOL_VDF, encoding="utf-8")
    tools = _enumerate([root], [])
    assert len(tools) == 2
    by_path = {tool.path: tool for tool in tools}
    system_tool = by_path[(system_target).resolve(strict=False)]
    user_tool = by_path[(user_target).resolve(strict=False)]
    assert system_tool.read_only is True
    assert system_tool.root == _isolated_system_dir.resolve(strict=False)
    assert user_tool.read_only is False
    assert user_tool.root == root.path
    assert system_tool.name == user_tool.name == "GE-Proton 9-1"


def test_duplicate_names_across_roots_stay_separate(tmp_path: Path) -> None:
    first = _make_root(tmp_path / "a")
    second = _make_root(tmp_path / "b")
    _write_tool(first.path / "compatibilitytools.d", "Same")
    _write_tool(second.path / "compatibilitytools.d", "Same")
    tools = _enumerate([first, second], [])
    assert len(tools) == 2
    assert tools[0].path != tools[1].path


def test_same_library_twice_dedupes(tmp_path: Path) -> None:
    library = _make_library(tmp_path)
    common = library.path / "steamapps" / "common"
    (common / "Proton 9.0").mkdir(parents=True)
    tools = _enumerate([], [library, library])
    assert len(tools) == 1


def test_used_from_mapping_first_wins(tmp_path: Path) -> None:
    first = _make_root(tmp_path / "a")
    second = _make_root(tmp_path / "b")
    _write_tool(first.path / "compatibilitytools.d", "GE-Proton9-1")
    _write_tool(second.path / "compatibilitytools.d", "GE-Proton9-1")
    _write_tool(second.path / "compatibilitytools.d", "UnusedBuild", vdf_text=None)
    _write_mapping(first.path, _mapping_entry(480, "GE-Proton9-1"))
    _write_mapping(
        second.path, _mapping_entry(480, "OtherBuild") + _mapping_entry(481, "UnusedBuild")
    )
    mapping, errors = load_tool_mapping([first, second])
    assert errors == []
    assert mapping[480] == "GE-Proton9-1"
    tools = _enumerate([first, second], [])
    used = used_by(tools, mapping)
    assert set(used) == {str(tools[0].path), str(tools[1].path), str(tools[2].path)}


def test_used_by_lists_app_ids_per_tool(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "DirMatch", vdf_text=None)
    _write_tool(toolsdir, "OtherDir")
    _write_mapping(
        root.path,
        _mapping_entry(480, "DirMatch")
        + _mapping_entry(481, "GE-Proton 9-1")
        + _mapping_entry(482, "DirMatch"),
    )
    mapping, _ = load_tool_mapping([root])
    tools = _enumerate([root], [])
    by_name = {tool.name: tool for tool in tools}
    assert used_by(tools, mapping) == {
        str(by_name["DirMatch"].path): [480, 482],
        str(by_name["GE-Proton 9-1"].path): [481],
    }


def test_unreferenced_tool_is_unused(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    _write_tool(root.path / "compatibilitytools.d", "GE-Proton9-1")
    _write_tool(root.path / "compatibilitytools.d", "OldBuild", vdf_text=None)
    _write_mapping(root.path, _mapping_entry(480, "GE-Proton9-1"))
    mapping, _ = load_tool_mapping([root])
    tools = _enumerate([root], [])
    assert set(used_by(tools, mapping)) == {str(tools[0].path)}


def test_used_matches_display_name(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    _write_tool(root.path / "compatibilitytools.d", "InternalDirName")
    _write_mapping(root.path, _mapping_entry(480, "GE-Proton 9-1"))
    mapping, _ = load_tool_mapping([root])
    tools = _enumerate([root], [])
    assert set(used_by(tools, mapping)) == {str(tools[0].path)}


def test_same_display_name_twins_all_stay_locked(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    vdf_text = VALID_TOOL_VDF.replace("GE-Proton 9-1", "Shared Name")
    _write_tool(toolsdir, "DirA", vdf_text=vdf_text)
    _write_tool(toolsdir, "DirB", vdf_text=vdf_text)
    _write_mapping(root.path, _mapping_entry(480, "Shared Name"))
    mapping, _ = load_tool_mapping([root])
    tools = _enumerate([root], [])
    assert set(used_by(tools, mapping)) == {str(tool.path) for tool in tools}


@pytest.fixture()
def trash_calls(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    def fake_send2trash(path: Path) -> None:
        calls.append(path)

    monkeypatch.setattr(deletion_module, "send2trash", fake_send2trash)
    return calls


def test_symlinked_toolsdir_is_skipped_and_reported(
    tmp_path_factory: pytest.TempPathFactory, trash_calls: list[Path]
) -> None:
    # The layout lives in a fixed-name directory instead of tmp_path, so
    # the warning message cannot match its own classification word through
    # a path named after the test.
    base = tmp_path_factory.mktemp("skiplayout")
    root = _make_root(base)
    victim = base / "victim"
    planted = _write_tool(victim, "Planted")
    toolsdir = root.path / "compatibilitytools.d"
    toolsdir.parent.mkdir(parents=True)
    toolsdir.symlink_to(victim, target_is_directory=True)
    tools, warnings = enumerate_tools([root], [])
    assert tools == []
    assert len(warnings) == 1
    assert warnings[0].path == toolsdir
    assert warnings[0].message.startswith("skipped symlinked tools directory:")
    # Repeated roots dedupe both the rows and the warnings.
    tools_again, warnings_again = enumerate_tools([root, root], [])
    assert tools_again == []
    assert len(warnings_again) == 1
    # The planted content is invisible to the deletion flow: nothing is
    # offered for deletion and the victim directory stays intact.
    results = delete_tools(tools, [root], used_paths=set())
    assert results == []
    assert trash_calls == []
    assert planted.is_dir()


def test_symlinked_common_dir_is_skipped_and_reported(
    tmp_path_factory: pytest.TempPathFactory, trash_calls: list[Path]
) -> None:
    base = tmp_path_factory.mktemp("skiplayout")
    library = _make_library(base)
    victim = base / "victim"
    planted = _write_tool(victim, "Proton 9.0", vdf_text=None)
    common = library.path / "steamapps" / "common"
    common.parent.mkdir(parents=True)
    common.symlink_to(victim, target_is_directory=True)
    tools, warnings = enumerate_tools([], [library])
    assert tools == []
    assert len(warnings) == 1
    assert warnings[0].path == common
    assert warnings[0].message.startswith("skipped symlinked common directory:")
    results = delete_tools(tools, [], used_paths=set(), libraries=[library])
    assert results == []
    assert trash_calls == []
    assert planted.is_dir()


def test_missing_toolsdir_is_silent(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    tools, warnings = enumerate_tools([root], [])
    assert tools == []
    assert warnings == []


def test_fd_enumeration_matches_expected_tool_fields(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "GE-Proton9-1", size=4)
    tools, warnings = enumerate_tools([root], [])
    assert warnings == []
    entry_identity = os.stat(toolsdir / "GE-Proton9-1", follow_symlinks=False)
    assert tools == [
        Tool(
            name="GE-Proton 9-1",
            path=(toolsdir / "GE-Proton9-1").resolve(strict=False),
            root=root.path,
            read_only=False,
            size_bytes=0,
            name_unverified=False,
            dev_ino=(entry_identity.st_dev, entry_identity.st_ino),
        )
    ]


def test_file_at_toolsdir_path_is_skipped_and_reported(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    root.path.mkdir(parents=True)
    planted = root.path / "compatibilitytools.d"
    planted.write_text("not a directory", encoding="utf-8")
    tools, warnings = enumerate_tools([root], [])
    assert tools == []
    assert len(warnings) == 1
    assert warnings[0].path == planted


def test_listdir_failure_on_pinned_fd_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "GE-Proton9-1")

    def failing_listdir(fd: int) -> list[str]:
        raise OSError(errno.EIO, "Input/output error")

    # The open succeeds, so the listing failure is the only skip signal and
    # must be reported instead of coming back as a silent empty state.
    monkeypatch.setattr(tools_module.os, "listdir", failing_listdir)
    tools, warnings = enumerate_tools([root], [])
    assert tools == []
    assert len(warnings) == 1
    assert warnings[0].path == toolsdir
    assert warnings[0].message.startswith("skipped unreadable tools directory:")


def test_path_fallback_matches_fd_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # With the descriptor scan disabled, the plain path walk must keep the
    # shared contract: non-directory entries are skipped silently, a
    # symlinked tools directory is skipped with the same warning, and a
    # missing directory stays a silent empty state.
    monkeypatch.setattr(tools_module, "_FD_ENUM_SUPPORTED", False)
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    real = _write_tool(toolsdir, "Real")
    (toolsdir / "Link").symlink_to(real, target_is_directory=True)
    (toolsdir / "notes.txt").write_text("hi", encoding="utf-8")
    tools, warnings = enumerate_tools([root], [])
    assert [tool.name for tool in tools] == ["GE-Proton 9-1"]
    assert warnings == []

    victim = tmp_path / "victim"
    planted = _write_tool(victim, "Planted")
    shutil.rmtree(toolsdir)
    toolsdir.symlink_to(victim, target_is_directory=True)
    tools, warnings = enumerate_tools([root], [])
    assert tools == []
    assert len(warnings) == 1
    assert warnings[0].path == toolsdir
    assert warnings[0].message.startswith("skipped symlinked tools directory:")

    missing = _make_root(tmp_path / "absent-base")
    tools, warnings = enumerate_tools([missing], [])
    assert tools == []
    assert warnings == []
    assert planted.is_dir()


def _patch_final_stat(monkeypatch: pytest.MonkeyPatch, watched: Path, *, swap: bool) -> list[bool]:
    """Route os.stat through a wrapper that can fake a swapped directory.

    Returns the list of observed verification calls. os.stat is only
    intercepted for the exact call the fd-based enumeration uses to
    verify the directory identity: the watched path, no dir_fd, and
    follow_symlinks False. When swap is set, that call reports a
    different inode, as it would after the directory was replaced.
    """
    real_stat = os.stat
    seen: list[bool] = []

    def tracked_stat(path: object, **kwargs: object) -> object:
        result = real_stat(path, **kwargs)  # type: ignore[arg-type]
        if (
            not kwargs.get("dir_fd")
            and kwargs.get("follow_symlinks", True) is False
            and Path(str(path)) == watched
        ):
            seen.append(True)
            if swap:
                return SimpleNamespace(st_dev=result.st_dev, st_ino=result.st_ino + 1)
        return result

    monkeypatch.setattr(os, "stat", tracked_stat)
    return seen


def test_toolsdir_swap_during_enumeration_discards_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "GE-Proton9-1")
    # Deterministic race simulation: the final same-inode verification
    # reports a changed inode, as it would when the directory was swapped
    # before or during enumeration. The rows from that root are discarded
    # and the skip is reported instead of enumerating swapped content.
    _patch_final_stat(monkeypatch, toolsdir, swap=True)
    tools, warnings = enumerate_tools([root], [])
    assert tools == []
    assert len(warnings) == 1
    assert warnings[0].path == toolsdir
    assert "changed during enumeration" in warnings[0].message


def test_toolsdir_verification_agreement_keeps_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "GE-Proton9-1")
    seen = _patch_final_stat(monkeypatch, toolsdir, swap=False)
    tools, warnings = enumerate_tools([root], [])
    # The verification call ran exactly once and agreed with the opened
    # directory inode, so the rows survive.
    assert seen == [True]
    assert [tool.name for tool in tools] == ["GE-Proton 9-1"]
    assert warnings == []


def test_common_dir_swap_during_enumeration_discards_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = _make_library(tmp_path)
    commondir = library.path / "steamapps" / "common"
    (commondir / "Proton 9.0").mkdir(parents=True)
    _patch_final_stat(monkeypatch, commondir, swap=True)
    tools, warnings = enumerate_tools([], [library])
    assert tools == []
    assert len(warnings) == 1
    assert warnings[0].path == commondir


def test_delete_rejects_toolsdir_symlink_planted_after_enumeration(
    tmp_path: Path, trash_calls: list[Path]
) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "Real", vdf_text=None)
    tools = _enumerate([root], [])
    # A link planted between enumeration and deletion must not redirect
    # the deletion onto the linked directory. The enumerated record binds
    # the target to the device and inode it was scanned with, and that
    # identity no longer matches anything under the planted link.
    victim = tmp_path / "victim"
    planted = _write_tool(victim, "Real", vdf_text=None)
    # The swap replaces the whole tools directory, so the planted link can
    # only exist after the original directory is gone.
    shutil.rmtree(toolsdir)
    toolsdir.symlink_to(victim, target_is_directory=True)
    results = delete_tools(tools, [root], used_paths=set())
    assert len(results) == 1
    assert results[0].status is DeletionStatus.REJECTED
    assert results[0].reject_reason is RejectReason.IDENTITY_MISMATCH
    assert trash_calls == []
    assert planted.is_dir()


def test_swap_to_symlink_during_build_cannot_relocate_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trash_calls: list[Path]
) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "Keep")
    real_entry = _write_tool(toolsdir, "SwapMe", vdf_text=None)
    real_identity = os.stat(real_entry, follow_symlinks=False)
    victim = tmp_path / "victim"
    planted = _write_tool(victim, "SwapMe")

    # Deterministic race replay: for exactly one entry build the tools
    # directory path is swapped to a symlink into the victim directory and
    # swapped straight back. The pinned descriptor keeps listing the real
    # entries and the post-loop verification passes, so the swap is
    # invisible to the row-discarding checks and the swapped row resolves
    # through the link into the victim.
    real_custom_tool = tools_module._custom_tool
    hold = tmp_path / "hold"
    swapped = {"done": False}

    def swap_for_one_build(entry: Path, root_path: Path, **kwargs: bool) -> Tool:
        if entry.name != "SwapMe" or swapped["done"]:
            return real_custom_tool(entry, root_path, **kwargs)
        swapped["done"] = True
        os.rename(toolsdir, hold)
        toolsdir.symlink_to(victim, target_is_directory=True)
        try:
            return real_custom_tool(entry, root_path, **kwargs)
        finally:
            toolsdir.unlink()
            os.rename(hold, toolsdir)

    monkeypatch.setattr(tools_module, "_custom_tool", swap_for_one_build)
    tools, warnings = enumerate_tools([root], [])
    assert warnings == []
    assert len(tools) == 2
    swapped_tool = next(tool for tool in tools if tool.path == victim / "SwapMe")
    kept_tool = next(tool for tool in tools if tool.path == toolsdir / "Keep")
    # The swapped row even adopted the display name read through the link,
    # but its recorded identity is the real entry the descriptor saw.
    assert swapped_tool.name == "GE-Proton 9-1"
    assert swapped_tool.dev_ino == (real_identity.st_dev, real_identity.st_ino)
    assert kept_tool.name == "GE-Proton 9-1"

    # The attacker plants the tools directory link again at delete time so
    # the approval boundary relocates onto the victim directory. The
    # recorded identity refers to the real entry directory, so the target
    # under the planted link cannot match and the batch must reject
    # instead of trashing victim content.
    shutil.rmtree(toolsdir)
    toolsdir.symlink_to(victim, target_is_directory=True)
    results = delete_tools([swapped_tool], [root], used_paths=set())
    assert len(results) == 1
    assert results[0].status is DeletionStatus.REJECTED
    assert results[0].reject_reason is RejectReason.IDENTITY_MISMATCH
    assert trash_calls == []
    assert planted.is_dir()


def test_current_toolsdirs_treats_non_directory_as_absent(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    toolsdir.parent.mkdir(parents=True)
    # A missing tools directory is absent.
    assert deletion_module._current_toolsdirs([root]) == []
    # A planted symlink counts as absent instead of relocating the
    # approval boundary onto the linked directory.
    victim = tmp_path / "victim"
    victim.mkdir()
    toolsdir.symlink_to(victim, target_is_directory=True)
    assert deletion_module._current_toolsdirs([root]) == []
    # A plain file at the path counts as absent too.
    toolsdir.unlink()
    toolsdir.write_text("not a directory", encoding="utf-8")
    assert deletion_module._current_toolsdirs([root]) == []
    # A real tools directory still resolves for containment.
    toolsdir.unlink()
    toolsdir.mkdir()
    assert deletion_module._current_toolsdirs([root]) == [toolsdir.resolve(strict=False)]


def test_delete_rejects_common_parent_even_if_flagged_writable(
    tmp_path: Path, trash_calls: list[Path]
) -> None:
    library = _make_library(tmp_path)
    root = _make_root(tmp_path)
    target = library.path / "steamapps" / "common" / "Proton 9.0"
    target.mkdir(parents=True)
    tool = Tool(name="Proton 9.0", path=target, root=library.root, read_only=False)
    results = delete_tools([tool], [root], used_paths=set(), libraries=[library])
    assert len(results) == 1
    assert results[0].status is DeletionStatus.REJECTED
    assert results[0].reject_reason is RejectReason.READ_ONLY
    assert trash_calls == []
    assert target.is_dir()


def test_delete_rejects_system_parent_even_if_flagged_writable(
    _isolated_system_dir: Path, trash_calls: list[Path]
) -> None:
    target = _isolated_system_dir / "DistroBuild"
    target.mkdir(parents=True)
    (target / "compatibilitytool.vdf").write_text(VALID_TOOL_VDF, encoding="utf-8")
    tool = Tool(name="DistroBuild", path=target, root=_isolated_system_dir, read_only=False)
    results = delete_tools([tool], [], used_paths=set())
    assert len(results) == 1
    assert results[0].status is DeletionStatus.REJECTED
    assert results[0].reject_reason is RejectReason.READ_ONLY
    assert trash_calls == []
    assert target.is_dir()


def test_delete_unused_writable_tool_to_trash(tmp_path: Path, trash_calls: list[Path]) -> None:
    root = _make_root(tmp_path)
    target = _write_tool(root.path / "compatibilitytools.d", "OldBuild", vdf_text=None)
    tools = _enumerate([root], [])
    # The healthy flow carries the entry identity recorded at enumeration
    # time, and the recorded identity matches the directory on disk.
    entry_identity = os.stat(target, follow_symlinks=False)
    assert tools[0].dev_ino == (entry_identity.st_dev, entry_identity.st_ino)
    results = delete_tools(tools, [root], used_paths=set())
    assert trash_calls == [target.resolve(strict=False)]
    assert len(results) == 1
    assert results[0].status is DeletionStatus.DELETED
    assert results[0].mode is DeleteMode.TRASH


def test_delete_permanent_uses_remove_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _make_root(tmp_path)
    _write_tool(root.path / "compatibilitytools.d", "OldBuild", vdf_text=None)
    tools = _enumerate([root], [])
    calls: list[Path] = []
    monkeypatch.setattr(deletion_module, "_remove_tree", lambda path: calls.append(path))
    results = delete_tools(tools, [root], mode=DeleteMode.PERMANENT, used_paths=set())
    assert len(calls) == 1
    assert results[0].status is DeletionStatus.DELETED
    assert results[0].mode is DeleteMode.PERMANENT


def test_delete_rejects_outside_parent(tmp_path: Path, trash_calls: list[Path]) -> None:
    root = _make_root(tmp_path)
    (root.path / "compatibilitytools.d").mkdir(parents=True)
    victim = tmp_path / "victim"
    (victim / "Evil").mkdir(parents=True)
    tool = Tool(
        name="Evil",
        path=victim / "Evil",
        root=root.path,
        read_only=False,
    )
    results = delete_tools([tool], [root], used_paths=set())
    assert results[0].status is not DeletionStatus.DELETED
    assert results[0].reject_reason is RejectReason.NOT_IN_TOOLSDIR
    assert trash_calls == []
    assert (victim / "Evil").is_dir()


def test_delete_rejects_nested_path(tmp_path: Path, trash_calls: list[Path]) -> None:
    root = _make_root(tmp_path)
    nested = root.path / "compatibilitytools.d" / "Outer" / "Inner"
    nested.mkdir(parents=True)
    tool = Tool(
        name="Inner",
        path=nested,
        root=root.path,
        read_only=False,
    )
    results = delete_tools([tool], [root], used_paths=set())
    assert results[0].reject_reason is RejectReason.NOT_DIRECT_CHILD
    assert trash_calls == []


def test_delete_rejects_sibling_dir_with_shared_prefix(
    tmp_path: Path, trash_calls: list[Path]
) -> None:
    root = _make_root(tmp_path)
    (root.path / "compatibilitytools.d").mkdir(parents=True)
    evil_dir = root.path / "compatibilitytools.d-evil" / "Evil"
    evil_dir.mkdir(parents=True)
    tool = Tool(
        name="Evil",
        path=evil_dir,
        root=root.path,
        read_only=False,
    )
    results = delete_tools([tool], [root], used_paths=set())
    assert results[0].reject_reason is RejectReason.NOT_IN_TOOLSDIR
    assert trash_calls == []
    assert evil_dir.is_dir()


def test_delete_rejects_symlink(tmp_path: Path, trash_calls: list[Path]) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    real = _write_tool(toolsdir, "Real", vdf_text=None)
    link = toolsdir / "Link"
    link.symlink_to(real, target_is_directory=True)
    tool = Tool(name="Link", path=link, root=root.path, read_only=False)
    results = delete_tools([tool], [root], used_paths=set())
    assert results[0].reject_reason is RejectReason.SYMLINK
    assert trash_calls == []
    assert link.is_symlink()


def test_delete_rejects_name_mismatch(tmp_path: Path, trash_calls: list[Path]) -> None:
    root = _make_root(tmp_path)
    odd = root.path / "compatibilitytools.d" / "Real" / ".."
    (root.path / "compatibilitytools.d" / "Real").mkdir(parents=True)
    tool = Tool(name="Real", path=odd, root=root.path, read_only=False)
    results = delete_tools([tool], [root], used_paths=set())
    assert results[0].reject_reason is RejectReason.NAME_MISMATCH
    assert trash_calls == []


def test_delete_rejects_read_only(tmp_path: Path, trash_calls: list[Path]) -> None:
    root = _make_root(tmp_path)
    target = _write_tool(root.path / "compatibilitytools.d", "Locked", vdf_text=None)
    tool = Tool(name="Locked", path=target, root=root.path, read_only=True)
    results = delete_tools([tool], [root], used_paths=set())
    assert results[0].reject_reason is RejectReason.READ_ONLY
    assert trash_calls == []
    assert target.is_dir()


def test_delete_rejects_stale_root(tmp_path: Path, trash_calls: list[Path]) -> None:
    current = _make_root(tmp_path / "current")
    gone = _make_root(tmp_path / "gone")
    target = _write_tool(gone.path / "compatibilitytools.d", "Left", vdf_text=None)
    tool = Tool(name="Left", path=target, root=gone.path, read_only=False)
    results = delete_tools([tool], [current], used_paths=set())
    assert results[0].reject_reason is RejectReason.STALE_ROOT
    assert trash_calls == []
    assert target.is_dir()


def test_delete_rejects_missing_dir(tmp_path: Path, trash_calls: list[Path]) -> None:
    root = _make_root(tmp_path)
    (root.path / "compatibilitytools.d").mkdir(parents=True)
    tool = Tool(
        name="Gone",
        path=root.path / "compatibilitytools.d" / "Gone",
        root=root.path,
        read_only=False,
    )
    results = delete_tools([tool], [root], used_paths=set())
    assert results[0].reject_reason is RejectReason.MISSING
    assert trash_calls == []


def test_delete_rejects_externally_deleted_dir_with_dev_ino_as_missing(
    tmp_path: Path, trash_calls: list[Path]
) -> None:
    root = _make_root(tmp_path)
    target = _write_tool(root.path / "compatibilitytools.d", "Vanished", vdf_text=None)
    tools = _enumerate([root], [])
    assert len(tools) == 1
    assert tools[0].dev_ino is not None

    # Externally delete the tool directory after enumeration.
    shutil.rmtree(target)

    results = delete_tools(tools, [root], used_paths=set())
    assert len(results) == 1
    assert results[0].status is DeletionStatus.REJECTED
    assert results[0].reject_reason is RejectReason.MISSING
    assert trash_calls == []


def test_delete_rejects_in_use(tmp_path: Path, trash_calls: list[Path]) -> None:
    root = _make_root(tmp_path)
    target = _write_tool(root.path / "compatibilitytools.d", "ActiveBuild", vdf_text=None)
    tools = _enumerate([root], [])
    results = delete_tools(tools, [root], used_paths={str(target.resolve(strict=False))})
    assert len(results) == 1
    assert results[0].status is DeletionStatus.REJECTED
    assert results[0].reject_reason is RejectReason.IN_USE
    assert trash_calls == []
    assert target.is_dir()


def test_is_reclaimable_all_combinations() -> None:
    path_writable = Path("/tools/writable")
    path_readonly = Path("/tools/readonly")
    writable = Tool(name="Writable", path=path_writable, root=Path("/root"), read_only=False)
    readonly = Tool(name="ReadOnly", path=path_readonly, root=Path("/root"), read_only=True)

    used_set = {str(path_writable), str(path_readonly)}
    empty_set: set[str] = set()

    # Unused and writable, so it is reclaimable.
    assert is_reclaimable(writable, empty_set, usage_known=True) is True
    # Used tools stay locked even when writable.
    assert is_reclaimable(writable, used_set, usage_known=True) is False
    # Read-only tools are never reclaimable while unused.
    assert is_reclaimable(readonly, empty_set, usage_known=True) is False
    # Used wins over read-only, so a used read-only tool is not reclaimable.
    assert is_reclaimable(readonly, used_set, usage_known=True) is False


def test_tool_category_labels() -> None:
    assert ToolCategory.USED.label() == "Used"
    assert ToolCategory.RECLAIMABLE.label() == "Reclaimable"
    assert ToolCategory.READ_ONLY.label() == "Read-only"


def test_tool_category_all_combinations() -> None:
    path_writable = Path("/tools/writable")
    path_readonly = Path("/tools/readonly")
    writable = Tool(name="Writable", path=path_writable, root=Path("/root"), read_only=False)
    readonly = Tool(name="ReadOnly", path=path_readonly, root=Path("/root"), read_only=True)

    used_set = {str(path_writable), str(path_readonly)}
    empty_set: set[str] = set()

    # Unused and writable, so reclaimable.
    assert tool_category(writable, empty_set, usage_known=True) is ToolCategory.RECLAIMABLE
    # Used wins over writable.
    assert tool_category(writable, used_set, usage_known=True) is ToolCategory.USED
    # Unused read-only tools stay read-only.
    assert tool_category(readonly, empty_set, usage_known=True) is ToolCategory.READ_ONLY
    # Used wins over read-only.
    assert tool_category(readonly, used_set, usage_known=True) is ToolCategory.USED


def test_tool_category_unknown_when_usage_unavailable() -> None:
    writable = Tool(name="W", path=Path("/tools/w"), root=Path("/root"), read_only=False)
    readonly = Tool(name="R", path=Path("/tools/r"), root=Path("/root"), read_only=True)

    # A failed mapping load leaves writable unmatched tools Unknown and
    # never reclaimable; read-only and used states stay resolvable.
    assert tool_category(writable, set(), usage_known=False) is ToolCategory.UNKNOWN
    assert tool_category(readonly, set(), usage_known=False) is ToolCategory.READ_ONLY
    assert tool_category(writable, {str(writable.path)}, usage_known=False) is ToolCategory.USED
    assert is_reclaimable(writable, set(), usage_known=False) is False
    assert ToolCategory.UNKNOWN.label() == "Unknown"


@pytest.mark.skipif(os.geteuid() == 0, reason="permission checks do not apply to root")
def test_unreadable_tool_vdf_display_name_mapping_stays_unknown(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "GE-Proton9-1")
    vdf_path = toolsdir / "GE-Proton9-1" / "compatibilitytool.vdf"
    vdf_path.chmod(0)
    try:
        tools = _enumerate([root, root], [])
    finally:
        vdf_path.chmod(0o644)
    # The unreadable read falls back to the directory name, and the
    # unverified flag survives the dedupe of the repeated root.
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "GE-Proton9-1"
    assert tool.name_unverified is True
    used = used_by(tools, {700: "GE-Proton 9-1"})
    assert used == {}
    assert tool_category(tool, used, usage_known=True) is ToolCategory.UNKNOWN
    assert is_reclaimable(tool, used, usage_known=True) is False


def test_overcap_tool_vdf_display_name_mapping_stays_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "GE-Proton9-1", vdf_text=None)
    vdf_path = toolsdir / "GE-Proton9-1" / "compatibilitytool.vdf"
    vdf_path.write_bytes(VALID_TOOL_VDF.encode("utf-8"))
    monkeypatch.setattr(tools_module, "MAX_VDF_BYTES", 4)
    tools = _enumerate([root], [])
    tool = tools[0]
    assert tool.name == "GE-Proton9-1"
    assert tool.name_unverified is True
    used = used_by(tools, {700: "GE-Proton 9-1"})
    assert tool_category(tool, used, usage_known=True) is ToolCategory.UNKNOWN
    assert is_reclaimable(tool, used, usage_known=True) is False


def test_unverified_tool_directory_name_mapping_stays_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A mapping value holding the directory name still matches the
    # fallback name, so the tool stays Used even though its own vdf
    # could not be read.
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "GE-Proton9-1", vdf_text=None)
    vdf_path = toolsdir / "GE-Proton9-1" / "compatibilitytool.vdf"
    vdf_path.write_bytes(VALID_TOOL_VDF.encode("utf-8"))
    monkeypatch.setattr(tools_module, "MAX_VDF_BYTES", 4)
    tools = _enumerate([root], [])
    tool = tools[0]
    used = used_by(tools, {700: "GE-Proton9-1"})
    assert used == {str(tool.path): [700]}
    assert tool_category(tool, used, usage_known=True) is ToolCategory.USED


def test_healthy_tool_display_name_mapping_unaffected(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "GE-Proton9-1")
    tools = _enumerate([root], [])
    tool = tools[0]
    assert tool.name_unverified is False
    selected = used_by(tools, {700: "GE-Proton 9-1"})
    assert tool_category(tool, selected, usage_known=True) is ToolCategory.USED
    unmatched = used_by(tools, {700: "Other Tool"})
    assert tool_category(tool, unmatched, usage_known=True) is ToolCategory.RECLAIMABLE


def test_delete_rejects_conflicting_aliases(tmp_path: Path, trash_calls: list[Path]) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    target = _write_tool(toolsdir, "ToolGroup", vdf_text=None)

    tool_writable = Tool(name="ToolGroup", path=target, root=root.path, read_only=False)
    tool_readonly = Tool(name="ToolGroup", path=target, root=root.path, read_only=True)

    results = delete_tools([tool_writable, tool_readonly], [root], used_paths=set())
    assert len(results) == 1
    assert results[0].status is DeletionStatus.REJECTED
    assert results[0].reject_reason is RejectReason.READ_ONLY
    assert trash_calls == []
    assert target.is_dir()


def test_delete_rejects_conflicting_alias_name_mismatch(
    tmp_path: Path, trash_calls: list[Path]
) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    target = _write_tool(toolsdir, "ToolName", vdf_text=None)

    tool_valid = Tool(name="ToolName", path=target, root=root.path, read_only=False)
    sub = target / "sub"
    sub.mkdir()
    tool_mismatch = Tool(name="ToolName", path=sub / "..", root=root.path, read_only=False)

    results = delete_tools([tool_valid, tool_mismatch], [root], used_paths=set())
    assert len(results) == 1
    assert results[0].status is DeletionStatus.REJECTED
    assert results[0].reject_reason is RejectReason.NAME_MISMATCH
    assert trash_calls == []
    assert target.is_dir()
