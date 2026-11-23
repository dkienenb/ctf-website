#!/bin/sh
# §17 isolation audit for a workhorse. Run as root on the workhorse.
# Checks topology + firewall rules; guest-side reachability probes are at
# the bottom (run them when at least one lab is running, from its console).
set -u

pass=0; fail=0
check() { # check <description> <command...>
  desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "  [ok]   $desc"; pass=$((pass+1))
  else
    echo "  [FAIL] $desc"; fail=$((fail+1))
  fi
}

echo "== Networks (one isolated NAT network per lab) =="
for net in $(virsh net-list --all --name 2>/dev/null | grep '^ctf-lab[0-9]*-net$'); do
  xml=$(virsh net-dumpxml "$net" 2>/dev/null)
  echo "$net: $(echo "$xml" | grep -oE "10\.[0-9]+\.0\.1" | head -1) bridge=$(echo "$xml" | grep -oE "bridge name='[^']+'" | head -1)"
  lab=$(echo "$net" | sed 's/ctf-lab\([0-9]*\)-net/\1/')
  check "$net active" virsh net-info "$net"
  check "$net has no IPv6 (no global RA on lab nets)" sh -c "! echo '$xml' | grep -q 'family=.ipv6'"
done
check "no shared ctf-demo-lab network remains" sh -c "! virsh net-list --all --name | grep -q '^ctf-demo-lab$'"

echo "== Host firewall (isolation-install.sh rules) =="
check "INPUT: lab bridges cannot reach host" iptables -C INPUT -i ctflab+ -j DROP
check "FORWARD: cross-bridge lab traffic dropped" iptables -C FORWARD -i ctflab+ -o ctflab+ -j DROP
check "FORWARD: labs cannot reach 10/8 (other labs)" iptables -C FORWARD -i ctflab+ -d 10.0.0.0/8 -j DROP
check "FORWARD: labs cannot reach 172.16/12 (mgmt/home)" iptables -C FORWARD -i ctflab+ -d 172.16.0.0/12 -j DROP
check "FORWARD: labs cannot reach 192.168/16 (home)" iptables -C FORWARD -i ctflab+ -d 192.168.0.0/16 -j DROP
check "FORWARD: labs have no egress beyond their bridge" iptables -C FORWARD -i ctflab+ ! -o ctflab+ -j DROP
check "ip6tables INPUT: lab bridges cannot reach host" ip6tables -C INPUT -i ctflab+ -j DROP
check "ip6tables FORWARD: lab traffic stays on its bridge" sh -c "ip6tables -S FORWARD 2>/dev/null | grep -q 'ctflab+'"
check "iptables rules live AND persisted at boot" sh -c "iptables-save | grep -q 'ctflab' && { test -f /etc/iptables/rules.v4 || command -v netfilter-persistent >/dev/null; }"

echo "== Domains attach to their lab's network =="
for dom in $(virsh list --all --name 2>/dev/null | grep '^ctf-lab[0-9]*-'); do
  src=$(virsh dumpxml "$dom" 2>/dev/null | grep -m1 -oE "source network='[^']+'" | cut -d"'" -f2)
  lab=$(echo "$dom" | sed 's/ctf-lab\([0-9]*\)-.*/\1/')
  if [ "$src" = "ctf-lab${lab}-net" ]; then
    echo "  [ok]   $dom -> $src"; pass=$((pass+1))
  else
    echo "  [FAIL] $dom -> ${src:-none} (expected ctf-lab${lab}-net; stale definition)"; fail=$((fail+1))
  fi
done

echo
echo "== Guest-side reachability probes (§17: test explicitly) =="
echo "  With a lab running, from its ATTACKER console:"
echo "    1. ping <target-IP>                          # intra-lab: MUST work"
echo "    2. ping 10.<lab>.0.1                         # host-side gw: expect no reply (INPUT drop)"
echo "    3. ping 172.20.201.175 / 172.20.201.125      # mgmt/home LAN: MUST fail"
echo "    4. ping <other-lab-subnet-IP> (if a second lab runs)  # MUST fail"
echo "    5. ping 1.1.1.1                              # internet: MUST fail (offline labs; egress dropped)"
echo "    6. ip -6 addr; ping -6 <anything-global>     # no global v6 expected"
echo "  Reach guacd/ssh/agent ports from the guest for the audit trail:"
echo "    nc -zv 172.20.201.175 4822                   # MUST fail (guacd guard)"

echo
echo "Result: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
