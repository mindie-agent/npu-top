"""Diagnostic operation wrappers for `npu-top` HTTP, CLI, MCP, and collector entry points.

[Role] Wraps entry points with `mindie_diagnostics` operations and classifies `argparse` errors as `caller`.
[Invariants] Preserves original exceptions and return values while recording operation status and HTTP error codes.
[Timeouts & Locks] Uses `ContextVar` (`_ACTIVE`) for per-thread/coroutine operation correlation; zero locks.
[Side-Effect Policy] Emits structured diagnostic events via `mindie_diagnostics`.
"""
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
import sys

from mindie_diagnostics import get_recorder

_ACTIVE = ContextVar("top_diagnostic_operation", default=None)
_SOURCE_ROOT = Path(__file__).resolve().parent
_INTERNAL_TYPES = (RuntimeError, KeyError, AttributeError, IndexError, AssertionError, ZeroDivisionError)


class _TopFailureMarker:
    __slots__ = ("result", "emitted")

    def __init__(self, result):
        self.result = result
        self.emitted = False


def _is_internal_top_exception(exc: BaseException) -> bool:
    if type(exc) not in _INTERNAL_TYPES:
        return False
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending and len(seen) < 32:
        cur = pending.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(cur_id := id(cur))
        del cur_id
        for cls in type(cur).__mro__:
            if cls.__name__ in {"ClientError", "RemoteDevError"}:
                return False
        if isinstance(cur, (OSError, TimeoutError, ValueError, KeyboardInterrupt, SystemExit, GeneratorExit)):
            return False
        cat = getattr(cur, "category", None)
        if isinstance(cat, str) and cat:
            return False
        if getattr(cur, "__cause__", None) is not None:
            pending.append(cur.__cause__)
        if getattr(cur, "__context__", None) is not None and not getattr(cur, "__suppress_context__", False):
            pending.append(cur.__context__)
    if pending:
        return False
    tb = exc.__traceback__
    while tb is not None:
        mod = str(tb.tb_frame.f_globals.get("__name__") or "")
        if mod == "npu_top" or mod.startswith("npu_top."):
            return True
        tb = tb.tb_next
    return False


def _find_top_marker(exc: BaseException | None) -> _TopFailureMarker | None:
    if exc is None:
        return None
    seen: set[int] = set()
    pending: list[BaseException | None] = [exc]
    while pending and len(seen) < 32:
        cur = pending.pop(0)
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        marker = getattr(cur, "_npu_top_diag", None)
        if type(marker) is _TopFailureMarker:
            try:
                exc._npu_top_diag = marker
            except Exception:
                pass
            return marker
        if getattr(cur, "__cause__", None) is not None:
            pending.append(cur.__cause__)
        if getattr(cur, "__context__", None) is not None and not getattr(cur, "__suppress_context__", False):
            pending.append(cur.__context__)
    return None


def report_internal_failure(
    operation_name: str,
    error: BaseException | None,
    *,
    stage: str = "observed_entry",
    category: str = "internal_exception",
    reason_code: str | None = None,
    contract_name: str | None = None,
):
    existing = _find_top_marker(error)
    if existing is not None:
        return existing.result
    try:
        from mindie_diagnostics.integration import record_failure
        base_kwargs = {
            "component": "npu-top",
            "operation": operation_name,
            "stage": stage,
            "category": category,
            "exception": error,
        }
        v2_kwargs: dict[str, object] = {"source_root": _SOURCE_ROOT}
        if reason_code is not None:
            v2_kwargs["reason_code"] = reason_code
        if contract_name is not None:
            v2_kwargs["contract_name"] = contract_name
        try:
            result = record_failure(**base_kwargs, **v2_kwargs)
        except TypeError:
            result = record_failure(**base_kwargs)
    except Exception:
        return None
    if error is not None and isinstance(result, dict):
        try:
            error._npu_top_diag = _TopFailureMarker(result)
        except Exception:
            pass
    return result if isinstance(result, dict) else None


def capture_failure(error, category="internal_error"):
    operation = _ACTIVE.get()
    if operation is not None:
        operation.fail(category, exception=error)
        return operation.summary()["operation_id"]
    return None


def record_http_status(status):
    operation = _ACTIVE.get()
    if operation is not None:
        operation.event("WARNING" if status >= 400 else "DEBUG", "http.response", error_code=int(status))
        if status >= 500:
            operation.fail("http_response", error_code=int(status))


def observed(name, *, level="INFO"):
    def decorate(function):
        @wraps(function)
        def call(*args, **kwargs):
            with get_recorder("npu-top").operation(name, level=level) as operation:
                token = _ACTIVE.set(operation)
                try:
                    result = function(*args, **kwargs)
                except SystemExit as exc:
                    traceback = exc.__traceback__
                    while traceback is not None:
                        frame = traceback.tb_frame
                        if frame.f_globals.get("__name__") == "argparse" and frame.f_code.co_name == "error":
                            operation.fail("argument_validation", classification="caller")
                            break
                        traceback = traceback.tb_next
                    raise
                except Exception as exc:
                    if _is_internal_top_exception(exc):
                        operation.fail("internal_exception", exception=exc)
                        ref = report_internal_failure(name, exc, stage="observed_entry")
                        marker = _find_top_marker(exc)
                        inc = ref.get("incident_id") if isinstance(ref, dict) else None
                        if (
                            isinstance(inc, str)
                            and len(inc) == 32
                            and all(c in "0123456789abcdef" for c in inc)
                            and (type(marker) is not _TopFailureMarker or not marker.emitted)
                        ):
                            if type(marker) is _TopFailureMarker:
                                marker.emitted = True
                            sys.stderr.write(f"mindie-diagnostic-incident: {inc}\n")
                            sys.stderr.flush()
                    raise
                finally:
                    _ACTIVE.reset(token)
                if type(result) is int and result != 0:
                    operation.fail("returned_failure", exit_code=result)
                elif isinstance(result, dict):
                    reply = result.get("result", {})
                    if result.get("error") or (isinstance(reply, dict) and reply.get("isError")):
                        operation.fail("request_failed")
                return result
        return call
    return decorate
