"""Non-destructive bridge between `npu-top` observations and coordinator `machines.json`.

[Role] Reads/writes C1 machine inventories atomically, imports coordinator machines into npu-top's existing server registry, and merges observed card tags, hardware, and model-weight mounts back into `machines.json`.
[Invariants] Coordinator-owned machine/container/SSH/user metadata and unrelated keys are preserved; writes replace a same-directory temporary file; observations neither create devices nor grant allocation authority.
[Timeouts & Locks] Local file/SQLite I/O only; reads and read-merge-writes share the adjacent `machines.json.lock` with coordinator writers, with bounded acquisition; no network or remote mutation.
[Side-Effect Policy] Only explicit `write_machines`/`sync_to_coordinator`/`import_from_coordinator` calls mutate the selected files; reads are side-effect free.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .agent_view import weight_mounts
from .ssh_access import validate_endpoint

LOCK_TIMEOUT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.02


class InventoryBridgeError(ValueError):
    """A machine inventory document cannot be read, validated, or written safely."""


def configured_coordinator_paths(inventory_files: Sequence[Path | str] = ()) -> list[Path]:
    """Return deduplicated coordinator `machines.json` paths from settings and environment."""
    candidates: list[Path] = [Path(item).expanduser() for item in inventory_files if str(item).strip()]
    coord_state_dir = (os.environ.get("MINDIE_COORDINATOR_STATE_DIR") or "").strip()
    if coord_state_dir:
        candidates.append(Path(coord_state_dir).expanduser() / "machines.json")
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def read_machines(path: Path | str) -> dict[str, Any]:
    path = Path(path).expanduser()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except TimeoutError:
        raise
    except (OSError, UnicodeError) as exc:
        raise InventoryBridgeError(f"machine inventory unavailable: {path} ({type(exc).__name__})") from exc
    except json.JSONDecodeError as exc:
        raise InventoryBridgeError(f"invalid machine inventory JSON: {path}") from exc
    _validate(document, path)
    return document


@contextlib.contextmanager
def inventory_lock(path: Path | str) -> Iterator[None]:
    """Serialize writers on the adjacent ``machines.json.lock`` file."""
    target = Path(path).expanduser().resolve()
    lock_path = target.with_name(target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    with lock_path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except TimeoutError:
                    raise
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise InventoryBridgeError("timed out waiting for machine inventory lock") from exc
                    time.sleep(_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    if time.monotonic() >= deadline:
                        raise InventoryBridgeError("timed out waiting for machine inventory lock") from exc
                    time.sleep(_LOCK_POLL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def write_machines(path: Path | str, document: Mapping[str, Any]) -> Path:
    """Atomically replace ``path`` without leaving a partial document."""
    _validate(document, Path(path))
    target = Path(path).expanduser().resolve()
    with inventory_lock(target):
        return _write_machines_unlocked(target, document)


def _write_machines_unlocked(target: Path, document: Mapping[str, Any]) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if previous_mode is not None:
            os.chmod(temporary, previous_mode)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
        return target
    except BaseException:
        try:
            os.unlink(temporary)
        except TimeoutError:
            raise
        except OSError:
            pass
        raise


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # Windows has no portable directory-handle fsync; os.replace is the
        # durability boundary available there.
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except TimeoutError:
        raise
    except OSError as exc:
        raise InventoryBridgeError(f"cannot open inventory directory for fsync: {path}") from exc
    try:
        os.fsync(descriptor)
    except TimeoutError:
        raise
    except OSError as exc:
        raise InventoryBridgeError(f"inventory directory fsync failed: {path}") from exc
    finally:
        os.close(descriptor)


def _validate(document: Any, path: Path) -> None:
    if not isinstance(document, dict) or not isinstance(document.get("machines"), list):
        raise InventoryBridgeError(f"machine inventory requires a machines array: {path}")
    for index, machine in enumerate(document["machines"], 1):
        if not isinstance(machine, dict) or not isinstance(machine.get("host"), dict):
            raise InventoryBridgeError(f"invalid machine record {index}: {path}")
        if not isinstance(machine.get("tags", []), list):
            raise InventoryBridgeError(f"invalid machine tags at record {index}: {path}")
        if not isinstance(machine.get("mounts", []), list):
            raise InventoryBridgeError(f"invalid machine mounts at record {index}: {path}")
        if machine.get("hardware") is not None and not isinstance(machine.get("hardware"), dict):
            raise InventoryBridgeError(f"invalid machine hardware at record {index}: {path}")


def coordinator_servers(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return npu-top server records from a validated C1 document."""
    servers = []
    for index, machine in enumerate(document.get("machines") or [], 1):
        host_data = machine.get("host") or {}
        host = str(host_data.get("ip") or host_data.get("host") or "").strip()
        if not host:
            raise InventoryBridgeError(f"machine record {index} has no host endpoint")
        try:
            port = int(host_data.get("port") or 22)
        except (TypeError, ValueError) as exc:
            raise InventoryBridgeError(f"machine record {index} has an invalid port") from exc
        username = str(host_data.get("user") or "root").strip() or "root"
        try:
            validate_endpoint(host, port, username)
        except ValueError as exc:
            raise InventoryBridgeError(f"machine record {index} has an invalid endpoint") from exc
        tags = [str(tag) for tag in machine.get("tags") or [] if str(tag).strip()]
        machine_type = host_data.get("machine_type") or (machine.get("container") or {}).get("machine_type")
        if machine_type and str(machine_type).casefold() not in {tag.casefold() for tag in tags}:
            tags.append(str(machine_type))
        servers.append({
            "name": str(machine.get("alias") or host), "host": host, "port": port,
            "username": username, "tags": tags,
        })
    return servers


