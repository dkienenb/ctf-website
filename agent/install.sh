#!/bin/sh
# Workhorse install: KVM/libvirt + restricted ctf-agent service (§14, §18).
set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo ./install.sh" >&2
  exit 1
fi

echo "==> Installing KVM/libvirt"
apt-get update
apt-get install -y qemu-kvm libvirt-daemon-system libvirt-clients
systemctl enable --now libvirtd

echo "==> Creating restricted service user"
id ctf-agent >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin ctf-agent
usermod -aG kvm,libvirt ctf-agent

echo "==> Storage directories"
mkdir -p /var/lib/libvirt/images/ctf/images/attacker /var/lib/libvirt/images/ctf/images/target /var/lib/libvirt/images/ctf/disks /etc/ctf
chown -R ctf-agent:ctf-agent /var/lib/libvirt/images/ctf

echo "==> Installing agent to /opt/ctf/agent"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
mkdir -p /opt/ctf
cp -r "$REPO_DIR/agent" /opt/ctf/agent
chown -R ctf-agent:ctf-agent /opt/ctf/agent

if [ ! -f /etc/ctf/agent.toml ]; then
  cp "$REPO_DIR/agent/agent.toml.example" /etc/ctf/agent.toml
  chown root:ctf-agent /etc/ctf/agent.toml
  chmod 640 /etc/ctf/agent.toml
  echo "NOTE: edit /etc/ctf/agent.toml with the controller URL, host name, and token."
fi

echo "==> systemd service"
cp "$REPO_DIR/agent/ctf-agent.service" /etc/systemd/system/ctf-agent.service
systemctl daemon-reload
systemctl enable ctf-agent
systemctl restart ctf-agent
systemctl --no-pager status ctf-agent | head -5
