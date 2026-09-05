"""Minute clock for the frozen CB rule. No market features or entry gates."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class CBReplayClock:
    capital: float = 100.0
    equity: float = 100.0
    peak: float = 100.0
    history: list = field(default_factory=list)
    was_true: bool = False
    pause_until: int = -1
    crises: int = 0
    paused_minutes: int = 0
    last_boundary: int | None = None

    def minute(self, boundary: int, closes: list[tuple[int, float]]) -> list[dict]:
        if boundary % 60_000:
            raise ValueError('CB requires a whole-minute boundary')
        if self.last_boundary is not None and boundary != self.last_boundary + 60_000:
            raise ValueError('CB minute clock must advance without skipping/repeating')
        previously_paused = self.last_boundary is not None and self.last_boundary < self.pause_until
        old_deadline = self.pause_until
        for timestamp, net in closes:
            if timestamp != boundary:
                raise ValueError('Close must belong to the evaluated minute')
            self.equity += net
            self.peak = max(self.peak, self.equity)
            self.history.append([timestamp, net])
        self.history = [x for x in self.history if boundary - 14_400_000 < x[0] <= boundary]
        rolling_pct = sum(x[1] for x in self.history) / self.capital * 100
        dd = (self.peak - self.equity) / self.capital * 100
        condition = dd >= 1.5 and rolling_pct <= -0.5 and len(self.history) >= 2
        events = []
        if condition and not self.was_true:
            self.pause_until = max(self.pause_until, boundary + 21_600_000)
            self.crises += 1
            events.append(dict(event='CIRCUIT_BREAKER_TRIGGERED', boundary=boundary,
                               until=self.pause_until, realized_dd_pct=dd,
                               rolling_net_4h_pct=rolling_pct, min2_count=len(self.history)))
        self.was_true = condition
        paused = boundary < self.pause_until
        if previously_paused and not paused:
            events.append(dict(event='CIRCUIT_BREAKER_RELEASED', boundary=boundary,
                               until=old_deadline))
        self.paused_minutes += int(paused)
        self.last_boundary = boundary
        return events

    @property
    def paused(self):
        return self.last_boundary is not None and self.last_boundary < self.pause_until

    def to_state(self):
        return asdict(self)

    @classmethod
    def from_state(cls, data):
        return cls(**data)
