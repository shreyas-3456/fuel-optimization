import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from itertools import count
from typing import Any

from django.db import connection

log = logging.getLogger("core.profiler")

_current_profile: ContextVar["RequestProfile | None"] = ContextVar(
    "current_request_profile",
    default=None,
)
_request_ids = count(1)


def _now_ms() -> float:
    return time.perf_counter() * 1000


def _truncate(value: Any, limit: int = 500) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}..."


@dataclass
class RequestProfile:
    request_id: str
    method: str
    path: str
    started_ms: float = field(default_factory=_now_ms)
    cpu_started_ms: float = field(default_factory=lambda: time.process_time() * 1000)
    db_total_ms: float = 0.0
    query_count: int = 0
    steps: list[dict[str, Any]] = field(default_factory=list)
    queries: list[dict[str, Any]] = field(default_factory=list)

    def add_step(self, name: str, elapsed_ms: float, extra: dict[str, Any] | None = None) -> None:
        record = {
            "request_id": self.request_id,
            "event": "code_step",
            "name": name,
            "elapsed_ms": round(elapsed_ms, 3),
        }
        if extra:
            record.update(extra)
        self.steps.append(record)
        log.info(json.dumps(record, default=str))

    def add_query(self, sql: str, params: Any, elapsed_ms: float, many: bool) -> None:
        self.query_count += 1
        self.db_total_ms += elapsed_ms
        record = {
            "request_id": self.request_id,
            "event": "sql_query",
            "query_number": self.query_count,
            "elapsed_ms": round(elapsed_ms, 3),
            "many": many,
            "sql": _truncate(sql),
            "params": _truncate(params),
        }
        self.queries.append(record)
        log.info(json.dumps(record, default=str))

    def summary(self, status_code: int | None = None, error: str | None = None) -> dict[str, Any]:
        elapsed_ms = _now_ms() - self.started_ms
        cpu_ms = (time.process_time() * 1000) - self.cpu_started_ms
        code_ms = max(0.0, elapsed_ms - self.db_total_ms)
        payload = {
            "request_id": self.request_id,
            "event": "request_profile",
            "method": self.method,
            "path": self.path,
            "status_code": status_code,
            "elapsed_ms": round(elapsed_ms, 3),
            "cpu_ms": round(cpu_ms, 3),
            "db_total_ms": round(self.db_total_ms, 3),
            "code_estimated_ms": round(code_ms, 3),
            "query_count": self.query_count,
            "step_count": len(self.steps),
        }
        if error:
            payload["error"] = error
        return payload


@contextmanager
def profile_step(name: str, **extra: Any):
    started = _now_ms()
    try:
        yield
    finally:
        elapsed_ms = _now_ms() - started
        profile = _current_profile.get()
        if profile is not None:
            profile.add_step(name, elapsed_ms, extra or None)
        else:
            record = {
                "event": "code_step",
                "name": name,
                "elapsed_ms": round(elapsed_ms, 3),
                **extra,
            }
            log.info(json.dumps(record, default=str))


def profiled(name: str | None = None):
    def decorator(func):
        step_name = name or f"{func.__module__}.{func.__name__}"

        @wraps(func)
        def wrapper(*args, **kwargs):
            with profile_step(step_name):
                return func(*args, **kwargs)

        return wrapper

    return decorator


class ProfilingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = f"req-{next(_request_ids)}"
        profile = RequestProfile(
            request_id=request_id,
            method=request.method,
            path=request.get_full_path(),
        )
        token = _current_profile.set(profile)
        status_code = None
        error = None

        def execute_with_profile(execute, sql, params, many, context):
            started = _now_ms()
            try:
                return execute(sql, params, many, context)
            finally:
                profile.add_query(sql, params, _now_ms() - started, many)

        try:
            log.info(json.dumps({
                "request_id": request_id,
                "event": "request_start",
                "method": request.method,
                "path": request.get_full_path(),
            }))
            with connection.execute_wrapper(execute_with_profile):
                response = self.get_response(request)
            status_code = response.status_code
            response["X-Profiler-Request-Id"] = request_id
            response["X-Profiler-Elapsed-Ms"] = str(profile.summary(status_code)["elapsed_ms"])
            return response
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
            raise
        finally:
            log.info(json.dumps(profile.summary(status_code=status_code, error=error)))
            _current_profile.reset(token)
