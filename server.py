"""Compatibility shim: the host lives in the vectorworks_mcp package.

Run with: python -X utf8 -m vectorworks_mcp   (or: python server.py)
The frozen 0.7 server is legacy/server_v07.py (VW_MCP_TOOL_PROFILE=legacy).
"""

from vectorworks_mcp.server import main

if __name__ == "__main__":
    raise SystemExit(main())
