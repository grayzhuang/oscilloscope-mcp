"""``oscilloscope_mcp`` — standalone MCP server for bench oscilloscope debug.

Per-instrument plug-in pattern (issue #84). A new oscilloscope (or
future signal generator / DMM) is added by dropping in two files:

1. ``oscilloscope_mcp/instruments/<vendor>_<series>.py`` — driver implementing
   :class:`~oscilloscope_mcp.instruments._base.Scope` with the vendor's SCPI
   dialect.
2. ``oscilloscope_mcp/instruments/profiles/<model>.yaml`` — capability spec
   (analog BW, sample rate vs channel count, memory depth, etc.) used by
   :mod:`oscilloscope_mcp.helpers.caveat_calc` to emit observation-limit
   warnings.

The MCP tool layer in :mod:`oscilloscope_mcp.server` imports
:func:`oscilloscope_mcp.instruments.open_scope` for dispatch and exposes
``scope_query`` and ``scope_screenshot`` over stdio to any MCP client
(Claude Code, Cursor, custom agent).

This package is fully independent of ``circt-agent``: it has its own
``pyproject.toml`` and shares no code. The wider repo manages it for
governance / CI / planning, but the runtime artifact ships on its own.
"""
