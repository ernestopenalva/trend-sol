"""Isolated BE_OFF + CB forward experiments. Never submits Binance orders."""
from __future__ import annotations

from copy import deepcopy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from src.logging_utils import JsonlLogger
from src.monitor.circuit_breaker_shadow import CircuitBreakerPosition, CircuitBreakerShadow, _bucket, _iso, _parse_ts
from src.monitor.fast_drop_semantics import (
    closed_before, fast_drop_allowed, fast_drop_boundary, fast_drop_values,
    loss_reached, normal_stop_precedes_fast,
)
from src.position.phantom_execution import PhantomExecutionClient
from src.telemetry_writer import TelemetryWriter


class FastDropPosition(CircuitBreakerPosition):
    fast_drop_evaluated = False

    def to_state(self):
        return {**super().to_state(), 'fast_drop_evaluated': self.fast_drop_evaluated}

    @classmethod
    def from_state(cls, state, config, client, logger):
        value = super().from_state(state, config, client, logger)
        value.fast_drop_evaluated = bool(state.get('fast_drop_evaluated', False))
        return value


class FastDropEmaShadow(CircuitBreakerShadow):
    """Fixed June/July/August 2026 selection; forward is first out-of-sample test."""

    def __init__(self, project_root, config, logger, telemetry, *, cohort_started_at):
        key = 'be_off_cb_fast_drop_ema_shadow'
        self.minute_closes = {}
        self.context_history = []
        path = project_root / config.get('instrumentation', {}).get(key, {}).get(
            'state_file', 'data/state/be_off_cb_fast_drop_ema_shadow.json')
        if path.exists():
            raw = json.loads(path.read_text(encoding='utf8'))
            self.minute_closes = {int(k): float(v) for k, v in raw.get('fast_drop_minute_closes', {}).items()}
            self.context_history = raw.get('fast_drop_context_history', [])
        super().__init__(project_root, config, logger, telemetry, settings_key=key,
                         strategy='BE_OFF_CB_FAST_DROP_EMA_SHADOW',
                         shadow_kind='BE_OFF_CB_FAST_DROP_EMA_SHADOW', pair_prefix='fastdropema',
                         be_off=True, cohort_started_at=cohort_started_at)

    def _position_type(self):
        return FastDropPosition

    def _load_state(self):
        super()._load_state()
        if self.state_path.exists():
            raw = json.loads(self.state_path.read_text(encoding='utf8'))
            flags = {p['pair_id']: bool(p.get('fast_drop_evaluated', False)) for p in raw.get('positions', [])}
            for position in self.positions:
                position.fast_drop_evaluated = flags.get(position.pair_id, False)

    def _extra_state(self):
        return {'fast_drop_minute_closes': self.minute_closes,
                'fast_drop_context_history': self.context_history}

    def on_closed_5m(self, snapshot):
        if self.enabled and snapshot:
            for value in (self.latest_market_context, snapshot):
                if value and (value.get('tf_5m') or {}).get('latest_closed_at_ms') is not None:
                    close = value['tf_5m']['latest_closed_at_ms']
                    self.context_history = [item for item in self.context_history
                        if item['tf_5m']['latest_closed_at_ms'] != close] + [deepcopy(value)]
            self.context_history = sorted(self.context_history,
                key=lambda item: item['tf_5m']['latest_closed_at_ms'])[-6:]
        super().on_closed_5m(snapshot)

    def _context_at(self, stamp):
        candidates = [*self.context_history, self.latest_market_context]
        eligible = [item for item in candidates if item and closed_before(
            (item.get('tf_5m') or {}).get('latest_closed_at_ms'), stamp)]
        return max(eligible, key=lambda item: item['tf_5m']['latest_closed_at_ms']) if eligible else None

    def on_closed_1m(self, payload):
        if payload.get('x'):
            self._run_input({'kind': 'reference_1m', 'boundary': int(payload['T']) + 1,
                             'close': float(payload['c'])})

    def _process_reference_1m(self, item):
        boundary = int(item['boundary'])
        if boundary in self.minute_closes and self.minute_closes[boundary] != float(item['close']):
            raise ValueError('Conflicting FAST_DROP reference candle')
        self.minute_closes[boundary] = float(item['close'])
        latest = max(self.minute_closes)
        self.minute_closes = {k: v for k, v in self.minute_closes.items() if k >= latest - 10 * 60_000}
        return True

    def _process_tick(self, price, observed_at):
        moment = _parse_ts(observed_at)
        self._event_moment = moment
        stamp = int(moment.timestamp() * 1000)
        # Match the study's minute-end label for the minute containing the tick.
        boundary = fast_drop_boundary(stamp)
        reference = self.minute_closes.get(boundary - 5 * 60_000)
        snapshot = self._context_at(stamp)
        context = (snapshot or {}).get('tf_5m', {}).get('ema_context', 'UNAVAILABLE')
        for position in list(self.open_positions):
            if position.fast_drop_evaluated or not loss_reached(position.entry_price, price):
                continue
            if not reference:
                # No decision was possible: retry on a later tick after receipt/restart.
                continue
            # First loss-level crossing only, exactly as the fixed replay.
            position.fast_drop_evaluated = True
            target, velocity = fast_drop_values(position.entry_price, reference)
            # A previously armed higher PL/TRAIL stop precedes this loss level.
            if normal_stop_precedes_fast(position.effective_stop, target):
                continue
            if not fast_drop_allowed(velocity, context):
                continue
            position._update_trough(price, observed_at)
            position.market_context_exit = deepcopy(snapshot)
            position.client.set_price(price)
            position._cb_market_ts = observed_at
            event = position._close_at_market(price, 'FAST_DROP', observed_at, target)
            if event and position.status == 'CLOSED':
                record = self.ledger._record(position, self.config, 'CIRCUIT_BREAKER_SHADOW')
                details = {'loss_at_trigger_pct': position.pnl_pct(price),
                           'velocity_5m_pct_per_min': velocity, 'ema_context': context,
                           'trigger_at': observed_at, 'exit_price': position.exit_price}
                details.update({'reference_boundary_ms': boundary-5*60_000,
                                'reference_price': reference, 'velocity_target_price': target,
                                'ema_latest_closed_at_ms': snapshot['tf_5m']['latest_closed_at_ms']})
                record.update(details)
                self.closed_records.append(record)
                net = float(record['net_pnl_pct']) * float(position.position_notional_usdt) / 100
                self.pending_closes.append({'boundary': boundary, 'net': net, 'pair_id': position.pair_id})
                self._event('FAST_DROP', pair_id=position.pair_id,
                            source_candle_open_time=position.source_candle_open_time, **details)
        self.positions = [p for p in self.positions if p.status == 'OPEN']
        super()._process_tick(price, observed_at)


