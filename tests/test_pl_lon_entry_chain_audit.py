import unittest
from tools.pl_lon_entry_chain_audit import select_focus, classify, independent_gates, GATES

class EntryChainAuditTests(unittest.TestCase):
    def row(self,path):return {'horizon':'60','ambiguous_anchor':False,
        'missing_reason':'AVAILABLE_NO_APPROVED_SIGNAL_FILTER_REASON_UNAUDITABLE',
        'recovery':{'recovered':True},'path':path}

    def test_universe_must_match_exactly_before_reconstruction(self):
        rows=[self.row('HIGH_FIRST') for _ in range(26)]+[self.row('LOW_FIRST') for _ in range(18)]
        self.assertEqual(len(select_focus(rows)),44)
        with self.assertRaises(ValueError):select_focus(rows[:-1])

    def test_categories_do_not_conflate_no_candidate_and_filtered_candidate(self):
        gates={k:{'passed':False} for k in GATES}
        row={'evaluated':True,'all_gates':gates,'all_pass':False}
        self.assertEqual(classify([row]),'A')
        gates['recovery']['passed']=True
        self.assertEqual(classify([row]),'B')
        self.assertEqual(classify([]),'E')
        row['all_pass']=True
        self.assertEqual(classify([row]),'E')

    def test_independent_evaluation_restores_logger_and_diagnostic(self):
        class Engine:
            logger=object()
            last_diagnostic={'original':True}
            def _empty_diagnostic(self):return {'gates':{}}
            def gate(self,name):self.last_diagnostic['gates'][name]={'passed':True}
            def _gate_trend(self):self.gate('trend')
            def _gate_pullback(self):self.gate('pullback')
            def _gate_exhaustion(self):self.gate('exhaustion')
            def _gate_reversal(self):self.gate('recovery')
        e=Engine();logger=e.logger
        result=independent_gates(e)
        self.assertEqual(set(result),set(GATES));self.assertIs(e.logger,logger)
        self.assertEqual(e.last_diagnostic,{'original':True})

    def test_actual_entry_engine_gate_audit_does_not_mutate_candles_or_admission_state(self):
        from copy import deepcopy
        from src.monitor.entry_engine import EntryEngine
        from tools.market_bot_replay import NullLogger
        from tests.test_ge_replay_study import _config
        e=EntryEngine('SOLUSDT',_config(),NullLogger())
        fields=('entry_candles','trend_candles','last_evaluated_entry_open_time','pending_ge_evaluation','last_diagnostic')
        before={k:deepcopy(getattr(e,k)) for k in fields}
        logger=e.logger
        independent_gates(e)
        self.assertEqual(before,{k:getattr(e,k) for k in fields})
        self.assertIs(e.logger,logger)

if __name__=='__main__':unittest.main()
