#!/bin/sh
# Isolate the save failure: boots a minimal VM with a STANDALONE disk (no
# backing chain, no cdrom) and attempts the same virsh save. Run with sudo
# on the workhorse. Safe: creates and removes its own throwaway domain.
set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo ./debug-save.sh" >&2
  exit 1
fi

DISKS=/var/lib/libvirt/images/ctf/disks
OVERLAY="$DISKS/ctf-lab1-atk.qcow2"

echo "==> (1) Image info: the failing overlay and its backing chain"
qemu-img info "$OVERLAY" 2>&1 | head -12 || echo "overlay missing — is a lab running?"
echo
echo "==> (2) Flattening a standalone copy (no backing file)"
qemu-img convert -O qcow2 "$OVERLAY" "$DISKS/debug-standalone.qcow2"
qemu-img info "$DISKS/debug-standalone.qcow2" | head -6

echo
echo "==> (3) Defining a throwaway domain on the standalone disk"
cat > "$DISKS/debug-save.xml" <<XML
<domain type='kvm'>
  <name>ctf-debug-save</name>
  <memory unit='MiB'>512</memory>
  <vcpu>1</vcpu>
  <os><type arch='x86_64' machine='q35'>hvm</type><boot dev='hd'/></os>
  <devices>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source file='$DISKS/debug-standalone.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <console type='pty'><target type='serial' port='0'/></console>
    <memballoon model='virtio'/>
  </devices>
</domain>
XML
virsh --connect qemu:///system define "$DISKS/debug-save.xml"
virsh --connect qemu:///system start ctf-debug-save
sleep 5

echo
echo "==> (4) Attempting virsh save on the standalone-disk VM"
if virsh --connect qemu:///system save ctf-debug-save "$DISKS/debug-standalone.save" 2>&1; then
  echo "RESULT: standalone save OK — the qcow2 BACKING CHAIN is the blocker."
  echo "        Production fix: flatten per-instance disks at allocate time."
else
  echo "RESULT: standalone save ALSO fails — deeper QEMU/libvirt issue."
  echo "        Paste: virsh version; qemu-system-x86_64 --version"
fi

echo
echo "==> (5) Cleaning up the throwaway"
virsh --connect qemu:///system destroy ctf-debug-save 2>/dev/null || true
virsh --connect qemu:///system undefine ctf-debug-save 2>/dev/null || true
rm -f "$DISKS/debug-standalone.qcow2" "$DISKS/debug-standalone.save" "$DISKS/debug-save.xml"
echo "done. Your lab and its state are untouched."