class PolicyShadow(CircuitBreakerShadow):
    """BE_OFF + frozen CB with an isolated context admission policy."""

    def __init__(self, project_root: Path, config: Dict[str, Any], logger: JsonlLogger,
                 telemetry: TelemetryWriter | None, *, settings_key: str, strategy: str,
                 pair_prefix: str, policy: str, cohort_started_at: str) -> None:
        self.policy = policy
        super().__init__(project_root, config, logger, telemetry, settings_key=settings_key,
                         strategy=strategy, shadow_kind=strategy, pair_prefix=pair_prefix,
                         be_off=True, cohort_started_at=cohort_started_at)

    def _entry_policy(self, context: Dict[str, Any]) -> tuple[bool, str]:
        ema_context = context.get("ema_context")
        macd_context = context.get("macd_context")
        if self.policy == "MACD_BU_MINUS":
            if macd_context in (None, "UNAVAILABLE"):
                return False, "ENTRY_BLOCKED_MACD_UNAVAILABLE"
            return (False, "ENTRY_BLOCKED_MACD_BU_MINUS") if macd_context == "BU-" else (True, "ENTRY_ACCEPTED_MACD")
        if ema_context in (None, "UNAVAILABLE") or macd_context in (None, "UNAVAILABLE"):
            return False, "ENTRY_BLOCKED_CONTEXT_UNAVAILABLE"
        allowed = ema_context in ("LON", "BUL", "BEA") and macd_context in ("BU+", "BE+")
        return (True, "ENTRY_ACCEPTED_EMA_MACD") if allowed else (False, f"ENTRY_BLOCKED_EMA_MACD_{ema_context}_{macd_context}")


