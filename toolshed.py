#!/usr/bin/env python3
"""Toolshed — MCP Proxy Server

Multiplexes multiple MCP backends through 2 tools (list_tools, run_tool).
Reduces tool schema tokens from ~5-7k to ~400 per turn.

Usage:
    python3 toolshed.py --config toolshed.json --port 8750
"""

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
import time
from pathlib import Path

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger("toolshed")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def _expand_env_vars(obj):
    """Recursively expand ${VAR} in string values from process environment."""
    if isinstance(obj, str):
        return re.sub(
            r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), obj
        )
    if isinstance(obj, dict):
        return {k: _expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env_vars(v) for v in obj]
    return obj


def load_config(path: str) -> dict:
    """Load and validate toolshed.json config."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(p) as f:
        config = json.load(f)

    if "servers" not in config:
        raise ValueError("Config must have 'servers' key")

    for name, srv in config["servers"].items():
        stype = srv.get("type")
        if stype not in ("http", "stdio"):
            raise ValueError(f"Server '{name}' must have type 'http' or 'stdio'")
        if stype == "http" and "url" not in srv:
            raise ValueError(f"HTTP server '{name}' must have 'url'")
        if stype == "stdio":
            if "command" not in srv:
                raise ValueError(f"stdio server '{name}' must have 'command'")
            if "args" not in srv:
                raise ValueError(f"stdio server '{name}' must have 'args'")

    config = _expand_env_vars(config)  # type: ignore[assignment]
    config.setdefault("groups", {})
    return config


# ---------------------------------------------------------------------------
# Backend Manager
# ---------------------------------------------------------------------------


class BackendManager:
    """Manages long-lived MCP client connections to backend servers."""

    def __init__(self):
        self._sessions: dict[str, ClientSession] = {}
        self._ctx: dict = {}  # transport context managers
        self._session_ctx: dict = {}  # ClientSession context managers
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def sessions(self) -> dict[str, ClientSession]:
        return self._sessions

    async def connect_http(self, name: str, url: str) -> None:
        """Connect to an HTTP MCP backend (streamable-http)."""
        try:
            self._ctx[name] = streamablehttp_client(url=url)
            read, write, _ = await self._ctx[name].__aenter__()
            self._session_ctx[name] = ClientSession(read, write)
            self._sessions[name] = await self._session_ctx[name].__aenter__()
            await self._sessions[name].initialize()
            self._locks[name] = asyncio.Lock()
            logger.info("Connected to HTTP backend: %s (%s)", name, url)
        except Exception:
            await self._cleanup_backend(name)
            raise

    async def spawn_stdio(
        self, name: str, command: str, args: list, env: dict | None = None
    ) -> None:
        """Spawn a stdio MCP backend as a subprocess."""
        try:
            # Merge with parent env so subprocess has PATH etc.
            actual_env = {**os.environ, **env} if env else None
            params = StdioServerParameters(command=command, args=args, env=actual_env)
            self._ctx[name] = stdio_client(params)
            read, write = await self._ctx[name].__aenter__()
            self._session_ctx[name] = ClientSession(read, write)
            self._sessions[name] = await self._session_ctx[name].__aenter__()
            await self._sessions[name].initialize()
            self._locks[name] = asyncio.Lock()
            logger.info("Spawned stdio backend: %s (%s)", name, command)
        except Exception:
            await self._cleanup_backend(name)
            raise

    async def list_backend_tools(self, name: str):
        """List tools from a specific backend. Returns list of Tool objects."""
        if name not in self._sessions:
            raise KeyError(f"No backend named '{name}'")
        result = await self._sessions[name].list_tools()
        return result.tools

    async def call_backend_tool(self, name: str, tool: str, args: dict) -> dict:
        """Call a tool on a backend. Returns a JSON-friendly dict."""
        if name not in self._sessions:
            return {
                "error": f"Unknown server '{name}'",
                "available_servers": list(self._sessions.keys()),
            }

        async with self._locks[name]:
            try:
                result = await self._sessions[name].call_tool(tool, args)

                # Surface MCP-level tool errors
                if result.isError:
                    text = (
                        getattr(result.content[0], "text", "Unknown error")
                        if result.content
                        else "Unknown error"
                    )
                    return {"error": f"Tool '{tool}' error: {text}", "server": name}

                # Convert CallToolResult content to JSON-friendly format
                if len(result.content) == 1 and result.content[0].type == "text":
                    text_val = getattr(result.content[0], "text", "")
                    try:
                        return json.loads(text_val)
                    except json.JSONDecodeError:
                        return {"result": text_val}
                return {
                    "content": [
                        {"type": c.type, "text": getattr(c, "text", "")}
                        for c in result.content
                    ]
                }
            except Exception as e:
                return {"error": f"Backend '{name}' unreachable: {e}", "server": name}

    async def _cleanup_backend(self, name: str) -> None:
        for store in (self._session_ctx, self._ctx):
            try:
                if name in store:
                    await store[name].__aexit__(None, None, None)
            except Exception:
                pass
        self._sessions.pop(name, None)
        self._session_ctx.pop(name, None)
        self._ctx.pop(name, None)
        self._locks.pop(name, None)

    async def shutdown(self) -> None:
        for name in list(self._sessions):
            await self._cleanup_backend(name)
        logger.info("All backends shut down")


# ---------------------------------------------------------------------------
# Tool Catalog
# ---------------------------------------------------------------------------


class ToolCatalog:
    """Discovers, caches, and indexes tool metadata from backends."""

    def __init__(self, manager: BackendManager, groups_config: dict | None = None):
        self._manager = manager
        self._groups_config = groups_config or {}
        self._catalog: dict[str, dict] = {}
        self._ttl_seconds = 300

    async def discover_all(self) -> None:
        for name in list(self._manager.sessions):
            await self.discover_one(name)

    async def discover_one(self, server_name: str) -> None:
        try:
            tools = await self._manager.list_backend_tools(server_name)
            self._catalog[server_name] = {
                "tools": [
                    {
                        "server": server_name,
                        "name": t.name,
                        "description": t.description or "",
                        "inputSchema": getattr(t, "inputSchema", {}),
                    }
                    for t in tools
                ],
                "last_refresh": time.time(),
                "status": "connected",
            }
            logger.info("Discovered %d tools from %s", len(tools), server_name)
        except Exception as e:
            logger.error("Discovery failed for %s: %s", server_name, e)
            self._catalog[server_name] = {
                "tools": [],
                "last_refresh": time.time(),
                "status": "unreachable",
            }

    def list_all(self) -> list[dict]:
        result = []
        for server_name, entry in self._catalog.items():
            for tool in entry["tools"]:
                result.append({**tool, "group": server_name})
        return result

    def list_by_group(self, group: str) -> list[dict]:
        # Manual groups take precedence
        if group in self._groups_config:
            result = []
            for ref in self._groups_config[group]:
                server, tool_name = ref.split(":", 1)
                for tool in self._catalog.get(server, {}).get("tools", []):
                    if tool["name"] == tool_name:
                        result.append({**tool, "group": group})
            return result
        # Auto-group = server name
        if group in self._catalog:
            return [{**t, "group": group} for t in self._catalog[group]["tools"]]
        return []

    def get_groups(self) -> list[str]:
        groups = set(self._catalog.keys())
        groups.update(self._groups_config.keys())
        return sorted(groups)

    def get_server_tools(self, server_name: str) -> list[str]:
        return [t["name"] for t in self._catalog.get(server_name, {}).get("tools", [])]

    async def force_refresh(self, server: str | None = None) -> dict:
        if server:
            await self.discover_one(server)
            count = len(self._catalog.get(server, {}).get("tools", []))
            return {"refreshed": [server], "tools_count": count}
        await self.discover_all()
        total = sum(len(e["tools"]) for e in self._catalog.values())
        return {"refreshed": list(self._catalog.keys()), "tools_count": total}

    def is_stale(self, server_name: str) -> bool:
        entry = self._catalog.get(server_name)
        if not entry:
            return True
        return (time.time() - entry.get("last_refresh", 0)) > self._ttl_seconds


# ---------------------------------------------------------------------------
# Tool implementations (testable without MCP)
# ---------------------------------------------------------------------------


def list_tools_impl(catalog: ToolCatalog, group: str = "") -> dict:
    if group:
        tools = catalog.list_by_group(group)
        groups = [group] if tools else []
    else:
        tools = catalog.list_all()
        groups = catalog.get_groups()
    return {"tools": tools, "count": len(tools), "groups": groups}


async def run_tool_impl(
    manager: BackendManager,
    catalog: ToolCatalog,
    server: str,
    tool: str,
    args: dict | None = None,
) -> dict:
    args = args or {}
    if server not in manager.sessions:
        return {
            "error": f"Unknown server '{server}'",
            "available_servers": list(manager.sessions.keys()),
        }

    known = catalog.get_server_tools(server)
    if known and tool not in known:
        return {
            "error": f"Unknown tool '{tool}' on server '{server}'",
            "available_tools": known,
        }

    result = await manager.call_backend_tool(server, tool, args)

    if (
        isinstance(result, dict)
        and "error" in result
        and "unreachable" in str(result.get("error", ""))
    ):
        asyncio.create_task(catalog.force_refresh(server))

    return result


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

_manager: BackendManager | None = None
_catalog: ToolCatalog | None = None
_refresh_task: asyncio.Task | None = None

mcp_server = FastMCP("toolshed", json_response=True)


@mcp_server.tool()
async def list_tools(group: str = "") -> dict:
    """List available tools from all MCP backends.

    Returns tool names, descriptions, and which server owns them.
    Use the optional group parameter to filter (e.g. group="memory").
    Call this to discover what's available before using run_tool.
    """
    assert _catalog is not None, "Server not started"
    return list_tools_impl(_catalog, group)


@mcp_server.tool()
async def run_tool(server: str, tool: str, args: dict = {}) -> dict:
    """Run a tool on a specific MCP backend server.

    Use list_tools first to discover available servers and tool names.
    Pass the server name, tool name, and arguments dict.
    """
    assert _manager is not None and _catalog is not None, "Server not started"
    return await run_tool_impl(_manager, _catalog, server, tool, args)


@mcp_server.custom_route("/refresh", methods=["GET"])
async def refresh_endpoint(request: Request) -> JSONResponse:
    """Force refresh of backend tool catalogs."""
    assert _catalog is not None, "Server not started"
    server = request.query_params.get("server")
    try:
        info = await _catalog.force_refresh(server=server)
        return JSONResponse({"ok": True, **info})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def _background_refresh():
    assert _catalog is not None
    while True:
        await asyncio.sleep(_catalog._ttl_seconds)
        try:
            await _catalog.discover_all()
            logger.debug("Background refresh completed")
        except Exception as e:
            logger.error("Background refresh failed: %s", e)


async def startup(config_path: str) -> None:
    global _manager, _catalog, _refresh_task

    config = load_config(config_path)
    _manager = BackendManager()

    for name, srv in config["servers"].items():
        try:
            if srv["type"] == "http":
                await _manager.connect_http(name, srv["url"])
            elif srv["type"] == "stdio":
                await _manager.spawn_stdio(
                    name, srv["command"], srv["args"], srv.get("env")
                )
        except Exception as e:
            logger.warning("Backend '%s' unavailable at startup: %s", name, e)

    _catalog = ToolCatalog(_manager, config.get("groups", {}))
    await _catalog.discover_all()

    total = sum(len(e["tools"]) for e in _catalog._catalog.values())
    logger.info("Toolshed ready: %d backends, %d tools", len(_manager.sessions), total)

    _refresh_task = asyncio.create_task(_background_refresh())


async def shutdown_server() -> None:
    global _refresh_task
    if _refresh_task:
        _refresh_task.cancel()
        try:
            await _refresh_task
        except asyncio.CancelledError:
            pass
    if _manager:
        await _manager.shutdown()
    logger.info("Toolshed shut down")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Toolshed MCP Proxy Server")
    parser.add_argument("--port", type=int, default=8750, help="Port to listen on")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument(
        "--config", default="toolshed.json", help="Path to toolshed.json config"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s"
    )

    mcp_server.settings.port = args.port
    mcp_server.settings.host = args.host

    def handle_signal(signum, _frame):
        logger.info("Received signal %s, shutting down", signum)
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    import anyio

    async def run():
        await startup(args.config)
        try:
            await mcp_server.run_streamable_http_async()
        finally:
            await shutdown_server()

    anyio.run(run)


if __name__ == "__main__":
    main()
