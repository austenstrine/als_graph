# ALS Map MCP stack launcher

One command on Raider to run the HTTP MCP server + Cloudflare tunnel, show health, and follow usage logs.

## Quick start

```bash
# from repo (or ~/.local/bin after install-desktop)
./scripts/als_map_stack start
./scripts/als_map_stack status
./scripts/als_map_stack logs -f
./scripts/als_map_stack doctor
./scripts/als_map_stack install-desktop   # desktop icon + PATH symlink
```

## What it manages

| Piece | Detail |
|-------|--------|
| HTTP MCP | systemd user unit `als-map-mcp-http.service` → `scripts/als_map_mcp_http.py` on `127.0.0.1:8787` |
| Tunnel | `als-map-mcp-tunnel.service` → `cloudflared … run als-map-mcp` |
| Token | `~/.config/als_map/mcp_token` + `EnvironmentFile` `~/.config/als_map/env` (never committed) |
| Access log | `~/.local/state/als_map/access.log` |

Public: `https://mcp.austenstrine.dev/mcp` — health `…/health`.

## Commands

| Command | Role |
|---------|------|
| `start` | Ensure token exists; enable+start both units; print status panel |
| `stop` / `restart` | Stop or restart units |
| `status` | Units active/enabled, local+public `/health`, recent access + journal lines |
| `logs [-f] [N]` | Tail access log (`-f` follow) |
| `doctor` | Checklist: scripts, token, units, cloudflared, health endpoints |
| `watch` | Start if needed, print status, follow access log + periodic health line |
| `install-desktop` | Install `.desktop`, SVG icon, `~/.local/bin/als_map_stack` |

## Access log format

Space-separated fields (no Authorization / bearer values):

```
ts ip method path status mcp_method tool
```

Example:

```
2026-11-10T05:12:01Z 203.0.113.10 POST /mcp 200 tools/call als_search
2026-11-10T05:12:02Z 127.0.0.1 GET /health 200 - -
```

Client IP prefers `CF-Connecting-IP`, then `X-Forwarded-For`, else the socket peer.

## Desktop icon (GNOME)

```bash
./scripts/als_map_stack install-desktop
```

Then: **Activities → search “ALS Map MCP” → right-click → Pin to Dash** (or drag onto favorites).

The launcher opens a terminal (`gnome-terminal` preferred) running `als_map_stack watch`. Right-click actions: Status / Stop / Doctor.

Icon source in repo: `assets/als-map-mcp.svg`.

## Sample status panel

```
ALS Map MCP stack
  repo     /home/…/Development/als_graph
  token    ~/.config/als_map/mcp_token ok
  env      ~/.config/als_map/env ok
  access   ~/.local/state/als_map/access.log

Units
  als-map-mcp-http.service     active=ok active   enabled=enabled
  als-map-mcp-tunnel.service   active=ok active   enabled=enabled

Health
  local    ok 200  {"ok":true,"server":"als-map",…}
  public   ok 200  {"ok":true,"server":"als-map",…}
```

## Safety

- Bearer token is **never** printed by `als_map_stack` or written to the access log.
- Do not commit `~/.config/als_map/*` or `~/.cloudflared/*.json`.
- Stack prefers existing user systemd units; it does not rewrite tunnel credentials.
