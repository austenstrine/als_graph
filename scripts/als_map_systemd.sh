# shellcheck shell=bash
# Shared safe helpers for systemd --user (als_map_stack / als_map_isolate).
#
# Hard rule after Raider GUI crash: NEVER kill/restart user systemd, session
# dbus, or hung `systemctl --user` children as "recovery". If the user bus is
# wedged (e.g. "Transport endpoint is not connected"), fail soft with a clear
# message and leave the session alone.
#
# Source from repo scripts:
#   # shellcheck source=als_map_systemd.sh
#   source "$(dirname "$0")/als_map_systemd.sh"

# Probe timeout for bus / is-system-running (seconds).
ALS_USER_BUS_PROBE_SEC="${ALS_USER_BUS_PROBE_SEC:-3}"
# Timeout for real systemctl --user mutations (seconds).
ALS_USER_SYSTEMCTL_TIMEOUT_SEC="${ALS_USER_SYSTEMCTL_TIMEOUT_SEC:-30}"

als_user_systemd_fail_msg() {
  cat >&2 <<'EOF'
als_map: user systemd bus unavailable or unresponsive.

Refusing to run systemctl --user (and refusing any kill/restart of
systemd --user, session dbus, or hung systemctl children).

Likely: "Transport endpoint is not connected" after a session glitch.
Recovery is a normal re-login or host reboot — not process kills.

Until the user bus is healthy again, skip stack restart / unit install /
isolated smoke on this host.
EOF
}

# True when text looks like a dead/wedged user bus.
als_user_bus_error_text() {
  local text="$1"
  case "$text" in
    *"Transport endpoint is not connected"*|*"Failed to connect to bus"*|*"Failed to connect to the bus"*|*"No medium found"*|*"Failed to get properties"*|*"Failed to connect"*)
      return 0
      ;;
  esac
  return 1
}

# Return 0 only if systemctl --user can actually talk to the user manager.
als_user_bus_ok() {
  local probe_sec="${ALS_USER_BUS_PROBE_SEC}"
  local out rc=0

  if command -v timeout >/dev/null 2>&1; then
    out="$(timeout "${probe_sec}s" systemctl --user is-system-running 2>&1)" || rc=$?
  else
    out="$(systemctl --user is-system-running 2>&1)" || rc=$?
  fi

  # timeout(1) → 124
  if [[ "$rc" -eq 124 ]]; then
    return 1
  fi
  if als_user_bus_error_text "$out"; then
    return 1
  fi
  # Only treat a live user manager as OK. "offline" means no user manager
  # (common in headless VMs) — fail soft rather than hang or kill.
  case "$out" in
    running*|degraded*|maintenance*|starting*)
      return 0
      ;;
  esac
  # offline / unknown / empty / unrecognized → not OK
  return 1
}

# Run systemctl --user with a hard timeout. Fails soft if bus is wedged.
# Never attempts kill/recovery. Exit 75 (EX_TEMPFAIL) on wedged bus / timeout.
# stdout from systemctl is passed through; stderr is scanned for bus errors.
als_user_systemctl() {
  if ! als_user_bus_ok; then
    als_user_systemd_fail_msg
    return 75
  fi
  local t="${ALS_USER_SYSTEMCTL_TIMEOUT_SEC}"
  local err_file rc=0 err
  err_file="$(mktemp)"

  if command -v timeout >/dev/null 2>&1; then
    timeout "${t}s" systemctl --user "$@" 2>"$err_file" || rc=$?
  else
    systemctl --user "$@" 2>"$err_file" || rc=$?
  fi
  err="$(cat "$err_file" 2>/dev/null || true)"
  rm -f "$err_file"
  if [[ -n "$err" ]]; then
    printf '%s\n' "$err" >&2
  fi

  if [[ "$rc" -eq 124 ]]; then
    echo "als_map: systemctl --user timed out after ${t}s (args: $*)." >&2
    echo "als_map: leaving hung systemctl alone — do NOT kill systemd --user / dbus." >&2
    return 75
  fi
  if als_user_bus_error_text "$err"; then
    als_user_systemd_fail_msg
    return 75
  fi
  return "$rc"
}
