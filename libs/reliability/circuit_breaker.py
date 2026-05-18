from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class State(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True)
class CircuitBreakerConfig:
    consecutive_failure_threshold: int = 5
    failure_rate_window_seconds: float = 30.0
    failure_rate_threshold: float = 0.5
    failure_rate_min_samples: int = 5
    open_duration_seconds: float = 60.0


class CircuitBreaker:
    """Thread-safe circuit breaker with rolling-window failure tracking.

    Opens when either:
    - ``consecutive_failure_threshold`` consecutive failures are recorded, or
    - More than ``failure_rate_threshold`` of the last
      ``failure_rate_window_seconds`` of samples failed (requires at least
      ``failure_rate_min_samples`` samples in the window — guards against
      opening on a single failure from a cold container).

    Auto-transitions to ``half_open`` after ``open_duration_seconds``. While
    half-open, exactly one in-flight probe is allowed: the first caller to
    ``allow_call()`` is permitted through and all subsequent callers are
    rejected (return False) until that probe records an outcome. The probe's
    success closes the breaker; its failure re-opens it. This matters under
    Modal's ``@modal.concurrent`` where hundreds of webhook requests may hit
    ``allow_call()`` simultaneously at the moment we transition out of OPEN.
    """

    def __init__(
        self,
        name: str,
        config: CircuitBreakerConfig | None = None,
        on_transition: Callable[[State, State], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._name = name
        self._cfg = config or CircuitBreakerConfig()
        self._on_transition = on_transition
        self._clock = clock
        self._lock = threading.Lock()
        self._state = State.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._window: deque[tuple[float, bool]] = deque()
        # Tracks whether a probe call is in flight while half-open. Set by
        # allow_call() when it grants a probe; cleared by record_success /
        # record_failure (or any state transition). Prevents a burst of
        # concurrent callers from all firing probes simultaneously.
        self._probe_in_flight = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def state(self) -> State:
        with self._lock:
            self._maybe_half_open_locked()
            return self._state

    def allow_call(self) -> bool:
        with self._lock:
            self._maybe_half_open_locked()
            if self._state == State.CLOSED:
                return True
            if self._state == State.HALF_OPEN and not self._probe_in_flight:
                self._probe_in_flight = True
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._maybe_half_open_locked()
            self._consecutive_failures = 0
            self._append_locked(success=True)
            if self._state == State.HALF_OPEN:
                self._transition_locked(State.CLOSED)
            self._probe_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self._maybe_half_open_locked()
            self._consecutive_failures += 1
            self._append_locked(success=False)
            if self._state == State.HALF_OPEN:
                self._transition_locked(State.OPEN)
                self._probe_in_flight = False
                return
            if self._state == State.CLOSED and self._should_open_locked():
                self._transition_locked(State.OPEN)
            self._probe_in_flight = False

    def _maybe_half_open_locked(self) -> None:
        if self._state != State.OPEN or self._opened_at is None:
            return
        if self._clock() - self._opened_at >= self._cfg.open_duration_seconds:
            self._transition_locked(State.HALF_OPEN)

    def _should_open_locked(self) -> bool:
        if self._consecutive_failures >= self._cfg.consecutive_failure_threshold:
            return True
        if len(self._window) >= self._cfg.failure_rate_min_samples:
            failures = sum(1 for _, ok in self._window if not ok)
            if failures / len(self._window) > self._cfg.failure_rate_threshold:
                return True
        return False

    def _append_locked(self, *, success: bool) -> None:
        now = self._clock()
        self._window.append((now, success))
        cutoff = now - self._cfg.failure_rate_window_seconds
        while self._window and self._window[0][0] < cutoff:
            self._window.popleft()

    def _transition_locked(self, new_state: State) -> None:
        if new_state == self._state:
            return
        old = self._state
        self._state = new_state
        self._opened_at = self._clock() if new_state == State.OPEN else None
        if new_state == State.CLOSED:
            self._window.clear()
            self._consecutive_failures = 0
        # Any state change ends the previous half-open probe (if any). The
        # next half-open epoch starts with a fresh probe slot.
        self._probe_in_flight = False
        if self._on_transition is not None:
            self._on_transition(old, new_state)
