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
IMAGES=/var/lib/ctf/agent/images
ISO_URL="https://dl-cdn.alpinelinux.org/alpine/v3.20/releases/x86_64/alpine-virt-3.20.3-x86_64.iso"

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

echo "==> Ensuring the private demo lab network (NAT, per-lab isolation lands with the isolation work)"
if ! virsh --connect qemu:///system net-info ctf-demo-lab >/dev/null 2>&1; then
  cat > /tmp/ctf-demo-lab.xml <<'NETXML'
<network>
  <name>ctf-demo-lab</name>
  <forward mode='nat'/>
  <ip address='192.168.100.1' netmask='255.255.255.0'>
    <dhcp start='192.168.100.10' end='192.168.100.200'/>
  </ip>
</network>
NETXML
  virsh --connect qemu:///system net-define /tmp/ctf-demo-lab.xml
  rm -f /tmp/ctf-demo-lab.xml
fi
virsh --connect qemu:///system net-autostart ctf-demo-lab
virsh --connect qemu:///system net-start ctf-demo-lab 2>/dev/null || true

chown -R ctf-agent:ctf-agent /var/lib/ctf/agent/images

echo "==> Demo templates ready:"
virsh --connect qemu:///system net-info ctf-demo-lab | head -2
ls -la "$IMAGES/attacker" "$IMAGES/target"
