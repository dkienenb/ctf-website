#!/bin/sh
# Deploy the per-workhorse guacd (remote-desktop backend) bound to the
# workhorse's management interface, plus a firewall rule keeping lab guests
# away from it (handoff §16: guacd must not be reachable from player networks).
set -e

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo: sudo ./guacd-install.sh" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required: sudo apt-get install -y docker.io" >&2
  exit 1
fi

MGMT_IP="$(hostname -I | awk '{print $1}')"
echo "==> Management IP: $MGMT_IP"

echo "==> guacd on host network (binds all interfaces, incl. management)"
# --network host is REQUIRED: the grant's VNC hop addresses the guest on the
# workhorse's loopback (libvirt binds VNC to 127.0.0.1). Inside a bridge
# network the container's 127.0.0.1 is not the host's, and every connection
# dies with Guacamole error 519 (UPSTREAM_NOT_FOUND).
docker rm -f ctf-guacd 2>/dev/null || true
docker run -d --name ctf-guacd --restart unless-stopped \
  --network host guacamole/guacd:1.6.0

echo "==> Firewall guard: lab guests may not reach guacd"
iptables -I INPUT -s 192.168.100.0/24 -p tcp --dport 4822 -j DROP 2>/dev/null || true
iptables -C INPUT -s 192.168.100.0/24 -p tcp --dport 4822 -j DROP 2>/dev/null || \
  iptables -I INPUT -s 192.168.100.0/24 -p tcp --dport 4822 -j DROP

echo "==> Set management_ip = \"$MGMT_IP\" in /etc/ctf/agent.toml and restart the agent."
echo "==> guacd is running; the controller reaches it at $MGMT_IP:4822."
