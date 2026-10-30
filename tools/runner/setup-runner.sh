#!/usr/bin/env bash
# Snowmapper: a self-hosted GitHub Actions runner on a fresh Ubuntu 24.04
# server (x86, 4 vCPU / 8 GB RAM / 80 GB disk or more, e.g. Hetzner CPX31).
# The heavy jobs (SNOWPACK cycle, site build, tiles) run there once the
# repository variable RUNNER is set to "self-hosted"; see
# docs/privat-und-eigener-runner.md.
#
# As root on the server:
#   RUNNER_TOKEN=<token from GitHub> bash setup-runner.sh
# The token: GitHub › repository › Settings › Actions › Runners ›
# New self-hosted runner (it is valid for one hour).
set -euo pipefail

: "${RUNNER_TOKEN:?set RUNNER_TOKEN (GitHub › Settings › Actions › Runners › New self-hosted runner)}"
REPO_URL="${REPO_URL:-https://github.com/gianio/snow-mapper-v17}"
RUNNER_NAME="${RUNNER_NAME:-snowmapper-$(hostname -s)}"
RUNNER_USER=runner
RUNNER_DIR=/home/$RUNNER_USER/actions-runner

[ "$(id -u)" -eq 0 ] || { echo "run as root"; exit 1; }
[ "$(uname -m)" = "x86_64" ] || echo "warning: not x86_64 -- the SNOWPACK build cache is then built once on this machine"

echo "== packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq build-essential cmake git curl jq zip unzip ca-certificates gnupg \
  python3 python3-venv python3-pip libgdal-dev gdal-bin libproj-dev \
  unattended-upgrades ufw
# the GitHub CLI: the workflows fetch artifacts and cancel runs with it
if ! command -v gh >/dev/null; then
  install -d -m 0755 /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
  chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list
  apt-get update -qq && apt-get install -y -qq gh
fi

echo "== security updates, firewall, swap"
dpkg-reconfigure -f noninteractive unattended-upgrades
ufw allow OpenSSH >/dev/null && ufw --force enable >/dev/null
# the SNOWPACK cycle peaks at several GB; swap keeps a spike from killing it
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== runner user"
id "$RUNNER_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash "$RUNNER_USER"
# the workflows install a few packages with sudo apt-get, nothing else
echo "$RUNNER_USER ALL=(root) NOPASSWD: /usr/bin/apt-get" > /etc/sudoers.d/$RUNNER_USER
chmod 440 /etc/sudoers.d/$RUNNER_USER

echo "== actions runner"
VER="${RUNNER_VERSION:-$(curl -fsSL https://api.github.com/repos/actions/runner/releases/latest | jq -r .tag_name | sed 's/^v//')}"
[ -n "$VER" ] && [ "$VER" != "null" ] || { echo "could not find the runner version; set RUNNER_VERSION"; exit 1; }
sudo -u "$RUNNER_USER" mkdir -p "$RUNNER_DIR"
cd "$RUNNER_DIR"
if [ ! -f config.sh ]; then
  sudo -u "$RUNNER_USER" curl -fsSL -o runner.tgz \
    "https://github.com/actions/runner/releases/download/v${VER}/actions-runner-linux-x64-${VER}.tar.gz"
  sudo -u "$RUNNER_USER" tar xzf runner.tgz && rm runner.tgz
fi
./bin/installdependencies.sh >/dev/null
sudo -u "$RUNNER_USER" ./config.sh --unattended --replace \
  --url "$REPO_URL" --token "$RUNNER_TOKEN" --name "$RUNNER_NAME" \
  --labels self-hosted,linux,x64,snowmapper --work _work
./svc.sh install "$RUNNER_USER"
./svc.sh start

# old job workspaces and the tool cache grow without bound: clear them weekly
cat > /etc/cron.weekly/actions-runner-clean <<CRON
#!/bin/sh
find $RUNNER_DIR/_work -mindepth 1 -maxdepth 1 -not -name _tool -mtime +7 -exec rm -rf {} + 2>/dev/null || true
CRON
chmod +x /etc/cron.weekly/actions-runner-clean

echo
echo "Done: runner '$RUNNER_NAME' is online for $REPO_URL."
echo "Next: GitHub › Settings › Secrets and variables › Actions › Variables › RUNNER = self-hosted"
