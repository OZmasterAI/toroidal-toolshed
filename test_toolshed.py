"""Tests for Toolshed MCP Proxy Server."""

import json
import os
import time

import pytest

from toolshed import (
    BackendManager,
    ToolCatalog,
    _expand_env_vars,
    list_tools_impl,
    load_config,
    run_tool_impl,
)


# ---------------------------------------------------------------------------
# Config tests (Task 2)
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_load_valid(self, tmp_path):
        cfg = {
            "servers": {"demo": {"type": "http", "url": "http://127.0.0.1:9999/mcp"}},
            "groups": {},
        }
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps(cfg))
        result = load_config(str(p))
        assert "servers" in result
        assert result["servers"]["demo"]["type"] == "http"
        assert "groups" in result

    def test_load_missing(self):
        with pytest.raises(FileNotFoundError):
            load_config("/nonexistent/path.json")

    def test_load_no_servers(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{}")
        with pytest.raises(ValueError, match="servers"):
            load_config(str(p))

    def test_load_bad_type(self, tmp_path):
        cfg = {"servers": {"x": {"type": "grpc"}}}
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(cfg))
        with pytest.raises(ValueError, match="http.*stdio"):
            load_config(str(p))

    def test_http_missing_url(self, tmp_path):
        cfg = {"servers": {"x": {"type": "http"}}}
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(cfg))
        with pytest.raises(ValueError, match="url"):
            load_config(str(p))

    def test_stdio_missing_command(self, tmp_path):
        cfg = {"servers": {"x": {"type": "stdio", "args": []}}}
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(cfg))
        with pytest.raises(ValueError, match="command"):
            load_config(str(p))

    def test_stdio_missing_args(self, tmp_path):
        cfg = {"servers": {"x": {"type": "stdio", "command": "python3"}}}
        p = tmp_path / "bad.json"
        p.write_text(json.dumps(cfg))
        with pytest.raises(ValueError, match="args"):
            load_config(str(p))

    def test_groups_optional(self, tmp_path):
        cfg = {"servers": {"x": {"type": "http", "url": "http://x/mcp"}}}
        p = tmp_path / "cfg.json"
        p.write_text(json.dumps(cfg))
        result = load_config(str(p))
        assert result["groups"] == {}

    def test_real_config(self):
        result = load_config("toolshed.json")
        assert "memory" in result["servers"]
        assert result["servers"]["memory"]["type"] == "http"
        assert "groups" in result


class TestEnvExpansion:
    def test_expand_string(self):
        os.environ["_TEST_VAR"] = "hello"
        assert _expand_env_vars("${_TEST_VAR}") == "hello"
        del os.environ["_TEST_VAR"]

    def test_expand_missing(self):
        assert _expand_env_vars("${_NONEXISTENT_VAR_XYZ}") == "${_NONEXISTENT_VAR_XYZ}"

    def test_expand_nested(self):
        os.environ["_TEST_VAR"] = "val"
        result = _expand_env_vars({"key": "${_TEST_VAR}", "list": ["${_TEST_VAR}"]})
        assert result["key"] == "val"
        assert result["list"] == ["val"]
        del os.environ["_TEST_VAR"]


# ---------------------------------------------------------------------------
# ToolCatalog tests (Tasks 5-6) — mock data, no live backends
# ---------------------------------------------------------------------------

MOCK_CATALOG_DATA = {
    "memory": {
        "tools": [
            {
                "server": "memory",
                "name": "search_knowledge",
                "description": "Search memory",
                "inputSchema": {},
            },
            {
                "server": "memory",
                "name": "remember_this",
                "description": "Save to memory",
                "inputSchema": {},
            },
            {
                "server": "memory",
                "name": "get_memory",
                "description": "Get by ID",
                "inputSchema": {},
            },
        ],
        "last_refresh": time.time(),
        "status": "connected",
    },
    "skills-v2": {
        "tools": [
            {
                "server": "skills-v2",
                "name": "list_skills",
                "description": "List skills",
                "inputSchema": {},
            },
            {
                "server": "skills-v2",
                "name": "invoke_skill",
                "description": "Run a skill",
                "inputSchema": {},
            },
        ],
        "last_refresh": time.time(),
        "status": "connected",
    },
}

MOCK_GROUPS = {
    "research": ["memory:search_knowledge", "memory:get_memory"],
    "save": ["memory:remember_this", "skills-v2:capture_skill"],
}


def _make_catalog() -> ToolCatalog:
    mgr = BackendManager()
    cat = ToolCatalog(mgr, MOCK_GROUPS)
    cat._catalog = MOCK_CATALOG_DATA.copy()
    return cat


