# Adding a new workhorse

Runbook for provisioning any additional Kali workhorse from this repository.
Every workhorse is identical: Kali with KVM, the restricted `ctf-agent`
service, outbound-only connectivity to the portal controller, and
owner-supplied base images.

Fill these in once, then paste each block:

```sh
UPSTREAM_URL="<the upstream repository URL>"
CONTROLLER_URL="http://<portal-gateway-host>:8090"   # where the portal/Caddy runs
HOST_NAME="workhorse-02"                             # unique, matches step 3
CAPACITY=1                                           # concurrent lab slots this host offers
```

## 1. Clone this repo on the new workhorse

```sh
sudo apt-get update && sudo apt-get install -y git
git clone "$UPSTREAM_URL" /opt/ctf/website
cd /opt/ctf/website
```

## 2. Pre-flight checks (all three must pass)

```sh
grep -qE "vmx|svm" /proc/cpuinfo && echo "CPU virtualization: OK" || echo "FAIL: no vmx/svm"
ls /dev/kvm >/dev/null 2>&1 && echo "KVM device: OK" || echo "FAIL: no /dev/kvm (enable VT-x/AMD-V in firmware)"
df -h /var/lib | tail -1
```

Storage note: lab admission stops automatically when free disk or RAM drops
below the 20% reserve (owner decision, 2026-11-14). Plan images + disks +
saved-memory headroom; the controller enforces it from agent heartbeats.

## 3. Install KVM/libvirt and the agent service (idempotent)

```sh
cd /opt/ctf/website/agent
sudo ./install.sh
```

This installs `qemu-kvm` + `libvirt-daemon-system`, enables `libvirtd`,
creates the login-disabled `ctf-agent` user (groups: `kvm`, `libvirt` only),
creates `/var/lib/libvirt/images/ctf/{images,disks}`, installs the systemd unit, and
starts the agent (it will poll without a valid token until step 5).

## 4. Register the host on the controller machine (token shown once)

```sh
# on the machine that runs the portal:
.venv/bin/python manage.py register_host "$HOST_NAME" --capacity "$CAPACITY"
```

Copy the printed token — it is stored only as a SHA-256 hash and cannot be
retrieved again. Re-running `register_host` rotates it.

## 5. Configure the agent

```sh
sudo mkdir -p /etc/ctf
sudo cp /opt/ctf/website/agent/agent.toml.example /etc/ctf/agent.toml
sudoedit /etc/ctf/agent.toml
#   controller_url = "$CONTROLLER_URL"
#   host_name      = "$HOST_NAME"        # must exactly match the registered name
#   token          = "<token from step 4>"
sudo chown root:ctf-agent /etc/ctf/agent.toml && sudo chmod 640 /etc/ctf/agent.toml
sudo systemctl restart ctf-agent
```

## 6. Verify from the workhorse

```sh
systemctl --no-pager status ctf-agent | head -5
journalctl -u ctf-agent -n 20 --no-pager          # should show no tracebacks
curl -s -X POST "$CONTROLLER_URL/agentapi/heartbeat" \
     -H "Authorization: Bearer <token>" -H "Content-Type: application/json" -d '{}'
# expected: {"protocol": "1", "lease_deadlines": {}}
```

The agent is outbound-only — no inbound firewall rule is ever required.

## 7. Verify from the controller

```sh
.venv/bin/python manage.py shell -c "
from portal.apps.labs.models import Host
h = Host.objects.get(name='$HOST_NAME')
print(h.online, h.last_heartbeat, h.free_disk_gb, '/', h.total_disk_gb, 'GB', h.admits)
"
```

A fresh heartbeat with resources and `admits=True` means the host is live.

## 8. Host firewall (§17 isolation) — run once per workhorse

```sh
cd /opt/ctf/website/agent
sudo ./isolation-install.sh   # idempotent; persists via iptables-persistent
sudo ./guacd-install.sh       # per-workhorse guacd on host network (desktops)
sudo ./demo-setup.sh          # demo templates (owner-supplied templates later)
```

Per-lab libvirt networks (`ctf-lab<N>-net`, subnet `10.<lab>.0.0/24`) are
created by the agent on demand; this ruleset blocks lab→host, lab→other
labs, and lab→home/mgmt LAN. Audit any time with
`sudo ./verify-isolation.sh` and its guest-side probe list.

## 9. Install base images (owner-supplied launch content)

Each role needs two files; missing templates make allocations fail loudly
(never silently replaced — handoff §13):

```sh
# on the controller or wherever images live:
rsync -av images/attacker/ "$HOST_NAME":/var/lib/libvirt/images/ctf/images/attacker/
rsync -av images/target/   "$HOST_NAME":/var/lib/libvirt/images/ctf/images/target/
#   images/attacker/domain.xml     libvirt XML with @NAME@ and @DISK@ placeholders
#   images/attacker/disk-template  qcow2 base image (Kali attacker)
#   images/target/…                same pair for the vulnerable target
sudo chown -R ctf-agent:ctf-agent /var/lib/libvirt/images/ctf/images
```

## 10. First real allocation test

Create a challenge with a lab template on the portal, request + claim its lab
from a player account, and confirm on the workhorse:

```sh
watch -n2 "sudo virsh --connect qemu:///system list --all | grep ctf-"
```

You should see `ctf-<op>-atk` and `ctf-<op>-tgt` start, and the lab state on
the portal move reserved → starting → running.

## Maintenance

- Update the agent: `cd /opt/ctf/website && git pull && sudo cp -r agent /opt/ctf/ && sudo systemctl restart ctf-agent`
- Rotate the token: re-run `register_host` on the controller, update `/etc/ctf/agent.toml`, restart the agent.
- Drain the host for maintenance: set it offline on the controller (labs save,
  new allocations stop), wait for `saving` labs to complete, then reboot.
- Deadlines: the agent enforces the last acknowledged lease deadline locally —
  expiry holds even when the controller link is down.
