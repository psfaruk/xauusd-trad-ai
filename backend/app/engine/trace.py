"""Decision trace (SPEC §8.3) — stored in signals.trace, rendered in the UI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CheckResult:
    name: str
    passed: bool
    value: str  # human-readable, e.g. "close 2694.2 > EMA50 2688.7"


class Trace:
    """Ordered check log for one bar-close evaluation."""

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.direction: str | None = None
        self.checks: list[CheckResult] = list()
        self.params: dict[str, Any] = params or {}

    def add(self, name: str, passed: bool, value: Any) -> CheckResult:
        res = CheckResult(name=name, passed=bool(passed), value=str(value))
        self.checks.append(res)
        return res

    def demote_misses(self, names: tuple[str, ...]) -> None:
        """D-070 — candidate-pattern negative checks become informational
        when a DIFFERENT trigger fired the signal.

        A red `pullback`/`sfp_sweep` line on a FIRED zone signal is a
        CANDIDATE miss (that pattern was absent on this bar), not a
        failed gate — the zone fired on its own merit via the D-049
        fallback. Demoting keeps the fired signal's trace honest (every
        red line on a fired signal must be a real, actionable miss).
        """
        for c in self.checks:
            if not c.passed and c.name in names:
                c.passed = True
                c.value += " (candidate miss — zone fired on its own merit)"

    @property
    def failed(self) -> CheckResult | None:
        for c in self.checks:
            if not c.passed:
                return c
        return None

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "checks": [
                {"name": c.name, "pass": c.passed, "value": c.value} for c in self.checks
            ],
            "params": self.params,
        }
