#!/usr/bin/env bash
# Operator validation: container, HTTP health/metrics, recent logs, SQLite, optional CLI.
# Intended to run ON the VPS (or any host with docker + the same paths).
#
# Environment (all optional):
#   APP_DIR        — app root (default: /opt/crypto-options-bot)
#   CONTAINER      — docker container name (default: crypto-options-bot)
#   HEALTH_URL     — default: http://127.0.0.1:9091/health
#   METRICS_URL    — default: http://127.0.0.1:9091/metrics
#   LOG_SINCE      — docker logs --since (default: 30m)
#   STRICT_MARKS   — if set to 1, fail when /health "marks" is empty (default: warn only)
#   SKIP_SQLITE    — if 1, skip DB checks
#   SKIP_CLI       — if 1, skip `python -m bot.cli status`
#   SKIP_IPIFY     — if 1, skip outbound IP check

set -u

APP_DIR="${APP_DIR:-/opt/crypto-options-bot}"
CONTAINER="${CONTAINER:-crypto-options-bot}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:9091/health}"
METRICS_URL="${METRICS_URL:-http://127.0.0.1:9091/metrics}"
LOG_SINCE="${LOG_SINCE:-30m}"
STRICT_MARKS="${STRICT_MARKS:-0}"
SKIP_SQLITE="${SKIP_SQLITE:-0}"
SKIP_CLI="${SKIP_CLI:-0}"
SKIP_IPIFY="${SKIP_IPIFY:-0}"

PASS=0
FAIL=0
WARN=0

pass() { echo "[ok]   $*"; PASS=$((PASS + 1)); }
fail() { echo "[FAIL] $*" >&2; FAIL=$((FAIL + 1)); }
warn() { echo "[warn] $*" >&2; WARN=$((WARN + 1)); }

need_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        fail "missing command: $1"
        return 1
    fi
    return 0
}

json_get() {
    # args: json_string key -> prints value or empty
    local json="$1"
    local key="$2"
    if command -v jq >/dev/null 2>&1; then
        echo "${json}" | jq -r ".${key} // empty" 2>/dev/null || true
        return 0
    fi
    python3 -c "import json,sys; d=json.loads(sys.argv[1]); v=d.get(sys.argv[2]); print('' if v is None else (json.dumps(v) if isinstance(v,(dict,list)) else v))" "${json}" "${key}" 2>/dev/null || true
}

echo "=== crypto-options-bot runtime validation ==="
echo "APP_DIR=${APP_DIR} CONTAINER=${CONTAINER}"
echo

need_cmd docker || true
need_cmd curl || true

# --- Docker ---
state=$(docker inspect -f '{{.State.Status}}' "${CONTAINER}" 2>/dev/null || echo missing)
if [[ "${state}" == "running" ]]; then
    pass "container ${CONTAINER} state=running"
else
    fail "container ${CONTAINER} state=${state} (expected running)"
fi

hstat=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${CONTAINER}" 2>/dev/null || echo missing)
if [[ "${hstat}" == "healthy" ]]; then
    pass "docker health=${hstat}"
elif [[ "${hstat}" == "none" ]]; then
    warn "docker has no Healthcheck; relying on /health only"
else
    warn "docker health=${hstat} (expected healthy when healthcheck exists)"
fi

# --- HTTP /health ---
if ! hb=$(curl -fsS "${HEALTH_URL}" 2>/dev/null); then
    fail "GET ${HEALTH_URL} (non-200 or connection error)"
