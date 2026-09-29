import pytest

from kong.tools.builtin.list_dir import ListDirTool
from kong.tools.builtin.read_file import ReadFileTool
from kong.tools.validator import ToolValidationError, validate_arguments


def test_valid_arguments() -> None:
    assert validate_arguments(ReadFileTool(), {"path": "note.txt"}) == {
        "path": "note.txt", "offset": 0, "max_chars": None
    }


def test_missing_required_argument() -> None:
    with pytest.raises(ToolValidationError, match="path"):
        validate_arguments(ReadFileTool(), {})


def test_wrong_argument_type() -> None:
    with pytest.raises(ToolValidationError, match="path"):
        validate_arguments(ReadFileTool(), {"path": 123})


def test_extra_field_rejected() -> None:
    with pytest.raises(ToolValidationError, match="extra"):
        validate_arguments(ReadFileTool(), {"path": "note.txt", "extra": True})


def test_default_argument_applied() -> None:
    assert validate_arguments(ListDirTool(), {}) == {"path": "."}
