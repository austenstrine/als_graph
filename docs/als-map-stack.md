# ALS Map MCP stack launcher

One command on Raider to run the HTTP MCP server + Cloudflare tunnel, show health, and follow usage logs.

## Quick start

```bash
# from repo (or ~/.local/bin after install-desktop)
als_map_stack start      # bring HTTP MCP + tunnel up
als_map_stack stop       # easy off switch (both units)
als_map_stack restart    # required after code/branch changes
als_map_stack status      # units, git checkout, local+public health
als_map_stack probe      # human-readable live tool schema (tunnel)
als_map_stack logs -f
als_map_stack doctor
als_map_stack install-desktop   # desktop icon + PATH symlink

# process isolation (smoke / unit templates) — see below
als_map_isolate install-units
als_map_isolate run -- python -m py_compile scripts/als_map_lib.py
als_map_isolate profiles
```

**Restart after shipping tools.** The systemd unit runs whatever is checked out in `WorkingDirectory` (`~/Development/als_graph`). A live process keeps the old tool list until `als_map_stack restart` (or stop then start).

## What it manages

| Piece | Detail |
|-------|--------|
| HTTP MCP | systemd user unit `als-map-mcp-http.service` → `scripts/als_map_mcp_http.py` on `127.0.0.1:8787` |
| Tunnel | `als-map-mcp-tunnel.service` → `cloudflared … run als-map-mcp` |
| Agent slice | `als-map-agent.slice` — parent cgroup for smoke scopes (`scripts/als_map_isolate`) |
| Token | `~/.config/als_map/mcp_token` + `EnvironmentFile` `~/.config/als_map/env` (never committed) |
| Access log | `~/.local/state/als_map/access.log` |

Public: `https://mcp.austenstrine.dev/mcp` — health `…/health`.

## Commands

| Command | Role |
|---------|------|
| `start` | Ensure token exists; enable+start both units; print status panel |
| `stop` | Stop tunnel then HTTP MCP (easy full off) |
| `restart` | `daemon-reload` + restart both units; reloads code from the current checkout |
| `status` | Units, **git branch/HEAD**, local+public `/health`, recent access + journal |
| `probe [--local] [--json]` | Live Bearer `tools/list` via tunnel (or `--local`); human-readable tool + arg schema (aliases: `schema`, `tools`) |
| `logs [-f] [N]` | Tail access log (`-f` follow) |
| `doctor` | Checklist: scripts, token, units, cloudflared, health endpoints |
| `watch` | Start if needed, print status, follow access log + periodic health line |
| `install-desktop` | Install `.desktop`, SVG icon, `~/.local/bin/als_map_stack` |

Exact Austen commands:

```bash
als_map_stack stop
als_map_stack start
als_map_stack restart
```

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

Then: **Activities → search “ALS Map MCP”** (`Name=ALS Map MCP`) → right-click → Pin to Dash (or drag onto favorites). `install-desktop` also drops an executable launcher on `~/Desktop/als-map-mcp.desktop`.

Alternates: run `als_map_stack watch` from a terminal, or open `~/.local/share/applications/als-map-mcp.desktop`.

The launcher opens a terminal (`gnome-terminal` preferred) running `als_map_stack watch`. Right-click actions: Status / Stop / Doctor / Probe.

Icon: circular AS logo (`assets/als-map-mcp.png` + scalable `assets/als-map-mcp.svg`), from `~/Pictures/AS_LOGO`.

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

## Process isolation (cgroups)

Prefer **systemd/cgroups isolation** over query-tool throttling or sleep/backoff that would slow ALS Map search. **No `CPUQuota`.**

Repo templates live under `systemd/user/`:

| Unit | Role | Limits |
|------|------|--------|
| `als-map-agent.slice` | Parent for smoke/test scopes | `MemoryMax=6G`, `TasksMax=512` |
| smoke scope (`als_map_isolate run`) | Agent `py_compile` / `index_create` / similar | `MemoryMax=4G`, `TasksMax=256`, `OOMScoreAdjust=700`, `Nice=10`, `IOSchedulingClass=best-effort` / priority `6` |
| `als-map-mcp-http.service` | Live HTTP MCP | `MemoryMax=8G`, `TasksMax=128`, `OOMScoreAdjust=200` (ceilings only) |
| `als-map-mcp-tunnel.service` | cloudflared | `MemoryMax=256M`, `TasksMax=32`, `OOMScoreAdjust=100` |

```bash
# Install slice + MCP *limit drop-ins* into ~/.config/systemd/user (safe daemon-reload).
# Does NOT restart live MCP — host apply is deferred until the desktop is stable.
# Drop-ins preserve existing ExecStart/WorkingDirectory; full templates stay in systemd/user/.
scripts/als_map_isolate install-units
# optional on a fresh host: scripts/als_map_isolate install-units --force-mcp-units

# Run smoke under a transient scope in als-map-agent.slice
scripts/als_map_isolate run -- .venv/bin/python - <<'PY'
from als_map_lib import open_db_rw
# …
PY
```

Override smoke ceilings with `ALS_MAP_SMOKE_MEMORY_MAX`, `ALS_MAP_SMOKE_TASKS_MAX`, `ALS_MAP_SMOKE_OOM_SCORE`, `ALS_MAP_SMOKE_NICE`, `ALS_MAP_SMOKE_IO_*`.

## Safe apply / reload (do not kill user systemd)

After a wedged `systemctl --user` (`Transport endpoint is not connected`), **never** kill user systemd, session dbus, or hung `systemctl --user` children as recovery — that path is the suspected GUI-crash trigger.

`als_map_stack` and `als_map_isolate` share `scripts/als_map_systemd.sh`:

- Probe the user bus with a short timeout before mutations.
- Wrap `systemctl --user` in `timeout`; on timeout or bus errors, **fail soft** (exit `75`) with a clear message.
- Recovery is re-login / reboot when Austen confirms the desktop is stable — not process kills.

## Safety

- Bearer token is **never** printed by `als_map_stack` or written to the access log.
- Do not commit `~/.config/als_map/*` or `~/.cloudflared/*.json`.
- Stack prefers existing user systemd units; `als_map_isolate install-units` installs the agent slice plus MCP `*.service.d/limits.conf` drop-ins (does not clobber ExecStart) and does not restart MCP by itself.
- Do not rewrite tunnel credentials.