def _server_id(host: str, port: int, username: str) -> str:
    return hashlib.sha256(f"{host}|{port}|{username}".encode()).hexdigest()[:32]


def import_from_coordinator(path: Path | str, db: Any) -> dict[str, Any]:
    """Register coordinator machines in npu-top's existing SQLite server table."""
    document = read_machines(path)
    existing = {(row["host"], int(row["port"]), row["username"]): row for row in db.list_servers()}
    registered = updated = unchanged = 0
    for record in coordinator_servers(document):
        endpoint = (record["host"], record["port"], record["username"])
        current = existing.get(endpoint)
        if current is None:
            db.upsert_server({**record, "id": _server_id(*endpoint)})
            registered += 1
            continue
        tags = list(current.get("tags") or [])
        merged = list(tags)
        for tag in record["tags"]:
            if tag.casefold() not in {item.casefold() for item in merged}:
                merged.append(tag)
        if merged != tags:
            db.update_server(current["id"], tags=merged)
            updated += 1
        else:
            unchanged += 1
    return {
        "path": str(Path(path).expanduser()), "machines": len(document["machines"]),
        "registered": registered, "updated": updated, "unchanged": unchanged,
    }


def _card_profile(
    names: list[str], chips_per_device: list[int], tags: list[str],
) -> tuple[str | None, str | None, int | None]:
    """Return ``(soc, family_tag, dies_per_package)`` from observed names/tags."""
    tokens = {str(tag).casefold() for tag in tags}
    names_text = " ".join(names).casefold()
    dual = any(count >= 2 for count in chips_per_device) or "a3" in tokens or "910c" in tokens
    if dual or "910c" in names_text:
        return "Ascend910C", "A3", 2
    if "a5" in tokens or "950" in tokens or "950" in names_text:
        return "Ascend950", "A5", 1
    if "a2" in tokens or "910b" in tokens or "910b" in names_text:
        return "Ascend910B", "A2", 1
    if "310p" in names_text:
        return "Ascend310P", "310P", 1
    return None, None, None


def observed_machine_records(
    servers: list[dict[str, Any]],
    snapshots: dict[str, dict[str, Any]],
    *,
    include_unprobed: bool = False,
) -> list[dict[str, Any]]:
    """Project cached server observations into non-destructive C1 updates."""
    records = []
    for server in servers:
        snapshot = snapshots.get(server.get("id"))
        if not snapshot or snapshot.get("status", "online") != "online":
            if include_unprobed:
                records.append({
                    "alias": str(server.get("name") or server["host"]),
                    "host": server["host"],
                    "port": int(server["port"]),
                    "username": str(server.get("username") or "root"),
                    "hardware": {},
                    "tags": [str(tag) for tag in server.get("tags") or [] if str(tag).strip()],
                    "mounts": [],
                })
            continue
        devices = [device for device in snapshot.get("devices") or [] if isinstance(device, dict)]
        names = [str(device.get("name") or "") for device in devices if device.get("name")]
        chips_per_device = [len(device.get("chips") or []) for device in devices]
        soc, family, dies = _card_profile(names, chips_per_device, list(server.get("tags") or []))
        if dies is None and family is not None:
            dies = 2 if family == "A3" else 1
        hardware: dict[str, Any] = {}
        if soc:
            hardware["soc"] = soc
        if dies:
            hardware["dies_per_package"] = dies
            logical_count = len(devices) or int((snapshot.get("summary") or {}).get("npu_count") or 0)
            if logical_count > 0:
                hardware["npu_count"] = logical_count * dies
        tags = []
        if family:
            tags.append(family)
        if family == "A3":
            tags.extend(["910C", "dual-die"])
        elif family == "A2":
            tags.extend(["910B", "single-die"])
        elif family == "A5":
            tags.extend(["950", "single-die"])
        elif family == "310P":
            tags.append("single-die")
        if include_unprobed:
            known = {item.casefold() for item in tags}
            for tag in server.get("tags") or []:
                token = str(tag).strip()
                if token and token.casefold() not in known:
                    tags.append(token)
                    known.add(token.casefold())
        mounts = [
            {"target": row["target"], "weight_candidate": True}
            for row in weight_mounts(snapshot)
            if row.get("weight_candidate") and row.get("target")
        ]
        record = {
            "alias": str(server.get("name") or server["host"]), "host": server["host"],
            "port": int(server["port"]), "username": str(server.get("username") or "root"),
            "hardware": hardware, "tags": tags, "mounts": mounts,
        }
        records.append(record)
    return records


