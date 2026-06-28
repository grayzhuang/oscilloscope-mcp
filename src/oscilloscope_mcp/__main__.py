"""``python -m oscilloscope_mcp`` entry — starts the MCP server on stdio."""

from __future__ import annotations

from oscilloscope_mcp.server import build_server


def main() -> None:
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()
