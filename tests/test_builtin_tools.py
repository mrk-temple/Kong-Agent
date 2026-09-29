import asyncio

from kong.tools.base import ToolResult
from kong.tools.builtin.list_dir import ListDirTool
from kong.tools.builtin.read_file import ReadFileTool


def test_read_file_reads_temporary_file(tmp_path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("你好\nhello", encoding="utf-8")

    result = asyncio.run(ReadFileTool().run(path=str(path)))

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert result.output == "你好\nhello"
    assert result.error is None


def test_read_file_missing_path_returns_failure(tmp_path) -> None:
    result = asyncio.run(ReadFileTool().run(path=str(tmp_path / "missing.txt")))

    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.output is None
    assert result.error


def test_read_file_pages_unicode_without_skipping_content(tmp_path):
    from kong.workspace import Workspace
    text = "中文\n" * 2000
    (tmp_path / "long.txt").write_text(text, encoding="utf-8")
    tool = ReadFileTool(Workspace(tmp_path))
    parts, offset = [], 0
    while offset < len(text):
        result = asyncio.run(tool.run(path="long.txt", offset=offset, max_chars=333))
        assert result.success
        parts.append(result.output["content"])
        offset = result.output["next_offset"]
    assert "".join(parts) == text and not result.output["truncated"]


def test_list_dir_lists_temporary_contents(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    (tmp_path / "folder").mkdir()

    result = asyncio.run(ListDirTool().run(path=str(tmp_path)))

    assert isinstance(result, ToolResult)
    assert result.success is True
    assert {item["name"]: item["type"] for item in result.output} == {
        "note.txt": "file",
        "folder": "dir",
    }
    assert result.error is None


def test_list_dir_missing_path_returns_failure(tmp_path) -> None:
    result = asyncio.run(ListDirTool().run(path=str(tmp_path / "missing")))

    assert isinstance(result, ToolResult)
    assert result.success is False
    assert result.output is None
    assert result.error
