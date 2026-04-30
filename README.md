# Toolshed

MCP proxy server that multiplexes multiple MCP backends through just 2 tools.

## The Problem

MCP-powered agents load tool schemas into their context window on every turn. If you have 20+ tools across several MCP servers, that's thousands of tokens spent before the agent even starts thinking. As you add more servers, the cost grows linearly.

## The Solution

Toolshed sits between your agent and your MCP backends. Instead of exposing every tool schema directly, it exposes just two tools:

- **`list_tools`** — discover available tools at runtime
- **`run_tool`** — call any backend tool by name

Your agent sees ~400 tokens of schema instead of thousands. Tool discovery happens on-demand through the proxy.

## Requirements

- Python 3.10+
- `mcp` SDK >= 1.22.0

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure your backends

Copy the example config and edit it:

```bash
cp toolshed.example.json toolshed.json
```

`toolshed.json` defines the MCP servers that Toolshed proxies to:

```json
{
  "servers": {
    "my-http-server": {
      "type": "http",
      "url": "http://127.0.0.1:8000/mcp"
    },
    "my-stdio-server": {
      "type": "stdio",
      "command": "python3",
      "args": ["path/to/your/mcp_server.py"],
      "env": {}
    }
  },
  "groups": {
    "research": ["my-http-server:search", "my-http-server:fetch"],
    "ops": ["my-http-server:health_check"]
  }
}
```

**Server types:**

| Type | Description |
|------|-------------|
| `http` | Connects to a running MCP server via streamable-http. Set `url` to the server's MCP endpoint. |
| `stdio` | Spawns a subprocess and communicates via stdin/stdout. Set `command`, `args`, and optionally `env`. |

**Groups** are optional. They let you organize tools across servers into logical collections (e.g. "research", "ops"). Pass a group name to `list_tools` to filter results.

### 3. Start the server

```bash
python3 toolshed.py --config toolshed.json --port 8750
```

The server starts on `http://127.0.0.1:8750`. On startup it connects to all configured backends and builds a tool catalog.

### 4. Connect your agent

Toolshed exposes a standard MCP streamable-HTTP endpoint, so it works with **any MCP-compatible client** — not just Claude Code.

**Claude Code** (`~/.claude/mcp.json`):

```json
{
  "mcpServers": {
    "toolshed": {
      "type": "http",
      "url": "http://127.0.0.1:8750/mcp"
    }
  }
}
```

**Other MCP clients:** Point your client at `http://127.0.0.1:8750/mcp` using streamable-HTTP transport. Any agent or framework with MCP support (Claude Agent SDK, OpenAI Agents SDK, LangChain, Cursor, etc.) can connect.

Your agent now has access to all backend tools through `list_tools` and `run_tool`.

## Usage

### list_tools

Discover available tools across all backends:

```
list_tools()                    # all tools
list_tools(group="research")    # filter by group
```

Returns tool names, descriptions, input schemas, and which server owns each tool.

### run_tool

Call a specific tool on a specific backend:

```
run_tool(server="my-http-server", tool="search", args={"query": "hello"})
```

Returns the raw result from the backend — Toolshed does not modify or summarize responses.

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET/POST /mcp` | MCP protocol endpoint (streamable-http) |
| `GET /refresh` | Force refresh the full tool catalog |
| `GET /refresh?server=name` | Refresh one backend's tools |

## Running as a Service (systemd)

Edit `toolshed.service` to set the correct paths for your system, then:

```bash
cp toolshed.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now toolshed
```

Check status:

```bash
systemctl --user status toolshed
journalctl --user -u toolshed -f
```

## How It Works

1. On startup, Toolshed connects to every backend in `toolshed.json`
2. It calls `tools/list` on each backend and caches the results
3. When an agent calls `list_tools`, Toolshed returns the cached catalog
4. When an agent calls `run_tool`, Toolshed forwards the call to the right backend and returns the raw result
5. Hit `/refresh` to rebuild the catalog without restarting

## Project Structure

```
toolshed.py             # Server (single file, all logic)
toolshed_bridge.py      # stdio-to-HTTP bridge for Claude Code MCP integration
torus-launcher.sh       # Startup script that launches all MCP backend servers on first session
toolshed.json           # Your backend config (gitignored)
toolshed.example.json   # Example config to copy
test_toolshed.py        # Test suite
toolshed.service        # systemd unit template
requirements.txt        # Python dependencies
```

## Works with Toroidal Skills

Toolshed and [Toroidal Skills](https://github.com/OZmasterAI/toroidal-skills) are designed to work together. Register the skill server as a backend in `toolshed.json` and your agent gets skill discovery, invocation, and quality tracking through the same 2-tool interface:

```json
{
  "servers": {
    "torus-skills": {
      "type": "http",
      "url": "http://127.0.0.1:8743/mcp"
    }
  }
}
```

Both can also be used independently.

## As a submodule

Can be used as a submodule in any project, including [Torus-Framework](https://github.com/OZmasterAI/Torus-Framework):

```bash
git submodule add https://github.com/OZmasterAI/toroidal-toolshed.git toolshed
```

## Built with

Built with [Torus Framework](https://github.com/OZmasterAI/Torus-Framework) — a self-evolving quality framework for Claude Code.

## License

Apache-2.0
