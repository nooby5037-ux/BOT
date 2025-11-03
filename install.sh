#!/bin/bash
# MADE BY GG , BOT INSTALLER
set -euo pipefail

# ---------------- Colors & helpers ----------------
ESC=$(printf '\033')
RED="${ESC}[31m"
GREEN="${ESC}[32m"
YELLOW="${ESC}[33m"
CYAN="${ESC}[36m"
BOLD="${ESC}[1m"
RESET="${ESC}[0m"

info() { printf "\n${CYAN}%s${RESET}\n" "$1"; }
ok()   { printf "${GREEN}✔ %s${RESET}\n" "$1"; }
warn() { printf "${YELLOW}⚠ %s${RESET}\n" "$1"; }
err()  { printf "${RED}✖ %s${RESET}\n" "$1"; }

# Typing effect (for banner)
type_out() {
  local s="$1"
  local delay=${2:-0.02}
  for ((i=0;i<${#s};i++)); do
    printf "%s" "${s:$i:1}"
    sleep "$delay"
  done
  printf "\n"
}

# Spinner for background actions
spinner() {
  local pid=$1
  local msg=$2
  local spin='/-\|'
  printf "  %s " "$msg"
  while kill -0 "$pid" 2>/dev/null; do
    for i in $(seq 0 3); do
      printf "\b%s" "${spin:$i:1}"
      sleep 0.12
    done
  done
  printf "\b"
  printf " ${GREEN}done${RESET}\n"
}

# Dots loader simple
dots() {
  local msg="$1"
  printf "%s" "$msg"
  for i in {1..4}; do
    printf "."
    sleep 0.45
  done
  printf " "
}

# ---------------- Banner ----------------
clear
printf "${BOLD}${CYAN}"
type_out "  ███╗   ███╗ █████╗ ██████╗     ____  ____  ____  " 0.003
type_out "  ████╗ ████║██╔══██╗██╔══██╗   / ___||  _ \\|  _ \\ " 0.003
type_out "  ██╔████╔██║███████║██████╔╝   \\___ \\| |_) | |_) |" 0.003
type_out "  ██║╚██╔╝██║██╔══██║██╔══██╗    ___) |  __/|  __/ " 0.003
type_out "  ██║ ╚═╝ ██║██║  ██║██║  ██║   |____/|_|   |_|    " 0.003
printf "${RESET}\n"
printf "${BOLD}${YELLOW}            MADE BY GG , BOT INSTALLER${RESET}\n\n"
sleep 0.6

# ---------------- Pre-checks & update ----------------
info "System check & preparing (apt update + install essentials)."

# Run apt update/upgrade in background while showing spinner
{
  sudo apt update -y >/dev/null 2>&1
  sudo apt upgrade -y >/dev/null 2>&1
  sudo apt install -y python3 python3-venv python3-pip git curl wget build-essential >/dev/null 2>&1
} &
spinner $! "Updating system and installing essentials"

ok "System updated and essentials installed"

# ---------------- File checks ----------------
SCRIPT_DIR="$(pwd)"
REQ_FILE="$SCRIPT_DIR/requirements.txt"
BOT_FILE="$SCRIPT_DIR/bot.py"
ENV_TEMPLATE="$SCRIPT_DIR/env.txt"
ENV_FILE="$SCRIPT_DIR/.env"

if [ ! -f "$BOT_FILE" ]; then
  err "Missing bot.py in current directory ($SCRIPT_DIR). Ab yeh folder mein bot.py daal."
  exit 1
fi
if [ ! -f "$REQ_FILE" ]; then
  warn "requirements.txt nahi mila — agar koi dependencies hain toh .env create karne ke baad manually install kar."
fi

# ---------------- Ask user for token/admin ----------------
echo
type_out "${BOLD}Step:${RESET} Enter bot credentials (token & admin id) — jaldi kro, bas ek line type karo."
read -rp "👉 Enter your Discord Bot Token: " DISCORD_TOKEN
read -rp "👉 Enter Admin ID(s) (comma-separated): " ADMIN_IDS

# ---------------- Create .env ----------------
info "Creating .env file from template..."
# If env.txt exists, try to preserve other keys — replace DISCORD_TOKEN and ADMIN_IDS
if [ -f "$ENV_TEMPLATE" ]; then
  # Make a copy then replace or append
  cp "$ENV_TEMPLATE" "$ENV_FILE"
  # replace/insert DISCORD_TOKEN and ADMIN_IDS
  if grep -qE '^DISCORD_TOKEN=' "$ENV_FILE"; then
    sed -i "s~^DISCORD_TOKEN=.*~DISCORD_TOKEN=${DISCORD_TOKEN}~g" "$ENV_FILE"
  else
    printf "\nDISCORD_TOKEN=%s\n" "$DISCORD_TOKEN" >> "$ENV_FILE"
  fi
  if grep -qE '^ADMIN_IDS=' "$ENV_FILE"; then
    sed -i "s~^ADMIN_IDS=.*~ADMIN_IDS=${ADMIN_IDS}~g" "$ENV_FILE"
  else
    printf "ADMIN_IDS=%s\n" "$ADMIN_IDS" >> "$ENV_FILE"
  fi
else
  cat > "$ENV_FILE" <<EOF
# --- Discord Bot Configuration ---
DISCORD_TOKEN=${DISCORD_TOKEN}

# --- Admin Settings ---
ADMIN_IDS=${ADMIN_IDS}

# --- Optional Network Settings ---
HOST_IP=
DEFAULT_OS_IMAGE=ubuntu:22.04
DOCKER_NETWORK=bridge

# --- Limits ---
MAX_VPS_PER_USER=3
MAX_CONTAINERS=100
EOF
fi
ok ".env created at $ENV_FILE"

# ---------------- Setup Python venv (optional) ----------------
info "Setting up Python virtual environment (recommended)"
VENV_DIR="$SCRIPT_DIR/.venv_gg"
if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi
# Activate and install
# Use background process to pip install with spinner
if [ -f "$REQ_FILE" ]; then
  (
    source "$VENV_DIR/bin/activate"
    pip install --upgrade pip >/dev/null 2>&1
    pip install -r "$REQ_FILE" >/dev/null 2>&1
  ) &
  spinner $! "Installing Python packages (inside venv)"
  ok "Python packages installed in $VENV_DIR"
else
  warn "requirements.txt nahi mila — skipping pip install"
fi

# ---------------- Create systemd service ----------------
SERVICE_NAME="gg-bot"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

info "Creating systemd service (${SERVICE_NAME}) so bot auto-starts on reboot."

sudo bash -c "cat > $SERVICE_PATH" <<EOF
[Unit]
Description=GG Discord Bot Service
After=network.target

[Service]
Type=simple
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${SCRIPT_DIR}/.venv_gg/bin/python ${BOT_FILE}
Restart=always
RestartSec=5
EnvironmentFile=${ENV_FILE}

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME" >/dev/null 2>&1
ok "Service $SERVICE_NAME created and enabled"

# ---------------- Start service (ask user) ----------------
echo
read -rp "🚀 Start the bot now? (y/N): " START_NOW
if [[ "$START_NOW" =~ ^[Yy]$ ]]; then
  sudo systemctl start "$SERVICE_NAME"
  sleep 1
  if systemctl is-active --quiet "$SERVICE_NAME"; then
    ok "Bot started successfully via systemd (sudo systemctl start ${SERVICE_NAME})"
  else
    warn "Bot did not start cleanly. Check logs: sudo journalctl -u ${SERVICE_NAME} -f"
  fi
else
  info "You can start bot later with:"
  printf "   ${BOLD}sudo systemctl start %s${RESET}\n" "$SERVICE_NAME"
fi

# ---------------- Final message ----------------
printf "\n${BOLD}${GREEN}🎉 Installation complete — MADE BY GG , BOT INSTALLER${RESET}\n\n"
printf "Useful commands:\n"
printf "  %s\n" "sudo systemctl status ${SERVICE_NAME}"
printf "  %s\n" "sudo journalctl -u ${SERVICE_NAME} -f"
printf "  %s\n" "To run manually (venv): source ${VENV_DIR}/bin/activate && python ${BOT_FILE}"
printf "\n${CYAN}Thanks — agar kuch change chahiye (service name, venv path, banner), bata dena.${RESET}\n"