def _match_machine(
    machines: list[dict[str, Any]], observation: Mapping[str, Any],
) -> dict[str, Any] | None:
    alias = str(observation.get("alias") or "").casefold()
    if alias:
        matches = [row for row in machines if str(row.get("alias") or "").casefold() == alias]
        if len(matches) == 1:
            return matches[0]
    ip = str(observation.get("host") or "").casefold()
    if ip:
        matches = [
            row for row in machines
            if str((row.get("host") or {}).get("ip") or "").casefold() == ip
        ]
        if len(matches) == 1:
            return matches[0]
    return None


def merge_observations(
    document: Mapping[str, Any], observations: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, int]]:
    """Return a merged copy plus update counts; never deletes existing data."""
    merged = copy.deepcopy(dict(document))
    _validate(merged, Path("<in-memory>"))
    machines = merged["machines"]
    counts = {"matched": 0, "added": 0, "hardware": 0, "tags": 0, "mounts": 0}
    for observation in observations:
        record = _match_machine(machines, observation)
        if record is None:
            host = str(observation.get("host") or "").strip()
            if not host:
                continue
            record = {
                "alias": str(observation.get("alias") or host),
                "host": {
                    "ip": host, "port": int(observation.get("port") or 22),
                    "user": str(observation.get("username") or "root"),
                },
            }
            machines.append(record)
            counts["added"] += 1
        else:
            counts["matched"] += 1
        hardware = observation.get("hardware") or {}
        if hardware:
            current_hardware = record.get("hardware")
            current_hardware = {} if current_hardware is None else dict(current_hardware)
            if any(current_hardware.get(key) != value for key, value in hardware.items()):
                current_hardware.update(hardware)
                record["hardware"] = current_hardware
                counts["hardware"] += 1
        tags = list(record.get("tags") or [])
        known = {str(tag).casefold() for tag in tags}
        changed = False
        for tag in observation.get("tags") or []:
            if str(tag).casefold() not in known:
                tags.append(tag)
                known.add(str(tag).casefold())
                changed = True
        if changed:
            record["tags"] = tags
            counts["tags"] += 1
        mounts = list(record.get("mounts") or [])
        by_target = {mount.get("target"): mount for mount in mounts if isinstance(mount, dict)}
        changed = False
        for mount in observation.get("mounts") or []:
            target = mount.get("target")
            if not mount.get("weight_candidate") or not target:
                continue
            current = by_target.get(target)
            if current is None:
                current = {"target": target}
                mounts.append(current)
                by_target[target] = current
            if current.get("weight_candidate") is not True:
                current["weight_candidate"] = True
                changed = True
            if changed:
                counts["mounts"] += 1
                changed = False
        if mounts != record.get("mounts"):
            record["mounts"] = mounts
    return merged, counts


def sync_to_coordinator(
    path: Path | str,
    servers: list[dict[str, Any]],
    snapshots: dict[str, dict[str, Any]],
    *,
    include_unprobed: bool = False,
    create_if_missing: bool = False,
) -> dict[str, Any]:
    target = Path(path).expanduser().resolve()
    observations = observed_machine_records(servers, snapshots, include_unprobed=include_unprobed)
    with inventory_lock(target):
        if create_if_missing and not target.exists():
            document: dict[str, Any] = {"version": 1, "default_alias": None, "machines": []}
        else:
            document = read_machines(target)
        merged, counts = merge_observations(document, observations)
        written = _write_machines_unlocked(target, merged)
    return {
        "path": str(written), "machines": len(merged["machines"]),
        "observations": len(observations), **counts,
    }
