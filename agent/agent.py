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
import queue
import threading
import urllib.error
import urllib.parse
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
        """virsh probe that reports (returncode, output) instead of raising —
        for net-info/dumpxml/net-update checks. stderr is folded in: net-update
        refusals (e.g. duplicate entries) surface there. Overridable in tests
        (tests_agent's RecordingExecutor)."""
        result = subprocess.run(
            ["virsh", "--connect", self.uri, *args],
            capture_output=True, text=True, timeout=60,
        )
        return result.returncode, result.stdout + result.stderr

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
    BUILD_NET_NAME = "ctf-build-net"
    BUILD_NET_XML = (
        "<network>\n"
        f"  <name>{BUILD_NET_NAME}</name>\n"
        "  <bridge name='ctfbuildbr0' stp='off' delay='0'/>\n"
        "  <forward mode='nat'/>\n"
        "  <ip address='10.250.0.1' netmask='255.255.255.0'>\n"
        "    <dhcp>\n"
        "      <range start='10.250.0.10' end='10.250.0.50'/>\n"
        "    </dhcp>\n"
        "  </ip>\n"
        "</network>\n"
    )
    BUILD_VNC_PORT = "6911"  # labs use 5900..6899; builds live outside that range

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

    def _publish_staged(self, role, version, file_name, staged_dir, expected_sha):
        """Verify a staged file against its hash and move it into the
        template directory atomically (the version dir appears complete or
        not at all)."""
        staged_file = staged_dir / file_name
        hasher = hashlib.sha256()
        with staged_file.open("rb") as src:
            for block in iter(lambda: src.read(1024 * 1024), b""):
                hasher.update(block)
        if hasher.hexdigest() != expected_sha:
            shutil.rmtree(staged_dir, ignore_errors=True)
            raise AgentError(f"transferred {file_name} failed sha256 verification")
        target_dir = self.template_dir(role, version)
        target_dir.mkdir(parents=True, exist_ok=True)
        os.replace(staged_file, target_dir / file_name)
        shutil.rmtree(staged_dir, ignore_errors=True)
        return target_dir / file_name

    def template_fetch(self, command):
        """Stream a staged template file from the controller (Path A v2).

        The command carries only instructions + hashes — bytes flow over a
        token-authenticated streaming GET, so multi-gigabyte images never
        touch agent memory or the command queue. Idempotent: a target file
        whose hash already matches is left untouched."""
        payload = command["payload"]
        role = self._check_name(payload.get("role", ""), "role")
        if role not in self.ROLES:
            raise AgentError(f"unknown template role: {role}")
        version = self._check_name(payload.get("version", ""), "template version")
        file_name = payload.get("file", "")
        if file_name not in self.TEMPLATE_FILES:
            raise AgentError(f"refusing template file: {file_name} (closed list)")
        sha = str(payload.get("sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise AgentError("template-fetch requires a 64-hex sha256")
        base_url = payload.get("base_url") or getattr(self, "controller_base", "")
        token = payload.get("token") or getattr(self, "controller_token", "")
        if not base_url or not token:
            raise AgentError("template-fetch requires the controller base URL and token")

        target_dir = self.template_dir(role, version)
        existing = target_dir / file_name
        if existing.exists():
            existing_sha = hashlib.sha256()
            with existing.open("rb") as src:
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    existing_sha.update(block)
            if existing_sha.hexdigest() == sha:
                return {"fetched": f"{role}/{version}/{file_name}", "already_present": True}

        staging = self.disks_dir / self.STAGING_DIRNAME / f"fetch-{role}.{version}.{file_name}.{sha[:12]}"
        staging.mkdir(parents=True, exist_ok=True)
        client = ControllerClient({"controller_url": base_url, "token": token,
                                   "host_name": payload.get("host_name", "")})
        downloaded = client.download_artifact(role, version, file_name, sha, staging / file_name)
        if not downloaded:
            shutil.rmtree(staging, ignore_errors=True)
            raise AgentError(f"controller does not hold {role}/{version}/{file_name} (host-local capture?)")
        self._publish_staged(role, version, file_name, staging, sha)
        return {"fetched": f"{role}/{version}/{file_name}"}

    def template_verify(self, command):
        """Prove a staged version matches what the controller pinned (§13):
        both the base disk and the pinned domain.xml, each against its own
        content hash."""
        payload = command["payload"]
        role = payload.get("role", "")
        version = payload.get("version", "")
        expected_sha = str(payload.get("sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise AgentError("template-verify requires a 64-hex sha256")
        directory = self.template_dir(role, version)
        disk = directory / "disk-template"
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
        xml_sha = str(payload.get("xml_sha256", ""))
        if xml_sha:
            if not re.fullmatch(r"[0-9a-f]{64}", xml_sha):
                raise AgentError("template-verify xml_sha256 must be 64-hex")
            xml_path = directory / "domain.xml"
            if not xml_path.exists():
                raise AgentError(f"staged template missing: {xml_path}")
            xml_hasher = hashlib.sha256()
            with xml_path.open("rb") as src:
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    xml_hasher.update(block)
            if xml_hasher.hexdigest() != xml_sha:
                raise AgentError(f"staged domain.xml hash mismatch for {role}/{version}")
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

    # --- interactive template building (§10) -----------------------------
    # One build slot per host: a scratch VM booted from a staged base (or the
    # demo pair), customized by the operator (desktop console + SSH command
    # batches), then captured as a new versioned template. Build guests live
    # on an isolated network outside the per-lab range.

    def build_domain(self, role):
        self._check_name(role, "role")
        if role not in self.ROLES:
            raise AgentError(f"unknown template role: {role}")
        return f"ctf-build-{role}"

    def _ensure_build_net(self):
        code, out = self.virsh_probe("net-info", self.BUILD_NET_NAME)
        if code == 0 and "Active:     yes" in out:
            return
        xml_path = self.disks_dir / f"{self.BUILD_NET_NAME}.xml"
        xml_path.write_text(self.BUILD_NET_XML)
        if code != 0:
            self.virsh("net-define", str(xml_path))
        self.virsh_tolerant("net-autostart", self.BUILD_NET_NAME)
        self.virsh_tolerant("net-start", self.BUILD_NET_NAME)

    def _build_disk(self, role):
        return self.disks_dir / f"{self.build_domain(role)}.qcow2"

    def build_start(self, command):
        """Boot a scratch build VM from a staged base (or the demo pair).

        Target templates carry no VNC device (targets are never streamed to
        players), but BUILD-time targets get one injected so the operator can
        watch the provisioning. The captured template keeps the base XML
        unchanged — the console device is build-time only."""
        payload = command["payload"]
        role = self._check_name(payload.get("role", ""), "role")
        if role not in self.ROLES:
            raise AgentError(f"unknown template role: {role}")
        new_version = self._check_name(payload.get("version", ""), "template version")
        base_version = payload.get("base_version", "")
        domain = self.build_domain(role)
        if self.domain_exists(domain):
            raise AgentError(
                f"build slot busy: {domain} exists (capture or discard it first)"
            )
        base_dir = self.template_dir(role, base_version)
        base_xml = base_dir / "domain.xml"
        base_disk = base_dir / "disk-template"
        if not base_xml.exists() or not base_disk.exists():
            raise AgentError(f"missing base template: {base_dir} (template content is owner-supplied)")
        self._ensure_build_net()
        build_disk = self._build_disk(role)
        self._ensure_disk(build_disk, base_disk, payload)
        xml_text = base_xml.read_text()
        if "<graphics" not in xml_text:
            # Build-time console for target templates (and any base lacking VNC).
            xml_text = xml_text.replace(
                "</devices>",
                "  <graphics type='vnc' port='@VNC_PORT@' autoport='no' listen='127.0.0.1'/>\n  </devices>",
            )
        xml = (xml_text
               .replace("@NAME@", domain)
               .replace("@DISK@", str(build_disk))
               .replace("@VNC_PORT@", self.BUILD_VNC_PORT)
               .replace("@LAB_NET@", self.BUILD_NET_NAME))
        xml_path = self.disks_dir / f"{domain}.xml"
        xml_path.write_text(xml)
        self.virsh("define", str(xml_path))
        self.virsh("start", domain)
        return {"build": domain, "role": role, "version": new_version, "vnc_port": self.BUILD_VNC_PORT}

    def build_status(self, command):
        payload = command["payload"]
        role = payload.get("role", "")
        domain = self.build_domain(role)
        ip = self._build_guest_ip()
        return {"build": domain, "state": self.domain_state(domain), "guest_ip": ip}

    def _build_guest_ip(self):
        """The build guest's DHCP lease on the build network ('' until up).

        virsh net-dhcp-leases columns: Expiry, MAC, Protocol, IP-address,
        Hostname, Client-ID/Hostname — so the address is matched by VALUE,
        not by column position (the positional parse returned the expiry
        time and broke command batches)."""
        code, out = self.virsh_probe("net-dhcp-leases", self.BUILD_NET_NAME)
        if code != 0:
            return ""
        ipv4 = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")
        for line in out.splitlines():
            match = ipv4.search(line)
            if match and not line.lower().startswith("expiry"):
                return match.group(1)
        return ""

    def _run_ssh(self, guest_ip, ssh_user, password, script, timeout=300):
        """Run a command batch in the build guest over SSH. Overridable in tests."""
        if password:
            command = ["sshpass", "-p", password, "ssh", "-o", "StrictHostKeyChecking=no",
                       "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=15",
                       f"{ssh_user}@{guest_ip}", "sh -s"]
        else:
            command = ["ssh", "-o", "StrictHostKeyChecking=no",
                       "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=15",
                       f"{ssh_user}@{guest_ip}", "sh -s"]
        result = subprocess.run(command, input=script, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr

    def build_exec(self, command):
        """Run an operator-authored command batch inside the build guest."""
        payload = command["payload"]
        role = payload.get("role", "")
        domain = self.build_domain(role)
        if self.domain_state(domain) != "running":
            raise AgentError(f"build guest {domain} is not running")
        guest_ip = self._build_guest_ip()
        if not guest_ip:
            raise AgentError("build guest has no DHCP lease yet; retry shortly")
        ssh_user = payload.get("ssh_user") or "root"
        password = payload.get("ssh_password") or ""
        script = payload.get("script") or ""
        if not isinstance(script, str) or not script.strip():
            raise AgentError("build-exec requires a nonempty script")
        try:
            exit_code, out, err = self._run_ssh(guest_ip, ssh_user, password, script)
        except subprocess.TimeoutExpired:
            raise AgentError("build-exec timed out (300s)")
        if exit_code == 255:
            # ssh's own failure code: unreachable host, refused connection,
            # auth failure — a transport problem, not a script result.
            raise AgentError(
                "SSH connection failed (is sshd running in the guest, and are the credentials right?)"
            )
        return {
            "exit_code": exit_code,
            "script_ok": exit_code == 0,
            "stdout": out[:8000], "stderr": err[:4000], "guest_ip": guest_ip,
        }

    def _compact_disk(self, source, destination):
        """Compact a build disk into a fresh template image.

        -O qcow2 -c: compressed clusters — an installed system typically
        shrinks 3–5×. qemu decompresses transparently at boot, so nothing
        downstream (allocation, backing files, streaming transfer) changes."""
        result = subprocess.run(
            ["qemu-img", "convert", "-O", "qcow2", "-c", str(source), str(destination)],
            capture_output=True, text=True, timeout=3600, check=True,
        )
        return result

    def _graceful_shutdown(self, domain):
        """Attempt a clean ACPI shutdown, then force-off as the fallback."""
        self.virsh_tolerant("shutdown", domain)
        for _ in range(15):
            if self.domain_state(domain) in ("shut off", "shutoff"):
                return
            time.sleep(2)
        self.virsh_tolerant("destroy", domain)

    def build_capture(self, command):
        """Capture the customized build disk as a new versioned template.

        Conversion runs in the staging area and the destination is published
        atomically AFTER verification — a failed conversion leaves no
        half-built version directory behind, and a retry works."""
        payload = command["payload"]
        role = self._check_name(payload.get("role", ""), "role")
        if role not in self.ROLES:
            raise AgentError(f"unknown template role: {role}")
        version = self._check_name(payload.get("version", ""), "template version")
        domain = self.build_domain(role)
        if not self.domain_exists(domain):
            raise AgentError(f"no build guest to capture: {domain}")
        build_disk = self._build_disk(role)
        if not build_disk.exists():
            raise AgentError(f"build disk missing: {build_disk}")
        target_dir = self.images_dir / role / version
        if target_dir.exists():
            raise AgentError(f"template version already exists: {role}/{version}")
        self._graceful_shutdown(domain)
        base_xml = self.images_dir / role / "domain.xml"
        base_version = payload.get("base_version", "")
        if base_version:
            base_xml = self.template_dir(role, base_version) / "domain.xml"
        if not base_xml.exists():
            raise AgentError(f"base template xml missing: {base_xml}")
        staging = self.disks_dir / self.STAGING_DIRNAME / f"capture-{role}.{version}"
        staging.mkdir(parents=True, exist_ok=True)
        try:
            converted = staging / "disk-template"
            self._compact_disk(build_disk, converted)
            hasher = hashlib.sha256()
            size = 0
            with converted.open("rb") as src:
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    hasher.update(block)
                    size += len(block)
            shutil.copy2(base_xml, staging / "domain.xml")
            self._sanitize_captured_xml(staging / "domain.xml")
            # Publish atomically: the version directory appears complete or
            # not at all (a failed conversion leaves nothing behind).
            os.replace(staging, target_dir)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self.virsh_tolerant("undefine", domain)
        build_disk.unlink(missing_ok=True)
        (self.disks_dir / f"{domain}.xml").unlink(missing_ok=True)
        return {
            "captured": f"{role}/{version}", "sha256": hasher.hexdigest(),
            "size": size, "host_local": True,
        }

    def _sanitize_captured_xml(self, xml_path):
        """Strip build-time scaffolding from a captured template's XML.

        The captured disk is an INSTALLED OS: it must boot its own disk, not
        re-run whatever installer ISO the base template happened to carry.
        Install media rides only in the build VM — remove the cdrom device
        and the cdrom boot preference so the next scratch build from this
        template boots straight into the captured system."""
        text = xml_path.read_text()
        text = re.sub(
            r"\n?[ \t]*<disk type='file' device='cdrom'>.*?</disk>",
            "", text, flags=re.DOTALL)
        text = re.sub(r"\n?[ \t]*<boot dev='cdrom'/>", "", text)
        xml_path.write_text(text)

    def build_upload(self, command):
        """(Re-)push a captured template's artifacts to the controller.

        Reverse transfer retry for a capture whose chunked upload failed —
        the template directory is host-local and the controller record is
        waiting on its disk. Idempotent: the controller re-assembles,
        verifies the sha256, and re-attaches."""
        payload = command["payload"]
        role = self._check_name(payload.get("role", ""), "role")
        if role not in self.ROLES:
            raise AgentError(f"unknown template role: {role}")
        version = self._check_name(payload.get("version", ""), "template version")
        build_id = payload.get("build_id")
        if not build_id:
            raise AgentError("build-upload requires build_id (the controller record to attach to)")
        directory = self.template_dir(role, version)
        disk = directory / "disk-template"
        if not disk.exists():
            raise AgentError(f"nothing to upload: {disk} is missing")
        files = [(name, path) for name, path in (
            ("domain.xml", directory / "domain.xml"),
            ("disk-template", disk),
        ) if path.exists()]
        client = getattr(self, "client", None)
        if client is None:
            raise AgentError("build-upload has no controller link (agent wiring bug)")
        client.upload_build_artifacts(build_id, role, version, files)
        return {"uploaded": [name for name, _ in files], "role": role, "version": version}

    def build_discard(self, command):
        """Destroy the build guest and its scratch disk without capturing."""
        payload = command["payload"]
        role = payload.get("role", "")
        domain = self.build_domain(role)
        self._graceful_shutdown(domain)
        self.virsh_tolerant("undefine", domain)
        self._build_disk(role).unlink(missing_ok=True)
        (self.disks_dir / f"{domain}.xml").unlink(missing_ok=True)
        return {"discarded": domain}

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

    def target_mac(self, lab_id):
        """Deterministic per-lab target MAC (the hook for the DHCP pin and
        the 'target' DNS name). Derived from the lab id, so every allocation
        of the same lab reuses it."""
        return f"52:54:00:aa:{int(lab_id) % 256:02x}:01"

    def target_ip(self, lab_id):
        """The target's pinned lab address: 10.<lab>.0.10."""
        return f"10.{int(lab_id)}.0.10"

    def _lab_net_xml(self, lab_id, net):
        """The lab network definition: NAT bridge, DHCP range, the target's
        DHCP pin + DNS name, and HERMETIC DNS — dnsmasq has no upstream at
        all, so only lab-local names (target / target.lab) resolve and
        every other query gets NXDOMAIN. Labs never touch outside DNS."""
        return (
            "<network xmlns:dnsmasq='http://libvirt.org/schemas/network/dnsmasq/1.0'>\n"
            f"  <name>{net['name']}</name>\n"
            f"  <bridge name='{net['bridge']}' stp='off' delay='0'/>\n"
            "  <forward mode='nat'/>\n"
            "  <domain name='lab' localOnly='yes'/>\n"
            "  <dns forwardPlainNames='no'/>\n"
            f"  <ip address='{net['gateway']}' netmask='255.255.255.0'>\n"
            "    <dhcp>\n"
            f"      <range start='{net['dhcp_start']}' end='{net['dhcp_end']}'/>\n"
            f"      <host mac='{self.target_mac(lab_id)}' name='target' ip='{self.target_ip(lab_id)}'/>\n"
            "    </dhcp>\n"
            "  </ip>\n"
            "  <dnsmasq:options>\n"
            "    <dnsmasq:option value='no-resolv'/>\n"
            "  </dnsmasq:options>\n"
            "</network>\n"
        )

    def _ensure_lab_net(self, lab_id):
        """Define/start this lab's isolated network if not already active.

        Cross-lab and lab-to-host isolation is enforced by the host firewall
        (isolation-install.sh) plus libvirt's own per-network FORWARD rules;
        the agent never touches iptables (it runs without CAP_NET_ADMIN).
        Networks defined before the hermetic-DNS change are redefined when
        nothing is attached to them (a released lab's guests are undefined,
        so at allocate time the net is empty; a preserved recovery attacker
        keeps its net untouched)."""
        net = self.lab_net(lab_id)
        xml_path = self.disks_dir / f"{net['name']}.xml"
        xml_path.write_text(self._lab_net_xml(lab_id, net))
        code, out = self.virsh_probe("net-info", net["name"])
        defined = code == 0
        active = defined and "Active:     yes" in out
        if defined and active:
            dump_code, dump = self.virsh_probe("net-dumpxml", net["name"])
            if dump_code == 0 and "localOnly" not in dump:
                if "connections='0'" in dump or "connections=" not in dump:
                    if "connections=" not in dump:
                        print(f"agent: {net['name']} predates hermetic DNS but "
                              "reports no attachment count; leaving it alone",
                              file=sys.stderr)
                    else:
                        self.virsh_tolerant("net-destroy", net["name"])
                        self.virsh_tolerant("net-undefine", net["name"])
                        defined = active = False
                else:
                    print(f"agent: {net['name']} has attached guests; its DNS "
                          "stays upstream-forwarding until the lab is fully recycled",
                          file=sys.stderr)
        if not defined:
            self.virsh("net-define", str(xml_path))
        if not active:
            self.virsh_tolerant("net-autostart", net["name"])
            self.virsh_tolerant("net-start", net["name"])
        self._ensure_target_dns(net, lab_id)
        return net

    def _ensure_target_dns(self, net, lab_id):
        """Register the 'target' DNS name on the lab network's dnsmasq.

        A static dhcp-host entry does double duty: it pins the target's
        lease to 10.<lab>.0.10 AND dnsmasq serves that hostname in the
        network's DNS zone, so attackers resolve 'target' without learning
        the subnet. Called on every allocate: for freshly defined networks
        the entry already rode in the XML and the update is a harmless
        duplicate refusal; for networks defined before this feature it adds
        the pin without disturbing a running lease."""
        host_xml = (
            f"<host mac='{self.target_mac(lab_id)}' "
            f"name='target' ip='{self.target_ip(lab_id)}'/>"
        )
        code, out = self.virsh_probe(
            "net-update", net["name"], "add", "dhcp", "host", host_xml,
            "--live", "--config")
        if code != 0 and "already in use" not in out and "duplicate" not in out.lower() \
                and "already exists" not in out.lower():
            print(f"agent: target DNS pin not applied on {net['name']}: "
                  f"{out.strip()[:200]}", file=sys.stderr)

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
            lab = int(payload.get("lab", 0))
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
            if role == "target":
                # Deterministic target identity (§17): a per-lab MAC so the
                # lab network can pin the target's DHCP lease (10.<lab>.0.10)
                # and register the DNS name "target" for it — players resolve
                # the target without ever learning the subnet.
                marker = "<interface type='network'>"
                if marker in xml:
                    xml = xml.replace(
                        marker,
                        f"{marker}\n      <mac address='{self.target_mac(lab)}'/>",
                        1)
                else:
                    print(f"agent: target template {domain_xml.name} has no "
                          "network interface; target DNS pin impossible",
                          file=sys.stderr)
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
        """Coordinated save-to-disk: attacker, then target (§12).

        Idempotent under the dual-expiry race: the controller's durable save
        and the agent's own host-local deadline enforcement (§18) can both
        fire at lease expiry. Whichever runs second finds the domains already
        saved and undefined; that is a successful no-op, not a failure that
        would push the lab into recovery."""
        attacker, target, saved_dir = self._names(command)
        saved_dir.mkdir(parents=True, exist_ok=True)
        already_saved = []
        for domain, filename in ((attacker, "attacker.save"), (target, "target.save")):
            if self.domain_exists(domain):
                self.virsh("save", domain, str(saved_dir / filename))
            elif (saved_dir / filename).exists():
                already_saved.append(domain)  # a concurrent save captured it already
            else:
                raise AgentError(
                    f"virsh save {domain} failed: domain missing and no prior save exists"
                )
        for domain in (attacker, target):
            if self.domain_exists(domain):
                self.virsh("undefine", domain)
        return {"saved_dir": str(saved_dir), "already_saved": already_saved}

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
    "template-remove": lambda executor, command: executor.template_remove(command),
    "template-fetch": lambda executor, command: executor.template_fetch(command),
    "template-verify": lambda executor, command: executor.template_verify(command),
    "template-list": lambda executor, command: executor.template_inventory(),
    "build-start": lambda executor, command: executor.build_start(command),
    "build-status": lambda executor, command: executor.build_status(command),
    "build-exec": lambda executor, command: executor.build_exec(command),
    "build-capture": lambda executor, command: executor.build_capture(command),
    "build-discard": lambda executor, command: executor.build_discard(command),
    "build-upload": lambda executor, command: executor.build_upload(command),
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

    def request(self, path, payload=None, method=None, timeout=15):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            self.base + path,
            data=data,
            method=method or ("POST" if data is not None else "GET"),
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())

    def heartbeat(self, resources):
        return self.request("/agentapi/heartbeat", resources)

    def poll(self):
        return self.request("/agentapi/commands")

    def result(self, op_id, status, detail):
        return self.request(f"/agentapi/commands/{op_id}/result", {"status": status, "result": detail})

    def upload_build_artifacts(self, build_id, role, version, files, disk_sha="", chunk_bytes=1024 * 1024):
        """Push captured build artifacts back to the controller, chunked.

        Keeps multi-gigabyte captures out of agent memory: each chunk is
        read, base64-encoded, and POSTed immediately; the controller
        assembles on disk and verifies against the capture hash. The chunk
        rides inside a JSON body as base64 (1.37× inflation), so 1 MiB
        chunks stay under Django's 2.5MB DATA_UPLOAD_MAX_MEMORY_SIZE —
        2 MiB chunks are rejected wholesale and the push dies on chunk 1."""
        for file_name, path in files:
            if not path.exists():
                continue  # e.g. target templates may omit nothing; domain.xml always exists post-capture
            file_sha = hashlib.sha256()
            with path.open("rb") as src:
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    file_sha.update(block)
            file_sha = file_sha.hexdigest()
            size = path.stat().st_size
            total = max(1, -(-size // chunk_bytes))
            with path.open("rb") as src:
                seq = 0
                while True:
                    block = src.read(chunk_bytes)
                    if not block:
                        break
                    seq += 1
                    self.request("/agentapi/build-artifact", {
                        "build_id": build_id, "role": role, "version": version,
                        "file": file_name, "seq": seq, "total": total,
                        "sha256": file_sha, "size": size,
                        "chunk_bytes": chunk_bytes,
                        "data_b64": base64.b64encode(block).decode("ascii"),
                    }, timeout=600)
                    # The FINAL chunk returns only after the controller has
                    # streamed-verified the whole file — give it minutes.

    def download_artifact(self, role, version, file_name, sha256, destination):
        """Stream one staged template file from the controller to destination.

        Returns False when the controller reports it missing (already
        present, or a host-local image without a controller-side file)."""
        query = urllib.parse.urlencode({"role": role, "version": version, "file": file_name})
        request = urllib.request.Request(
            f"{self.base}/agentapi/template-artifact?{query}",
            headers={"Authorization": f"Bearer {self.token}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                hasher = hashlib.sha256()
                with destination.open("wb") as out:
                    while True:
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        hasher.update(block)
                        out.write(block)
        except urllib.error.HTTPError as error:
            if error.code in (404, 409):
                return False
            raise
        if sha256 and hasher.hexdigest() != sha256:
            destination.unlink(missing_ok=True)
            raise AgentError(f"downloaded {file_name} failed sha256 verification")
        return True


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


def run_once(client, executor, config, deadlines, build_inline=False):
    """One heartbeat/poll/deadline cycle. Returns handled command count.

    Build operations run on a dedicated worker thread (build-exec batches
    can run five minutes, disk conversion thirty) so heartbeats and
    host-local lease enforcement keep flowing while they work. With
    build_inline=True the caller executes them synchronously (tests)."""
    try:
        response = client.heartbeat(collect_resources(config, len(deadlines)))
        deadlines.update(response.get("lease_deadlines", {}))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        pass  # controller link lost; local deadlines still enforced
    # The executor normally never talks to the controller, but build-upload
    # (the reverse artifact transfer) is the exception — wire the link on.
    executor.client = client
    handled = 0
    try:
        commands = client.poll().get("commands", [])
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        commands = []  # controller link lost; local deadlines still enforced (§18)
    for command in commands:
        deadline = command.get("acknowledged_deadline")
        if deadline:
            deadlines[command["payload"].get("lab", command["op_id"])] = deadline
        op_id = command.get("op_id", "")
        if op_id in IN_FLIGHT:
            continue  # long build ops stay PENDING until their result lands;
            # the controller re-polls them every cycle — execute them once.
        IN_FLIGHT.add(op_id)
        if command.get("operation", "").startswith("build-") and not build_inline:
            BUILD_QUEUE.put((client, executor, command))
            continue
        try:
            detail = execute(executor, command)
            client.result(command["op_id"], "ok", detail)
            _post_build_side_effects(client, executor, command, detail)
        except Exception as error:  # report failures; never retry destructively
            print(f"agent: command {command['op_id'][:8]} failed: {error}", file=sys.stderr)
            client.result(command["op_id"], "failed", {"error": str(error)[:500]})
        finally:
            IN_FLIGHT.discard(command.get("op_id", ""))
        handled += 1
    enforce_deadlines(client, executor, deadlines)
    return handled


def _post_build_side_effects(client, executor, command, detail):
    """After a successful capture, push the captured artifacts back to the
    controller so the MachineImage record becomes fully distributed-able."""
    if command.get("operation") != "build-capture" or not detail.get("captured"):
        return
    role = command["payload"].get("role", "")
    version = command["payload"].get("version", "")
    build_id = command["payload"].get("build_id")
    if not build_id:
        return  # older controller: capture stays host-local
    directory = executor.template_dir(role, version)
    try:
        client.upload_build_artifacts(
            build_id, role, version,
            files=(
                ("domain.xml", directory / "domain.xml"),
                ("disk-template", directory / "disk-template"),
            ),
            disk_sha=detail.get("sha256", ""),
        )
        print(f"agent: captured {role}/{version} uploaded to controller", file=sys.stderr)
    except Exception as error:
        # The template is already staged on this host; the controller keeps
        # the host-local record and the operator can re-capture or upload.
        print(f"agent: artifact upload failed for {role}/{version}: {error}", file=sys.stderr)


BUILD_QUEUE = queue.Queue()
IN_FLIGHT = set()  # op_ids currently being executed; pending build ops are
# re-polled every cycle until their result lands, so run_once must not
# enqueue the same long command again while it runs.


def build_worker(client, executor):
    """Dedicated build-operation worker: long batches and disk conversion
    never block the heartbeat/poll/lease-enforcement loop."""
    while True:
        command = BUILD_QUEUE.get()
        if command is None:
            return
        _, _, _ = command
        c, e, cmd = command
        e.client = c  # build-upload (reverse transfer) needs the controller link
        try:
            detail = execute(e, cmd)
            c.result(cmd["op_id"], "ok", detail)
            _post_build_side_effects(c, e, cmd, detail)
        except Exception as error:
            print(f"agent: command {cmd['op_id'][:8]} failed: {error}", file=sys.stderr)
            c.result(cmd["op_id"], "failed", {"error": str(error)[:500]})
        finally:
            IN_FLIGHT.discard(cmd.get("op_id", ""))
        BUILD_QUEUE.task_done()


def ensure_build_worker(client, executor):
    worker = threading.Thread(target=build_worker, args=(client, executor), daemon=True)
    worker.start()
    return worker


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
    executor.controller_base = client.base
    executor.controller_token = client.token
    deadlines = {}
    ensure_build_worker(client, executor)
    print(f"agent: host={config['host_name']} controller={client.base}")
    while True:
        run_once(client, executor, config, deadlines)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
