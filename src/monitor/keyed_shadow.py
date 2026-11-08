"""MC-PROP-20261101: one shadow wallet, live reducers, entry-fixed ladder."""
from copy import deepcopy
import json

from src.monitor.circuit_breaker_shadow import CircuitBreakerShadow, _parse_ts
from src.monitor.forward_experiment_shadows import (
    EmaMacdHist1mShadow, FastDropEmaShadow, ExperimentalRiskShadow,
    PolicyShadow, FastDropPosition,
)
from src.monitor.fast_drop_semantics import loss_reached
from src.monitor.price_structure import capture_live, STATES


class KeyedPosition(FastDropPosition):
    def to_state(self):
        return {**super().to_state(), 'entry_house': deepcopy(getattr(self, 'entry_house', None))}

    @classmethod
    def from_state(cls, state, config, client, logger):
        config = deepcopy(config)
        house = state.get('entry_house')
        if house:
            config['trailing'].update(activation_atr=house['activation_atr'], gap_atr=house['gap_atr'])
        value = super().from_state(state, config, client, logger)
        value.entry_house = deepcopy(state.get('entry_house'))
        if house:
            value.price_structure['keyed_entry_house'] = deepcopy(house)
        return value


class KeyedShadow(EmaMacdHist1mShadow, FastDropEmaShadow, ExperimentalRiskShadow):
    # Cooperative economic methods match the independent switch replay; constructors
    # are deliberately replaced by one CB wallet, one journal and one checkpoint.
    def __init__(self, project_root, config, logger, telemetry, *, settings_key,
                 cohort_started_at):
        self.policy = 'EMA_MACD'
        self.experiment = None
        self._decision = {}
        self._entry_house = None
        self.structure_ready = lambda: False
        settings = config['instrumentation'][settings_key]
        path = project_root / settings['state_file']
        raw = json.loads(path.read_text(encoding='utf8')) if path.exists() else {}
        self.tactical_candles = raw.get('tactical_candles', [])
        self.admission_audit = {}
        self.minute_closes = {int(k): float(v) for k,v in raw.get('fast_drop_minute_closes', {}).items()}
        self.context_history = raw.get('fast_drop_context_history', [])
        CircuitBreakerShadow.__init__(self, project_root, config, logger, telemetry,
            settings_key=settings_key, strategy=settings_key.upper(),
            shadow_kind=settings_key.upper(), pair_prefix=settings_key, be_off=True,
            cohort_started_at=cohort_started_at)

    def _extra_state(self):
        return {**FastDropEmaShadow._extra_state(self),
                'tactical_candles': self.tactical_candles}

    def _position_type(self):
        return KeyedPosition

    def _load_state(self):
        CircuitBreakerShadow._load_state(self)
        if self.state_path.exists():
            raw = json.loads(self.state_path.read_text(encoding='utf8'))
            by_pair = {p.pair_id: p for p in self.positions}
            self.positions = [KeyedPosition.from_state(row, self._exit_config(),
                by_pair[row['pair_id']].client, self.logger) for row in raw.get('positions', [])]

    def _process_reference_1m(self, item):
        result = EmaMacdHist1mShadow._process_reference_1m(self, item)
        candle = item['candle']
        FastDropEmaShadow._process_reference_1m(self,
            {'boundary': candle['close_time'] + 1, 'close': candle['close']})
        return result

    def _run_input(self, item, *, recovering=False):
        if item['kind'] in ('signal', 'tick'):
            if not recovering:
                at = item['signal']['ts'] if item['kind'] == 'signal' else item['observed_at']
                item = {**item, 'structure': capture_live(self.config['symbol'], at, 'close'),
                        'structure_ready': bool(self.structure_ready())}
            if 'structure' not in item:
                raise ValueError('Keyed input lacks persisted causal structure')
            self._decision = item['structure']
            self._ready_for_input = item['structure_ready']
        return CircuitBreakerShadow._run_input(self, item, recovering=recovering)

    def _house(self):
        cell = [self._decision.get('wave_close'), self._decision.get('marola_90_close')]
        valid = all(x in STATES for x in cell)
        rules = self.config['instrumentation']['keyed_policy']
        label = rules['map'].get('/'.join(cell), 'BE_OFF_CB') if valid else 'BE_OFF_CB'
        return {'cell': cell, 'label': label, **deepcopy(rules['arms'][label]),
                'valid': valid, 'green': valid and '/'.join(cell) in rules['green_cells'],
                'reason': None if valid else 'SEM_ROTULO',
                'structure': deepcopy(self._decision)}

    def _process_signal(self, signal):
        self._entry_house = self._house()
        self._event_moment = _parse_ts(signal.ts)
        self._event('KEYED_ENTRY_DECISION', source_candle_open_time=signal.source_candle_open_time,
                    house=self._entry_house)
        if not getattr(self, '_ready_for_input', False):
            return self._block('ENTRY_BLOCKED_STRUCTURE_WARMUP', signal,
                               signal.source_candle_open_time // 300000 * 300000)
        if self._entry_house['filter'] == 'HIST':
            self.policy = 'EMA_MACD'
            return EmaMacdHist1mShadow._process_signal(self, signal)
        return CircuitBreakerShadow._process_signal(self, signal)

    def _entry_policy(self, context):
        house = self._entry_house
        if self.settings.get('green_only') and not house['green']:
            return False, ('ENTRY_BLOCKED_CASA_VERMELHA' if house['valid'] else
                           'ENTRY_BLOCKED_SEM_ROTULO')
        filt = house['filter']
        if filt is None:
            return True, 'ENTRY_ACCEPTED' if house['valid'] else 'ENTRY_ACCEPTED_SEM_ROTULO'
        if filt == 'HIST':
            return EmaMacdHist1mShadow._entry_policy(self, context)
        self.policy = 'MACD_BU_MINUS' if filt == 'BU' else 'EMA_MACD'
        return PolicyShadow._entry_policy(self, context)

    def _exit_config(self):
        cfg = CircuitBreakerShadow._exit_config(self)
        if self._entry_house:
            cfg['trailing'].update(activation_atr=self._entry_house['activation_atr'],
                                   gap_atr=self._entry_house['gap_atr'])
        return cfg

    def _open(self, signal, bucket):
        CircuitBreakerShadow._open(self, signal, bucket)
        position = self.positions[-1]
        position.entry_house = deepcopy(self._entry_house)
        # Entry selection must be in both the durable checkpoint and trade audit.
        position.price_structure['keyed_entry_house'] = deepcopy(self._entry_house)
        self._event('KEYED_OPEN', pair_id=position.pair_id, entry_house=self._entry_house)
        self._save_state()

    def _audit_reducer(self, kind, house, candidates, victims, at):
        self._event_moment = _parse_ts(at)
        self._event('KEYED_REDUCER_DECISION', reducer=kind, house=house,
            decision='CLOSED' if victims else 'NO_ACTION',
            positions=[{'pair_id':p.pair_id, 'entry_house':p.entry_house,
                        'affected':p in victims} for p in candidates],
            victim_pair_ids=[p.pair_id for p in victims],
            one_shot_consumed=(all(p.fast_drop_evaluated for p in candidates) if kind == 'FAST' else None),
            reason=house['reason'])

    def _process_tick(self, price, observed_at):
        prior = list(self.open_positions)
        house = self._house()
        crossing = [p for p in prior if not p.fast_drop_evaluated and loss_reached(p.entry_price, price)]
        if crossing and house['reducer'] != 'FAST':
            for p in crossing:
                p.fast_drop_evaluated = True
            self._audit_reducer('FAST', house, crossing, [], observed_at)
        FastDropEmaShadow._process_tick(self, price, observed_at)
        if crossing and house['reducer'] == 'FAST':
            self._audit_reducer('FAST', house, crossing,
                [p for p in crossing if p.status == 'CLOSED' and p.exit_reason == 'FAST_DROP'], observed_at)
        hard = [p for p in prior if p.status == 'CLOSED' and p.exit_reason == 'HARD_STOP']
        if hard and self._context_fields().get('ema_context') == 'SHO':
            negatives = [p for p in self.open_positions if p.pnl_pct(price) < 0]
            victims = negatives if house['reducer'] == 'CLUSTER' else []
            for p in victims:
                self._close_and_record(p, price, observed_at, 'HS_BEAR_CLUSTER_EXIT', price)
            self._audit_reducer('CLUSTER', house, negatives, victims, observed_at)
            self.positions = [p for p in self.positions if p.status == 'OPEN']
        self._save_state()

    def _advance_clock(self, moment, price, *, from_signal=False):
        prior = self.circuit_breaker_active
        CircuitBreakerShadow._advance_clock(self, moment, price, from_signal=from_signal)
        if not prior and self.circuit_breaker_active:
            house = self._house()
            candidates = list(self.open_positions)
            victims = candidates if house['reducer'] == 'CB' else []
            at = moment.isoformat()
            for p in victims:
                self._close_and_record(p, price, at, 'CIRCUIT_BREAKER_EXIT_ALL', price)
            self._audit_reducer('CB', house, candidates, victims, at)
            self.positions = [p for p in self.positions if p.status == 'OPEN']


class EmaMacdClusterAct10Shadow(PolicyShadow, ExperimentalRiskShadow):
    def __init__(self, project_root, config, logger, telemetry, *, cohort_started_at):
        self.experiment = 'HS_BEAR_CLUSTER_EXIT'
        self.policy = 'EMA_MACD'
        CircuitBreakerShadow.__init__(self, project_root, config, logger, telemetry,
            settings_key='be_off_cb_ema_macd_cluster_act10_shadow',
            strategy='BE_OFF_CB_EMA_MACD_CLUSTER_ACT10_SHADOW', pair_prefix='emamacdclusteract10',
            shadow_kind='BE_OFF_CB_EMA_MACD_CLUSTER_ACT10_SHADOW', be_off=True,
            cohort_started_at=cohort_started_at)

    def _exit_config(self):
        cfg = CircuitBreakerShadow._exit_config(self)
        cfg['trailing'].update(activation_atr=self.settings['trail_activation_atr'],
                               gap_atr=self.settings['trail_gap_atr'])
        return cfg


class EmaMacdClusterAct10K1Shadow(EmaMacdClusterAct10Shadow):
    """EMA_MACD+ACT10 cluster arm with the post-filter K1 spacing-in-decline gate."""

    def __init__(self, project_root, config, logger, telemetry, *, cohort_started_at):
        self.k1_block_count = 0
        self.policy = 'EMA_MACD'
        self.experiment = 'HS_BEAR_CLUSTER_EXIT'
        CircuitBreakerShadow.__init__(self, project_root, config, logger, telemetry,
            settings_key='ema_macd_cluster_act10_k1_shadow',
            strategy='EMA_MACD_CLUSTER_ACT10_K1_SHADOW',
            pair_prefix='emamacdclusteract10k1',
            shadow_kind='EMA_MACD_CLUSTER_ACT10_K1_SHADOW', be_off=True,
            cohort_started_at=cohort_started_at)

    def _exit_config(self):
        # Keep the base arm's independent CB, BE-off, ACT10/GAP13 and HS cluster.
        return EmaMacdClusterAct10Shadow._exit_config(self)

    def _load_cb_state(self):
        super()._load_cb_state()
        if self.state_path.exists():
            data = json.loads(self.state_path.read_text(encoding='utf-8'))
            self.k1_block_count = int(data.get('k1_block_count', 0))

    def _extra_state(self):
        return {'k1_block_count': self.k1_block_count}

    def _k1_block_details(self, signal):
        opened = list(self.open_positions)
        negative = [position for position in opened if float(signal.price) < float(position.entry_price)]
        if negative:
            lowest_entry = min(float(position.entry_price) for position in opened)
            required_price = lowest_entry - float(self.settings.get('spacing_queda_atr', 1.0)) * float(signal.entry_atr)
            if float(signal.price) > required_price:
                return {'signal_price': float(signal.price),
                    'open_positions_count': len(opened),
                    'negative_positions_count': len(negative),
                    'lowest_open_entry_price': lowest_entry,
                    'required_signal_price': required_price,
                    'entry_atr': float(signal.entry_atr)}
        return None

    def _open(self, signal, bucket):
        details = self._k1_block_details(signal)
        if details is not None:
            self.k1_block_count += 1
            self._block('ENTRY_BLOCKED_SPACING_QUEDA', signal, bucket, **details)
            return False
        EmaMacdClusterAct10Shadow._open(self, signal, bucket)
        return True
