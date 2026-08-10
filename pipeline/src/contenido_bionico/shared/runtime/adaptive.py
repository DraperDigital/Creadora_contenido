"""Adaptive concurrency limiter (AIMD) for API-bound agent calls.

The animation scene-authors are bottlenecked by the Anthropic API's rate, not by
local CPU (a run that 401-free still failed had ~40 GB RAM free and low CPU).
Firing a *fixed* number of concurrent `claude` calls is wrong in both
directions: too low wastes throughput, too high trips 429 (rate limit) / 529
(overloaded) and the calls collapse. This limiter self-tunes the number of
in-flight API calls instead of hard-coding one: it grows while calls succeed and
backs off the moment the API throttles, settling near the most the API currently
allows. No magic number.
"""
from __future__ import annotations

import os
import re
import threading

# Substrings in a FAILED agent call that mean "the API throttled us", as opposed
# to a genuine content/code error. Matched case-insensitively against the tail.
_THROTTLE_RE = re.compile(
    r"\b429\b|\b529\b|\b503\b|socket connection was closed|overloaded|"
    r"rate[ _-]?limit|too many requests|service unavailable",
    re.IGNORECASE,
)


def is_throttle_error(text: str) -> bool:
    """True when an agent failure looks like API throttling/overload."""
    return bool(text) and bool(_THROTTLE_RE.search(text))


def default_max_concurrency() -> int:
    """Machine-aware ceiling for concurrent API calls.

    Scales with cores so a bigger machine starts from a higher cap, but stays
    bounded: the real limit is the API's per-session rate, not the CPU, so
    dozens of concurrent calls are never useful no matter how many cores exist.
    Capped at 4 — measured runs show the per-session API rate rarely sustains
    more, and the AIMD limiter would just oscillate against a higher cap.
    Override with BIONICO_MAX_API_CONCURRENCY.
    """
    env = os.environ.get("BIONICO_MAX_API_CONCURRENCY")
    if env and env.isdigit() and int(env) > 0:
        return int(env)
    cores = os.cpu_count() or 4
    return max(2, min(cores - 2, 4))


def cpu_workers(fraction: float = 0.70, *, minimum: int = 1,
                ram_per_worker_gb: float = 1.5) -> int:
    """Concurrent CPU-bound (render) workers, adapted to the host machine.

    Targets `fraction` of the logical cores (default 70%), so a bigger Mac/PC
    parallelizes more heavy renders and a smaller one stays conservative — the
    tool scales itself instead of a hard-coded cap. Additionally capped by FREE
    RAM (~`ram_per_worker_gb` per concurrent Remotion/ffmpeg render) so a
    low-memory machine never oversubscribes and OOMs. `BIONICO_RENDER_WORKERS`
    overrides everything. `psutil` is optional: without it, only the core target
    applies (still bounded, just no RAM cap).

    Unlike `default_max_concurrency` (which bounds API-BOUND agent calls, where
    the ceiling is the API rate, not the CPU), this bounds LOCAL render work.
    """
    env = os.environ.get("BIONICO_RENDER_WORKERS")
    if env and env.isdigit() and int(env) > 0:
        return max(minimum, int(env))
    cores = os.cpu_count() or 4
    workers = max(minimum, round(cores * fraction))
    try:
        import psutil  # optional dependency
        free_gb = psutil.virtual_memory().available / 1e9
        ram_cap = max(minimum, int(free_gb / max(0.25, ram_per_worker_gb)))
        workers = min(workers, ram_cap)
    except Exception:  # noqa: BLE001 — psutil missing / any failure: core target only
        pass
    return max(minimum, workers)


class AdaptiveLimiter:
    """Thread-safe AIMD gate. Call acquire() before an API call and
    release(throttled) after. The in-flight ceiling rises by 1 after `grow_after`
    consecutive successes (additive increase) and halves on any throttle
    (multiplicative decrease), kept within [minimum, maximum]."""

    def __init__(self, *, maximum: int | None = None, start: int | None = None,
                 minimum: int = 1, grow_after: int = 3, on_change=None):
        self.maximum = max(1, maximum or default_max_concurrency())
        self.minimum = max(1, min(minimum, self.maximum))
        # Default start of 3 (minimum + 2): the scene authors are API-bound,
        # not CPU-bound, and starting at 2 measurably wasted ~8 min per run
        # ramping up. The AIMD logic still backs off on any throttle.
        start = self.minimum + 2 if start is None else start
        self.limit = max(self.minimum, min(start, self.maximum))
        self.grow_after = max(1, grow_after)
        self._on_change = on_change
        self._inflight = 0
        self._streak = 0
        self._cv = threading.Condition()

    def acquire(self) -> None:
        with self._cv:
            while self._inflight >= self.limit:
                self._cv.wait()
            self._inflight += 1

    def release(self, throttled: bool = False) -> None:
        with self._cv:
            self._inflight = max(0, self._inflight - 1)
            before = self.limit
            if throttled:
                self._streak = 0
                self.limit = max(self.minimum, self.limit // 2)
            else:
                self._streak += 1
                if self._streak >= self.grow_after and self.limit < self.maximum:
                    self.limit += 1
                    self._streak = 0
            changed = self.limit != before
            self._cv.notify_all()
        if changed and self._on_change:
            try:
                self._on_change(before, self.limit, throttled)
            except Exception:  # noqa: BLE001 - logging must never break a run
                pass

    @property
    def current_limit(self) -> int:
        return self.limit
