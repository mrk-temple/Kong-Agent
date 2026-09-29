import pytest

from kong.tools.builtin.list_dir import ListDirTool
from kong.tools.builtin.read_file import ReadFileTool
from kong.tools.registry import ToolRegistry


def test_register_and_get_tool() -> None:
    registry = ToolRegistry()
    tool = ReadFileTool()

    registry.register(tool)

    assert registry.get("read_file") is tool


def test_get_unknown_tool() -> None:
    registry = ToolRegistry()

    with pytest.raises(KeyError, match="Unknown tool: missing"):
        registry.get("missing")


def test_duplicate_registration() -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool())

    with pytest.raises(ValueError, match="Tool already registered: read_file"):
        registry.register(ReadFileTool())


def test_names_and_definitions() -> None:
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(ListDirTool())

    assert registry.names() == ["read_file", "list_dir"]
    definitions = registry.definitions()
    assert [definition.name for definition in definitions] == registry.names()
    assert definitions[0].description == ReadFileTool.description
    assert "path" in definitions[0].input_schema["properties"]
    assert "path" in definitions[0].input_schema["required"]
    assert definitions[1].input_schema["properties"]["path"]["default"] == "."
