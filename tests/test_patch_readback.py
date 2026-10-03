"""I-01/04: literal patches must consume the text view returned by read_file."""
import asyncio

import pytest

from kong.tools.builtin.patch_file import PatchFileTool
from kong.tools.builtin.read_file import ReadFileTool
from kong.workspace import Workspace


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_patch_uses_read_view_and_preserves_unmodified_bytes(tmp_path, newline):
    before = "# 保留前缀\ndef clamp(value, low, high):\n    return min(low, max(value, high))\n# 保留尾部\n"
    (tmp_path / "limits.py").write_bytes(before.replace("\n", newline).encode("utf-8"))
    workspace = Workspace(tmp_path)
    read = asyncio.run(ReadFileTool(workspace).run(path="limits.py"))
    old = read.output.split("# 保留前缀\n", 1)[1].split("# 保留尾部", 1)[0]
    replacement = "def clamp(value, low, high):\n    return max(low, min(value, high))\n"
    result = asyncio.run(PatchFileTool(workspace).run(path="limits.py", old_text=old, new_text=replacement))
    assert result.success, result.error
    expected = before.replace(old, replacement).replace("\n", newline).encode("utf-8")
    assert (tmp_path / "limits.py").read_bytes() == expected


@pytest.mark.parametrize("text", ["same\r\nline\r\nsame\r\nline\r\n", "same\r\nline\nsame\nline\r\n"])
def test_normalized_duplicate_is_rejected_without_writing(tmp_path, text):
    original = text.encode()
    (tmp_path / "note.txt").write_bytes(original)
    result = asyncio.run(PatchFileTool(Workspace(tmp_path)).run(
        path="note.txt", old_text="same\nline\n", new_text="replacement"))
    assert not result.success
    assert (tmp_path / "note.txt").read_bytes() == original


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_multiline_insertion_at_single_line_match_keeps_file_convention(tmp_path, newline):
    original = f"prefix{newline}old{newline}tail{newline}"
    (tmp_path / "note.txt").write_bytes(original.encode())
    result = asyncio.run(PatchFileTool(Workspace(tmp_path)).run(path="note.txt", old_text="old", new_text="new\nline"))
    assert result.success
    assert (tmp_path / "note.txt").read_bytes() == original.replace("old", f"new{newline}line").encode()


def test_mixed_newline_patch_preserves_bytes_outside_unique_match(tmp_path):
    original = b"prefix\nold\r\nvalue\r\ntail\n"
    (tmp_path / "note.txt").write_bytes(original)
    result = asyncio.run(PatchFileTool(Workspace(tmp_path)).run(
        path="note.txt", old_text="old\nvalue\n", new_text="new\nvalue\n"))
    assert result.success
    assert (tmp_path / "note.txt").read_bytes() == b"prefix\nnew\r\nvalue\r\ntail\n"


@pytest.mark.parametrize("old,new", [("missing", "new"), ("old", "old")])
def test_rejected_patch_has_version_diagnostic_and_no_write(tmp_path, old, new):
    original = b"old\r\n"
    (tmp_path / "note.txt").write_bytes(original)
    result = asyncio.run(PatchFileTool(Workspace(tmp_path)).run(path="note.txt", old_text=old, new_text=new))
    assert not result.success
    assert result.output["code"] in {"patch.match_count", "patch.no_change"}
    assert len(result.output["source_sha256"]) == 64
    assert (tmp_path / "note.txt").read_bytes() == original
