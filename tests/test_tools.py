"""Unit tests for core.tools and the tool deletion validator."""

from __future__ import annotations

from pathlib import Path

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
    enumerate_tools,
    parse_compat_tool_text,
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


def test_parse_empty_raises() -> None:
    with pytest.raises(CompatToolParseError):
        parse_compat_tool_text("")


def test_enumerate_custom_tool(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "GE-Proton9-1")
    tools = enumerate_tools([root], [])
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "GE-Proton 9-1"
    assert tool.path == (toolsdir / "GE-Proton9-1").resolve(strict=False)
    assert tool.root == root.path
    assert tool.read_only is False


def test_enumerate_missing_dirs_is_empty(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    library = _make_library(tmp_path)
    assert enumerate_tools([root], [library]) == []


def test_enumerate_missing_vdf_falls_back_to_dirname(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "MyBuild", vdf_text=None)
    (toolsdir / "MyBuild" / "compatibilitytool.vdf").write_bytes(b"\xff\xfe broken")
    _write_tool(toolsdir, "PlainDir", vdf_text=None)
    tools = enumerate_tools([root], [])
    assert {tool.name for tool in tools} == {"MyBuild", "PlainDir"}


def test_enumerate_malformed_vdf_falls_back(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    _write_tool(toolsdir, "Broken", vdf_text='"compatibilitytools"\n{\n')
    (toolsdir / "Broken" / "extra.txt").write_text("x", encoding="utf-8")
    tools = enumerate_tools([root], [])
    assert len(tools) == 1
    assert tools[0].name == "Broken"


def test_enumerate_skips_symlinks_and_files(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    real = _write_tool(toolsdir, "Real")
    (toolsdir / "Link").symlink_to(real, target_is_directory=True)
    (toolsdir / "notes.txt").write_text("hi", encoding="utf-8")
    tools = enumerate_tools([root], [])
    assert [tool.name for tool in tools] == ["GE-Proton 9-1"]


def test_enumerate_managed_tools_are_read_only(tmp_path: Path) -> None:
    library = _make_library(tmp_path)
    common = library.path / "steamapps" / "common"
    (common / "Proton 9.0").mkdir(parents=True)
    (common / "Proton - Experimental").mkdir(parents=True)
    (common / "SteamLinuxRuntime").mkdir(parents=True)
    (common / "SteamLinuxRuntime_sniper").mkdir(parents=True)
    (common / "ProtonNotes.txt").write_text("hi", encoding="utf-8")
    tools = enumerate_tools([], [library])
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
    tools = enumerate_tools([], [])
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == "GE-Proton 9-1"
    assert tool.read_only is True
    assert tool.root == _isolated_system_dir.resolve(strict=False)


def test_missing_system_dir_is_empty(_isolated_system_dir: Path) -> None:
    assert not _isolated_system_dir.exists()
    assert enumerate_tools([], []) == []


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
    tools = enumerate_tools([root], [])
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
    tools = enumerate_tools([first, second], [])
    assert len(tools) == 2
    assert tools[0].path != tools[1].path


def test_same_library_twice_dedupes(tmp_path: Path) -> None:
    library = _make_library(tmp_path)
    common = library.path / "steamapps" / "common"
    (common / "Proton 9.0").mkdir(parents=True)
    tools = enumerate_tools([], [library, library])
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
    tools = enumerate_tools([first, second], [])
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
    tools = enumerate_tools([root], [])
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
    tools = enumerate_tools([root], [])
    assert set(used_by(tools, mapping)) == {str(tools[0].path)}


def test_used_matches_display_name(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    _write_tool(root.path / "compatibilitytools.d", "InternalDirName")
    _write_mapping(root.path, _mapping_entry(480, "GE-Proton 9-1"))
    mapping, _ = load_tool_mapping([root])
    tools = enumerate_tools([root], [])
    assert set(used_by(tools, mapping)) == {str(tools[0].path)}


def test_same_display_name_twins_all_stay_locked(tmp_path: Path) -> None:
    root = _make_root(tmp_path)
    toolsdir = root.path / "compatibilitytools.d"
    vdf_text = VALID_TOOL_VDF.replace("GE-Proton 9-1", "Shared Name")
    _write_tool(toolsdir, "DirA", vdf_text=vdf_text)
    _write_tool(toolsdir, "DirB", vdf_text=vdf_text)
    _write_mapping(root.path, _mapping_entry(480, "Shared Name"))
    mapping, _ = load_tool_mapping([root])
    tools = enumerate_tools([root], [])
    assert set(used_by(tools, mapping)) == {str(tool.path) for tool in tools}


@pytest.fixture()
def trash_calls(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    def fake_send2trash(path: Path) -> None:
        calls.append(path)

    monkeypatch.setattr(deletion_module, "send2trash", fake_send2trash)
    return calls


def test_delete_unused_writable_tool_to_trash(tmp_path: Path, trash_calls: list[Path]) -> None:
    root = _make_root(tmp_path)
    target = _write_tool(root.path / "compatibilitytools.d", "OldBuild", vdf_text=None)
    tools = enumerate_tools([root], [])
    results = delete_tools(tools, [root])
    assert trash_calls == [target.resolve(strict=False)]
    assert len(results) == 1
    assert results[0].status is DeletionStatus.DELETED
    assert results[0].mode is DeleteMode.TRASH


def test_delete_permanent_uses_remove_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _make_root(tmp_path)
    _write_tool(root.path / "compatibilitytools.d", "OldBuild", vdf_text=None)
    tools = enumerate_tools([root], [])
    calls: list[Path] = []
    monkeypatch.setattr(deletion_module, "_remove_tree", lambda path: calls.append(path))
    results = delete_tools(tools, [root], mode=DeleteMode.PERMANENT)
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
    results = delete_tools([tool], [root])
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
    results = delete_tools([tool], [root])
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
    results = delete_tools([tool], [root])
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
    results = delete_tools([tool], [root])
    assert results[0].reject_reason is RejectReason.SYMLINK
    assert trash_calls == []
    assert link.is_symlink()


def test_delete_rejects_name_mismatch(tmp_path: Path, trash_calls: list[Path]) -> None:
    root = _make_root(tmp_path)
    odd = root.path / "compatibilitytools.d" / "Real" / ".."
    (root.path / "compatibilitytools.d" / "Real").mkdir(parents=True)
    tool = Tool(name="Real", path=odd, root=root.path, read_only=False)
    results = delete_tools([tool], [root])
    assert results[0].reject_reason is RejectReason.NAME_MISMATCH
    assert trash_calls == []


def test_delete_rejects_read_only(tmp_path: Path, trash_calls: list[Path]) -> None:
    root = _make_root(tmp_path)
    target = _write_tool(root.path / "compatibilitytools.d", "Locked", vdf_text=None)
    tool = Tool(name="Locked", path=target, root=root.path, read_only=True)
    results = delete_tools([tool], [root])
    assert results[0].reject_reason is RejectReason.READ_ONLY
    assert trash_calls == []
    assert target.is_dir()


def test_delete_rejects_stale_root(tmp_path: Path, trash_calls: list[Path]) -> None:
    current = _make_root(tmp_path / "current")
    gone = _make_root(tmp_path / "gone")
    target = _write_tool(gone.path / "compatibilitytools.d", "Left", vdf_text=None)
    tool = Tool(name="Left", path=target, root=gone.path, read_only=False)
    results = delete_tools([tool], [current])
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
    results = delete_tools([tool], [root])
    assert results[0].reject_reason is RejectReason.MISSING
    assert trash_calls == []