class EmaMacdHist1mShadow(PolicyShadow):
    """EMA_MACD admission plus closed 5m histogram and closed 1m confirmation."""

    def __init__(self, project_root, config, logger, telemetry, *, cohort_started_at):
        key = 'ema_macd_hist_1m_shadow'
        path = project_root / config.get('instrumentation', {}).get(key, {}).get(
            'state_file', 'data/state/ema_macd_hist_1m_shadow.json')
        raw = json.loads(path.read_text(encoding='utf8')) if path.exists() else {}
        self.tactical_candles = raw.get('tactical_candles', [])
        self.admission_audit = {}
        super().__init__(project_root, config, logger, telemetry, settings_key=key,
                         strategy='EMA_MACD_HIST_1M_SHADOW', pair_prefix='emamacdhist1m',
                         policy='EMA_MACD', cohort_started_at=cohort_started_at)

    def _extra_state(self):
        return {'tactical_candles': self.tactical_candles}

    def on_closed_1m(self, payload):
        if payload.get('x'):
            candle = {'open_time': int(payload['t']), 'close_time': int(payload['T']),
                      **{name: float(payload[key]) for name, key in
                         (('open', 'o'), ('high', 'h'), ('low', 'l'), ('close', 'c'))}}
            self._run_input({'kind': 'reference_1m', 'candle': candle})

    def _process_reference_1m(self, item):
        candle = item['candle']
        by_open = {row['open_time']: row for row in self.tactical_candles}
        by_open[candle['open_time']] = candle
        self.tactical_candles = sorted(by_open.values(), key=lambda row: row['close_time'])[-10:]
        return True

    def _process_signal(self, signal):
        if self.last_signal_source is not None and signal.source_candle_open_time <= self.last_signal_source:
            return super()._process_signal(signal)
        moment = _parse_ts(signal.ts)
        timestamp = int(moment.timestamp() * 1000)
        context = self._context_fields()
        snapshot = (self.latest_market_context or {}).get('tf_5m') or {}
        eligible = [row for row in self.tactical_candles if row['close_time'] <= timestamp]
        previous, current = (eligible[-2], eligible[-1]) if len(eligible) >= 2 else (None, None)
        base_pass, base_reason = super()._entry_policy(context)
        hist, prior_hist = snapshot.get('macd_histogram'), snapshot.get('macd_histogram_previous')
        closed_5m = snapshot.get('latest_closed_at_ms')
        hist_pass = (closed_5m is not None and closed_5m <= timestamp and hist is not None
                     and prior_hist is not None and hist > 0 and hist > prior_hist)
        tactical_pass = bool(current and previous and current['close'] > previous['close']
                             and current['close'] > current['open'])
        self.admission_audit = {
            **context, **{key: snapshot.get(key) for key in
                ('previous_open_at_ms', 'previous_closed_at_ms', 'macd_signal', 'macd_signal_previous',
                 'macd_histogram', 'macd_histogram_previous')},
            'one_minute_previous': previous, 'one_minute_current': current,
            'ema_macd_pass': base_pass, 'histogram_pass': bool(hist_pass),
            'confirmation_1m_pass': tactical_pass,
        }
        self._admission_reason = (base_reason if not base_pass else
            'ENTRY_BLOCKED_HISTOGRAM' if not hist_pass else
            'ENTRY_BLOCKED_CONFIRMATION_1M' if not tactical_pass else 'ENTRY_ACCEPTED_EMA_MACD_HIST_1M')
        result = super()._process_signal(signal)
        self._event_at(moment, 'ADMISSION_FILTERS', source_candle_open_time=signal.source_candle_open_time,
                       **self.admission_audit, final_decision='admitted' if result else 'blocked',
                       filter_reason=self._admission_reason)
        return result

    def _entry_policy(self, context):
        return (all(self.admission_audit.get(key, False) for key in
                    ('ema_macd_pass', 'histogram_pass', 'confirmation_1m_pass')), self._admission_reason)


class ElasticPosition(CircuitBreakerPosition):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hs_elastic = False
        self.hs_elastic_started_at = None
        self.hs_original_at = None
        self.hs_original_pnl_pct = None
        self.hs_original_context = None
        self.hs_elastic_worst_pnl_pct = None
        self.hs_elastic_returned_entry_at = None
        self.hs_elastic_returned_economic_be_at = None
        self.hs_elastic_extra_seconds = 0.0
        self._elastic_hard_stop_price = self.hard_stop_price

    def to_state(self):
        value = super().to_state()
        for name in (
            "hs_elastic", "hs_elastic_started_at", "hs_original_at", "hs_original_pnl_pct",
            "hs_original_context", "hs_elastic_worst_pnl_pct", "hs_elastic_returned_entry_at",
            "hs_elastic_returned_economic_be_at", "hs_elastic_extra_seconds", "_elastic_hard_stop_price",
        ):
            value[name] = getattr(self, name)
        return value

    @classmethod
    def from_state(cls, state, config, client, logger):
        value = super().from_state(state, config, client, logger)
        for name in (
            "hs_elastic", "hs_elastic_started_at", "hs_original_at", "hs_original_pnl_pct",
            "hs_original_context", "hs_elastic_worst_pnl_pct", "hs_elastic_returned_entry_at",
            "hs_elastic_returned_economic_be_at", "hs_elastic_extra_seconds", "_elastic_hard_stop_price",
        ):
            if name in state:
                setattr(value, name, state[name])
        return value


