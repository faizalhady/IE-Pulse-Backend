"""
modules/universe/mcp_server.py
──────────────────────────────
The universe as an MCP server — one access layer, any client (Claude Code,
Claude Desktop, LibreChat, the eval harness). stdio transport.

    python -m modules.universe.mcp_server

Claude Code registration (from the backend root):
    claude mcp add universe -- python -m modules.universe.mcp_server
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from modules.universe import tools as T

mcp = FastMCP("universe_mcp")
_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


@mcp.tool(name="universe_describe", annotations=_RO)
def universe_describe(view: str | None = None) -> str:
    """The Jabil Universe's views and their columns, each with its meaning in Jabil's
    words (workcell = customer, units = boards counted once, fiscal year starts
    September …). Call this FIRST, before writing SQL. Pass a view name for one view,
    nothing for all of them."""
    return json.dumps(T.describe(view), default=str)


@mcp.tool(name="universe_query", annotations=_RO)
def universe_query(sql: str) -> str:
    """Run ONE read-only SELECT over the universe's views (v_workcell, v_units_out_daily,
    v_ole_weekly, v_ole_daily, v_process, v_cycle_time, v_route, v_demand, v_fpy_daily,
    v_output_daily). DuckDB SQL. Results are capped at 200 rows — aggregate, filter and
    ORDER BY with a LIMIT. Only the views are reachable; anything else is refused with
    the reason."""
    return json.dumps(T.query(sql), default=str)


@mcp.tool(name="universe_define", annotations=_RO)
def universe_define(term: str) -> str:
    """What a Jabil word means and the traps around it — from the universe's rules,
    vocabulary and gotchas (e.g. 'workcell', 'OLE', 'terminal step', 'fiscal year',
    'AOP', 'bay'). Use it before answering a knowledge question or when a column's
    comment is not enough."""
    return json.dumps(T.define(term), default=str)


if __name__ == "__main__":
    mcp.run()
