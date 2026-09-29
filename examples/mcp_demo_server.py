"""Local MCP acceptance server; no network, files, or credential access.

Configure with command='python', args=['absolute/path/to/this/file.py'].
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Kong local demo")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Return the sum of two integers; useful for checking the MCP connection."""
    return a + b


if __name__ == "__main__":
    mcp.run()