class ExperimentalRiskShadow(CircuitBreakerShadow):
    """One of the three mutually exclusive exit-risk hypotheses."""

    def __init__(self, project_root: Path, config: Dict[str, Any], logger: JsonlLogger,
                 telemetry: TelemetryWriter | None, *, settings_key: str, strategy: str,
                 pair_prefix: str, experiment: str, cohort_started_at: str) -> None:
        self.experiment = experiment
        super().__init__(project_root, config, logger, telemetry, settings_key=settings_key,
                         strategy=strategy, shadow_kind=strategy, pair_prefix=pair_prefix,
                         be_off=True, cohort_started_at=cohort_started_at)

    def _position_type(self):
        return ElasticPosition if self.experiment == "HS_BULL_ELASTIC" else CircuitBreakerPosition

    def on_closed_5m(self, snapshot: Dict[str, Any] | None) -> None:
        if not self.enabled or not snapshot:
            return
        self.latest_market_context = deepcopy(snapshot)
        if self.experiment != "HS_BULL_ELASTIC":
            self._save_state()
            return
        context = self._context_fields()
        close_price = (snapshot.get("tf_5m") or {}).get("close")
        observed_at = (snapshot.get("tf_5m") or {}).get("latest_closed_at_ms")
        if close_price is None:
            return
        stamp = datetime.fromtimestamp(float(observed_at) / 1000, timezone.utc).isoformat() if observed_at else None
        for position in list(self.open_positions):
            if not isinstance(position, ElasticPosition) or not position.hs_elastic or context.get("ema_context") == "LON":
                continue
            original = float(position._elastic_hard_stop_price)
            if float(close_price) <= original:
                self._finish_elastic_clock(position, stamp)
                self._event("HS_ELASTIC_EXIT_CONTEXT_LOST", pair_id=position.pair_id, price=close_price, **context)
                self._close_and_record(position, float(close_price), stamp, "HARD_STOP_ELASTIC_CONTEXT_LOST", original)
            else:
                self._finish_elastic_clock(position, stamp)
                position.hs_elastic = False
                position.hard_stop_price = original
                position._refresh_effective_stop()
                self._event("HS_ELASTIC_ENDED_RECOVERED", pair_id=position.pair_id, price=close_price, **context)
        self.positions = [item for item in self.positions if item.status == "OPEN"]
        self._save_state()

    def _process_tick(self, price: float, observed_at: str) -> None:
        prior_breaker = self.circuit_breaker_active
        prior = list(self.open_positions)
        context = self._context_fields()
        if self.experiment == "HS_BULL_ELASTIC":
            for position in prior:
                if not isinstance(position, ElasticPosition):
                    continue
                original = float(position._elastic_hard_stop_price)
                pnl = position.pnl_pct(price)
                if not position.hs_elastic and price <= original and context.get("ema_context") == "LON":
                    position.hs_elastic = True
                    position.hs_original_at = observed_at
                    position.hs_elastic_started_at = observed_at
                    position.hs_original_pnl_pct = pnl
                    position.hs_original_context = deepcopy(context)
                    position.hs_elastic_worst_pnl_pct = pnl
                    position.hard_stop_price = None
                    position.effective_stop = position.review_stop
                    position.stop_type = "review"
                    position._refresh_effective_stop()
                    self._event("HS_ELASTIC_STARTED", pair_id=position.pair_id, price=price, pnl_pct=pnl, **context)
                if position.hs_elastic:
                    position.hs_elastic_worst_pnl_pct = min(float(position.hs_elastic_worst_pnl_pct or pnl), pnl)
                    if price >= position.entry_price and not position.hs_elastic_returned_entry_at:
                        position.hs_elastic_returned_entry_at = observed_at
                    economic_be = position.entry_price * (1 + self._fee_pct() / 100)
                    if price >= economic_be and not position.hs_elastic_returned_economic_be_at:
                        position.hs_elastic_returned_economic_be_at = observed_at
        super()._process_tick(price, observed_at)
        for position in prior:
            if isinstance(position, ElasticPosition) and position.status == "CLOSED" and position.hs_elastic_started_at:
                self._finish_elastic_clock(position, position.close_ts or observed_at)
                for record in self.closed_records:
                    if record.get("pair_id") == position.pair_id:
                        record.update({
                            "hs_elastic": True,
                            "hs_original_at": position.hs_original_at,
                            "hs_original_pnl_pct": position.hs_original_pnl_pct,
                            "hs_original_context": position.hs_original_context,
                            "hs_elastic_started_at": position.hs_elastic_started_at,
                            "hs_elastic_worst_pnl_pct": position.hs_elastic_worst_pnl_pct,
                            "hs_elastic_returned_entry_at": position.hs_elastic_returned_entry_at,
                            "hs_elastic_returned_economic_be_at": position.hs_elastic_returned_economic_be_at,
                            "hs_elastic_extra_seconds": position.hs_elastic_extra_seconds,
                            "control_pair_id": f"beoffcb-{position.source_candle_open_time}",
                            "control_exit_pending": True,
                        })
        hard_stops = [item for item in prior if item.status == "CLOSED" and item.exit_reason == "HARD_STOP"]
        if self.experiment == "HS_BEAR_CLUSTER_EXIT" and hard_stops and context.get("ema_context") == "SHO":
            victims = [item for item in self.open_positions if item.pnl_pct(price) < 0]
            self._event("HS_BEAR_CLUSTER_TRIGGERED", trigger_pair_ids=[item.pair_id for item in hard_stops],
                        victim_pair_ids=[item.pair_id for item in victims], price=price, **context)
            for position in victims:
                self._close_and_record(position, price, observed_at, "HS_BEAR_CLUSTER_EXIT", price)
            self.positions = [item for item in self.positions if item.status == "OPEN"]
        if self.experiment == "CB_EXIT_ALL" and not prior_breaker and self.circuit_breaker_active:
            victims = list(self.open_positions)
            self._event("CB_EXIT_ALL_TRIGGERED", victim_pair_ids=[item.pair_id for item in victims], price=price, **context)
            for position in victims:
                self._close_and_record(position, price, observed_at, "CIRCUIT_BREAKER_EXIT_ALL", price)
            self.positions = [item for item in self.positions if item.status == "OPEN"]
        self._save_state()

    @staticmethod
    def _finish_elastic_clock(position: ElasticPosition, ended_at: str | None) -> None:
        start = _parse_ts(position.hs_elastic_started_at)
        end = _parse_ts(ended_at)
        if start and end:
            position.hs_elastic_extra_seconds = max(0.0, (end - start).total_seconds())

    def _process_signal(self, signal):
        prior = self.circuit_breaker_active
        result = super()._process_signal(signal)
        if self.experiment == "CB_EXIT_ALL" and not prior and self.circuit_breaker_active:
            for position in list(self.open_positions):
                self._close_and_record(position, signal.price, signal.ts, "CIRCUIT_BREAKER_EXIT_ALL", signal.price)
            self.positions = [item for item in self.positions if item.status == "OPEN"]
            self._save_state()
        return result

    def _close_and_record(self, position, price: float, observed_at: str | None, reason: str, reference: float) -> None:
        if position.status != "OPEN":
            return
        position.market_context_exit = deepcopy(self.latest_market_context)
        position._cb_market_ts = observed_at
        event = position._close_at_market(price, reason, observed_at or _iso(datetime.now(timezone.utc)), reference)
        if not event or position.status != "CLOSED":
            return
        record = self.ledger._record(position, self.config, "CIRCUIT_BREAKER_SHADOW")
        record["control_pair_id"] = f"beoffcb-{position.source_candle_open_time}"
        record["control_exit_pending"] = True
        self.closed_records.append(record)
        moment = _parse_ts(position.close_ts) or datetime.now(timezone.utc)
        stamp = int(moment.timestamp() * 1000)
        boundary = stamp - stamp % 60_000 + 60_000
        net = float(record["net_pnl_pct"]) * float(position.position_notional_usdt) / 100
        self.pending_closes.append({"boundary": boundary, "net": net, "pair_id": position.pair_id})
        self._event("EXPERIMENTAL_CLOSE", pair_id=position.pair_id, reason=reason,
                    net_pnl_pct=record.get("net_pnl_pct"), control_exit_pending=True,
                    control_pair_id=f"beoffcb-{position.source_candle_open_time}",
                    source_candle_open_time=position.source_candle_open_time)
