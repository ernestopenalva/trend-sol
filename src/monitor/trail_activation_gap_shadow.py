"""Two isolated BE_OFF_CB clones; only configured ATR trailing differs."""
from copy import deepcopy
import json
from datetime import datetime
from src.monitor.circuit_breaker_shadow import CircuitBreakerPosition, CircuitBreakerShadow


def owner(position):
    if position.stop_type == 'trailing':
        return 'TRAIL'
    if position.stop_type == 'profit_lock':
        return position.profit_lock_step or 'PL'
    if position.stop_type == 'hard_stop':
        return 'HS'
    return str(position.stop_type).upper()


class TrailAuditPosition(CircuitBreakerPosition):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.trail_audit = {'activation_time': None, 'first_dominance_time': None,
                            'peak_at_first_dominance': None, 'peak_at_first_dominance_atr': None,
                            'owner_episodes': [{'at': self.open_ts, 'owner': owner(self)}]}
        self.simultaneous_at_entry = None

    def on_tick(self, price, market_ts=None):
        was_active = self.trailing_active
        event = super().on_tick(price, market_ts)
        audit = self.trail_audit
        if not was_active and self.trailing_active:
            audit['activation_time'] = market_ts
        current = owner(self)
        if current == 'TRAIL' and audit['first_dominance_time'] is None:
            audit['first_dominance_time'] = market_ts
            audit['peak_at_first_dominance'] = self.highest_price
            audit['peak_at_first_dominance_atr'] = self.peak_atr()
        if audit['owner_episodes'][-1]['owner'] != current:
            audit['owner_episodes'].append({'at': market_ts, 'owner': current})
        return event

    def to_state(self):
        return {**super().to_state(), 'trail_audit': deepcopy(self.trail_audit),
                'simultaneous_at_entry': self.simultaneous_at_entry}

    @classmethod
    def from_state(cls, state, config, client, logger):
        value = super().from_state(state, config, client, logger)
        value.trail_audit = deepcopy(state.get('trail_audit', value.trail_audit))
        value.simultaneous_at_entry = state.get('simultaneous_at_entry')
        return value

    def trail_telemetry(self):
        audit = self.trail_audit
        durations = {}
        episodes = audit['owner_episodes']
        for i, row in enumerate(episodes):
            until = episodes[i+1]['at'] if i+1 < len(episodes) else (self.close_ts if self.status == 'CLOSED' else None)
            if row['at'] and until:
                seconds = (datetime.fromisoformat(until)-datetime.fromisoformat(row['at'])).total_seconds()
                durations[row['owner']] = durations.get(row['owner'], 0)+max(0, seconds)
        context = self.market_context_exit if self.status == 'CLOSED' else self.market_context_entry
        return {'arm': self.shadow_kind, 'trade_id': self.pair_id,
                'signal_timestamp': self.open_ts, 'highest_price': self.highest_price,
                'trail_activation_atr': self.trailing_activation_atr, 'trail_gap_atr': self.trailing_gap_atr,
                'trail_activated': self.trailing_active, 'trail_activation_time': audit['activation_time'],
                'trail_candidate': self._current_trailing_stop(),
                'effective_stop': self.effective_stop, 'effective_stop_owner': owner(self),
                'first_trail_dominance_time': audit['first_dominance_time'],
                'peak_at_first_trail_dominance': audit['peak_at_first_dominance'],
                'peak_at_first_trail_dominance_atr': audit['peak_at_first_dominance_atr'],
                'stop_owner_episodes': deepcopy(episodes), 'stop_owner_seconds': durations,
                'simultaneous_positions': self.simultaneous_at_entry,
                'context_timestamp_ms': ((context or {}).get('tf_5m') or {}).get('latest_closed_at_ms')}


class TrailActivationGapShadow(CircuitBreakerShadow):
    def __init__(self, project_root, config, logger, telemetry, *, settings_key, strategy,
                 pair_prefix, cohort_started_at):
        # Inherit the control's admission/capital settings; paths/ATR pair are own.
        cfg = deepcopy(config)
        instrumentation = cfg.setdefault('instrumentation', {})
        settings = {**instrumentation.get('be_off_cb_shadow', {}),
                    'enabled': False,
                    'state_file': f'data/state/{settings_key}.json',
                    'ledger_file': f'data/trades/trades_{settings_key}.jsonl',
                    'events_file': f'data/telemetry/{settings_key}_events.jsonl',
                    **instrumentation.get(settings_key, {})}
        cfg['instrumentation'][settings_key] = settings
        super().__init__(project_root, cfg, logger, telemetry, settings_key=settings_key,
                         strategy=strategy, shadow_kind=strategy, pair_prefix=pair_prefix,
                         be_off=True, cohort_started_at=cohort_started_at)

    def _exit_config(self):
        config = super()._exit_config()
        config['trailing'].update(activation_atr=self.settings['trail_activation_atr'],
                                  gap_atr=self.settings['trail_gap_atr'])
        return config

    def _position_type(self):
        return TrailAuditPosition

    def _load_state(self):
        super()._load_state()
        if self.state_path.exists():
            raw = {p['pair_id']: p for p in json.loads(self.state_path.read_text(encoding='utf-8')).get('positions', [])}
            for position in self.positions:
                saved = raw[position.pair_id]
                if 'trail_audit' not in saved:
                    raise ValueError('New TRAIL arm cannot inherit an unaudited legacy position')
                position.trail_audit = deepcopy(saved['trail_audit'])
                position.simultaneous_at_entry = saved.get('simultaneous_at_entry')

    def _open(self, signal, bucket):
        super()._open(signal, bucket)
        position = self.open_positions[-1]
        position.simultaneous_at_entry = len(self.open_positions)
        self._event('TRAIL_POSITION_OPENED', source_candle_open_time=signal.source_candle_open_time,
                    **position.trail_telemetry())

    def _process_tick(self, price, observed_at):
        positions = list(self.open_positions)
        previous = {p.pair_id: deepcopy(p.trail_audit) for p in positions}
        super()._process_tick(price, observed_at)
        for position in positions:
            before = previous[position.pair_id];after = position.trail_audit
            for event, key in (('TRAIL_ACTIVATED', 'activation_time'),
                               ('TRAIL_FIRST_DOMINANCE', 'first_dominance_time')):
                if before[key] is None and after[key] is not None:
                    self._event(event, source_candle_open_time=position.source_candle_open_time,
                                **position.trail_telemetry())
            if before['owner_episodes'] != after['owner_episodes']:
                self._event('TRAIL_OWNER_CHANGED', source_candle_open_time=position.source_candle_open_time,
                            **position.trail_telemetry())
