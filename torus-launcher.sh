#!/usr/bin/env bash
# Torus Launcher — starts all MCP backend servers on first Claude Code session.
# Subsequent sessions find them already running and skip straight to the bridge.
#
# mcp.json entry:
#   { "command": "bash", "args": ["<REPO>/toolshed/torus-launcher.sh"] }

set -euo pipefail

TOOLSHED_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_DIR="$HOME/.claude"
LOG_DIR="/tmp/torus-mcp"
mkdir -p "$LOG_DIR"

# ── Server definitions: name, port, command ──
declare -A SERVERS=(
    [memory]="8742|python3 $CLAUDE_DIR/hooks/memory_server.py --http --port 8742"
    [torus-skills]="8743|python3 $CLAUDE_DIR/torus-skills/trs_skill_server.py --http --port 8743"
    [search]="8744|python3 $CLAUDE_DIR/hooks/search_server.py --http --port 8744"
    [web-search]="8745|python3 $CLAUDE_DIR/hooks/web_search_server.py --http --port 8745"
    [analytics]="8746|python3 $CLAUDE_DIR/hooks/analytics_server.py --http --port 8746"
    [model-router]="8747|python3 $CLAUDE_DIR/toroidal-model-router/mcp_bridge.py --http --port 8747"
    [indexer]="8748|python3 $CLAUDE_DIR/toroidal-indexer/indexer_server.py --http --port 8748"
    [torus]="8751|node $HOME/projects/torus-mcp-server/dist/index.js"
    [toolshed]="8750|python3 $TOOLSHED_DIR/toolshed.py --config $TOOLSHED_DIR/toolshed.json --port 8750"
)

# ── Boot order: backends first, then toolshed ──
BOOT_ORDER=(memory torus-skills search web-search analytics model-router indexer torus toolshed)

port_listening() {
    python3 -c "import socket; s=socket.socket(); s.settimeout(0.3); exit(0 if s.connect_ex(('127.0.0.1',$1))==0 else 1)" 2>/dev/null
}

wait_for_port() {
    local port=$1 name=$2 max_wait=${3:-15}
    local elapsed=0
    while ! port_listening "$port"; do
        sleep 0.5
        elapsed=$((elapsed + 1))
        if [ "$elapsed" -ge "$((max_wait * 2))" ]; then
            echo "[torus-launcher] WARN: $name :$port not ready after ${max_wait}s" >&2
            return 1
        fi
    done
    return 0
}

started=0

for name in "${BOOT_ORDER[@]}"; do
    IFS='|' read -r port cmd <<< "${SERVERS[$name]}"

    if port_listening "$port"; then
        continue
    fi

    echo "[torus-launcher] Starting $name on :$port" >&2
    nohup $cmd > "$LOG_DIR/$name.log" 2>&1 &

    # Wait for backends before starting toolshed
    if [ "$name" = "toolshed" ]; then
        wait_for_port "$port" "$name" 20
    else
        wait_for_port "$port" "$name" 15
    fi

    started=$((started + 1))
done

if [ "$started" -gt 0 ]; then
    echo "[torus-launcher] Started $started server(s)" >&2
fi

# Hand off to the bridge — exec replaces this process
exec python3 "$TOOLSHED_DIR/toolshed_bridge.py"