else
    pass "GET ${HEALTH_URL} -> 200"
    st=$(json_get "${hb}" status)
    st_norm=${st//$'\r'/}
    st_norm=${st_norm//$'\n'/}
    st_norm=${st_norm//\"/}
    if [[ "${st_norm}" == "ok" ]]; then
        pass "/health status ok"
    else
        fail "/health status unexpected: ${st}"
    fi

    ws=$(json_get "${hb}" ws_connected)
    ws_norm=${ws//\"/}
    if [[ "${ws_norm}" == "true" ]] || [[ "${ws}" == "True" ]]; then
        pass "/health ws_connected=true"
    else
        fail "/health ws_connected=${ws}"
    fi

    chain=$(json_get "${hb}" chain_instruments)
    chain_norm=${chain//\"/}
    if [[ -n "${chain_norm}" ]] && [[ "${chain_norm}" =~ ^[0-9]+$ ]] && (( chain_norm > 0 )); then
        pass "/health chain_instruments=${chain_norm} (>0)"
    else
        fail "/health chain_instruments invalid or zero: ${chain}"
    fi

    marks_raw=$(json_get "${hb}" marks)
    marks_empty=0
    if [[ -z "${marks_raw}" ]] || [[ "${marks_raw}" == "{}" ]] || echo "${marks_raw}" | grep -qE '^\{\s*\}$'; then
        marks_empty=1
    fi
    if (( marks_empty )); then
        if [[ "${STRICT_MARKS}" == "1" ]]; then
            fail "/health marks is empty (STRICT_MARKS=1)"
        else
            warn "/health marks is empty — directional/vol may lack spot until WS fields are fixed/deployed"
        fi
    else
        pass "/health marks non-empty"
    fi
fi

# --- HTTP /metrics ---
if ! mb=$(curl -fsS "${METRICS_URL}" 2>/dev/null | head -c 8192); then
    fail "GET ${METRICS_URL}"
else
    pass "GET ${METRICS_URL} -> body received"
    if echo "${mb}" | grep -q '^bot_ticks_total'; then
        pass "metrics include bot_ticks_total"
    else
        fail "metrics missing bot_ticks_total"
    fi
fi

# --- Recent logs: auth / wallet IP errors ---
bad_lines=$(docker logs "${CONTAINER}" --since "${LOG_SINCE}" 2>&1 | grep -iE '401|403|ip_not_whitelisted|wallet balances unavailable' || true)
if [[ -n "${bad_lines}" ]]; then
    fail "log lines in last ${LOG_SINCE} matched auth/IP/wallet errors (showing up to 5):"
    echo "${bad_lines}" | head -5 >&2
else
    pass "no 401/403/ip_not_whitelisted/wallet-unavailable patterns in last ${LOG_SINCE}"
fi

# --- Outbound IP (informational) ---
if [[ "${SKIP_IPIFY}" != "1" ]] && command -v curl >/dev/null 2>&1; then
    if pub=$(curl -fsS --connect-timeout 5 https://api.ipify.org 2>/dev/null); then
        pass "outbound public IP (for Delta whitelist): ${pub}"
    else
        warn "could not reach api.ipify.org (offline?)"
    fi
fi

# --- Files on host ---
logf="${APP_DIR}/logs/bot.log"
if [[ -f "${logf}" ]] || [[ -r "${logf}" ]]; then
    pass "log file exists: ${logf}"
else
    warn "log file missing or unreadable: ${logf}"
fi

promf="${APP_DIR}/runtime/metrics/bot.prom"
if [[ -f "${promf}" ]]; then
    pass "prometheus textfile exists: ${promf}"
else
    warn "prometheus textfile missing (optional): ${promf}"
fi

# --- SQLite ---
db="${APP_DIR}/data/bot.sqlite"
if [[ "${SKIP_SQLITE}" != "1" ]]; then
    if ! command -v sqlite3 >/dev/null 2>&1; then
        warn "sqlite3 not installed; skipping DB checks"
    elif [[ ! -f "${db}" ]]; then
        warn "database file missing: ${db}"
    else
        dc=$(sqlite3 "${db}" "SELECT COUNT(*) FROM decisions;" 2>/dev/null || echo 0)
        if [[ "${dc}" =~ ^[0-9]+$ ]] && (( dc >= 0 )); then
            pass "decisions row count: ${dc}"
            if (( dc == 0 )); then
                warn "decisions table is empty (bot may be brand new or not ticking)"
            fi
        else
            fail "could not read decisions count"
        fi
        ic=$(sqlite3 "${db}" "SELECT COUNT(*) FROM instruments;" 2>/dev/null || echo 0)
        if [[ "${ic}" =~ ^[0-9]+$ ]] && (( ic > 0 )); then
            pass "instruments row count: ${ic} (>0)"
        else
            warn "instruments count low or unreadable: ${ic}"
        fi
    fi
fi

# --- CLI status (non-interactive) ---
if [[ "${SKIP_CLI}" != "1" ]]; then
    if out=$(docker exec "${CONTAINER}" python -m bot.cli status 2>&1 </dev/null); then
        pass "docker exec bot.cli status exited 0"
    else
        fail "docker exec bot.cli status failed"
        echo "${out}" | tail -15 >&2
    fi
fi

echo
echo "=== summary: ${PASS} passed, ${WARN} warnings, ${FAIL} failed ==="
if (( FAIL > 0 )); then
    exit 1
fi
exit 0
