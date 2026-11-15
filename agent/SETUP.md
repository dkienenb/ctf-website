# Install this machine as a CTF workhorse (handoff §14, §18).

Run as root (sudo) on this or any other Kali workhorse. Idempotent.

```sh
sudo ./install.sh
```

What it does:

1. Installs KVM/libvirt (`qemu-kvm`, `libvirt-daemon-system`,
   `libvirt-clients`) and `docker.io` (for the per-workhorse guacd), and
   enables `libvirtd`.
2. Creates the restricted `ctf-agent` service user (no login shell) and grants
   only libvirt access via the `kvm` and `libvirt` groups.
3. Creates `/var/lib/libvirt/images/ctf/{images,disks}` for versioned base images and
   per-instance writable disks.
4. Copies this `agent/` directory to `/opt/ctf/agent` and installs the
   `ctf-agent.service` systemd unit. (Re-running install.sh after a
   `git pull` is how the deployed agent gets updated.)
5. Enables and starts the agent. It polls the portal controller.

Before starting, create the agent config from `agent.toml.example`:

```sh
sudo cp agent/agent.toml.example /etc/ctf/agent.toml
sudoedit /etc/ctf/agent.toml   # controller URL + token + host name
```

The controller generates the token:

```sh
.venv/bin/python manage.py register_host workhorse-01 --capacity 1
```

Token is shown once; put it in `/etc/ctf/agent.toml`.

## Base images (owner-supplied launch content)

Templates are now authored in the portal: **Administration → Machine
images** uploads versioned base images (domain.xml + qcow2/ISO), pins them
to challenges on the challenge's edit page, and distributes them to
workhorses through the agent's template commands (chunked, sha256-verified,
§13 explicit pinning). The demo templates below remain the fallback when a
challenge pins no version.

Each template directory needs two files:

- `images/attacker/domain.xml` — libvirt domain XML with `@NAME@`, `@DISK@`,
  `@VNC_PORT@`, and `@LAB_NET@` placeholders (`@LAB_NET@` is the per-lab
  isolated network the agent defines on demand, §17)
- `images/attacker/disk-template` — qcow2 base image (Kali attacker)
- `images/target/…` — same pair for the vulnerable target

Missing templates fail the allocation explicitly (handoff §13: no silent
version replacement). Re-running `sudo ./demo-setup.sh` refreshes the
deployed demo templates from the repo copy.

## Host firewall (§17) and desktops

```sh
sudo ./isolation-install.sh   # one-time per workhorse; idempotent; persists
sudo ./guacd-install.sh       # per-workhorse guacd on host network (required
                              # for the browser desktop stream)
```

Per-lab libvirt networks (`ctf-lab<N>-net`, subnet `10.<lab>.0.0/24`) are
created by the agent during allocation; the firewall blocks lab→host,
lab→other labs, and lab→home/mgmt LAN. Audit with `sudo ./verify-isolation.sh`
(it ends with the guest-side reachability probes required by §17).

## Lease enforcement

The agent enforces the last acknowledged lease deadline locally, so expiry
saves the guests even when the controller link is down (§11, §18).

## Adding more workhorses

See `agent/ADD-A-WORKHORSE.md` for the full clone → install → register →
verify → images runbook.
