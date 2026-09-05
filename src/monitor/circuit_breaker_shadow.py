"""Independent, order-free REAL_A shadow with the frozen circuit-breaker rule."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from src.logging_utils import JsonlLogger, now_iso
from src.monitor.context_shadow import RealAContextShadow
from src.monitor.entry_engine import EntrySignal
from src.position.bot_full_engine import BotFullExitPosition
from src.position.phantom_execution import PhantomExecutionClient
from src.telemetry_writer import TelemetryWriter
from src.trade_ledger import TradeLedger
from src.monitor.cb_replay_clock import CBReplayClock


class CircuitBreakerPosition(BotFullExitPosition):
    """Keep CB audit identity while using REAL_A's economic PL floor."""

    def _active_profit_lock_economic_floor(self) -> float | None:
        kind = self.shadow_kind
        try:
            self.shadow_kind = None
            return super()._active_profit_lock_economic_floor()
        finally:
            self.shadow_kind = kind

    def on_tick(self, price: float, market_ts: str | None = None):
        self._cb_market_ts = market_ts
        return super().on_tick(price, market_ts)

    def _close_at_market(self, price, reason, ts, trigger_reference):
        return super()._close_at_market(price, reason, self._cb_market_ts or ts, trigger_reference)


class CircuitBreakerShadow(RealAContextShadow):
    """REAL_A clone. Only admission is suspended by its own realized results."""

    def __init__(self, project_root: Path, config: Dict[str, Any], logger: JsonlLogger, telemetry: TelemetryWriter | None) -> None:
        super().__init__(
            project_root, config, logger, telemetry,
            settings_key="circuit_breaker_shadow", strategy="REAL_A_CB_SHADOW",
            shadow_kind="REAL_A_CB_SHADOW", pair_prefix="cb", predicate=lambda _engine, _snapshot: True,
        )
        self.capital = float(self.settings.get("initial_capital_usdt", config["capital"]["operational_balance_usdt"]))
        self.equity = self.peak_equity = self.capital
        self.closed_history: list[tuple[datetime, float]] = []
        self.circuit_breaker_active = False
        self.circuit_breaker_started_at: str | None = None
        self.circuit_breaker_until: str | None = None
        self.crises_triggered = self.blocked_circuit_breaker = 0
        self.market_points: list[tuple[datetime, float]] = []
        self.cohort_started_at: str | None = None
        self.cohort_start_price: float | None = None
        self.trigger_price: float | None = None
        self.clock = CBReplayClock(self.capital, self.capital, self.capital)
        self.pending_closes: list[dict] = []
        self.closed_records: list[dict] = []
        self.audit_events: list[dict] = []
        self.last_input_ms: int | None = None
        self.last_signal_source: int | None = None
        self._event_moment: datetime | None = None
        self.sequence = 0
        self._in_transaction = False
        self.pending_input_path = self.state_path.with_suffix(self.state_path.suffix + '.pending')
        if self.enabled:
            self._load_cb_state()
            self._recover_pending_input()

    def on_signal(self, signal: EntrySignal) -> bool:
        return self._run_input({'kind':'signal', 'signal':asdict(signal),
                                'context':deepcopy(self.latest_market_context)})

    def _process_signal(self, signal: EntrySignal) -> bool:
        if not self.enabled or not self.accept_new_entries:
            return False
        moment = _parse_ts(signal.ts)
        if moment is None:
            raise ValueError('CB signal timestamp is required')
        self._event_moment = moment
        self._advance_clock(moment, signal.price, from_signal=True)
        if self.last_signal_source is not None and signal.source_candle_open_time <= self.last_signal_source:
            return False
        self.last_signal_source = signal.source_candle_open_time
        self._event('SIGNAL_OPPORTUNITY', source_candle_open_time=signal.source_candle_open_time,
                    price=signal.price, detector_boundary_ms=self.clock.last_boundary)
        if self.circuit_breaker_active:
            self.blocked_circuit_breaker += 1
            return self._block("ENTRY_BLOCKED_CIRCUIT_BREAKER", signal, _bucket(signal.source_candle_open_time), breaker_until=self.circuit_breaker_until)
        bucket = _bucket(signal.source_candle_open_time)
        if len(self.open_positions) >= int(self.settings.get('max_open_positions', 5)):
            self.blocked_capacity += 1
            return self._block('ENTRY_BLOCKED_SHADOW_CAPACITY', signal, bucket)
        return super().on_signal(signal)

    def _open(self, signal: EntrySignal, bucket: int) -> None:
        notional = float(self.config['capital']['operational_balance_usdt']) * float(self.config['capital']['trade_size_pct']) / 100
        client = PhantomExecutionClient()
        client.set_price(signal.price)
        pair_id = f'{self.pair_prefix}-{signal.source_candle_open_time}'
        position = CircuitBreakerPosition(
            pair_id=pair_id, symbol=str(self.config['symbol']), entry_price=float(signal.price),
            quantity=notional / float(signal.price), entry_order={'shadow': True},
            open_ts=signal.ts, config=self._exit_config(), client=client, logger=self.logger,
            entry_atr=signal.entry_atr, atr_timeframe=signal.atr_timeframe, atr_period=signal.atr_period,
            source_candle_open_time=signal.source_candle_open_time, position_notional_usdt=notional,
            no_progress_enabled=False, no_progress_tolerance_seconds=None, no_progress_tolerance_source='DISABLED',
        )
        position.phantom, position.phantom_id, position.shadow_kind = True, pair_id, self.shadow_kind
        position.market_context_entry = deepcopy(self.latest_market_context)
        self.positions.append(position)
        self.entries_by_bucket[bucket] = self.entries_by_bucket.get(bucket, 0) + 1
        self.max_simultaneous_positions = max(self.max_simultaneous_positions, len(self.open_positions))
        self.logger.trade(position._trade_event('OPEN', signal.price, 0.0, None, price_source='signal'))
        self._event('OPEN', pair_id=pair_id, source_candle_open_time=signal.source_candle_open_time,
                    admission_bucket_open_time=bucket)
        self._emit_ema_entry(position)
        self._save_state()

    def _load_state(self) -> None:
        super()._load_state()
        # Restore the CB-specific position type as well as its persisted ladder state.
        self.positions = [
            CircuitBreakerPosition.from_state(item.to_state(), self._exit_config(), item.client, self.logger)
            for item in self.positions
        ]

    def on_kline(
        self, stream: str, payload: Dict[str, Any], snapshot: Dict[str, Any] | None
    ) -> None:
        """Never evaluate a second entry engine; only the shared signal is used."""
        return None

    def on_approved_real_a_signal(
        self, signal: EntrySignal, market_context: Dict[str, Any] | None
    ) -> bool:
        """Receive the shared gate-approved opportunity BEFORE REAL_A admission."""
        self.latest_market_context = deepcopy(market_context) if market_context else self.latest_market_context
        return self.on_signal(signal)

    def record_real_admission(self, signal: EntrySignal, outcome: str) -> None:
        """Observational only. Never changes the CB's admission or detector."""
        self._run_input({'kind':'real_outcome', 'source':signal.source_candle_open_time,
                         'ts':signal.ts, 'outcome':outcome})

    def on_tick(self, price: float, observed_at: str) -> None:
        self._run_input({'kind':'tick', 'price':price, 'observed_at':observed_at})

    def _process_tick(self, price: float, observed_at: str) -> None:
        if not self.enabled:
            return
        moment = _parse_ts(observed_at) or datetime.now(timezone.utc)
        self._event_moment = moment
        before = [item.to_state() for item in self.open_positions]
        market_changed = self._record_market_point(moment, price)
        self._advance_clock(moment, price)
        changed = False
        for position in list(self.open_positions):
            client = position.client
            if not isinstance(client, PhantomExecutionClient):
                continue
            client.set_price(price)
            event = position.on_tick(price, market_ts=observed_at)
            if not event or position.status != "CLOSED":
                continue
            position.market_context_exit = deepcopy(self.latest_market_context)
            record = self.ledger._record(position, self.config, 'CIRCUIT_BREAKER_SHADOW')
            self.closed_records.append(record)
            # All closes of [minute start, minute end) enter the next boundary,
            # before that boundary's shared signal, exactly once and in input order.
            stamp = int(moment.timestamp()*1000)
            boundary = stamp - stamp % 60_000 + 60_000
            net_dollars = float(record['net_pnl_pct']) * float(position.position_notional_usdt) / 100
            self.pending_closes.append({'boundary':boundary, 'net':net_dollars, 'pair_id':position.pair_id})
            self.logger.system("circuit_breaker_shadow_closed", pair_id=position.pair_id, reason=position.exit_reason)
            changed = True
        self.positions = [item for item in self.positions if item.status == "OPEN"]
        if changed or market_changed or before != [item.to_state() for item in self.open_positions]:
            self._save_state()

    def _run_input(self, item: dict, *, recovering: bool = False):
        """Single-writer transaction: durable input -> checkpoint -> projections.

        A crash before checkpoint replays the pending input; a crash after it
        only repairs projections. Neither path repeats a financial close.
        """
        if not self.enabled:
            return False
        if self._in_transaction:
            raise RuntimeError('Reentrant CB input')
        sequence = int(item['sequence']) if recovering else self.sequence+1
        if sequence != self.sequence+1:
            raise ValueError('CB pending input sequence is inconsistent')
        item = {**item, 'sequence':sequence}
        try:
            if not recovering:
                _atomic_json(self.pending_input_path, json.dumps(item, ensure_ascii=False))
            self._in_transaction = True
            if item['kind'] == 'tick':
                result = self._process_tick(item['price'], item['observed_at'])
            elif item['kind'] == 'signal':
                self.latest_market_context = item.get('context')
                result = self._process_signal(EntrySignal(**item['signal']))
            elif item['kind'] == 'real_outcome':
                self._event_at(_parse_ts(item['ts']), 'REAL_A_ADMISSION_OBSERVED',
                               source_candle_open_time=item['source'], outcome=item['outcome'])
                result = None
            else:
                raise ValueError('Unknown CB pending input kind')
            self.sequence = sequence
            self._in_transaction = False
            self._save_state()
            return result
        except Exception:
            # Do not process another input on mutated, uncommitted memory.
            self.enabled = False
            raise
        finally:
            self._in_transaction = False

    def _recover_pending_input(self):
        if not self.pending_input_path.exists():
            return
        item = json.loads(self.pending_input_path.read_text(encoding='utf-8'))
        if int(item['sequence']) > self.sequence:
            self._run_input(item, recovering=True)

    def _advance_clock(self, moment: datetime, price: float, *, from_signal: bool = False) -> None:
        stamp = int(moment.timestamp()*1000)
        if not from_signal and self.last_input_ms is not None and stamp < self.last_input_ms:
            raise ValueError('CB input is out of order; cohort requires reconciliation')
        if not from_signal:
            self.last_input_ms = stamp
        boundary = stamp - stamp % 60_000
        if self.clock.last_boundary is not None and boundary < self.clock.last_boundary:
            raise ValueError('Late CB input crosses an already evaluated minute; reconciliation required')
        if self.clock.last_boundary is None:
            self.clock.last_boundary = boundary - 60_000
        changed = False
        while self.clock.last_boundary < boundary:
            current = self.clock.last_boundary + 60_000
            due = [x for x in self.pending_closes if x['boundary'] == current]
            self.pending_closes = [x for x in self.pending_closes if x['boundary'] != current]
            for event in self.clock.minute(current, [(current,x['net']) for x in due]):
                at = datetime.fromtimestamp(current/1000, timezone.utc)
                if event['event'] == 'CIRCUIT_BREAKER_TRIGGERED':
                    self.circuit_breaker_started_at = _iso(at)
                    self.trigger_price = price if current == boundary else None
                self._event_at(at, event['event'], trigger_time=self.circuit_breaker_started_at,
                    release_time=_iso(at) if event['event'].endswith('RELEASED') else None,
                    cooldown_until=_iso(datetime.fromtimestamp(event['until']/1000, timezone.utc)),
                    price=price if current == boundary else None, clock_boundary_ms=current,
                    market_observed_at=_iso(moment),
                    sol_return_cooldown_pct=(price/self.trigger_price-1)*100
                        if event['event'].endswith('RELEASED') and self.trigger_price and current == boundary else None,
                    realized_equity=self.clock.equity, realized_peak=self.clock.peak,
                    sol_return_1h_pct=self._return_ago(at, price, 1) if current == boundary else None,
                    sol_return_4h_pct=self._return_ago(at, price, 4) if current == boundary else None,
                    sol_return_12h_pct=self._return_ago(at, price, 12) if current == boundary else None)
            changed = True
        self.equity, self.peak_equity = self.clock.equity, self.clock.peak
        self.closed_history = [(datetime.fromtimestamp(t/1000, timezone.utc), net) for t,net in self.clock.history]
        self.circuit_breaker_active = self.clock.paused
        self.circuit_breaker_until = _iso(datetime.fromtimestamp(self.clock.pause_until/1000, timezone.utc)) if self.clock.paused else None
        self.crises_triggered = self.clock.crises
        if changed:
            self._save_state()

    def _record_market_point(self, moment: datetime, price: float) -> bool:
        if self.cohort_started_at is None:
            self.cohort_started_at, self.cohort_start_price = _iso(moment), price
        added = not self.market_points or (moment - self.market_points[-1][0]).total_seconds() >= 60
        if added:
            self.market_points.append((moment, price))
        cutoff = moment - timedelta(hours=13)
        self.market_points = [(ts, item) for ts, item in self.market_points if ts >= cutoff]
        return added

    def _return_ago(self, moment: datetime, price: float, hours: int) -> float | None:
        target = moment - timedelta(hours=hours)
        candidates = [(ts, value) for ts, value in self.market_points if ts <= target]
        return None if not candidates else (price / candidates[-1][1] - 1) * 100

    def _return_since(self, since: datetime, price: float) -> float | None:
        candidates = [(ts, value) for ts, value in self.market_points if ts >= since]
        return None if not candidates else (price / candidates[0][1] - 1) * 100

    def _fee_pct(self) -> float:
        fees = self.config.get("fees", {})
        if not fees.get("enabled"):
            return 0.0
        taker = float(fees.get("taker_fee_pct", 0))
        return taker * (0.75 if fees.get("use_bnb_discount") else 1.0) * 2

    def _event_at(self, moment: datetime, event: str, **fields: Any) -> None:
        payload = {"ts": _iso(moment), "strategy": self.strategy, "shadow_kind": self.shadow_kind, "event": event, **fields}
        payload['event_id'] = f'cb-event-{len(self.audit_events)+1}'
        self.audit_events.append(payload)

    def _event(self, event: str, **fields: Any) -> None:
        """Route inherited admission events to the CB audit stream, not context telemetry."""
        self._event_at(self._event_moment or datetime.now(timezone.utc), event, **fields)

    def _load_cb_state(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if self.ledger.path.exists() and self.ledger.path.stat().st_size:
                raise ValueError('CB ledger exists without its checkpoint; reconciliation required')
            return
        if data.get('cb_schema') != 2:
            raise ValueError('Legacy CB state: archive the old cohort before starting this version')
        self.clock = CBReplayClock.from_state(data['clock'])
        self.sequence = int(data['sequence'])
        self.pending_closes = data['pending_closes']
        self.closed_records = data['closed_records']
        self.audit_events = data['audit_events']
        self.last_input_ms = data.get('last_input_ms')
        self.last_signal_source = data.get('last_signal_source')
        self.trigger_price = data.get('trigger_price')
        self.equity = float(data.get("realized_equity", self.capital)); self.peak_equity = float(data.get("realized_peak_equity", self.capital))
        self.closed_history = [(_parse_ts(item[0]), float(item[1])) for item in data.get("rolling_closed_history", []) if _parse_ts(item[0])]
        for name in ("circuit_breaker_active", "circuit_breaker_started_at", "circuit_breaker_until", "crises_triggered", "blocked_circuit_breaker", "cohort_started_at", "cohort_start_price"):
            setattr(self, name, data.get(name, getattr(self, name)))
        self.market_points = [(_parse_ts(item[0]), float(item[1])) for item in data.get("market_points", []) if _parse_ts(item[0])]
        self.latest_market_context = data.get('latest_market_context')
        self._check_reconciliation()
        self._project_committed()

    def _check_reconciliation(self):
        ids = [x['pair_id'] for x in self.closed_records]
        if len(set(ids)) != len(ids) or set(ids) & {x.pair_id for x in self.open_positions}:
            raise ValueError('CB duplicate close or closed position restored as open')
        pending_ids = {x['pair_id'] for x in self.pending_closes}
        if len(pending_ids) != len(self.pending_closes) or not pending_ids <= set(ids):
            raise ValueError('CB pending closes inconsistent with ledger')
        equity = peak = self.capital
        history = []
        for row in self.closed_records:
            if row['pair_id'] in pending_ids:
                continue
            net = float(row['net_pnl_pct'])*float(row['position_notional_usdt'])/100
            equity += net
            peak = max(peak,equity)
            timestamp = int(_parse_ts(row['closed_at']).timestamp()*1000)
            history.append([timestamp-timestamp%60_000+60_000,net])
        boundary = self.clock.last_boundary
        history = [x for x in history if boundary is not None and boundary-14_400_000 < x[0] <= boundary]
        if (equity,peak,history) != (self.clock.equity,self.clock.peak,self.clock.history):
            raise ValueError('CB ledger/equity/rolling history reconciliation failed')

    def _save_state(self) -> None:
        if not self.enabled or self._in_transaction:
            return
        latest = max(self.entries_by_bucket, default=0)
        payload: Dict[str, Any] = {
            'cb_schema':2, 'sequence':self.sequence, 'clock':self.clock.to_state(), 'pending_closes':self.pending_closes,
            'latest_market_context':self.latest_market_context,
            'trigger_price':self.trigger_price,
            'closed_records':self.closed_records, 'audit_events':self.audit_events,
            'last_input_ms':self.last_input_ms, 'last_signal_source':self.last_signal_source,
            "updated_at": now_iso(), "entries_by_bucket": {str(k): v for k, v in self.entries_by_bucket.items() if k >= latest - 86_400_000}, "positions": [item.to_state() for item in self.open_positions],
            "realized_equity": self.equity, "realized_peak_equity": self.peak_equity, "rolling_closed_history": [[_iso(ts), pnl] for ts, pnl in self.closed_history], "circuit_breaker_active": self.circuit_breaker_active, "circuit_breaker_started_at": self.circuit_breaker_started_at, "circuit_breaker_until": self.circuit_breaker_until, "crises_triggered": self.crises_triggered, "blocked_circuit_breaker": self.blocked_circuit_breaker, "cohort_started_at": self.cohort_started_at, "cohort_start_price": self.cohort_start_price, "market_points": [[_iso(ts), price] for ts, price in self.market_points],
        }
        for name in ("blocked_context", "blocked_context_unavailable", "blocked_capacity", "blocked_same_5m", "blocked_spacing", "max_simultaneous_positions"):
            payload[name] = getattr(self, name)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            _atomic_json(self.state_path, json.dumps(payload, ensure_ascii=False))
            self._project_committed()
        except Exception:
            self.enabled = False
            raise

    def _project_committed(self):
        """Ledger/events are idempotent projections of the committed snapshot.

        Crash after state commit and before projection is repaired at restore.
        No engine state is reconstructed from the possibly lagging projection.
        """
        for path, records in (
            (self.ledger.path, self.closed_records),
            (self.project_root/'data/telemetry/circuit_breaker_shadow_events.jsonl', self.audit_events),
        ):
            content = ''.join(json.dumps(x, ensure_ascii=False)+'\n' for x in records)
            if getattr(self, '_projected_'+str(path), None) == content:
                continue
            if path.exists():
                existing = path.read_text(encoding='utf-8')
                if existing == content:
                    setattr(self, '_projected_'+str(path), content)
                    continue
                if existing and not content.startswith(existing):
                    raise ValueError(f'CB projection diverges from committed state: {path}')
            _atomic_json(path, content)
            setattr(self, '_projected_'+str(path), content)


def _parse_ts(value: Any) -> datetime | None:
    if not value: return None
    try:
        item = datetime.fromisoformat(str(value).replace("Z", "+00:00")); return item.replace(tzinfo=item.tzinfo or timezone.utc).astimezone(timezone.utc)
    except ValueError: return None

def _iso(value: datetime) -> str: return value.astimezone(timezone.utc).isoformat()
def _bucket(value: int) -> int: return int(value) - int(value) % 300_000


def _atomic_json(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name+'.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    if os.name != 'nt':
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
