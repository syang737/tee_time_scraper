#!/usr/bin/env bash
#
# One-shot deployment for the tee time watcher.
#
#   ./deploy/setup.sh golf.example.com      # install, or update an install
#   ./deploy/setup.sh --check               # verify without changing anything
#
# Safe to re-run: it never overwrites an existing .env, and re-running after
# a git pull is the supported way to update.
#
# What it does NOT do: open the Lightsail firewall. That lives in the AWS
# console, and the script tells you when it is the thing standing in the way.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SUDO_USER:-$(id -un)}"
SERVICE_NAME="teetimes"
APP_PORT=8000
ENV_FILE="$REPO_DIR/.env"

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
info()  { printf '  %s\n' "$*"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '  \033[33m!\033[0m %s\n' "$*"; }
fail()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
die()   { printf '\n\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------

wait_for_apt() {
    # Fresh cloud instances run unattended-upgrades on first boot. Waiting is
    # the only safe option: removing the lock or killing the process can
    # leave dpkg half-configured.
    if sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; then
        info "apt is busy (probably first-boot updates); waiting..."
        while sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
            sleep 5
        done
    fi
}

apt_install() {
    wait_for_apt
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" >/dev/null
}

generate_secret() {
    python3 -c "import secrets; print(secrets.token_urlsafe(${1:-32}))"
}

# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

run_checks() {
    local failures=0

    bold "Checking deployment"

    if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
        ok "$SERVICE_NAME is running"
    else
        fail "$SERVICE_NAME is not running (journalctl -u $SERVICE_NAME -n 30)"
        failures=$((failures + 1))
    fi

    # The environment a process was started with never changes, so this is
    # what the app is actually using -- not what .env currently says.
    local pid
    pid="$(systemctl show -p MainPID --value "$SERVICE_NAME" 2>/dev/null || echo 0)"
    if [[ "$pid" != "0" ]] && sudo test -r "/proc/$pid/environ"; then
        local running_pw
        running_pw="$(sudo tr '\0' '\n' < "/proc/$pid/environ" \
            | sed -n 's/^GUI_PASSWORD=//p')"
        if [[ -z "$running_pw" ]]; then
            fail "the running process has an empty GUI_PASSWORD -- nobody can log in"
            failures=$((failures + 1))
        elif [[ -f "$ENV_FILE" ]] \
             && ! grep -qxF "GUI_PASSWORD=$running_pw" "$ENV_FILE"; then
            fail "the running GUI_PASSWORD differs from .env -- restart to pick it up"
            info "  sudo systemctl restart $SERVICE_NAME"
            failures=$((failures + 1))
        else
            ok "GUI_PASSWORD in the running process matches .env"
        fi
    fi

    # Dependencies drift when requirements.txt gains an entry and only
    # --check is re-run; --check installs nothing, so say so rather than
    # letting the app fail later for a reason that looks unrelated.
    if [[ -x "$REPO_DIR/.venv/bin/python" ]]; then
        local missing=""
        while read -r module; do
            "$REPO_DIR/.venv/bin/python" -c "import $module" 2>/dev/null \
                || missing="$missing $module"
        done < <(printf '%s\n' fastapi httpx curl_cffi)
        if [[ -n "$missing" ]]; then
            fail "missing from the venv:$missing"
            info "  ./deploy/setup.sh ${DOMAIN:-<your-domain>}   # installs them"
            failures=$((failures + 1))
        else
            ok "python dependencies are installed"
        fi
    fi

    if curl -fsS --max-time 5 "http://127.0.0.1:$APP_PORT/healthz" >/dev/null 2>&1; then
        ok "app answers on 127.0.0.1:$APP_PORT"
    else
        fail "app is not answering on 127.0.0.1:$APP_PORT"
        failures=$((failures + 1))
    fi

    local last_error
    last_error="$(curl -fsS --max-time 5 "http://127.0.0.1:$APP_PORT/healthz" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("last_error") or "")' 2>/dev/null || true)"
    if [[ -n "$last_error" ]]; then
        warn "poller reports: $last_error"
    else
        ok "poller has no errors"
    fi

    if systemctl is-active --quiet caddy 2>/dev/null; then
        ok "caddy is running"
    else
        warn "caddy is not running (fine if you are not using a domain yet)"
    fi

    if [[ -n "${DOMAIN:-}" ]]; then
        if curl -fsS --max-time 10 "https://$DOMAIN/healthz" >/dev/null 2>&1; then
            ok "https://$DOMAIN answers"
        else
            fail "https://$DOMAIN did not answer"
            info "  DNS pointing here?   dig +short $DOMAIN"
            info "  ports 80+443 open in the Lightsail firewall?"
            info "  certificate issued?  journalctl -u caddy -n 30"
            failures=$((failures + 1))
        fi
    fi

    echo
    if (( failures )); then
        die "$failures check(s) failed."
    fi
    bold "All checks passed."
}

# ---------------------------------------------------------------------------
# install steps
# ---------------------------------------------------------------------------

install_packages() {
    bold "Installing packages"
    wait_for_apt
    sudo apt-get update -qq
    apt_install python3-venv python3-pip git curl ca-certificates
    ok "python, git, curl"

    if ! command -v caddy >/dev/null 2>&1; then
        # Caddy is not in Ubuntu's default repositories.
        apt_install debian-keyring debian-archive-keyring apt-transport-https
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
            | sudo gpg --batch --yes --dearmor \
              -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
        curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
            | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
        wait_for_apt
        sudo apt-get update -qq
        apt_install caddy
        ok "caddy installed"
    else
        ok "caddy already installed"
    fi
}

install_python_env() {
    bold "Installing the application"
    if [[ ! -d "$REPO_DIR/.venv" ]]; then
        python3 -m venv "$REPO_DIR/.venv"
        ok "created .venv"
    fi
    "$REPO_DIR/.venv/bin/pip" install -q --upgrade pip
    "$REPO_DIR/.venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"
    ok "dependencies installed"
}

write_env() {
    bold "Configuring"
    if [[ -f "$ENV_FILE" ]]; then
        ok ".env already exists (left untouched)"
        if ! grep -q '^GUI_PASSWORD=.\+' "$ENV_FILE"; then
            warn "GUI_PASSWORD in .env is empty -- nobody will be able to log in"
        fi
        return
    fi

    local password secret topic
    password="$(generate_secret 12)"
    secret="$(generate_secret 32)"
    topic="teetimes-$(generate_secret 9 | tr -d '_-' | cut -c1-12)"

    cat > "$ENV_FILE" <<EOF
# Generated by deploy/setup.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ).
# This file is read only when the service starts:
#   sudo systemctl restart $SERVICE_NAME
GUI_PASSWORD=$password
SECRET_KEY=$secret
NTFY_TOPIC=$topic
NTFY_SERVER=https://ntfy.sh
POLL_INTERVAL_SECONDS=30
RENOTIFY_AFTER_MINUTES=10
REQUEST_DELAY_SECONDS=0.5
RETENTION_DAYS=5
DB_PATH=$REPO_DIR/teetimes.db
COOKIE_SECURE=${DOMAIN:+true}
EOF
    chmod 600 "$ENV_FILE"
    ok "wrote .env (permissions 600)"

    NEW_CREDENTIALS=1
    NEW_PASSWORD="$password"
    NEW_TOPIC="$topic"
}

install_service() {
    bold "Installing the service"
    # Render the checked-in unit for wherever the repo actually lives, rather
    # than the /home/ubuntu it is written against.
    sed -e "s|^User=.*|User=$SERVICE_USER|" \
        -e "s|/home/ubuntu/tee_time_scraper|$REPO_DIR|g" \
        "$REPO_DIR/deploy/teetimes.service" \
        | sudo tee "/etc/systemd/system/$SERVICE_NAME.service" >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable --quiet "$SERVICE_NAME"
    sudo systemctl restart "$SERVICE_NAME"
    ok "$SERVICE_NAME enabled and (re)started"
}

install_caddy_site() {
    [[ -z "${DOMAIN:-}" ]] && return 0
    bold "Configuring HTTPS for $DOMAIN"

    local resolved public
    resolved="$(dig +short "$DOMAIN" 2>/dev/null | tail -1)"
    public="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
    if [[ -z "$resolved" ]]; then
        warn "$DOMAIN does not resolve yet -- add an A record to $public"
        warn "certificate issuance will fail until it does"
    elif [[ -n "$public" && "$resolved" != "$public" ]]; then
        warn "$DOMAIN resolves to $resolved but this instance is $public"
    else
        ok "$DOMAIN resolves here"
    fi

    sudo mkdir -p /var/log/caddy
    sed "s|^golf\.potpourri\.lol|$DOMAIN|" "$REPO_DIR/deploy/Caddyfile" \
        | sudo tee /etc/caddy/Caddyfile >/dev/null
    sudo systemctl enable --quiet caddy
    sudo systemctl reload caddy 2>/dev/null || sudo systemctl restart caddy
    ok "caddy configured for $DOMAIN"
}

cap_journal() {
    # The poller runs forever; keep logs from ever filling a small disk.
    sudo mkdir -p /etc/systemd/journald.conf.d
    printf '[Journal]\nSystemMaxUse=200M\n' \
        | sudo tee /etc/systemd/journald.conf.d/size.conf >/dev/null
    sudo systemctl restart systemd-journald
    ok "journal capped at 200M"
}

# ---------------------------------------------------------------------------

DOMAIN=""
NEW_CREDENTIALS=0
CHECK_ONLY=0

case "${1:-}" in
    --check) CHECK_ONLY=1; DOMAIN="${2:-}" ;;
    -h|--help|"")
        cat <<EOF
