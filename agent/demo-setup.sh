#!/bin/sh
# Demo base images for a workhorse (run with sudo on the workhorse itself).
# Installs the two demo templates + a private NAT network so a lab
# allocation can boot for real. Demo-only: real Kali/target templates
# replace these before launch (handoff §13).
set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo ./demo-setup.sh" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
IMAGES=/var/lib/libvirt/images/ctf/images
DISKS=/var/lib/libvirt/images/ctf/disks
ISO_URL="https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/x86_64/alpine-virt-3.20.3-x86_64.iso"

# The agent (ctf-agent) must be able to create per-instance disks here.
mkdir -p "$IMAGES" "$DISKS"
chown ctf-agent:ctf-agent /var/lib/libvirt/images/ctf /var/lib/libvirt/images/ctf/images /var/lib/libvirt/images/ctf/disks
chmod o+x /var/lib/libvirt/images 2>/dev/null || true

for role in attacker target; do
  mkdir -p "$IMAGES/$role"
  cp "$REPO_DIR/agent/demo-images/$role/domain.xml" "$IMAGES/$role/domain.xml"
  if [ ! -f "$IMAGES/$role/demo.iso" ]; then
    echo "==> Downloading demo ISO for $role"
    curl -fL -o "$IMAGES/$role/demo.iso" "$ISO_URL"
  else
    echo "==> $role: demo.iso already present"
  fi
  if [ ! -f "$IMAGES/$role/disk-template" ]; then
    echo "==> Creating sparse demo base disk for $role"
    case "$role" in
      attacker) SIZE=20G ;;
      target)   SIZE=10G ;;
    esac
    qemu-img create -f qcow2 "$IMAGES/$role/disk-template" "$SIZE" >/dev/null
  else
    echo "==> $role: disk-template already present"
  fi
done

echo "==> Lab networks are per-lab now (§17): the agent defines ctflab<N>"
echo "    networks on demand; no shared ctf-demo-lab is created. After every"
echo "    lab has been reset off the old shared network, remove it with:"
echo "    virsh net-destroy ctf-demo-lab && virsh net-undefine ctf-demo-lab"

chown -R ctf-agent:ctf-agent "$IMAGES" "$DISKS"

echo "==> Demo templates ready (domains attach to per-lab ctflab<N> networks):"
ls -la "$IMAGES/attacker" "$IMAGES/target"
