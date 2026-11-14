# Install this machine as a CTF workhorse (handoff §14, §18).

Run as root (sudo) on this or any other Kali workhorse. Idempotent.

```sh
sudo ./install.sh
```

What it does:

1. Installs KVM/libvirt (`qemu-kvm`, `libvirt-daemon-system`, `libvirt-clients`)
   and enables `libvirtd`.
2. Creates the restricted `ctf-agent` service user (no login shell) and grants
   only libvirt access via the `kvm` and `libvirt` groups.
3. Creates `/var/lib/ctf/agent/{images,disks}` for versioned base images and
   per-instance writable disks.
4. Copies this `agent/` directory to `/opt/ctf/agent` and installs the
   `ctf-agent.service` systemd unit.
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

Each template directory needs two files:

- `images/attacker/domain.xml` — libvirt domain XML with `@NAME@` and `@DISK@` placeholders
- `images/attacker/disk-template` — qcow2 base image (Kali attacker)
- `images/target/…` — same pair for the vulnerable target

Missing templates fail the allocation explicitly (handoff §13: no silent
version replacement).

## Lease enforcement

The agent enforces the last acknowledged lease deadline locally, so expiry
saves the guests even when the controller link is down (§11, §18).
