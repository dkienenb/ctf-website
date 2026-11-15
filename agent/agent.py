"""CTF workhorse agent (handoff §14, §18).

A restricted Python service running on each workhorse. It performs only
predefined operations through local libvirt:

- Polls the controller for durable host commands.
- Executes a fixed vocabulary: allocate, save, reset-target, reset-full.
- Enforces the last acknowledged lease deadline locally, so expiry holds even
  when the controller link is lost (§11, §18).
- Reports heartbeats with capacity; a stale controller link never blocks the
  host-local deadline enforcement.

Security properties:
- No shell. Every libvirt interaction is a fixed-argv `virsh` invocation.
- Lab domain names are derived from lab ids, never from player input.
- Credentials live in the agent config, outside any player-reachable path.
- The agent cannot invent operations: it only executes what the controller
  issued for its own host id.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
import base64
import hashlib
import os
import re
import shutil

CONFIG_PATH = os.environ.get("CTF_AGENT_CONFIG", "/etc/ctf/agent.toml")
# Storage lives inside libvirt's own images area: Kali/Debian AppArmor
# profiles (virt-aa-helper, per-domain profiles) permit this path by
# default, so no per-host AppArmor surgery is needed.
DEFAULT_IMAGES_DIR = "/var/lib/libvirt/images/ctf/images"
DEFAULT_DISKS_DIR = "/var/lib/libvirt/images/ctf/disks"
LIBVIRT_URI = os.environ.get("CTF_AGENT_LIBVIRT_URI", "qemu:///system")
DOMAIN_PREFIX = "ctf"
HEARTBEAT_INTERVAL = 10
POLL_INTERVAL = 5
DEADLINE_CHECK_INTERVAL = 5
# §17: one isolated libvirt network per lab. Subnets are lab-keyed and
# deterministic (10.<lab>.0.0/24), so every workhorse derives the same
# topology and the host firewall can key on the range as a whole.
LAB_NET_PREFIX = "ctf-lab"
LAB_NET_SUFFIX = "-net"
LAB_SUBNET_LIMIT = 250  # 10.<lab>.0.0/24; a host never hosts this many labs anyway

try:
    import tomllib
except ImportError:  # Python < 3.11 fallback not needed on target hosts
    tomllib = None


class AgentError(Exception):
    pass


def load_config(path=CONFIG_PATH):
    with open(path, "rb") as handle:
        config = tomllib.load(handle)
    for key in ("controller_url", "token", "host_name"):
        if not config.get(key):
            raise AgentError(f"config missing required key: {key}")
    config.setdefault("images_dir", DEFAULT_IMAGES_DIR)
    config.setdefault("disks_dir", DEFAULT_DISKS_DIR)
    return config


def _domain_base(command):
    """Domain names are keyed to the LAB, not the command: every operation
    (allocate, save, reset) on the same lab must address the same guests."""
    lab = command.get("lab")
    if lab is not None:
        return f"lab{lab}"
    return f"{command.get('op_id', 'unknown')[:12]}"


class LibvirtExecutor:
    """Fixed-argv virsh wrapper. The command vocabulary is closed."""

    def __init__(self, uri=LIBVIRT_URI, images_dir=DEFAULT_IMAGES_DIR, disks_dir=DEFAULT_DISKS_DIR):
        self.uri = uri
        self.images_dir = Path(images_dir)
        self.disks_dir = Path(disks_dir)
        self.disks_dir.mkdir(parents=True, exist_ok=True)

    def virsh(self, *args):
        result = subprocess.run(
            ["virsh", "--connect", f"{self.uri}", *args],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            raise AgentError(f"virsh {' '.join(args[:2])} failed: {result.stderr.strip()[:300]}")
        return result.stdout

    def virsh_tolerant(self, *args):
        """Best-effort virsh call: tolerates missing domains (cleanup paths)."""
        subprocess.run(
            ["virsh", "--connect", self.uri, *args],
            capture_output=True, text=True, timeout=60,
        )

    def virsh_probe(self, *args):
        """virsh probe that reports (returncode, stdout) instead of raising —
        for net-info/dumpxml existence checks. Like every other virsh spawn,
        overridable in tests (tests_agent's RecordingExecutor)."""
        result = subprocess.run(
            ["virsh", "--connect", self.uri, *args],
            capture_output=True, text=True, timeout=60,
        )
        return result.returncode, result.stdout

    def domain_exists(self, name):
        result = subprocess.run(
            ["virsh", "--connect", self.uri, "dominfo", name],
            capture_output=True, text=True, timeout=60,
        )
        return result.returncode == 0

    # --- machine template staging (§10, §13) -----------------------------
    # Templates are operator-authored version directories under images_dir:
    #   images_dir/<role>/<version>/{domain.xml,disk-template}
    # A version-less allocate (demo mode) falls back to the bare role dir.

    ROLES = ("attacker", "target")
    TEMPLATE_FILES = ("domain.xml", "disk-template")
    STAGING_DIRNAME = ".staging"

    def _check_name(self, value, what):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", str(value or "")):
            raise AgentError(f"invalid {what}: refused for path safety")
        return str(value)

    def template_dir(self, role, version=""):
        """Directory holding a role's pinned template (version subdirectory
        when a version is pinned; the bare role directory in demo mode)."""
        self._check_name(role, "role")
        if role not in self.ROLES:
            raise AgentError(f"unknown template role: {role}")
        if version:
            self._check_name(version, "template version")
            return self.images_dir / role / version
        return self.images_dir / role

    def _inventory_files(self, directory):
        return {
            entry.name: entry.stat().st_size
            for entry in sorted(directory.iterdir())
            if entry.is_file() and not entry.name.startswith(".")
        }

    def template_inventory(self):
        """What template versions exist locally: {role: {version: {file: size}}}."""
        inventory = {}
        for role in self.ROLES:
            role_dir = self.images_dir / role
            if not role_dir.exists():
                continue
            bare = self._inventory_files(role_dir)
            if bare:
                inventory.setdefault(role, {})[""] = bare
            for version_dir in sorted(role_dir.iterdir()):
                if version_dir.is_dir() and not version_dir.name.startswith("."):
                    files = self._inventory_files(version_dir)
                    if files:
                        inventory.setdefault(role, {})[version_dir.name] = files
        return inventory

    def template_put(self, command):
        """Store one chunk of a staged template file (§10). Chunks accumulate
        under disks_dir/.staging keyed by content hash; when the last chunk
        lands, the file is reassembled, sha256-verified, and moved into the
        template directory. Idempotent: re-sending a chunk overwrites the
        same part; a complete file with the matching hash is left untouched."""
        payload = command["payload"]
        role = self._check_name(payload.get("role", ""), "role")
        if role not in self.ROLES:
            raise AgentError(f"unknown template role: {role}")
        version = self._check_name(payload.get("version", ""), "template version")
        file_name = payload.get("file", "")
        if file_name not in self.TEMPLATE_FILES:
            raise AgentError(f"refusing template file: {file_name} (closed list)")
        try:
            seq, total = int(payload.get("seq", 0)), int(payload.get("total", 0))
        except (TypeError, ValueError):
            raise AgentError("template-put requires integer seq/total")
        if not 1 <= seq <= total:
            raise AgentError("chunk seq outside 1..total")
        try:
            blob = base64.b64decode(payload.get("data_b64", ""), validate=True)
        except Exception:
            raise AgentError("template-put data_b64 is not valid base64")
        sha = str(payload.get("sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise AgentError("template-put requires a 64-hex sha256")

        staging = self.disks_dir / self.STAGING_DIRNAME / f"{role}.{version}.{file_name}.{sha[:12]}"
        staging.mkdir(parents=True, exist_ok=True)
        (staging / f"part-{seq:06d}").write_bytes(blob)
        parts = sorted(staging.glob("part-*"))
        if len(parts) < total:
            return {"staged": f"{seq}/{total}", "complete": False}
        if len(parts) > total:
            shutil.rmtree(staging, ignore_errors=True)
            raise AgentError("staging holds more parts than the transfer declared")

        # Reassemble and verify in the staging area; the template directory
        # is touched only after the hash proves the content (§13).
        incoming = staging / "assembled"
        hasher = hashlib.sha256()
        with incoming.open("wb") as out:
            for part in parts:
                with part.open("rb") as src:
                    shutil.copyfileobj(src, out, length=1024 * 1024)
                with part.open("rb") as src:
                    for block in iter(lambda: src.read(1024 * 1024), b""):
                        hasher.update(block)
        if hasher.hexdigest() != sha:
            shutil.rmtree(staging, ignore_errors=True)
            raise AgentError("reassembled template failed sha256 verification")
        total_bytes = sum(part.stat().st_size for part in parts)
        target_dir = self.template_dir(role, version)
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = target_dir / file_name
        os.replace(incoming, destination)
        shutil.rmtree(staging, ignore_errors=True)
        return {"staged": f"{role}/{version}/{file_name}", "complete": True, "bytes": total_bytes}

    def template_verify(self, command):
        """Prove a staged version matches what the controller pinned (§13)."""
        payload = command["payload"]
        role = payload.get("role", "")
        version = payload.get("version", "")
        expected_sha = str(payload.get("sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise AgentError("template-verify requires a 64-hex sha256")
        disk = self.template_dir(role, version) / "disk-template"
        if not disk.exists():
            raise AgentError(f"staged template missing: {disk}")
        hasher = hashlib.sha256()
        size = 0
        with disk.open("rb") as src:
            for block in iter(lambda: src.read(1024 * 1024), b""):
                hasher.update(block)
                size += len(block)
        if hasher.hexdigest() != expected_sha:
            raise AgentError(f"staged disk-template hash mismatch for {role}/{version}")
        expected_size = int(payload.get("size", size))
        if expected_size and size != expected_size:
            raise AgentError(f"staged disk-template size mismatch for {role}/{version}: {size} != {expected_size}")
        return {"verified": f"{role}/{version}", "bytes": size}

    def template_remove(self, command):
        """Remove one versioned template directory (explicit operator action)."""
        payload = command["payload"]
        role = payload.get("role", "")
        version = self._check_name(payload.get("version", ""), "template version")
        if role not in self.ROLES:
            raise AgentError(f"unknown template role: {role}")
        directory = self.images_dir / role / version
        if directory.exists():
            shutil.rmtree(directory)
            return {"removed": f"{role}/{version}"}
        return {"removed": None}

    # --- operations -------------------------------------------------------

    def lab_net(self, lab_id):
        """Per-lab isolated network name/subnet (§17). Deterministic."""
        try:
            lab_id = int(lab_id)
        except (TypeError, ValueError):
            raise AgentError("lab id missing or invalid in command payload")
        if not 1 <= lab_id <= LAB_SUBNET_LIMIT:
            raise AgentError(f"lab id {lab_id} outside the per-lab subnet range")
        name = f"{LAB_NET_PREFIX}{lab_id}{LAB_NET_SUFFIX}"
        return {
            "name": name,
            "bridge": f"ctflab{lab_id}br0",
            "gateway": f"10.{lab_id}.0.1",
            "subnet": f"10.{lab_id}.0.0/24",
            "dhcp_start": f"10.{lab_id}.0.10",
            "dhcp_end": f"10.{lab_id}.0.200",
        }

    def _ensure_lab_net(self, lab_id):
        """Define/start this lab's isolated network if not already active.

        Cross-lab and lab-to-host isolation is enforced by the host firewall
        (isolation-install.sh) plus libvirt's own per-network FORWARD rules;
        the agent never touches iptables (it runs without CAP_NET_ADMIN).
        """
        net = self.lab_net(lab_id)
        code, out = self.virsh_probe("net-info", net["name"])
        if code == 0 and "Active:     yes" in out:
            return net
        xml = (
            "<network>\n"
            f"  <name>{net['name']}</name>\n"
            f"  <bridge name='{net['bridge']}' stp='off' delay='0'/>\n"
            "  <forward mode='nat'/>\n"
            f"  <ip address='{net['gateway']}' netmask='255.255.255.0'>\n"
            "    <dhcp>\n"
            f"      <range start='{net['dhcp_start']}' end='{net['dhcp_end']}'/>\n"
            "    </dhcp>\n"
            "  </ip>\n"
            "</network>\n"
        )
        xml_path = self.disks_dir / f"{net['name']}.xml"
        xml_path.write_text(xml)
        if code != 0:
            self.virsh("net-define", str(xml_path))
        self.virsh_tolerant("net-autostart", net["name"])
        self.virsh_tolerant("net-start", net["name"])
        return net

    def _domain_network(self, name):
        """Source network of a defined domain ('' when unknown)."""
        code, out = self.virsh_probe("dumpxml", name)
        if code != 0:
            return ""
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("<source network="):
                return line.split('"')[1]
        return ""

    def _names(self, command):
        base = _domain_base(command)
        return f"ctf-{base}-atk", f"ctf-{base}-tgt", self.disks_dir / f"saved-{base}"

    def allocate(self, command):
        """Create/restore the attacker+target pair from pinned templates (§10)."""
        payload = command["payload"]
        attacker, target, saved_dir = self._names(command)
        defined = []
        try:
            return self._allocate_pair(payload, saved_dir, attacker, target, defined)
        except Exception:
            # Best-effort cleanup so failed allocations leave nothing behind.
            for domain in defined:
                self.virsh_tolerant("undefine", domain)
            for domain in (attacker, target):
                disk = self.disks_dir / f"{domain}.qcow2"
                if disk.exists():
                    disk.unlink()
            raise

    def _allocate_pair(self, payload, saved_dir, attacker, target, defined):
        if (saved_dir / "attacker.save").exists():
            # Resume saved execution state rather than a fresh boot (§12).
            # NOTE: a lab saved before the per-lab network migration resumes
            # on its saved network; move it with an admin reset-full.
            if (saved_dir / "target.save").exists():
                self.virsh("restore", str(saved_dir / "target.save"))
            self.virsh("restore", str(saved_dir / "attacker.save"))
            return {"domains": [attacker, target], "resumed": True}
        lab = payload.get("lab")
        net = self._ensure_lab_net(lab)
        expected_net = net["name"]
        attacker_template = self.template_dir("attacker", payload.get("attacker_version", ""))
        target_template = self.template_dir("target", payload.get("target_version", ""))
        vnc_port = str(payload.get("vnc_port", "5900"))
        for name, directory, role in ((attacker, attacker_template, "attacker"), (target, target_template, "target")):
            domain_xml = directory / "domain.xml"
            disk_source = self.disks_dir / f"{name}.qcow2"
            if not domain_xml.exists():
                raise AgentError(f"missing pinned template: {domain_xml} (template content is owner-supplied)")
            if self.domain_exists(name):
                # Same lab's own guest surviving from a scoped recovery (§13):
                # the attacker workspace is preserved as-is; the target is
                # always rebuilt because no direct target access is allowed.
                if role == "attacker":
                    state = self.domain_state(name)
                    port = self.vnc_port_of(name)
                    if state == "running" and port == vnc_port \
                            and self._domain_network(name) == expected_net:
                        continue  # definition matches; preserve untouched
                    # Stale definition (pre-dating per-lab ports or networks).
                    self.virsh_tolerant("destroy", name)
                    self.virsh_tolerant("undefine", name)
                    disk = self.disks_dir / f"{name}.qcow2"
                    if disk.exists():
                        disk.unlink()
                else:
                    self.virsh_tolerant("destroy", name)
                    self.virsh_tolerant("undefine", name)
            xml = (domain_xml.read_text()
                   .replace("@DISK@", str(disk_source))
                   .replace("@NAME@", name)
                   .replace("@VNC_PORT@", vnc_port)
                   .replace("@LAB_NET@", expected_net))
            xml_path = self.disks_dir / f"{name}.xml"
            xml_path.write_text(xml)
            self._ensure_disk(disk_source, directory / "disk-template", payload)
            self.virsh("define", str(xml_path))
            defined.append(name)
            self.virsh("start", name)
        return {"domains": [attacker, target], "resumed": False, "vnc_port": vnc_port}

    def domain_state(self, name):
        result = subprocess.run(
            ["virsh", "--connect", self.uri, "domstate", name],
            capture_output=True, text=True, timeout=60,
        )
        return result.stdout.strip().lower() if result.returncode == 0 else "unknown"

    def vnc_port_of(self, name):
        """Actual VNC port of a defined domain ('vnc://127.0.0.1:1' → 5901)."""
        import re

        result = subprocess.run(
            ["virsh", "--connect", self.uri, "domdisplay", name],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return "unknown"
        match = re.search(r":(\d+)$", result.stdout.strip())
        return str(5900 + int(match.group(1))) if match else "unknown"

    def _ensure_disk(self, source, template_disk, payload):
        """Per-instance writable disk backed by the versioned base image (§10)."""
        if source.exists():
            return
        if not template_disk.exists():
            raise AgentError(f"missing base image: {template_disk}")
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", str(template_disk), str(source)],
            capture_output=True, text=True, timeout=600, check=True,
        )

    def save(self, command):
        """Coordinated save-to-disk: attacker, then target (§12)."""
        attacker, target, saved_dir = self._names(command)
        saved_dir.mkdir(parents=True, exist_ok=True)
        self.virsh("save", attacker, str(saved_dir / "attacker.save"))
        self.virsh("save", target, str(saved_dir / "target.save"))
        for domain in (attacker, target):
            if self.domain_exists(domain):
                self.virsh("undefine", domain)
        return {"saved_dir": str(saved_dir)}

    def reset(self, command):
        """Destroy the pair and its writable disks; templates are recreated on
        the next allocate. Scope decides which guest is affected (§13)."""
        scope = command.get("scope", "target")
        attacker, target, saved_dir = self._names(command)
        domains = [target] if scope == "target" else [attacker, target]
        for domain in domains:
            self.virsh_tolerant("destroy", domain)
            self.virsh_tolerant("undefine", domain)
            disk = self.disks_dir / f"{domain}.qcow2"
            if disk.exists():
                disk.unlink()
        if saved_dir.exists():
            shutil.rmtree(saved_dir, ignore_errors=True)
        return {"reset": scope}

    def deadline_expired(self, command):
        """Host-local expiry: save both guests even without the controller (§11)."""
        return self.save(command)


VOCABULARY = {
    "allocate": lambda executor, command: executor.allocate(command),
    "save": lambda executor, command: executor.save(command),
    "reset-target": lambda executor, command: executor.reset({**command, "scope": "target"}),
    "reset-full": lambda executor, command: executor.reset({**command, "scope": "full"}),
    "recover": lambda executor, command: executor.reset({**command, "scope": "target"}),
    "refresh-lease": lambda executor, command: {"acknowledged": command.get("acknowledged_deadline")},
    "template-put": lambda executor, command: executor.template_put(command),
    "template-verify": lambda executor, command: executor.template_verify(command),
    "template-remove": lambda executor, command: executor.template_remove(command),
    "template-list": lambda executor, command: executor.template_inventory(),
}


def execute(executor, command):
    """Run one controller command through the restricted vocabulary."""
    operation = command.get("operation", "")
    if operation not in VOCABULARY:
        raise AgentError(f"operation not in restricted vocabulary: {operation}")
    return VOCABULARY[operation](executor, command)


class ControllerClient:
    def __init__(self, config):
        self.base = config["controller_url"].rstrip("/")
        self.token = config["token"]
        self.host_name = config["host_name"]

    def request(self, path, payload=None, method=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method or ("POST" if data is not None else "GET"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read())

    def heartbeat(self, resources):
        return self.request("/agentapi/heartbeat", resources)

    def poll(self):
        return self.request("/agentapi/commands")

    def result(self, op_id, status, detail):
        return self.request(f"/agentapi/commands/{op_id}/result", {"status": status, "result": detail})


def collect_resources(config, running_labs=0):
    # LibvirtExecutor's __init__ creates the disks directory (the images dir
    # is provisioned separately); both live on the same filesystem, so the
    # disk stat always has a real path even on a freshly provisioned host.
    executor = LibvirtExecutor(
        images_dir=config["images_dir"], disks_dir=config["disks_dir"],
    )
    target = executor.images_dir if executor.images_dir.exists() else executor.disks_dir
    usage = shutil.disk_usage(target)
    meminfo = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        meminfo[key.strip()] = int(rest.strip().split()[0])  # kB
    return {
        "agent_version": "1",
        "free_disk_gb": round(usage.free / 1e9, 2),
        "total_disk_gb": round(usage.total / 1e9, 2),
        "free_ram_gb": round(meminfo.get("MemAvailable", 0) / 1e6, 2),
        "total_ram_gb": round(meminfo.get("MemTotal", 0) / 1e6, 2),
        "running_labs": running_labs,
        "management_ip": config.get("management_ip", ""),
        "inventory": executor.template_inventory(),
    }


def run_once(client, executor, config, deadlines):
    """One heartbeat/poll/deadline cycle. Returns handled command count."""
    try:
        response = client.heartbeat(collect_resources(config, len(deadlines)))
        deadlines.update(response.get("lease_deadlines", {}))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        pass  # controller link lost; local deadlines still enforced
    handled = 0
    try:
        commands = client.poll().get("commands", [])
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        commands = []  # controller link lost; local deadlines still enforced (§18)
    for command in commands:
        deadline = command.get("acknowledged_deadline")
        if deadline:
            deadlines[command["payload"].get("lab", command["op_id"])] = deadline
        try:
            detail = execute(executor, command)
            client.result(command["op_id"], "ok", detail)
        except Exception as error:  # report failures; never retry destructively
            print(f"agent: command {command['op_id'][:8]} failed: {error}", file=sys.stderr)
            client.result(command["op_id"], "failed", {"error": str(error)[:500]})
        handled += 1
    enforce_deadlines(client, executor, deadlines)
    return handled


def enforce_deadlines(client, executor, deadlines):
    """Save any lab past its last acknowledged deadline, even offline (§11)."""
    import datetime

    now = time.time()
    for lab_id, iso in list(deadlines.items()):
        try:
            expiry = datetime.datetime.fromisoformat(iso).timestamp()
        except (ValueError, TypeError):
            continue
        if expiry <= now:
            try:
                executor.deadline_expired({"lab": lab_id, "op_id": f"local-{lab_id}"})
            except AgentError:
                print(f"agent: deadline save failed for lab {lab_id}; artifacts retained", file=sys.stderr)
            deadlines.pop(lab_id, None)


def main():
    config = load_config()
    client = ControllerClient(config)
    executor = LibvirtExecutor(images_dir=config["images_dir"], disks_dir=config["disks_dir"])
    deadlines = {}
    print(f"agent: host={config['host_name']} controller={client.base}")
    while True:
        run_once(client, executor, config, deadlines)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
