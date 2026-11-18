#!/usr/bin/env bash
# ==============================================================================
# TRILLIONAIRE ALGO ENGINE - PRODUCTION MUMBAI VPS PROVISIONING SCRIPT
# Target: AWS Lightsail / EC2 Ubuntu 22.04/24.04 LTS (Region: ap-south-1 Mumbai)
# ==============================================================================

set -euo pipefail

echo "========================================================================"
echo " [TRILLIONAIRE] Starting Mumbai Production VPS Provisioning..."
echo "========================================================================"

if [[ $EUID -ne 0 ]]; then
   echo "[ERROR] This script must be run as root or with sudo." >&2
   exit 1
fi

# 1. Update OS Packages
echo "`date '+%Y-%m-%d %H:%M:%S'` [1/7] Updating Base System Packages..."
apt-get update -y && apt-get upgrade -y
apt-get install -y --no-install-recommends \
    curl \
    wget \
    git \
    ca-certificates \
    gnupg \
    lsb-release \
    ufw \
    htop \
    iotop \
    jq \
    tzdata

# 2. Strict Indian Standard Time (IST) Synchronization
echo "`date '+%Y-%m-%d %H:%M:%S'` [2/7] Configuring System Timezone to Asia/Kolkata..."
timedatectl set-timezone Asia/Kolkata
timedatectl set-ntp on
echo "System Timezone set to: $(cat /etc/timezone) | Current IST: $(date)"

# 3. Install Official Docker Engine & Docker Compose Plugin
echo "`date '+%Y-%m-%d %H:%M:%S'` [3/7] Installing Docker CE and Compose Plugin..."
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg --yes
chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable docker
systemctl start docker
echo "Docker Version: $(docker --version)"

# 4. Configure Institutional UFW Firewall
echo "`date '+%Y-%m-%d %H:%M:%S'` [4/7] Hardening UFW Firewall..."
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
ufw allow 80/tcp comment 'HTTP Nginx'
ufw allow 443/tcp comment 'HTTPS SSL'
# Explicitly block direct public access to internal ports
ufw deny 8000/tcp comment 'Block direct FastAPI public access'
ufw deny 3000/tcp comment 'Block direct NextJS public access'
ufw deny 6379/tcp comment 'Block direct Redis public access'
ufw --force enable

# 5. Directory Setup & Permissions
echo "`date '+%Y-%m-%d %H:%M:%S'` [5/7] Preparing Production Directories in /opt/trillionaire..."
mkdir -p /opt/trillionaire/logs
mkdir -p /opt/trillionaire/research_lab/artifacts
mkdir -p /opt/trillionaire/research_lab/data_store

# 6. Install & Configure Systemd Service
echo "`date '+%Y-%m-%d %H:%M:%S'` [6/7] Installing Trillionaire Systemd Service..."
if [[ -f "/opt/trillionaire/deploy/systemd/trillionaire.service" ]]; then
    cp /opt/trillionaire/deploy/systemd/trillionaire.service /etc/systemd/system/trillionaire.service
    systemctl daemon-reload
    systemctl enable trillionaire.service
    echo "Trillionaire service enabled."
else
    echo "[WARN] /opt/trillionaire/deploy/systemd/trillionaire.service not found yet. Copy when deploying monorepo."
fi

# 7. Network & Zerodha Whitelisting Check
echo "`date '+%Y-%m-%d %H:%M:%S'` [7/7] Checking Public Static IP..."
STATIC_IP=$(curl -s https://checkip.amazonaws.com || curl -s https://ifconfig.me || echo "UNKNOWN")

echo "========================================================================"
echo " [SUCCESS] MUMBAI VPS PROVISIONING COMPLETED!"
echo "========================================================================"
echo " VPS Static IP: ${STATIC_IP}"
echo " IMPORTANT: Add this static IP to Zerodha Kite Connect Developer Console"
echo " (https://developers.kite.trade/apps) under 'Redirect / Static IP Whitelist'."
echo " Next Steps:"
echo " 1. Clone repository to /opt/trillionaire"
echo " 2. Populate /opt/trillionaire/.env and /opt/trillionaire/web_dashboard/.env.local"
echo " 3. Start stack: systemctl start trillionaire"
echo " 4. Check logs: journalctl -u trillionaire -f"
echo "========================================================================"
