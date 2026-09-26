#!/usr/bin/env bash
# Installs Docker Engine and the compose plugin on a Debian or Ubuntu host.
# Validated on Ubuntu 26.04 LTS (x86_64) and safe to re-run.
#
# Usage from the machine that holds the SSH key:
#   ssh -i <key.pem> ubuntu@<instance> 'bash -s' < scripts/provision-ec2.sh
#
# This script only prepares the container runtime; the application is started
# afterwards with `docker compose up -d --build` (see scripts/deploy-ec2.ps1).
set -uo pipefail

step() { echo; echo "=== $* ==="; }

step "identity"
id -un
. /etc/os-release
echo "os=$PRETTY_NAME arch=$(uname -m) kernel=$(uname -r)"
if sudo -n true 2>/dev/null; then
  echo "sudo=passwordless"
else
  echo "sudo=NEEDS_PASSWORD (expected on EC2 Ubuntu images to be passwordless)"
fi

step "capacity"
df -h / | tail -1
grep -i memtotal /proc/meminfo
getconf _NPROCESSORS_ONLN | sed 's/^/cpus=/' 2>/dev/null || true

step "docker presence"
if command -v docker >/dev/null 2>&1; then
  echo "docker=$(docker --version)"
else
  echo "docker=absent"
  step "installing docker via apt"
  export DEBIAN_FRONTEND=noninteractive
  sudo apt-get update -y
  if sudo apt-get install -y docker.io docker-compose-v2; then
    echo "apt install: ok"
  else
    echo "apt install: failed, falling back to the vendor convenience script"
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && sudo sh /tmp/get-docker.sh
  fi
fi

step "enabling the docker service"
sudo systemctl enable --now docker 2>&1 | tail -3
echo "active=$(sudo systemctl is-active docker)"

if ! sudo docker compose version >/dev/null 2>&1; then
  step "compose plugin missing, installing via get.docker.com"
  curl -fsSL https://get.docker.com -o /tmp/get-docker.sh && sudo sh /tmp/get-docker.sh
fi

step "adding the login user to the docker group"
sudo usermod -aG docker "$(id -un)" && echo "added $(id -un) to group docker (effective on next login)"

step "versions"
sudo docker --version
sudo docker compose version

step "summary"
echo "provisioning complete"
