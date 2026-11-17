"""SSH host probe orchestrator for NPU, CPU, memory, disk, mount, Docker, and `/proc` telemetry.

[Role] Executes bounded remote shell/Python probes over SSH under a single shared
monotonic collection deadline and merges parsed sections into structured host snapshots.
[Invariants] Observation-only; `npu_info_rc` gates NPU health while optional `usages`,
frequency (`-t common`), DCMI, Docker, and `/proc` passes degrade gracefully (`skipped` or
`unavailable`) without erasing base NPU observations.
[Timeouts & Locks] Enforces one shared monotonic collection deadline across base+infra,
frequency, DCMI, and process-detail passes (`outer_timeout >= sum(inner_budgets) + grace`).
Optional passes whose minimum outer budget exceeds remaining collection time are skipped
with `reason="collection_deadline_exhausted"`.
[Side-Effect Policy] Read-only remote SSH probes (`/proc`, `df`, `findmnt`, `docker`,
`npu-smi`, DCMI); never mutates remote host state or submits NPU work.
"""
from __future__ import annotations

import json
import math
import subprocess
import time
from pathlib import Path
from typing import Any

from remote_dev.core.ssh_transport import RemoteCompleted

from . import dcmi_telemetry
from .device_adapter import DeviceAdapter
from .npu_smi import parse_npu_common
from .probe_parsers import (
    EMPLOYEE_ID_PATTERN, INITIALS_PATTERN, INITIALS_STOP_WORDS, SECTION,
    attach_process_details, cpu_percent, extract_container_id, extract_ownership_labels,
    is_device_busy, parse_cpu_stat, parse_disks, parse_docker, parse_meminfo,
    parse_mounts, parse_process_details, split_sections,
)
from .ssh_access import capture_ssh

__all__ = [
    "DCMI_CLEANUP_GRACE_SECONDS", "DCMI_INNER_SECONDS", "DCMI_KILL_AFTER_SECONDS",
    "DCMI_OUTER_TIMEOUT_SECONDS", "DCMI_TIMEOUT_SECONDS", "EMPLOYEE_ID_PATTERN",
    "FAST_SCRIPT", "FREQUENCY_BUDGET_SECONDS", "FREQUENCY_CARD_TIMEOUT_SECONDS",
    "FREQUENCY_CLEANUP_GRACE_SECONDS", "FREQUENCY_OUTER_TIMEOUT_SECONDS", "FREQUENCY_SCRIPT",
    "HostProbe", "INFRA_SCRIPT", "INITIALS_PATTERN", "INITIALS_STOP_WORDS",
    "PROCESS_DETAIL_SCRIPT", "SECTION", "attach_process_details", "build_frequency_script",
    "build_process_detail_script", "cpu_percent", "dcmi_probe_plan",
    "extract_container_id", "extract_ownership_labels", "frequency_probe_plan",
    "is_device_busy", "parse_cpu_stat", "parse_disks", "parse_docker",
    "parse_meminfo", "parse_mounts", "parse_process_details", "split_sections",
]

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
DCMI_INNER_SECONDS = 5
DCMI_KILL_AFTER_SECONDS = 1
DCMI_CLEANUP_GRACE_SECONDS = 3
DCMI_OUTER_TIMEOUT_SECONDS = (
    DCMI_INNER_SECONDS + DCMI_KILL_AFTER_SECONDS + DCMI_CLEANUP_GRACE_SECONDS
)
DCMI_TIMEOUT_SECONDS = DCMI_OUTER_TIMEOUT_SECONDS
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


def build_process_detail_script(pids: list[int]) -> str:
    safe_pids = " ".join(str(pid) for pid in sorted(set(pids)) if pid > 0)
    return PROCESS_DETAIL_SCRIPT.replace("__NFM_PIDS__", safe_pids) + "\nexit 0\n"


def build_frequency_script(
    npu_ids: list[int], budget_seconds: int = FREQUENCY_BUDGET_SECONDS,
) -> str:
    """Build a bounded `-t common` query for validated integer card ids."""
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
    """Return `(inner_budget, outer_timeout)` with `outer >= budget + card + grace`."""
    available = int(configured_timeout) - FREQUENCY_CARD_TIMEOUT_SECONDS - FREQUENCY_CLEANUP_GRACE_SECONDS
    if available < 1:
        return None
    budget = min(FREQUENCY_BUDGET_SECONDS, available)
    return budget, budget + FREQUENCY_CARD_TIMEOUT_SECONDS + FREQUENCY_CLEANUP_GRACE_SECONDS


