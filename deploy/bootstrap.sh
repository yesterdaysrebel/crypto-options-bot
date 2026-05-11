#!/usr/bin/env bash
# One-shot VPS bootstrap for AWS Lightsail Mumbai (Ubuntu 24.04 LTS).
#
#   Run as root on a fresh Lightsail instance:
#     curl -fsSL https://raw.githubusercontent.com/<user>/crypto-options-bot/main/deploy/bootstrap.sh | bash
#   Or, after scp-ing the repo:
#     sudo bash deploy/bootstrap.sh
#
# Idempotent: re-running it tops up missing packages, refreshes the systemd unit, and
# preserves any existing /opt/crypto-options-bot/.env. NEVER overwrites secrets.
#
# Deploy manifests (docker-compose.prod.yml, deploy.sh, …) are NOT created here unless
# you either:
#   A) Set BOOTSTRAP_REPO_SLUG=owner/repo (and optional BOOTSTRAP_REPO_REF=main) so this
#      script can curl them from raw.githubusercontent.com, or
#   B) Copy them from your laptop (see final log lines if files are still missing).
#
# Example (public GitHub repo):
#   sudo BOOTSTRAP_REPO_SLUG=myuser/crypto-options-bot BOOTSTRAP_REPO_REF=main bash deploy/bootstrap.sh

set -euo pipefail

APP_USER=bot
APP_HOME=/opt/crypto-options-bot
SERVICE_NAME=crypto-options-bot

log() { echo "[$(date -Iseconds)] $*"; }

require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        echo "bootstrap.sh must run as root (use sudo)" >&2
        exit 1
    fi
}

install_packages() {
    log "apt: installing baseline packages"
    DEBIAN_FRONTEND=noninteractive apt-get update -y
    DEBIAN_FRONTEND=noninteractive apt-get upgrade -y
    DEBIAN_FRONTEND=noninteractive apt-get install -y \
        ca-certificates curl gnupg ufw fail2ban unattended-upgrades \
        logrotate git tzdata sqlite3 chrony jq

    if ! command -v docker >/dev/null 2>&1; then
        log "installing Docker Engine"
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
        chmod a+r /etc/apt/keyrings/docker.gpg
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
              https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
            > /etc/apt/sources.list.d/docker.list
        DEBIAN_FRONTEND=noninteractive apt-get update -y
        DEBIAN_FRONTEND=noninteractive apt-get install -y \
            docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        systemctl enable --now docker
    fi
}

create_user() {
    if ! id -u "${APP_USER}" >/dev/null 2>&1; then
        log "creating system user ${APP_USER}"
        useradd --system --create-home --home-dir "${APP_HOME}" \
                --shell /bin/bash "${APP_USER}"
    fi
    usermod -aG docker "${APP_USER}"
}

create_dirs() {
    log "ensuring ${APP_HOME} layout"
    install -d -o "${APP_USER}" -g "${APP_USER}" -m 0755 \
        "${APP_HOME}" \
        "${APP_HOME}/data" \
        "${APP_HOME}/data/snapshots" \
        "${APP_HOME}/reports" \
        "${APP_HOME}/journals" \
        "${APP_HOME}/logs" \
        "${APP_HOME}/runtime" \
        "${APP_HOME}/config" \
        "${APP_HOME}/releases"
    if [[ ! -f "${APP_HOME}/.env" ]]; then
        log "creating placeholder .env (chmod 600)"
        cat > "${APP_HOME}/.env" <<'ENV'
# Delta India API keys (required). Generate read-write keys with TRADING enabled.
DELTA_API_KEY=
DELTA_API_SECRET=

# Per-strategy live-trading gates (default OFF; bot still runs in dry-run mode).
GO_LIVE_DIRECTIONAL=false
GO_LIVE_IRON_CONDOR=false
GO_LIVE_VOL_STRANGLE=false

# Logging / persistence
LOG_LEVEL=INFO
DB_URL=sqlite:////app/data/bot.sqlite
ENV
        chmod 600 "${APP_HOME}/.env"
        chown "${APP_USER}:${APP_USER}" "${APP_HOME}/.env"
    fi
}

