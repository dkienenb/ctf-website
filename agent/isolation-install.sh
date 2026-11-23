#!/bin/sh
# §17 host firewall for per-lab lab networks (run once per workhorse, sudo).
#
# The agent creates one NAT network per lab (ctf-lab<N>-net, bridge
# ctflab<N>br0, subnet 10.<lab>.0.0/24). Libvirt itself inserts, at every
# net-start, position-1 rules that ACCEPT same-bridge traffic and REJECT
# anything routed INTO the bridge from elsewhere — that gives intra-lab
# connectivity and cross-lab blocking without the agent touching iptables
# (it runs without CAP_NET_ADMIN). This script adds the host-side rules
# libvirt does not provide. Idempotent: existing rules are skipped, so
# re-running never lands rules above libvirt's.
#
#   Lab to other labs       blocked (this script + libvirt REJECT)
#   Lab to home/mgmt LAN    blocked (RFC1918 FORWARD drops)
#   Lab to host services    blocked (INPUT drop; libvirt DHCP/DNS stays above)
#   Lab to internet         allowed via NAT (§17: "restricted initially";
#                           tighten with destination allow-lists later)
#   IPv6                    no RA/global v6 on lab nets; link-local only, and
#                           ip6tables blocks any lab traffic leaving the bridge
set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo ./isolation-install.sh" >&2
  exit 1
fi

insert() { # insert <table-family cmd> <chain> <rule spec...>
  # $1 = iptables|ip6tables, $2 = chain, rest = rule spec
  cmd="$1"; chain="$2"; shift 2
  if "$cmd" -C "$chain" "$@" 2>/dev/null; then
    echo "  = $chain $* (already present)"
  else
    "$cmd" -I "$chain" 1 "$@"
    echo "  + $chain $*"
  fi
}

echo "==> IPv4: labs cannot reach host services"
insert iptables INPUT -i ctflab+ -j DROP

echo "==> IPv4: labs cannot route to other labs' bridges"
insert iptables FORWARD -i ctflab+ -o ctflab+ -j DROP

echo "==> IPv4: labs cannot reach home/mgmt LANs (intra-lab traffic is"
echo "    bridged by libvirt's own ACCEPT and never hits FORWARD)"
insert iptables FORWARD -i ctflab+ -d 10.0.0.0/8 -j DROP
insert iptables FORWARD -i ctflab+ -d 172.16.0.0/12 -j DROP
insert iptables FORWARD -i ctflab+ -d 192.168.0.0/16 -j DROP

echo "==> IPv4: labs have NO egress at all (owner policy: offline labs;"
echo "    supersedes the earlier 'internet allowed' §17 posture). Intra-lab"
echo "    traffic never traverses FORWARD, so attacker<->target, DHCP and"
echo "    DNS via libvirt's dnsmasq keep working. Re-enable egress per-"
echo "    destination with ACCEPT rules inserted BEFORE this rule."
insert iptables FORWARD -i ctflab+ ! -o ctflab+ -j DROP

echo "==> IPv6: lab traffic may not leave its bridge, nor reach the host"
insert ip6tables INPUT -i ctflab+ -j DROP
insert ip6tables FORWARD -i ctflab+ ! -o ctflab+ -j DROP

echo "==> Persisting rules (iptables-persistent if installed)"
if command -v netfilter-persistent >/dev/null 2>&1; then
  netfilter-persistent save
  echo "  rules saved via netfilter-persistent"
else
  mkdir -p /etc/iptables
  iptables-save > /etc/iptables/rules.v4
  ip6tables-save > /etc/iptables/rules.v6
  echo "  NOTE: iptables-persistent is not installed; rules written to"
  echo "  /etc/iptables/rules.v[46] but will NOT load at boot."
  echo "  Install it: sudo apt-get install -y iptables-persistent"
fi

echo "==> §17 host firewall in place. Reachability tests (§17: test"
echo "    explicitly) are audited by agent/verify-isolation.sh, which"
echo "    also lists the guest-side probes to run from a lab console."