Usage:
  ./deploy/setup.sh <domain>     install or update, and serve it over HTTPS
  ./deploy/setup.sh localhost    install without a domain (plain HTTP on :$APP_PORT)
  ./deploy/setup.sh --check      verify an existing install, change nothing

Re-run after a git pull to update.
EOF
        exit 0 ;;
    localhost) DOMAIN="" ;;
    -*) die "unknown option: $1" ;;
    *)  DOMAIN="$1" ;;
esac

if (( CHECK_ONLY )); then
    run_checks
    exit 0
fi

[[ -f "$REPO_DIR/requirements.txt" ]] || die "run this from inside the repo"
command -v sudo >/dev/null || die "sudo is required"

bold "Deploying from $REPO_DIR as $SERVICE_USER"
echo

install_packages
install_python_env
write_env
cap_journal
install_service
install_caddy_site

echo
sleep 3   # let the service finish starting before judging it
run_checks

if (( NEW_CREDENTIALS )); then
    echo
    bold "Save these now -- they are not shown again"
    echo
    info "URL:        ${DOMAIN:+https://$DOMAIN}${DOMAIN:-http://127.0.0.1:$APP_PORT}"
    info "Password:   $NEW_PASSWORD"
    info "ntfy topic: $NEW_TOPIC"
    echo
    info "Subscribe to that topic in the ntfy app on your phone, then use the"
    info "dashboard's 'Send test notification' button to confirm it arrives."
    info "They are stored in $ENV_FILE if you lose them."
fi

if [[ -n "$DOMAIN" ]]; then
    echo
    info "If the site is unreachable, open ports 80 and 443 in the Lightsail"
    info "firewall (Networking -> IPv4 Firewall). Port 80 is required for"
    info "certificate issuance. Leave $APP_PORT closed."
fi
