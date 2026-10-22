"""SSH host probe and parser for NPU, CPU, memory, disk, mount, Docker, and `/proc` telemetry.

[Role] Executes bounded remote shell probes over SSH and parses raw sections into structured host snapshots.
[Invariants] Observation-only; `npu_info_rc` gates NPU health while optional `usages`/Docker/process-detail failures degrade gracefully without hiding NPU errors.
[Timeouts & Locks] Remote `npu-smi` commands use inner `timeout 8` and Docker commands use `timeout 5`/`timeout 10`; outer SSH timeout is `max(ssh_timeout + 25, 45)` when infrastructure is included (`>= sum(inner_budgets) + grace`). The optional `-t common` frequency pass has an 8 s inner budget, per-card `timeout 2`, a 3 s cleanup grace, and outer `budget + 2 + 3` (never a `min` that truncates the inner budget); a killed pass keeps any complete card sections already printed. The optional DCMI pass is one bounded `python3 -` helper (8 s) and is skipped with a structured status when the configured SSH timeout is smaller.
[Side-Effect Policy] Read-only remote SSH probes (`/proc`, `df`, `findmnt`, `docker ps/stats/info`, `npu-smi info`, `npu-smi info -t common`, DCMI `dcmi_init` + `dcmi_get_device_info`); never mutates remote host state or submits NPU work.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from . import dcmi_telemetry
from .device_adapter import DeviceAdapter
from .npu_smi import parse_npu_common


SECTION = "__NFM_SECTION__"
EMPLOYEE_ID_PATTERN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,3}\d{7,9})(?![A-Za-z0-9])")
INITIALS_PATTERN = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,4})(?![A-Za-z0-9])")
INITIALS_STOP_WORDS = {
    "app", "bin", "data", "dev", "etc", "home", "lib", "log", "logs", "mnt",
    "opt", "proc", "root", "run", "src", "sys", "test", "tmp", "usr", "var",
    "vllm", "work",
}
FAST_SCRIPT = r'''set +e
if [ -f /etc/profile.d/mindie-ascend-env.sh ]; then . /etc/profile.d/mindie-ascend-env.sh >/dev/null 2>&1; fi
export LD_LIBRARY_PATH=/usr/local/Ascend/driver/lib64/common:/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64:${LD_LIBRARY_PATH:-}
printf '__NFM_SECTION__hostname\n'; hostname 2>/dev/null
printf '__NFM_SECTION__proc_stat\n'; head -n 1 /proc/stat 2>/dev/null
printf '__NFM_SECTION__loadavg\n'; cat /proc/loadavg 2>/dev/null
printf '__NFM_SECTION__meminfo\n'; cat /proc/meminfo 2>/dev/null
printf '__NFM_SECTION__uptime\n'; cat /proc/uptime 2>/dev/null
printf '__NFM_SECTION__npu_info\n'; timeout 8 npu-smi info 2>&1; nfm_rc=$?
printf '__NFM_SECTION__npu_info_rc\n%s\n' "$nfm_rc"
printf '__NFM_SECTION__npu_usages\n'; timeout 8 npu-smi info -t usages 2>&1
'''

INFRA_SCRIPT = r'''set +e
printf '__NFM_SECTION__disk\n'; df -P -B1 -x tmpfs -x devtmpfs -x overlay 2>/dev/null
printf '__NFM_SECTION__mounts\n'; if command -v findmnt >/dev/null 2>&1; then findmnt -J -o TARGET,SOURCE,FSTYPE,OPTIONS 2>/dev/null; else cat /proc/mounts 2>/dev/null; fi
printf '__NFM_SECTION__docker\n'; if command -v docker >/dev/null 2>&1; then timeout 5 docker ps --no-trunc --format '{{json .}}' 2>&1; else printf 'unavailable\n'; fi
printf '__NFM_SECTION__docker_stats\n'; if command -v docker >/dev/null 2>&1; then timeout 10 docker stats --no-stream --format '{{json .}}' 2>/dev/null; fi
printf '__NFM_SECTION__docker_info\n'; if command -v docker >/dev/null 2>&1; then timeout 5 docker info --format '{{json .}}' 2>/dev/null; fi
'''

FREQUENCY_BUDGET_SECONDS = 8
FREQUENCY_CARD_TIMEOUT_SECONDS = 2
FREQUENCY_CLEANUP_GRACE_SECONDS = 3
FREQUENCY_OUTER_TIMEOUT_SECONDS = (
    FREQUENCY_BUDGET_SECONDS + FREQUENCY_CARD_TIMEOUT_SECONDS + FREQUENCY_CLEANUP_GRACE_SECONDS
)
DCMI_TIMEOUT_SECONDS = 8
FREQUENCY_SCRIPT = r'''set +e
nfm_start=$SECONDS
nfm_rc_all=0
for nfm_card in __NFM_CARDS__; do
    if [ $((SECONDS - nfm_start)) -ge __NFM_BUDGET__ ]; then break; fi
    printf '__NFM_SECTION__common_%s\n' "$nfm_card"
    timeout __NFM_CARD_TIMEOUT__ npu-smi info -t common -i "$nfm_card" 2>&1
    nfm_rc=$?
    printf '__NFM_SECTION__common_rc_%s\n%s\n' "$nfm_card" "$nfm_rc"
    if [ "$nfm_rc" -ne 0 ]; then nfm_rc_all=$nfm_rc; fi
done
exit "$nfm_rc_all"
'''

PROCESS_DETAIL_SCRIPT = r'''set +e
nfm_b64() { printf '%s' "$1" | base64 | tr -d '\n'; }
printf '__NFM_SECTION__process_details\n'
for nfm_pid in __NFM_PIDS__; do
    [ -d "/proc/$nfm_pid" ] || continue
    nfm_user="$(stat -c '%U' "/proc/$nfm_pid" 2>/dev/null)"
    nfm_cwd="$(readlink "/proc/$nfm_pid/cwd" 2>/dev/null)"
    nfm_exe="$(readlink "/proc/$nfm_pid/exe" 2>/dev/null)"
    nfm_name="$(cat "/proc/$nfm_pid/comm" 2>/dev/null)"
    nfm_cmdline="$(tr '\000' ' ' < "/proc/$nfm_pid/cmdline" 2>/dev/null)"
    nfm_cgroup="$(cat "/proc/$nfm_pid/cgroup" 2>/dev/null)"
    nfm_state="$(awk '/^State:/ {print $2}' "/proc/$nfm_pid/status" 2>/dev/null)"
    nfm_wchan="$(tr -d '\n' < "/proc/$nfm_pid/wchan" 2>/dev/null)"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$nfm_pid" "$(nfm_b64 "$nfm_user")" "$(nfm_b64 "$nfm_cwd")" \
        "$(nfm_b64 "$nfm_cmdline")" "$(nfm_b64 "$nfm_exe")" \
        "$(nfm_b64 "$nfm_name")" "$(nfm_b64 "$nfm_cgroup")" \
        "$(nfm_b64 "$nfm_state")" "$(nfm_b64 "$nfm_wchan")"
done
'''


def split_sections(output: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in output.splitlines():
        if line.startswith(SECTION):
            current = line[len(SECTION):].strip()
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in sections.items()}


def parse_meminfo(text: str) -> dict[str, int | None]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"([^:]+):\s+(\d+)", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    swap_total = values.get("SwapTotal")
    swap_free = values.get("SwapFree")
    return {
        "memory_total_bytes": total,
        "memory_used_bytes": total - available if total is not None and available is not None else None,
        "swap_total_bytes": swap_total,
        "swap_used_bytes": swap_total - swap_free if swap_total is not None and swap_free is not None else None,
    }


def parse_cpu_stat(text: str) -> tuple[int, int] | None:
    tokens = text.split()
    if not tokens or tokens[0] != "cpu":
        return None
    try:
        values = [int(value) for value in tokens[1:]]
    except ValueError:
        return None
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


def cpu_percent(previous: tuple[int, int] | None, current: tuple[int, int] | None) -> float | None:
    if not previous or not current:
        return None
    total = current[0] - previous[0]
    idle = current[1] - previous[1]
    if total <= 0:
        return None
    return round(max(0.0, min(100.0, (total - idle) * 100.0 / total)), 1)


def _is_excluded_storage(source: str | None, target: str | None, fstype: str | None = None) -> bool:
    mount = str(target or "").rstrip("/")
    excluded_mounts = ("/boot", "/efi", "/var/lib/docker/overlay", "/var/lib/docker/overlay2")
    return (
        str(source or "").casefold() == "overlay"
        or str(fstype or "").casefold() == "overlay"
        or any(mount == base or mount.startswith(base + "/") for base in excluded_mounts)
    )


def parse_disks(text: str) -> list[dict[str, Any]]:
    disks: list[dict[str, Any]] = []
    for line in text.splitlines()[1:]:
        cells = line.split()
        if len(cells) < 6:
            continue
        try:
            total, used, available = int(cells[1]), int(cells[2]), int(cells[3])
            percent = float(cells[4].rstrip("%"))
        except ValueError:
            continue
        mount = " ".join(cells[5:])
        if _is_excluded_storage(cells[0], mount):
            continue
        disks.append({
            "filesystem": cells[0], "total_bytes": total, "used_bytes": used,
            "available_bytes": available, "used_percent": percent, "mount": mount,
        })
    return disks


def parse_mounts(text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
        mounts = []

        def append_filesystems(filesystems: list[dict[str, Any]]) -> None:
            for item in filesystems:
                if not _is_excluded_storage(item.get("source"), item.get("target"), item.get("fstype")):
                    mounts.append({
                        "target": item.get("target"), "source": item.get("source"),
                        "fstype": item.get("fstype"), "options": item.get("options"),
                    })
                append_filesystems(item.get("children") or [])

        append_filesystems(payload.get("filesystems", []))
        return mounts
    except (json.JSONDecodeError, AttributeError):
        mounts = []
        for line in text.splitlines():
            cells = line.split()
            if len(cells) >= 4 and not _is_excluded_storage(cells[0], cells[1], cells[2]):
                mounts.append({"source": cells[0], "target": cells[1], "fstype": cells[2], "options": cells[3]})
        return mounts


def parse_docker(text: str, stats_text: str = "", info_text: str = "") -> dict[str, Any]:
    if text.strip() == "unavailable":
        return {"available": False, "containers": [], "running": 0}
    containers = []
    error = None
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            error = line[-500:]
            continue
        containers.append({
            "id": item.get("ID"), "name": item.get("Names"), "image": item.get("Image"),
            "status": item.get("Status"), "state": item.get("State"), "ports": item.get("Ports"),
        })
    stats: dict[str, dict[str, Any]] = {}
    for line in stats_text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        key = str(item.get("Name") or item.get("Container") or "")
        if key:
            stats[key] = {
                "cpu_percent": item.get("CPUPerc"), "memory": item.get("MemUsage"),
                "memory_percent": item.get("MemPerc"), "network_io": item.get("NetIO"),
                "block_io": item.get("BlockIO"), "pids": item.get("PIDs"),
            }
    for container in containers:
        container["stats"] = stats.get(str(container.get("name"))) or stats.get(str(container.get("id")))
    info: dict[str, Any] = {}
    try:
        raw_info = json.loads(info_text)
        info = {
            "server_version": raw_info.get("ServerVersion"), "driver": raw_info.get("Driver"),
            "docker_root_dir": raw_info.get("DockerRootDir"), "containers": raw_info.get("Containers"),
            "containers_running": raw_info.get("ContainersRunning"), "containers_stopped": raw_info.get("ContainersStopped"),
        }
    except (json.JSONDecodeError, AttributeError):
        pass
    return {"available": error is None, "containers": containers, "running": len(containers), "info": info, "error": error}


def build_process_detail_script(pids: list[int]) -> str:
    safe_pids = " ".join(str(pid) for pid in sorted(set(pids)) if pid > 0)
    return PROCESS_DETAIL_SCRIPT.replace("__NFM_PIDS__", safe_pids) + "\nexit 0\n"


def build_frequency_script(
    npu_ids: list[int], budget_seconds: int = FREQUENCY_BUDGET_SECONDS,
) -> str:
    """Build a bounded `-t common` query for validated integer card ids.

    The script keeps each card's exit code in a `common_rc_<card>` section and
    exits nonzero if any card failed, so a read-only sampling failure cannot be
    hidden by a trailing `exit 0`.
    """
    cards = sorted({int(npu_id) for npu_id in npu_ids
                    if not isinstance(npu_id, bool) and isinstance(npu_id, int) and npu_id >= 0})
    if not cards:
        return ""
    budget = max(1, int(budget_seconds))
    return (
        FREQUENCY_SCRIPT
        .replace("__NFM_CARDS__", " ".join(str(card) for card in cards))
        .replace("__NFM_BUDGET__", str(budget))
        .replace("__NFM_CARD_TIMEOUT__", str(FREQUENCY_CARD_TIMEOUT_SECONDS))
    )


def frequency_probe_plan(configured_timeout: int) -> tuple[int, int] | None:
    """Return `(inner_budget, outer_timeout)` with outer >= budget + card + grace.

    The optional pass is skipped when the configured SSH timeout cannot cover
    one card plus cleanup; the outer timeout never truncates the inner budget.
    """
    available = int(configured_timeout) - FREQUENCY_CARD_TIMEOUT_SECONDS - FREQUENCY_CLEANUP_GRACE_SECONDS
    if available < 1:
        return None
    budget = min(FREQUENCY_BUDGET_SECONDS, available)
    return budget, budget + FREQUENCY_CARD_TIMEOUT_SECONDS + FREQUENCY_CLEANUP_GRACE_SECONDS


def _decode_process_field(value: str) -> str | None:
    if not value:
        return None
    try:
        decoded = base64.b64decode(value).decode("utf-8", errors="replace").strip()
    except (ValueError, TypeError):
        return None
    return decoded or None


def extract_container_id(cgroup: str | None) -> str | None:
    if not cgroup:
        return None
    match = re.search(r"\b([0-9a-f]{64})\b", cgroup, re.IGNORECASE)
    return match.group(1).lower() if match else None


def extract_ownership_labels(cwd: str | None, container_name: str | None) -> list[dict[str, Any]]:
    sources = (("pwd", cwd), ("container", container_name))
    labels: dict[tuple[str, str], dict[str, Any]] = {}
    for kind, pattern in (("employee_id", EMPLOYEE_ID_PATTERN), ("initials", INITIALS_PATTERN)):
        for source, text in sources:
            if not text:
                continue
            for match in pattern.finditer(text):
                value = match.group(1)
                normalized = value.casefold()
                if kind == "initials" and normalized in INITIALS_STOP_WORDS:
                    continue
                key = (kind, normalized)
                label = labels.setdefault(key, {"value": value, "kind": kind, "sources": []})
                if source not in label["sources"]:
                    label["sources"].append(source)
    return list(labels.values())


_DISK_WAIT_WCHAN_TOKENS = ("lock_page", "folio_wait", "nfs_", "ceph_", "blk_mq", "io_schedule", "wait_on_page")


def _is_disk_io_wait(proc_state: str | None, wchan: str | None) -> bool:
    st = str(proc_state or "").strip().upper()
    wc = str(wchan or "").strip().lower()
    return st == "D" or any(tok in wc for tok in _DISK_WAIT_WCHAN_TOKENS)


def parse_process_details(text: str) -> dict[int, dict[str, Any]]:
    details: dict[int, dict[str, Any]] = {}
    for line in text.splitlines():
        cells = line.split("\t")
        if len(cells) not in (7, 9) or not cells[0].isdigit():
            continue
        user, cwd, command, executable, name, cgroup = (_decode_process_field(value) for value in cells[1:7])
        proc_state = _decode_process_field(cells[7]) if len(cells) >= 9 else None
        wchan = _decode_process_field(cells[8]) if len(cells) >= 9 else None
        pid = int(cells[0])
        details[pid] = {
            "pid": pid,
            "name": name,
            "user": user,
            "cwd": cwd,
            "command": command,
            "executable": executable,
            "container_id": extract_container_id(cgroup),
            "proc_state": proc_state,
            "wchan": wchan,
            "disk_io_wait": _is_disk_io_wait(proc_state, wchan),
        }
    return details


def attach_process_details(
    devices: list[dict[str, Any]],
    details: dict[int, dict[str, Any]],
    docker: dict[str, Any],
) -> None:
    containers = docker.get("containers") or []
    by_id = {
        str(container.get("id") or "").lower(): container
        for container in containers
        if container.get("id")
    }
    for device in devices:
        enriched = []
        for record in device.get("processes") or []:
            pid = int(record["pid"])
            detail = details.get(pid) or {}
            container_id = str(detail.get("container_id") or "").lower()
            container = None
            if container_id:
                match = next(
                    (item for item_id, item in by_id.items() if item_id == container_id or item_id.startswith(container_id) or container_id.startswith(item_id)),
                    None,
                )
                container = {
                    "id": (match or {}).get("id") or container_id,
                    "short_id": container_id[:12],
                    "name": (match or {}).get("name"),
                    "image": (match or {}).get("image"),
                    "status": (match or {}).get("status"),
                    "source": "cgroup" if match else "cgroup-unmatched",
                }
            enriched.append({
                **record,
                "name": detail.get("name") or record.get("npu_process_name"),
                "user": detail.get("user"),
                "cwd": detail.get("cwd"),
                "command": detail.get("command"),
                "executable": detail.get("executable"),
                "proc_state": detail.get("proc_state"),
                "wchan": detail.get("wchan"),
                "disk_io_wait": bool(detail.get("disk_io_wait")),
                "details_observed_at": detail.get("details_observed_at"),
                "container": container,
                "ownership_labels": extract_ownership_labels(
                    detail.get("cwd"), (container or {}).get("name"),
                ),
            })
        device["processes"] = enriched


def is_device_busy(device: dict[str, Any], hbm_threshold_mb: int) -> bool:
    return (
        bool(device.get("processes"))
        or float(device.get("aicore_percent") or 0) > 1
        or int((device.get("hbm") or {}).get("used_mb") or 0) >= hbm_threshold_mb
    )


class HostProbe:
    def __init__(self, adapter: DeviceAdapter, timeout: int, hbm_busy_threshold_mb: int = 8192) -> None:
        self.adapter = adapter
        self.timeout = timeout
        self.hbm_busy_threshold_mb = hbm_busy_threshold_mb
        self._cpu_counters: dict[str, tuple[int, int]] = {}
        self._infra_cache: dict[str, dict[str, Any]] = {}
        self._process_cache: dict[str, dict[int, dict[str, Any]]] = {}
        self._preflighted: set[str] = set()

    def _run_script(self, server: dict[str, Any], script: str, timeout: int) -> subprocess.CompletedProcess[str]:
        # Text-mode stdin translates LF to CRLF on Windows, which corrupts
        # shell options and heredocs. Keep the SSH wire format UTF-8 bytes.
        result = subprocess.run(
            [*self.adapter.ssh_base(server), "bash", "-s"],
            input=script.replace("\r\n", "\n").encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False, cwd=self.adapter.project_root,
        )
        return subprocess.CompletedProcess(
            result.args, result.returncode,
            result.stdout.decode("utf-8", errors="replace"),
            result.stderr.decode("utf-8", errors="replace"),
        )

    def _run_python(
        self, server: dict[str, Any], source: str, args: list[str], timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        """Bound the remote getter before the SSH deadline (no remote file write)."""
        result = subprocess.run(
            [*self.adapter.ssh_base(server), "timeout", "--signal=TERM", "--kill-after=1",
             str(max(1, timeout - 3)), "python3", "-", *args],
            input=source.replace("\r\n", "\n").encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False, cwd=self.adapter.project_root,
        )
        return subprocess.CompletedProcess(
            result.args, result.returncode,
            result.stdout.decode("utf-8", errors="replace"),
            result.stderr.decode("utf-8", errors="replace"),
        )

    def _collect_process_details(self, server: dict[str, Any], pids: list[int]) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
        if not pids:
            return {}, {"state": "unchanged"}
        try:
            result = self._run_script(server, build_process_detail_script(pids), min(self.timeout, 15))
        except (TimeoutError, OSError, subprocess.TimeoutExpired) as exc:
            return {}, {"state": "unavailable", "stage": "process_details", "error_type": type(exc).__name__}
        if result.returncode != 0:
            return {}, {"state": "unavailable", "stage": "process_details", "exit_code": result.returncode,
                        "error": (result.stderr or "process detail probe failed")[-800:]}
        sections = split_sections(result.stdout)
        if "process_details" not in sections:
            return {}, {"state": "unavailable", "stage": "process_details", "error": "probe response missing process_details"}
        observed_at = int(time.time())
        details = parse_process_details(sections["process_details"])
        for detail in details.values():
            detail["details_observed_at"] = observed_at
        # A process can exit between the device sample and /proc lookup.
        return details, {"state": "observed", "observed_at": observed_at}

    @staticmethod
    def _parse_frequency_sections(output: str) -> dict[int, dict[str, Any]]:
        parsed: dict[int, dict[str, Any]] = {}
        for name, text in split_sections(output).items():
            if not name.startswith("common_"):
                continue
            try:
                int(name.split("_", 1)[1])
            except (IndexError, ValueError):
                continue
            parsed.update(parse_npu_common(text))
        return parsed

    @staticmethod
    def _parse_frequency_exit_codes(output: str) -> dict[str, int]:
        codes: dict[str, int] = {}
        for name, text in split_sections(output).items():
            if not name.startswith("common_rc_"):
                continue
            try:
                card = int(name[len("common_rc_"):])
            except ValueError:
                continue
            lines = text.strip().splitlines()
            if not lines:
                continue
            try:
                codes[str(card)] = int(lines[0].strip())
            except ValueError:
                continue
        return codes

    @staticmethod
    def _frequency_status(
        cards: list[int], parsed: dict[int, dict[str, Any]], state: str, **extra: Any,
    ) -> dict[str, Any]:
        exit_codes = {str(key): int(value) for key, value in (extra.get("card_exit_codes") or {}).items()}
        observed = [card for card in cards
                    if exit_codes.get(str(card)) == 0
                    and (parsed.get(card) or {}).get("freq_mhz") is not None]
        missing = [card for card in cards if card not in observed]
        failed = sorted({int(card) for card, code in exit_codes.items() if code != 0}
                        | {int(card) for card in (extra.get("failed_cards") or [])})
        if state == "observed" and (not observed or missing or failed or extra.get("exit_code")):
            state = "partial" if observed else "unavailable"
        elif state == "partial" and not observed:
            state = "unavailable"
        return {
            "state": state, "stage": "npu_frequency",
            "observed_at": int(time.time()), "cards": cards,
            "observed_cards": observed, "missing_cards": missing,
            **extra, "failed_cards": failed, "card_exit_codes": exit_codes,
        }

    def _collect_npu_frequency(
        self, server: dict[str, Any], npu_ids: list[int],
    ) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
        """Read current AICore frequency from the optional card-scoped `-t common` pass.

        Frequency is supplementary telemetry: a timeout, SSH error, or driver
        that does not support `-t common` leaves the register absent instead of
        failing the main NPU health probe or falling back to the rated clock.
        A killed SSH call still contributes any complete sections already printed.
        """
        cards = sorted({int(npu_id) for npu_id in npu_ids
                        if not isinstance(npu_id, bool) and isinstance(npu_id, int) and npu_id >= 0})
        if not cards:
            return {}, {"state": "not_applicable"}
        plan = frequency_probe_plan(self.timeout)
        if plan is None:
            return {}, {
                "state": "unavailable", "stage": "npu_frequency",
                "error_type": "ConfiguredTimeoutTooSmall",
                "configured_timeout": self.timeout,
                "required_timeout": FREQUENCY_CARD_TIMEOUT_SECONDS + FREQUENCY_CLEANUP_GRACE_SECONDS + 1,
                "cards": cards, "observed_cards": [], "missing_cards": cards,
                "failed_cards": [], "card_exit_codes": {},
            }
        budget, outer_timeout = plan
        script = build_frequency_script(cards, budget)
        try:
            result = self._run_script(server, script, outer_timeout)
        except (subprocess.TimeoutExpired, TimeoutError) as exc:
            raw_stdout = getattr(exc, "stdout", None)
            output = raw_stdout.decode("utf-8", errors="replace") if isinstance(raw_stdout, bytes) else (raw_stdout or "")
            parsed = self._parse_frequency_sections(output)
            return parsed, self._frequency_status(
                cards, parsed, "partial" if parsed else "unavailable",
                error_type=type(exc).__name__, timed_out=True,
                card_exit_codes=self._parse_frequency_exit_codes(output),
            )
        except OSError as exc:
            return {}, {"state": "unavailable", "stage": "npu_frequency",
                        "error_type": type(exc).__name__}
        parsed = self._parse_frequency_sections(result.stdout)
        exit_codes = self._parse_frequency_exit_codes(result.stdout)
        if not parsed:
            return {}, {
                "state": "unavailable", "stage": "npu_frequency",
                "exit_code": result.returncode, "card_exit_codes": exit_codes,
                "error": (result.stderr or result.stdout or "frequency query returned no common section")[-800:],
            }
        extra: dict[str, Any] = {"card_exit_codes": exit_codes}
        if result.returncode != 0:
            extra["exit_code"] = result.returncode
            extra["error"] = (result.stderr or "frequency probe exited nonzero")[-800:]
        return parsed, self._frequency_status(
            cards, parsed, "observed" if result.returncode == 0 else "partial", **extra,
        )

    def _collect_dcmi(
        self, server: dict[str, Any], npu_ids: list[int],
    ) -> dict[str, Any]:
        """Read the DCMI AICORE_FREQREDUC_CAUSE mask without failing the main probe.

        The helper is a separate stdlib-only module sent over SSH stdin; it runs
        on the NPU host where `libdcmi.so` lives. A missing python3/library, a
        timeout, or a malformed reply degrades to an explicit unavailable block
        with null decoded fields.
        """
        cards = sorted({int(npu_id) for npu_id in npu_ids
                        if not isinstance(npu_id, bool) and isinstance(npu_id, int) and npu_id >= 0})
        if not cards:
            return {"state": "not_applicable", "stage": "dcmi"}
        if self.timeout < DCMI_TIMEOUT_SECONDS:
            return {
                "state": "unavailable", "stage": "dcmi",
                "error_type": "ConfiguredTimeoutTooSmall",
                "configured_timeout": self.timeout,
                "required_timeout": DCMI_TIMEOUT_SECONDS,
            }
        try:
            source = Path(dcmi_telemetry.__file__).read_text(encoding="utf-8")
        except TimeoutError:
            raise
        except OSError as exc:
            return {"state": "unavailable", "stage": "dcmi",
                    "error_type": type(exc).__name__, "error": str(exc)[-800:]}
        try:
            result = self._run_python(
                server, source,
                ["--cards", ",".join(str(card) for card in cards), "--device", "0"],
                DCMI_TIMEOUT_SECONDS,
            )
        except (subprocess.TimeoutExpired, TimeoutError) as exc:
            return {"state": "unavailable", "stage": "dcmi",
                    "error_type": type(exc).__name__, "timed_out": True}
        except OSError as exc:
            return {"state": "unavailable", "stage": "dcmi",
                    "error_type": type(exc).__name__, "error": str(exc)[-800:]}
        if result.returncode != 0:
            return {
                "state": "unavailable", "stage": "dcmi",
                "exit_code": result.returncode,
                "error": (result.stderr or "DCMI helper failed")[-800:],
            }
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return {"state": "unavailable", "stage": "dcmi",
                    "error_type": type(exc).__name__,
                    "error": (result.stdout or result.stderr or "invalid DCMI JSON")[-800:]}
        if not isinstance(payload, dict):
            return {"state": "unavailable", "stage": "dcmi",
                    "error_type": "InvalidPayload", "error": "DCMI helper returned non-object JSON"}
        payload["stage"] = "dcmi"
        payload.setdefault("observed_at", int(time.time()))
        return payload

    def collect(self, server: dict[str, Any], include_infrastructure: bool) -> dict[str, Any]:
        started = time.monotonic()
        if server["id"] not in self._preflighted:
            preflight = self.adapter.preflight(server)
            if not preflight["ok"]:
                raise RuntimeError(str(preflight.get("error") or "本地 OpenSSH 配置预检失败"))
            self._preflighted.add(server["id"])
        # Optional commands (usage details, Docker stats/info) vary across
        # driver/runtime versions. Their non-zero status must not replace the
        # explicit npu_info_rc health gate above.
        script = FAST_SCRIPT + (INFRA_SCRIPT if include_infrastructure else "") + "\nexit 0\n"
        effective_timeout = max(self.timeout + 25, 45) if include_infrastructure else self.timeout
        try:
            result = self._run_script(server, script, effective_timeout)
        except (subprocess.TimeoutExpired, TimeoutError) as exc:
            timeout = getattr(exc, "timeout", effective_timeout)
            raise RuntimeError(f"SSH 探查超时（{timeout:g}s）") from exc
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "SSH 探查失败")[-1200:])
        sections = split_sections(result.stdout)
        if not sections.get("proc_stat"):
            raise RuntimeError("远程探查结果不完整：缺少 /proc/stat")
        if sections.get("npu_info_rc") not in (None, "0"):
            raise RuntimeError(f"宿主机 npu-smi 探查失败：{sections.get('npu_info', '')[-800:]}")

        server_id = server["id"]
        current_cpu = parse_cpu_stat(sections.get("proc_stat", ""))
        cpu = cpu_percent(self._cpu_counters.get(server_id), current_cpu)
        if current_cpu:
            self._cpu_counters[server_id] = current_cpu
        load_values = sections.get("loadavg", "").split()
        memory = parse_meminfo(sections.get("meminfo", ""))
        npu = self.adapter.parse_npu(sections.get("npu_info", ""), sections.get("npu_usages", ""))
        devices = npu.get("devices", [])
        frequencies, frequency_status = self._collect_npu_frequency(
            server, [device["npu_id"] for device in devices],
        )
        for device in devices:
            frequency = frequencies.get(int(device["npu_id"])) or {}
            exit_code = (frequency_status.get("card_exit_codes") or {}).get(str(device["npu_id"]))
            # A partial command may print register-shaped text before failing.
            # Only a completed card read contributes values to stored telemetry.
            if exit_code == 0:
                if frequency.get("freq_mhz") is not None:
                    device["freq_mhz"] = frequency["freq_mhz"]
                if frequency.get("freq_max_mhz") is not None:
                    device["freq_max_mhz"] = frequency["freq_max_mhz"]
                if frequency.get("chips"):
                    device["frequency_chips"] = frequency["chips"]
            if exit_code is not None:
                device["frequency_exit_code"] = exit_code
            device["busy"] = is_device_busy(device, self.hbm_busy_threshold_mb)
        dcmi_status = self._collect_dcmi(server, [device["npu_id"] for device in devices])
        dcmi_cards = dcmi_status.get("cards") or {}
        for device in devices:
            card_status = dcmi_cards.get(str(device["npu_id"]))
            if card_status is not None:
                device["dcmi_freq_reduc"] = card_status

        if include_infrastructure:
            infrastructure = {
                "disks": parse_disks(sections.get("disk", "")),
                "mounts": parse_mounts(sections.get("mounts", "")),
                "docker": parse_docker(
                    sections.get("docker", ""), sections.get("docker_stats", ""), sections.get("docker_info", ""),
                ),
            }
            self._infra_cache[server_id] = infrastructure
        else:
            infrastructure = self._infra_cache.get(server_id, {"disks": [], "mounts": [], "docker": {"available": None, "containers": [], "running": None}})

        active_pids = sorted({
            int(process["pid"])
            for device in devices
            for process in (device.get("processes") or [])
            if int(process.get("pid") or 0) > 0
        })
        process_cache = self._process_cache.setdefault(server_id, {})
        stale_pids = active_pids if include_infrastructure else [pid for pid in active_pids if pid not in process_cache]
        details, detail_status = self._collect_process_details(server, stale_pids)
        # A failed refresh cannot relabel old PID metadata as newly observed.
        for pid in stale_pids:
            process_cache.pop(pid, None)
        process_cache.update(details)
        self._process_cache[server_id] = {pid: process_cache[pid] for pid in active_pids if pid in process_cache}
        attach_process_details(devices, self._process_cache[server_id], infrastructure["docker"])

        hbm_used = sum(int((device.get("hbm") or {}).get("used_mb") or 0) for device in devices)
        hbm_total = sum(int((device.get("hbm") or {}).get("total_mb") or 0) for device in devices)
        utils = [float(device["aicore_percent"]) for device in devices if device.get("aicore_percent") is not None]
        disks = infrastructure["disks"]
        summary = {
            "cpu_percent": cpu, "load1": _float(load_values, 0), "load5": _float(load_values, 1), "load15": _float(load_values, 2),
            **memory,
            "npu_util_percent": round(sum(utils) / len(utils), 1) if utils else None,
            "hbm_used_mb": hbm_used, "hbm_total_mb": hbm_total,
            "npu_count": len(devices), "busy_npu_count": sum(1 for device in devices if device["busy"]),
            "docker_running": infrastructure["docker"].get("running"),
            "disk_max_percent": max((disk["used_percent"] for disk in disks), default=None),
        }
        duration_ms = round((time.monotonic() - started) * 1000)
        collected_at_ns = time.time_ns()
        return {
            "server_id": server_id,
            "collected_at": collected_at_ns / 1_000_000_000,
            "collected_at_ns": collected_at_ns,
            "duration_ms": duration_ms,
            "hostname": sections.get("hostname") or server["name"], "summary": summary,
            "devices": devices, **infrastructure,
            "process_details": detail_status,
            "frequency_probe": frequency_status,
            "dcmi": dcmi_status,
        }


def _float(values: list[str], index: int) -> float | None:
    try:
        return float(values[index])
    except (IndexError, ValueError):
        return None
