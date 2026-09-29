from pydantic import BaseModel, ConfigDict, Field

from kong.tools.base import Tool, ToolResult


class ListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=30)


class LoadArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)


class ReadArgs(LoadArgs):
    resource: str = Field(min_length=1, max_length=240)
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=8000, ge=1, le=16000)


class SkillTool(Tool):
    """Host-owned context operation: not an environment action or completion evidence."""
    read_only = True
    family = "skills"
    environment = "skill_package"
    effects = ()

    def __init__(self, catalog):
        self.catalog = catalog


class SkillList(SkillTool):
    name = "skill_list"
    description = "List skill metadata and missing dependencies, paginated. Does not execute scripts or install anything."
    args_model = ListArgs

    async def run(self, **kwargs):
        return ToolResult(success=True, output=self.catalog.listing(**kwargs))


class SkillLoad(SkillTool):
    name = "skill_load"
    description = "Load full SKILL.md and dependency status. Use before following a skill; blocked means requirements missing, not permission to install or execute. Use a separate skill-only action batch, including before a PLAN proposal."
    args_model = LoadArgs

    async def run(self, **kwargs):
        return ToolResult(success=True, output=self.catalog.load(**kwargs))


class SkillRead(SkillTool):
    name = "skill_read"
    description = "Read a UTF-8 reference/script inside a skill package by relative resource path. Read only: scripts are never run. Does not access arbitrary workspace files."
    args_model = ReadArgs

    async def run(self, **kwargs):
        return ToolResult(success=True, output=self.catalog.read(**kwargs))