def dcmi_probe_plan(configured_timeout: int) -> tuple[int, int] | None:
    """Return `(inner_seconds, outer_timeout)` with `outer = inner + kill_after + grace`."""
    if int(configured_timeout) < DCMI_OUTER_TIMEOUT_SECONDS:
        return None
    return DCMI_INNER_SECONDS, DCMI_OUTER_TIMEOUT_SECONDS


def _cleanup_facts(result: Any) -> dict[str, str]:
    error = getattr(result, "cleanup_error", None)
    return {"cleanup_error": error} if error else {}


def _float(values: list[str], index: int) -> float | None:
    try:
        return float(values[index])
    except (IndexError, ValueError):
        return None


class HostProbe:
    def __init__(self, adapter: DeviceAdapter, timeout: int, hbm_busy_threshold_mb: int = 8192) -> None:
        self.adapter = adapter
        self.timeout = timeout
        self.hbm_busy_threshold_mb = hbm_busy_threshold_mb
        self._cpu_counters: dict[str, tuple[int, int]] = {}
        self._infra_cache: dict[str, dict[str, Any]] = {}
        self._infra_observed_at: dict[str, int] = {}
        self._process_cache: dict[str, dict[int, dict[str, Any]]] = {}
        self._preflighted: set[str] = set()

    def _run_script(self, server: dict[str, Any], script: str, timeout: int) -> RemoteCompleted:
        # Keep SSH stdin UTF-8 LF bytes so Windows text mode never injects CRLF.
        return capture_ssh(
            self.adapter, server, ["bash", "-s"], timeout_ms=timeout * 1000,
            input=script.replace("\r\n", "\n").encode("utf-8"),
        )

    def _run_python(
        self, server: dict[str, Any], source: str, args: list[str], inner_seconds: int,
    ) -> RemoteCompleted:
        """Bound the remote getter before the SSH deadline (`outer = inner + kill + grace`)."""
        inner = max(1, int(inner_seconds))
        outer = inner + DCMI_KILL_AFTER_SECONDS + DCMI_CLEANUP_GRACE_SECONDS
        return capture_ssh(
            self.adapter, server, [
                "timeout", "--signal=TERM",
                f"--kill-after={DCMI_KILL_AFTER_SECONDS}", str(inner),
                "python3", "-", *args,
            ],
            timeout_ms=outer * 1000,
            input=source.replace("\r\n", "\n").encode("utf-8"),
        )

    def _collect_process_details(
        self, server: dict[str, Any], pids: list[int], *, deadline: float | None = None,
    ) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
        if not pids:
            return {}, {"state": "unchanged"}
        outer = min(self.timeout, 15)
        if deadline is not None:
            rem = deadline - time.monotonic()
            avail = int(math.floor(rem))
            if avail < 4:
                return {}, {
                    "state": "skipped", "stage": "process_details",
                    "reason": "collection_deadline_exhausted",
                    "remaining_seconds": round(max(0.0, rem), 3), "required_timeout": 4,
                }
            outer = min(outer, avail)
        try:
            result = self._run_script(server, build_process_detail_script(pids), outer)
        except (TimeoutError, OSError, subprocess.TimeoutExpired) as exc:
            return {}, {"state": "unavailable", "stage": "process_details", "error_type": type(exc).__name__,
                        **_cleanup_facts(exc)}
        if result.returncode != 0:
            return {}, {"state": "unavailable", "stage": "process_details", "exit_code": result.returncode,
                        "error": (result.stderr or "process detail probe failed")[-800:], **_cleanup_facts(result)}
        sections = split_sections(result.stdout)
        if "process_details" not in sections:
            return {}, {"state": "unavailable", "stage": "process_details", "error": "probe response missing process_details"}
        observed_at = int(time.time())
        details = parse_process_details(sections["process_details"])
        for detail in details.values():
            detail["details_observed_at"] = observed_at
        return details, {"state": "observed", "observed_at": observed_at, **_cleanup_facts(result)}

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
        self, server: dict[str, Any], npu_ids: list[int], *, deadline: float | None = None,
    ) -> tuple[dict[int, dict[str, Any]], dict[str, Any]]:
        """Read current AICore frequency from the optional card-scoped `-t common` pass."""
        cards = sorted({int(npu_id) for npu_id in npu_ids
                        if not isinstance(npu_id, bool) and isinstance(npu_id, int) and npu_id >= 0})
        if not cards:
            return {}, {"state": "not_applicable"}
        min_req = FREQUENCY_CARD_TIMEOUT_SECONDS + FREQUENCY_CLEANUP_GRACE_SECONDS + 1
        plan = frequency_probe_plan(self.timeout)
        if plan is None:
            return {}, {
                "state": "unavailable", "stage": "npu_frequency",
                "error_type": "ConfiguredTimeoutTooSmall",
                "configured_timeout": self.timeout, "required_timeout": min_req,
                "cards": cards, "observed_cards": [], "missing_cards": cards,
                "failed_cards": [], "card_exit_codes": {},
            }
        if deadline is not None:
            rem = deadline - time.monotonic()
            plan = frequency_probe_plan(min(self.timeout, int(math.floor(rem))))
            if plan is None:
                return {}, {
                    "state": "skipped", "stage": "npu_frequency",
                    "reason": "collection_deadline_exhausted",
                    "remaining_seconds": round(max(0.0, rem), 3), "required_timeout": min_req,
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
                **_cleanup_facts(exc),
            )
        except OSError as exc:
            return {}, {"state": "unavailable", "stage": "npu_frequency", "error_type": type(exc).__name__}
        parsed = self._parse_frequency_sections(result.stdout)
        exit_codes = self._parse_frequency_exit_codes(result.stdout)
        if not parsed:
            return {}, {
                "state": "unavailable", "stage": "npu_frequency",
                "exit_code": result.returncode, "card_exit_codes": exit_codes,
                "error": (result.stderr or result.stdout or "frequency query returned no common section")[-800:],
                **_cleanup_facts(result),
            }
        extra: dict[str, Any] = {"card_exit_codes": exit_codes, **_cleanup_facts(result)}
        if result.returncode != 0:
            extra["exit_code"] = result.returncode
            extra["error"] = (result.stderr or "frequency probe exited nonzero")[-800:]
        return parsed, self._frequency_status(
            cards, parsed, "observed" if result.returncode == 0 else "partial", **extra,
        )

    def _collect_dcmi(
        self, server: dict[str, Any], npu_ids: list[int], *, deadline: float | None = None,
    ) -> dict[str, Any]:
        """Read the DCMI AICORE_FREQREDUC_CAUSE mask without failing the main probe."""
        cards = sorted({int(npu_id) for npu_id in npu_ids
                        if not isinstance(npu_id, bool) and isinstance(npu_id, int) and npu_id >= 0})
        if not cards:
            return {"state": "not_applicable", "stage": "dcmi"}
        plan = dcmi_probe_plan(self.timeout)
        if plan is None:
            return {
                "state": "unavailable", "stage": "dcmi",
                "error_type": "ConfiguredTimeoutTooSmall",
                "configured_timeout": self.timeout,
                "required_timeout": DCMI_OUTER_TIMEOUT_SECONDS,
            }
        if deadline is not None:
            rem = deadline - time.monotonic()
            plan = dcmi_probe_plan(min(self.timeout, int(math.floor(rem))))
            if plan is None:
                return {
                    "state": "skipped", "stage": "dcmi",
                    "reason": "collection_deadline_exhausted",
                    "remaining_seconds": round(max(0.0, rem), 3),
                    "required_timeout": DCMI_OUTER_TIMEOUT_SECONDS,
                }
        inner_seconds, _outer_timeout = plan
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
                inner_seconds,
            )
        except (subprocess.TimeoutExpired, TimeoutError) as exc:
            return {"state": "unavailable", "stage": "dcmi",
                    "error_type": type(exc).__name__, "timed_out": True, **_cleanup_facts(exc)}
        except OSError as exc:
            return {"state": "unavailable", "stage": "dcmi",
                    "error_type": type(exc).__name__, "error": str(exc)[-800:]}
        if result.returncode != 0:
            return {
                "state": "unavailable", "stage": "dcmi", "exit_code": result.returncode,
                "error": (result.stderr or "DCMI helper failed")[-800:], **_cleanup_facts(result),
            }
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            return {"state": "unavailable", "stage": "dcmi", "error_type": type(exc).__name__,
                    "error": (result.stdout or result.stderr or "invalid DCMI JSON")[-800:]}
        if not isinstance(payload, dict):
            return {"state": "unavailable", "stage": "dcmi",
                    "error_type": "InvalidPayload", "error": "DCMI helper returned non-object JSON"}
        payload["stage"] = "dcmi"
        payload.update(_cleanup_facts(result))
        payload.setdefault("observed_at", int(time.time()))
        return payload

    def collect(
        self, server: dict[str, Any], include_infrastructure: bool,
        *, deadline: float | None = None, budget_seconds: float | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        default_base = max(self.timeout + 25, 45) if include_infrastructure else self.timeout
        active_deadline = (
            float(deadline) if deadline is not None
            else started + float(budget_seconds) if budget_seconds is not None
            else started + max(float(default_base), 30.0)
        )
        if server["id"] not in self._preflighted:
            preflight = self.adapter.preflight(server)
            if not preflight["ok"]:
                raise RuntimeError(str(preflight.get("error") or "本地 OpenSSH 配置预检失败"))
            self._preflighted.add(server["id"])
        rem_base = max(1, int(math.ceil(active_deadline - time.monotonic())))
        effective_timeout = min(default_base, rem_base) if (deadline is not None or budget_seconds is not None) else default_base
        include_infra_cmds = include_infrastructure
        fast_script = FAST_SCRIPT
        infra_script = INFRA_SCRIPT
        if include_infrastructure and effective_timeout < 45:
            if effective_timeout >= 28:
                # 8 + 8 + 2 + 4 + 2 = 24s inner + >= 4s cleanup grace <= effective_timeout.
                infra_script = (
                    INFRA_SCRIPT.replace("timeout 10 docker stats", "timeout 4 docker stats")
                    .replace("timeout 5 docker ", "timeout 2 docker ")
                )
            elif effective_timeout >= 9:
                # Budget all 5 inner timeouts within effective_timeout - 4s cleanup grace (>= 5s pool).
                inner_pool = effective_timeout - 4
                d_ps, d_stats, d_info = 1, max(1, inner_pool // 5), 1
                rem_smi = max(2, inner_pool - (d_ps + d_stats + d_info))
                smi_info = (rem_smi + 1) // 2
                smi_usage = max(1, rem_smi - smi_info)
                fast_script = (
                    FAST_SCRIPT.replace("timeout 8 npu-smi info 2>&1", f"timeout {smi_info} npu-smi info 2>&1")
                    .replace("timeout 8 npu-smi info -t usages", f"timeout {smi_usage} npu-smi info -t usages")
                )
                infra_script = (
                    INFRA_SCRIPT.replace("timeout 5 docker ps", f"timeout {d_ps} docker ps")
                    .replace("timeout 10 docker stats", f"timeout {d_stats} docker stats")
                    .replace("timeout 5 docker info", f"timeout {d_info} docker info")
                )
            else:
                # Remaining deadline (< 9s) cannot cover 5 inner commands + 4s cleanup grace.
                include_infra_cmds = False
        if not include_infra_cmds and effective_timeout < 20:
            if effective_timeout >= 6:
                inner_pool = effective_timeout - 4
                smi_info = (inner_pool + 1) // 2
                smi_usage = inner_pool - smi_info
                fast_script = FAST_SCRIPT.replace("timeout 8 npu-smi info 2>&1", f"timeout {smi_info} npu-smi info 2>&1")
                if smi_usage >= 1:
                    fast_script = fast_script.replace("timeout 8 npu-smi info -t usages", f"timeout {smi_usage} npu-smi info -t usages")
                else:
                    fast_script = fast_script.replace("timeout 8 npu-smi info -t usages 2>&1", "true")
            else:
                smi_info = max(1, effective_timeout - 1)
                effective_timeout = max(effective_timeout, smi_info + 2)
                fast_script = (
                    FAST_SCRIPT.replace("timeout 8 npu-smi info 2>&1", f"timeout {smi_info} npu-smi info 2>&1")
                    .replace("timeout 8 npu-smi info -t usages 2>&1", "true")
                )
        script = fast_script + (infra_script if include_infra_cmds else "") + "\nexit 0\n"
        try:
            result = self._run_script(server, script, effective_timeout)
        except (subprocess.TimeoutExpired, TimeoutError) as exc:
            timeout = getattr(exc, "timeout", effective_timeout)
            cleanup = getattr(exc, "cleanup_error", None)
            raise RuntimeError(f"SSH 探查超时（{timeout:g}s）" + (f"; cleanup: {cleanup}" if cleanup else "")) from exc
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
            server, [device["npu_id"] for device in devices], deadline=active_deadline,
        )
        for device in devices:
            frequency = frequencies.get(int(device["npu_id"])) or {}
            exit_code = (frequency_status.get("card_exit_codes") or {}).get(str(device["npu_id"]))
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
        dcmi_status = self._collect_dcmi(
            server, [device["npu_id"] for device in devices], deadline=active_deadline,
        )
        dcmi_cards = dcmi_status.get("cards") or {}
        for device in devices:
            card_status = dcmi_cards.get(str(device["npu_id"]))
            if card_status is not None:
                device["dcmi_freq_reduc"] = card_status

        collected_at_ns = time.time_ns()
        collected_sec = int(collected_at_ns // 1_000_000_000)
        if include_infra_cmds:
            infrastructure = {
                "disks": parse_disks(sections.get("disk", "")),
                "mounts": parse_mounts(sections.get("mounts", "")),
                "docker": parse_docker(
                    sections.get("docker", ""), sections.get("docker_stats", ""), sections.get("docker_info", ""),
                ),
            }
            self._infra_cache[server_id] = infrastructure
            self._infra_observed_at[server_id] = collected_sec
            infrastructure_probe: dict[str, Any] = {"state": "observed", "observed_at": collected_sec}
        else:
            infrastructure = self._infra_cache.get(
                server_id, {"disks": [], "mounts": [], "docker": {"available": None, "containers": [], "running": None}},
            )
            cached_at = self._infra_observed_at.get(server_id)
            infrastructure_probe = (
                {"state": "skipped", "reason": "collection_deadline_exhausted", "cached_observed_at": cached_at}
                if include_infrastructure
                else {"state": "cached", "cached_observed_at": cached_at}
            )

        active_pids = sorted({
            int(p["pid"]) for d in devices for p in (d.get("processes") or []) if int(p.get("pid") or 0) > 0
        })
        process_cache = self._process_cache.setdefault(server_id, {})
        stale_pids = active_pids if include_infrastructure else [pid for pid in active_pids if pid not in process_cache]
        details, detail_status = self._collect_process_details(server, stale_pids, deadline=active_deadline)
        for pid in stale_pids:
            process_cache.pop(pid, None)
        process_cache.update(details)
        self._process_cache[server_id] = {pid: process_cache[pid] for pid in active_pids if pid in process_cache}
        attach_process_details(devices, self._process_cache[server_id], infrastructure["docker"])

        hbm_used = sum(int((d.get("hbm") or {}).get("used_mb") or 0) for d in devices)
        hbm_total = sum(int((d.get("hbm") or {}).get("total_mb") or 0) for d in devices)
        utils = [float(d["aicore_percent"]) for d in devices if d.get("aicore_percent") is not None]
        disks = infrastructure["disks"]
        summary = {
            "cpu_percent": cpu, "load1": _float(load_values, 0), "load5": _float(load_values, 1), "load15": _float(load_values, 2),
            **memory, "npu_util_percent": round(sum(utils) / len(utils), 1) if utils else None,
            "hbm_used_mb": hbm_used, "hbm_total_mb": hbm_total,
            "npu_count": len(devices), "busy_npu_count": sum(1 for d in devices if d["busy"]),
            "docker_running": infrastructure["docker"].get("running"),
            "disk_max_percent": max((disk["used_percent"] for disk in disks), default=None),
        }
        return {
            "server_id": server_id, "collected_at": collected_at_ns / 1_000_000_000,
            "collected_at_ns": collected_at_ns, "duration_ms": round((time.monotonic() - started) * 1000),
            "hostname": sections.get("hostname") or server["name"], "summary": summary,
            "devices": devices, **infrastructure, "infrastructure_probe": infrastructure_probe,
            "process_details": detail_status, "frequency_probe": frequency_status, "dcmi": dcmi_status,
            **({"transport": _cleanup_facts(result)} if _cleanup_facts(result) else {}),
        }
