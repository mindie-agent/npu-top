"""Interpret monitor outcomes without making observations an allocation source."""
from contextvars import ContextVar
from functools import wraps

from mindie_diagnostics import get_recorder

_ACTIVE = ContextVar("top_diagnostic_operation", default=None)


def capture_failure(error, category="internal_error"):
    operation = _ACTIVE.get()
    if operation is not None:
        operation.fail(category, exception=error)


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
