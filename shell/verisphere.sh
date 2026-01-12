#!/usr/bin/env bash
# ============================================================
# VeriSphere unified service controller (SAFE TO SOURCE)
# ============================================================

# SAFETY: never abort interactive shell
set +e
set +o errexit
set +o nounset
set +o pipefail

VS_ROOT="${VS_ROOT:-$HOME/verisphere}"

# ------------------------------------------------------------
# helpers
# ------------------------------------------------------------
_vsx_err()  { echo "❌ $*"; }
_vsx_info() { echo "ℹ️  $*"; }
_vsx_ok()   { echo "✅ $*"; }

_run_in_dir() {
  local dir="$1"; shift
  [[ -d "$dir" ]] || { _vsx_err "dir not found: $dir"; return 1; }
  ( cd "$dir" && "$@" )
}

_pids_on_port() {
  local port="$1"

  if command -v lsof >/dev/null 2>&1; then
    # IPv4 + IPv6
    lsof -ti :"$port" -sTCP:LISTEN 2>/dev/null
    return 0
  fi

  if command -v ss >/dev/null 2>&1; then
    # Extract PIDs from ss output (IPv4 + IPv6)
    ss -ltnp "( sport = :$port )" 2>/dev/null \
      | awk -F'pid=' '{print $2}' \
      | awk -F',' '{print $1}' \
      | sort -u
    return 0
  fi

  return 1
}

_kill_port() {
  local port="$1"
  local pids
  pids="$(_pids_on_port "$port")"
  if [[ -n "$pids" ]]; then
    _vsx_info "killing processes on port $port: $pids"
    kill $pids >/dev/null 2>&1
    sleep 0.3
  fi
}

_pid_alive() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1
}

_read_pidfile() {
  local f="$1"
  [[ -f "$f" ]] || return 1
  local pid
  pid="$(cat "$f" 2>/dev/null)"
  [[ -n "$pid" ]] || return 1
  echo "$pid"
}

_resolve_service() {
  [[ -n "${1:-}" ]] && { echo "$1"; return 0; }
  case "$PWD" in
    */frontend*) echo frontend ;;
    */app*)      echo app ;;
    */protocol*) echo protocol ;;
    *) _vsx_err "Cannot infer service; pass name explicitly"; return 1 ;;
  esac
}

# ============================================================
# Frontend (Vite)
# ============================================================
_frontend_start() {
  local dir="$VS_ROOT/frontend"
  local port="${VSF_PORT:-5173}"
  local pidfile="$dir/.vs-frontend.pid"
  local logfile="$dir/.vs-frontend.log"

  _kill_port "$port"

  _run_in_dir "$dir" bash -lc '
    command -v npm >/dev/null || exit 1
    [[ -d node_modules ]] || npm install --legacy-peer-deps || exit 1
    rm -f "'"$pidfile"'" "'"$logfile"'"
    nohup npm run dev -- --host 0.0.0.0 --port "'"$port"'" >"'"$logfile"'" 2>&1 &
    echo $! > "'"$pidfile"'"
  ' || return 1

  sleep 0.4
  _vsx_ok "frontend started"
  _vsx_info "http://localhost:$port"
}

_frontend_kill() {
  local dir="$VS_ROOT/frontend"
  local port="${VSF_PORT:-5173}"
  local pidfile="$dir/.vs-frontend.pid"

  local pid="$(_read_pidfile "$pidfile")"
  [[ -n "$pid" ]] && _pid_alive "$pid" && kill "$pid" >/dev/null 2>&1

  _kill_port "$port"
  rm -f "$pidfile"

  _vsx_info "frontend stopped"
}

_frontend_show() {
  local port="${VSF_PORT:-5173}"
  local pids

  pids="$(_pids_on_port "$port")"
  if [[ -n "$pids" ]]; then
    _vsx_ok "frontend running (pids: $pids)"
    _vsx_info "http://localhost:$port"
  else
    _vsx_info "frontend not running"
  fi
}

_frontend_test() {
  _run_in_dir "$VS_ROOT/frontend" bash -lc 'npm test'
}

# ============================================================
# App (FastAPI)
# ============================================================
_app_start() {
  local dir="$VS_ROOT/app"
  local port="${VSA_PORT:-8070}"
  local pidfile="$dir/.vs-app.pid"
  local logfile="$dir/.vs-app.log"

  _kill_port "$port"

  _run_in_dir "$dir" bash -lc '
    [[ -d .venv ]] || python3 -m venv .venv || exit 1
    . .venv/bin/activate || exit 1
    pip install -r requirements.txt || exit 1
    rm -f "'"$pidfile"'" "'"$logfile"'"
    nohup uvicorn app.main:app --host 0.0.0.0 --port "'"$port"'" >"'"$logfile"'" 2>&1 &
    echo $! > "'"$pidfile"'"
  ' || return 1

  sleep 0.4
  _vsx_ok "app started"
  _vsx_info "http://localhost:$port/healthz"
}

_app_kill() {
  local dir="$VS_ROOT/app"
  local port="${VSA_PORT:-8070}"
  local pidfile="$dir/.vs-app.pid"

  local pid="$(_read_pidfile "$pidfile")"
  [[ -n "$pid" ]] && _pid_alive "$pid" && kill "$pid" >/dev/null 2>&1

  _kill_port "$port"
  rm -f "$pidfile"

  _vsx_info "app stopped"
}

_app_show() {
  local port="${VSA_PORT:-8070}"
  local pids

  pids="$(_pids_on_port "$port")"
  if [[ -n "$pids" ]]; then
    _vsx_ok "app running (pids: $pids)"
    _vsx_info "http://localhost:$port/healthz"
  else
    _vsx_info "app not running"
  fi
}

_app_test() {
  _run_in_dir "$VS_ROOT/app" bash -lc '
    . .venv/bin/activate || exit 1
    pytest -q
  '
}

# ============================================================
# Protocol
# ============================================================
_protocol_start() { _vsx_info "protocol is a library"; }
_protocol_kill()  { _vsx_info "protocol has no runtime"; }
_protocol_show()  { _vsx_info "protocol is a library"; }
_protocol_test()  { _run_in_dir "$VS_ROOT/protocol" bash -lc 'npm test'; }

# ============================================================
# Dispatch
# ============================================================
startsvc() { "_$(_resolve_service "${1:-}")_start"; }
killsvc()  { "_$(_resolve_service "${1:-}")_kill"; }
showsvc()  { "_$(_resolve_service "${1:-}")_show"; }
testsvc()  { "_$(_resolve_service "${1:-}")_test"; }

