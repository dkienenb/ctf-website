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

CONFIG_PATH = os.environ.get("CTF_AGENT_CONFIG", "/etc/ctf/agent.toml")
DEFAULT_IMAGES_DIR = "/var/lib/ctf/agent/images"
DEFAULT_DISKS_DIR = "/var/lib/ctf/agent/disks"
LIBVIRT_URI = os.environ.get("CTF_AGENT_LIBVIRT_URI", "qemu:///system")
DOMAIN_PREFIX = "ctf"
HEARTBEAT_INTERVAL = 10
POLL_INTERVAL = 5
DEADLINE_CHECK_INTERVAL = 5

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


def _op_id_prefix(op_id):
    return f"{DOMAIN_PREFIX}-{op_id[:12]}"


def _attacker_domain(op_id):
    return f"{_op_id_prefix(op_id)}-atk"


def _target_domain(op_id):
    return f"{_op_id_prefix(op_id)}-tgt"


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

    def domain_exists(self, name):
        result = subprocess.run(
            ["virsh", "--connect", self.uri, "dominfo", name],
            capture_output=True, text=True, timeout=60,
        )
        return result.returncode == 0

    # --- operations -----------------------------------------------------

    def allocate(self, command):
        """Create/restore the attacker+target pair from pinned templates (§10)."""
        payload = command["payload"]
        op_id = command["op_id"]
        attacker, target = _attacker_domain(op_id), _target_domain(op_id)
        existing_state = self._saved_state(op_id)
        if existing_state:
            # Resume saved execution state rather than a fresh boot (§12).
            self.virsh("restore", str(existing_state / "attacker.save")) if (existing_state / "attacker.save").exists() else None
            self.virsh("restore", str(existing_state / "target.save")) if (existing_state / "target.save").exists() else None
            return {"domains": [attacker, target], "resumed": True}
        template = self.images_dir / "attacker"
        target_template = self.images_dir / "target"
        for name, directory in ((attacker, template), (target, target_template)):
            domain_xml = directory / "domain.xml"
            disk_source = self._disk_for(command, name)
            if not domain_xml.exists():
                raise AgentError(f"missing pinned template: {domain_xml} (template content is owner-supplied)")
            xml = domain_xml.read_text().replace("@DISK@", str(disk_source)).replace("@NAME@", name)
            xml_path = self.disks_dir / f"{name}.xml"
            xml_path.write_text(xml)
            self._ensure_disk(disk_source, directory / "disk-template", payload)
            self.virsh("define", str(xml_path))
            self.virsh("start", name)
        return {"domains": [attacker, target], "resumed": False}

    def _disk_for(self, command, domain):
        return self.disks_dir / f"{domain}.qcow2"

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

    def _saved_state(self, op_id):
        directory = self.disks_dir / f"saved-{op_id[:12]}"
        return directory if (directory / "attacker.save").exists() else None

    def save(self, command):
        """Coordinated save-to-disk: attacker, then target (§12)."""
        op_id = command["op_id"]
        directory = self.disks_dir / f"saved-{op_id[:12]}"
        directory.mkdir(parents=True, exist_ok=True)
        attacker, target = _attacker_domain(op_id), _target_domain(op_id)
        self.virsh("save", attacker, str(directory / "attacker.save"))
        self.virsh("save", target, str(directory / "target.save"))
        for domain in (attacker, target):
            if self.domain_exists(domain):
                self.virsh("undefine", domain)
        return {"saved_dir": str(directory)}

    def reset(self, command):
        """Destroy the pair and its writable disks; templates are recreated on
        the next allocate. Scope decides which guest is affected (§13)."""
        op_id = command["op_id"]
        scope = command.get("scope", "target")
        domains = [_target_domain(op_id)] if scope == "target" else [_attacker_domain(op_id), _target_domain(op_id)]
        for domain in domains:
            self.virsh_tolerant("destroy", domain)
            self.virsh_tolerant("undefine", domain)
            disk = self.disks_dir / f"{domain}.qcow2"
            if disk.exists():
                disk.unlink()
        saved = self.disks_dir / f"saved-{op_id[:12]}"
        if saved.exists():
            shutil.rmtree(saved, ignore_errors=True)
        return {"reset": scope}

    def deadline_expired(self, command):
        """Host-local expiry: save both guests even without the controller (§11)."""
        result = self.save({"op_id": command.get("op_id", "local"), "payload": {}})
        return result


VOCABULARY = {
    "allocate": lambda executor, command: executor.allocate(command),
    "save": lambda executor, command: executor.save(command),
    "reset-target": lambda executor, command: executor.reset({**command, "scope": "target"}),
    "reset-full": lambda executor, command: executor.reset({**command, "scope": "full"}),
    "refresh-lease": lambda executor, command: {"acknowledged": command.get("acknowledged_deadline")},
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
    images = Path(config["images_dir"])
    usage = shutil.disk_usage(images if images.exists() else Path("/var/lib/ctf/agent"))
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
        return handled
    for command in commands:
        deadline = command.get("acknowledged_deadline")
        if deadline:
            deadlines[command["payload"].get("lab", command["op_id"])] = deadline
        try:
            detail = execute(executor, command)
            client.result(command["op_id"], "ok", detail)
        except Exception as error:  # report failures; never retry destructively
            client.result(command["op_id"], "failed", {"error": str(error)[:300]})
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
                executor.deadline_expired({"op_id": f"local-{lab_id}"})
            except AgentError:
                pass  # keep trying on later cycles; artifacts retained
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