class TestToolCatalog:
    def test_list_all(self):
        cat = _make_catalog()
        tools = cat.list_all()
        assert len(tools) == 5
        assert all("server" in t and "name" in t and "description" in t for t in tools)

    def test_list_all_has_group(self):
        cat = _make_catalog()
        tools = cat.list_all()
        for t in tools:
            assert "group" in t

    def test_auto_groups(self):
        cat = _make_catalog()
        groups = cat.get_groups()
        assert "memory" in groups
        assert "skills-v2" in groups

    def test_manual_groups(self):
        cat = _make_catalog()
        groups = cat.get_groups()
        assert "research" in groups
        assert "save" in groups

    def test_list_by_auto_group(self):
        cat = _make_catalog()
        tools = cat.list_by_group("memory")
        assert len(tools) == 3
        assert all(t["server"] == "memory" for t in tools)

    def test_list_by_manual_group(self):
        cat = _make_catalog()
        tools = cat.list_by_group("research")
        assert len(tools) == 2
        names = {t["name"] for t in tools}
        assert names == {"search_knowledge", "get_memory"}

    def test_list_by_unknown_group(self):
        cat = _make_catalog()
        tools = cat.list_by_group("nonexistent")
        assert tools == []

    def test_get_server_tools(self):
        cat = _make_catalog()
        names = cat.get_server_tools("memory")
        assert "search_knowledge" in names
        assert "remember_this" in names

    def test_is_stale_fresh(self):
        cat = _make_catalog()
        assert not cat.is_stale("memory")

    def test_is_stale_old(self):
        cat = _make_catalog()
        cat._catalog["memory"]["last_refresh"] = time.time() - 600
        assert cat.is_stale("memory")

    def test_is_stale_missing(self):
        cat = _make_catalog()
        assert cat.is_stale("nonexistent")


# ---------------------------------------------------------------------------
# Tool impl tests (Task 7) — no MCP, no live backends
# ---------------------------------------------------------------------------


class TestListToolsImpl:
    def test_all(self):
        cat = _make_catalog()
        result = list_tools_impl(cat)
        assert result["count"] == 5
        assert len(result["tools"]) == 5
        assert "groups" in result
        assert "memory" in result["groups"]

    def test_filtered_auto_group(self):
        cat = _make_catalog()
        result = list_tools_impl(cat, group="memory")
        assert result["count"] == 3
        assert all(t["server"] == "memory" for t in result["tools"])
        assert result["groups"] == ["memory"]

    def test_filtered_manual_group(self):
        cat = _make_catalog()
        result = list_tools_impl(cat, group="research")
        assert result["count"] == 2

    def test_filtered_unknown_group(self):
        cat = _make_catalog()
        result = list_tools_impl(cat, group="nonexistent")
        assert result["count"] == 0
        assert result["groups"] == []


class TestRunToolImpl:
    @pytest.mark.asyncio
    async def test_bad_server(self):
        mgr = BackendManager()
        cat = _make_catalog()
        result = await run_tool_impl(mgr, cat, "nonexistent", "foo")
        assert "error" in result
        assert "available_servers" in result

    @pytest.mark.asyncio
    async def test_bad_tool(self):
        mgr = BackendManager()
        cat = _make_catalog()
        # Simulate a connected session so server check passes
        mgr._sessions["memory"] = None  # type: ignore[assignment]
        result = await run_tool_impl(mgr, cat, "memory", "nonexistent_tool")
        assert "error" in result
        assert "available_tools" in result
        assert "search_knowledge" in result["available_tools"]


# ---------------------------------------------------------------------------
# Integration tests — require live backends
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestHTTPBackend:
    @pytest.mark.asyncio
    async def test_connect(self):
        mgr = BackendManager()
        await mgr.connect_http("memory", "http://127.0.0.1:8742/mcp")
        assert "memory" in mgr.sessions
        tools = await mgr.list_backend_tools("memory")
        assert len(tools) > 0
        assert any(t.name == "search_knowledge" for t in tools)
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_call_tool(self):
        mgr = BackendManager()
        await mgr.connect_http("memory", "http://127.0.0.1:8742/mcp")
        result = await mgr.call_backend_tool("memory", "health_check", {})
        assert "error" not in result
        await mgr.shutdown()


@pytest.mark.integration
class TestStdioBackend:
    @pytest.mark.asyncio
    async def test_spawn(self):
        mgr = BackendManager()
        await mgr.spawn_stdio("search", "python3", ["path/to/your/search_server.py"])
        assert "search" in mgr.sessions
        tools = await mgr.list_backend_tools("search")
        assert len(tools) > 0
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_cleanup(self):
        mgr = BackendManager()
        await mgr.spawn_stdio("search", "python3", ["path/to/your/search_server.py"])
        await mgr.shutdown()
        assert "search" not in mgr.sessions


@pytest.mark.integration
class TestIntegrationRoundtrip:
    @pytest.mark.asyncio
    async def test_discover_and_list(self):
        mgr = BackendManager()
        await mgr.connect_http("memory", "http://127.0.0.1:8742/mcp")
        cat = ToolCatalog(mgr)
        await cat.discover_all()
        tools = cat.list_all()
        assert len(tools) > 0
        assert any(t["name"] == "search_knowledge" for t in tools)

        result = list_tools_impl(cat)
        assert result["count"] > 0
        assert "memory" in result["groups"]
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_run_real_tool(self):
        mgr = BackendManager()
        await mgr.connect_http("memory", "http://127.0.0.1:8742/mcp")
        cat = ToolCatalog(mgr)
        await cat.discover_all()
        result = await run_tool_impl(mgr, cat, "memory", "health_check")
        assert "error" not in result
        await mgr.shutdown()

    @pytest.mark.asyncio
    async def test_force_refresh(self):
        mgr = BackendManager()
        await mgr.connect_http("memory", "http://127.0.0.1:8742/mcp")
        cat = ToolCatalog(mgr)
        await cat.discover_all()
        old_time = cat._catalog["memory"]["last_refresh"]
        import asyncio

        await asyncio.sleep(0.01)
        info = await cat.force_refresh()
        assert info["tools_count"] > 0
        assert cat._catalog["memory"]["last_refresh"] > old_time
        await mgr.shutdown()
