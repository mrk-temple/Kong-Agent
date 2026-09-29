from kong.tools.builtin.list_dir import ListDirTool
from kong.tools.builtin.read_file import ReadFileTool
from kong.tools.builtin.search_files import SearchFilesTool
from kong.tools.builtin.write_file import WriteFileTool
from kong.tools.builtin.patch_file import PatchFileTool
from kong.tools.builtin.process_exec import ProcessExecTool
from kong.tools.registry import ToolRegistry
from kong.workspace import Workspace


def default_registry(workspace: Workspace, *, allow_process: bool = False, skill_roots=(), web_config=None,
                     allow_terminal=False, allow_browser=False, mcp_servers=None) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (ListDirTool(workspace), ReadFileTool(workspace), SearchFilesTool(workspace),
                 WriteFileTool(workspace), PatchFileTool(workspace)):
        registry.register(tool)
    if allow_process:
        registry.register(ProcessExecTool(workspace, enabled=True))
    if allow_terminal:
        from kong.integrations.terminal import TerminalTool
        registry.register(TerminalTool(workspace))
    if allow_browser:
        from kong.integrations.browser import BrowserTool
        registry.register(BrowserTool(workspace))
    if mcp_servers is not None:
        from kong.integrations.mcp import MCPTool
        registry.register(MCPTool(workspace, mcp_servers))
    if web_config is not None and web_config.mode != "disabled":
        from kong.web.service import WebService
        from kong.web.tools import WebFetch, WebRead, WebSearch
        service = WebService(workspace.root, web_config)
        for cls in (WebFetch, WebRead):
            registry.register(cls(service))
        if web_config.status()["search_available"]:
            registry.register(WebSearch(service))
    from kong.skills.catalog import SkillCatalog
    from kong.skills.tools import SkillList, SkillLoad, SkillRead
    catalog = SkillCatalog(workspace.root, registry, skill_roots)
    for cls in (SkillList, SkillLoad, SkillRead):
        registry.register(cls(catalog))
    return registry
