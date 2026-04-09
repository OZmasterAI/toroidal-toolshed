#!/usr/bin/env python3
"""Stdio-to-HTTP bridge for Toolshed.

Claude Code spawns this as a stdio MCP server. It proxies all requests
to the running Toolshed HTTP server at 127.0.0.1:8750, with automatic
reconnection if the HTTP connection drops.

This works around Claude Code's lack of retry for HTTP MCP connections
(anthropics/claude-code#31198).
"""

import asyncio
import logging
import sys

from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("toolshed-bridge")

TOOLSHED_URL = "http://127.0.0.1:8750/mcp"
RECONNECT_DELAY = 2  # seconds

bridge = FastMCP("toolshed")

# Upstream connection state
_upstream: ClientSession | None = None
_upstream_ctx = None
_upstream_session_ctx = None
_connect_lock = asyncio.Lock()

# Exceptions that indicate upstream connection issues
_CONN_ERRORS = (ConnectionError, OSError, RuntimeError)


async def _connect() -> ClientSession:
    """Connect (or reconnect) to upstream toolshed HTTP server."""
    global _upstream, _upstream_ctx, _upstream_session_ctx

    async with _connect_lock:
        if _upstream is not None:
            return _upstream

        logger.info("Connecting to toolshed at %s", TOOLSHED_URL)
        _upstream_ctx = streamablehttp_client(url=TOOLSHED_URL)
        read, write, _ = await _upstream_ctx.__aenter__()
        _upstream_session_ctx = ClientSession(read, write)
        _upstream = await _upstream_session_ctx.__aenter__()
        await _upstream.initialize()
        logger.info("Connected to toolshed")
        return _upstream


async def _disconnect():
    """Tear down upstream connection."""
    global _upstream, _upstream_ctx, _upstream_session_ctx

    async with _connect_lock:
        for ctx in (_upstream_session_ctx, _upstream_ctx):
            if ctx is None:
                continue
            try:
                await ctx.__aexit__(None, None, None)
            except (ConnectionError, OSError, RuntimeError):
                pass
        _upstream = None
        _upstream_ctx = None
        _upstream_session_ctx = None


async def _ensure_connected() -> ClientSession:
    """Connect to toolshed on first use, with retry."""
    for attempt in range(10):
        try:
            return await _connect()
        except _CONN_ERRORS as e:
            delay = min(2 * (attempt + 1), 15)
            logger.warning(
                "Toolshed not ready (attempt %d/10): %s. Retrying in %ds...",
                attempt + 1,
                e,
                delay,
            )
            await asyncio.sleep(delay)
    raise ConnectionError("Could not connect to toolshed after 10 attempts")


async def _get_upstream() -> ClientSession:
    """Get upstream session, connecting lazily on first use."""
    if _upstream is not None:
        return _upstream
    return await _ensure_connected()


async def _call_with_reconnect(fn):
    """Call fn(upstream) with one reconnect attempt on failure."""
    try:
        upstream = await _get_upstream()
        return await fn(upstream)
    except _CONN_ERRORS as e:
        logger.warning("Upstream call failed, reconnecting: %s", e)
        await _disconnect()
        await asyncio.sleep(RECONNECT_DELAY)
        upstream = await _connect()
        return await fn(upstream)


@bridge.tool()
async def list_tools(group: str = "") -> str:
    """List available tools, optionally filtered by group.

    Call this to discover what's available before using run_tool.
    """
    result = await _call_with_reconnect(
        lambda up: up.call_tool("list_tools", {"group": group})
    )
    return result.content[0].text if result.content else "[]"


@bridge.tool()
async def run_tool(server: str, tool: str, args: dict | None = None) -> str:
    """Run a tool on a specific MCP backend server.

    Use list_tools first to discover available servers and tool names.
    Pass the server name, tool name, and arguments dict.
    """
    if args is None:
        args = {}
    result = await _call_with_reconnect(
        lambda up: up.call_tool(
            "run_tool", {"server": server, "tool": tool, "args": args}
        )
    )
    return result.content[0].text if result.content else "{}"


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    import anyio

    async def run():
        await bridge.run_stdio_async()

    anyio.run(run)


if __name__ == "__main__":
    main()
