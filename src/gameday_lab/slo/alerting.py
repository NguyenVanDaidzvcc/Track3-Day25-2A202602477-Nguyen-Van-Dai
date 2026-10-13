"""Part E — Multi-window, multi-burn-rate alerting.

Input: per-minute total/bad request counts.
An alert fires when both its long and short windows reach the rule's burn rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isclose


@dataclass(frozen=True)
class AlertRule:
    name: str
    severity: str
    long_min: int
    short_min: int
    burn_rate: float


DEFAULT_RULES: tuple[AlertRule, ...] = (
    AlertRule("fast_burn_2pct_1h", "page", 60, 5, 14.4),
    AlertRule("slow_burn_5pct_6h", "page", 360, 30, 6.0),
    AlertRule("budget_leak_10pct_3d", "ticket", 4320, 360, 1.0),
)


@dataclass(frozen=True)
class AlertEvent:
    rule: str
    severity: str
    start_min: int
    end_min: int  # Exclusive


class WindowSums:
    """Fast O(1) window sums using a prefix-sum array."""

    def __init__(self, values: list[int]) -> None:
        self.prefix = [0]
        for value in values:
            self.prefix.append(self.prefix[-1] + value)

    def sum(self, end_inclusive: int, length: int) -> int:
        """Sum a trailing window, clipping to available data."""
        if end_inclusive < 0 or length <= 0:
            return 0

        end = min(end_inclusive + 1, len(self.prefix) - 1)
        start = max(0, end - length)
        return self.prefix[end] - self.prefix[start]


class BurnRateAlerter:
    def __init__(
        self,
        slo: float = 0.995,
        rules: tuple[AlertRule, ...] = DEFAULT_RULES,
    ) -> None:
        if not 0 < slo < 1:
            raise ValueError("slo must be in (0, 1)")
        self.slo = slo
        self.rules = rules

    def burn_rate(
        self,
        total: WindowSums,
        bad: WindowSums,
        end: int,
        length: int,
    ) -> float:
        """Return error ratio divided by SLO error budget ratio."""
        requests = total.sum(end, length)
        if requests == 0:
            return 0.0

        failures = bad.sum(end, length)
        return (failures / requests) / (1 - self.slo)

    @staticmethod
    def _at_least(actual: float, threshold: float) -> bool:
        """Account for floating-point noise at an exact burn-rate threshold."""
        return actual >= threshold or isclose(
            actual, threshold, rel_tol=1e-12, abs_tol=1e-12
        )

    def evaluate(self, total: list[int], bad: list[int]) -> list[AlertEvent]:
        """Return contiguous firing episodes, ordered by start minute."""
        if len(total) != len(bad):
            raise ValueError("length mismatch")

        total_sums = WindowSums(total)
        bad_sums = WindowSums(bad)
        events: list[AlertEvent] = []
        n = len(total)

        for rule in self.rules:
            episode_start: int | None = None

            for minute in range(n):
                long_burn = self.burn_rate(
                    total_sums, bad_sums, minute, rule.long_min
                )
                short_burn = self.burn_rate(
                    total_sums, bad_sums, minute, rule.short_min
                )

                firing = self._at_least(
                    long_burn, rule.burn_rate
                ) and self._at_least(
                    short_burn, rule.burn_rate
                )

                if firing and episode_start is None:
                    episode_start = minute

                elif not firing and episode_start is not None:
                    events.append(
                        AlertEvent(
                            rule=rule.name,
                            severity=rule.severity,
                            start_min=episode_start,
                            end_min=minute,
                        )
                    )
                    episode_start = None

            if episode_start is not None:
                events.append(
                    AlertEvent(
                        rule=rule.name,
                        severity=rule.severity,
                        start_min=episode_start,
                        end_min=n,
                    )
                )

        return sorted(events, key=lambda event: event.start_min)


def error_budget_report(
    total: list[int],
    bad: list[int],
    slo: float = 0.995,
) -> dict[str, int | float | bool]:
    """Report error budget usage and whether releases should be frozen."""

    requests = sum(total)
    failures = sum(bad)
    allowed_bad = (1 - slo) * requests

    availability = 1 - failures / requests if requests else 0.0
    consumed = failures / allowed_bad if allowed_bad else 0.0
    remaining = round(1 - consumed, 4)

    return {
        "requests": requests,
        "allowed_bad": round(allowed_bad, 1),
        "bad": failures,
        "availability": round(availability, 6),
        "budget_consumed": round(consumed, 4),
        "budget_remaining": remaining,
        "freeze_releases": remaining <= 0,
    }