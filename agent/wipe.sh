#!/bin/sh
# Wipe everything the workhorse deployment created (idempotent).
# Keeps: KVM/libvirt packages, the ctf-agent user, and the OS itself.
# Run with sudo on the workhorse. The controller-side host registration and
# token are reset separately (register_host rotates them).
set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo ./wipe.sh" >&2
  exit 1
fi

echo "==> Stopping the agent service"
systemctl stop ctf-agent 2>/dev/null || true
systemctl disable ctf-agent 2>/dev/null || true

echo "==> Destroying ctf guest domains"
for domain in $(virsh --connect qemu:///system list --all --name 2>/dev/null | grep '^ctf-' || true); do
  virsh --connect qemu:///system destroy "$domain" 2>/dev/null || true
  virsh --connect qemu:///system undefine "$domain" --nvram 2>/dev/null || true
  echo "   removed $domain"
done

echo "==> Removing the demo lab network"
virsh --connect qemu:///system net-destroy ctf-demo-lab 2>/dev/null || true
virsh --connect qemu:///system net-undefine ctf-demo-lab 2>/dev/null || true

echo "==> Removing storage, agent install, and config"
rm -rf /var/lib/libvirt/images/ctf
rm -rf /opt/ctf/agent
rm -f /etc/ctf/agent.toml /etc/ctf/tunnel-token

echo "==> Workhorse wiped."
echo "    To bring it back: sudo ./install.sh && sudo ./demo-setup.sh,"
echo "    then re-run register_host on the controller for a fresh token."