install_logrotate() {
    log "installing /etc/logrotate.d/${SERVICE_NAME}"
    cat > "/etc/logrotate.d/${SERVICE_NAME}" <<EOF
${APP_HOME}/logs/*.log {
    daily
    rotate 14
    missingok
    compress
    delaycompress
    notifempty
    copytruncate
    create 0640 ${APP_USER} ${APP_USER}
}
EOF
}

install_systemd() {
    local unit_file="/etc/systemd/system/${SERVICE_NAME}.service"
    log "installing ${unit_file}"
    cat > "${unit_file}" <<EOF
[Unit]
Description=Crypto Options Bot (Delta India)
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_HOME}
EnvironmentFile=-${APP_HOME}/.env
ExecStartPre=/usr/bin/docker compose -f ${APP_HOME}/docker-compose.prod.yml pull
ExecStart=/usr/bin/docker compose -f ${APP_HOME}/docker-compose.prod.yml up --remove-orphans
ExecStop=/usr/bin/docker compose -f ${APP_HOME}/docker-compose.prod.yml down
Restart=on-failure
RestartSec=10
TimeoutStopSec=120
KillSignal=SIGTERM
# Hardening
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}.service" || true
}

configure_firewall() {
    log "configuring UFW (allow SSH only)"
    ufw --force reset
    ufw default deny incoming
    ufw default allow outgoing
    ufw allow OpenSSH
    yes | ufw enable
}

configure_unattended_upgrades() {
    log "enabling unattended-upgrades"
    dpkg-reconfigure -fnoninteractive unattended-upgrades
}

ensure_timezone() {
    log "setting timezone to Asia/Kolkata"
    timedatectl set-timezone Asia/Kolkata || true
    systemctl enable --now chrony
}

fetch_deploy_manifests_from_github() {
    # Optional: pull compose + helper scripts onto a fresh VPS (no git clone, no CI yet).
    local slug="${BOOTSTRAP_REPO_SLUG:-}"
    local ref="${BOOTSTRAP_REPO_REF:-main}"
    if [[ -z "${slug}" ]]; then
        return 0
    fi
    local base="https://raw.githubusercontent.com/${slug}/${ref}"
    log "fetching deploy manifests from ${base}/deploy/"
    install -d -o "${APP_USER}" -g "${APP_USER}" -m 0755 "${APP_HOME}"
    local tmp
    tmp="$(mktemp -d)"
    trap 'rm -rf "${tmp}"' RETURN
    for rel in \
        deploy/docker-compose.prod.yml \
        deploy/deploy.sh \
        deploy/health-check.sh \
        deploy/rollback.sh; do
        local name="${rel#deploy/}"
        if curl -fsSL "${base}/${rel}" -o "${tmp}/${name}"; then
            install -m 0644 -o "${APP_USER}" -g "${APP_USER}" "${tmp}/${name}" "${APP_HOME}/${name}"
            log "  wrote ${APP_HOME}/${name}"
        else
            log "ERROR: failed to download ${base}/${rel}"
            return 1
        fi
    done
    chmod 0755 "${APP_HOME}/deploy.sh" "${APP_HOME}/health-check.sh" "${APP_HOME}/rollback.sh"

    install -d -o "${APP_USER}" -g "${APP_USER}" -m 0755 \
        "${APP_HOME}/config/strategies"
    mkdir -p "${tmp}/config/strategies"
    for rel in \
        config/global.yaml \
        config/strategies/directional.yaml \
        config/strategies/iron_condor.yaml \
        config/strategies/vol_strangle.yaml; do
        if curl -fsSL "${base}/${rel}" -o "${tmp}/${rel}"; then
            install -m 0644 -o "${APP_USER}" -g "${APP_USER}" "${tmp}/${rel}" "${APP_HOME}/${rel}"
            log "  wrote ${APP_HOME}/${rel}"
        else
            log "ERROR: failed to download ${base}/${rel}"
            return 1
        fi
    done
}

warn_if_deploy_files_missing() {
    if [[ -f "${APP_HOME}/docker-compose.prod.yml" ]]; then
        return 0
    fi
    echo "" >&2
    log "WARNING: ${APP_HOME}/docker-compose.prod.yml is missing — systemd will fail until you add deploy files."
    echo "" >&2
    echo "  Option A — re-bootstrap with GitHub slug (public repo):" >&2
    echo "    sudo BOOTSTRAP_REPO_SLUG=YOUR_GITHUB_USER/crypto-options-bot BOOTSTRAP_REPO_REF=main bash deploy/bootstrap.sh" >&2
    echo "" >&2
    echo "  Option B — from your laptop (repo checkout); replace USER and HOST:" >&2
    echo "    scp deploy/docker-compose.prod.yml deploy/deploy.sh deploy/health-check.sh deploy/rollback.sh USER@HOST:/tmp/" >&2
    echo "    ssh USER@HOST 'sudo install -o ${APP_USER} -g ${APP_USER} -m 0644 /tmp/docker-compose.prod.yml ${APP_HOME}/docker-compose.prod.yml'" >&2
    echo "    ssh USER@HOST 'for f in deploy.sh health-check.sh rollback.sh; do sudo install -o ${APP_USER} -g ${APP_USER} -m 0755 /tmp/\${f} ${APP_HOME}/\${f}; done'" >&2
    echo "    rsync -av --mkpath ./config/ USER@HOST:/tmp/config-stage/ && ssh USER@HOST \"sudo rsync -a /tmp/config-stage/ ${APP_HOME}/config/\"" >&2
    echo "" >&2
}

main() {
    require_root
    install_packages
    create_user
    create_dirs
    fetch_deploy_manifests_from_github
    install_logrotate
    install_systemd
    configure_firewall
    configure_unattended_upgrades
    ensure_timezone
    warn_if_deploy_files_missing
    log "bootstrap complete. Next steps:"
    log "  1) Edit ${APP_HOME}/.env with your Delta India API keys"
    log "  2) If config/ is empty, rsync ./config/ from your laptop to ${APP_HOME}/config/"
    log "  3) docker login ghcr.io (if image is private), then: systemctl start ${SERVICE_NAME}.service"
}

main "$@"
