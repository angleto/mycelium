"""MCP entry point: `python -m mycelium_mcp.main` (stdio transport)."""

from __future__ import annotations

from mycelium_mcp.server import mcp


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
