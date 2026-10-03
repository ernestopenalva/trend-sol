import unittest
from tools.be_off_cb_defensive_review_report import coverage_for_cluster, generate


class DefensiveReportTests(unittest.TestCase):
    def test_cb_trigger_alone_does_not_mean_existing_hs_intercepted(self):
        c={'trades':[1,2,3],'end_ms':100,'cb_triggers_during':[100]}
        result=coverage_for_cluster({'counterfactual_exits':[]},c)
        self.assertTrue(result['no_eligible_exit_predicate'])
        self.assertEqual(result['hs_without_eligible_exit_predicate'],3)

    def test_union_deduplicates_same_target_and_marks_partial_coverage(self):
        c={'trades':[1,2,3],'end_ms':100}
        rows=[{'mechanism':m,'opened_ms':1,'at_ms':80} for m in ('FAST_DROP_EMA','CB_EXIT_ALL')]
        r=coverage_for_cluster({'counterfactual_exits':rows},c)
        self.assertEqual(r['covered_union'],1)
        self.assertTrue(r['multiple_hs_without_eligible_exit_predicate'])

    def test_report_keeps_all_months_and_explicit_limits_even_empty(self):
        d={'trades':[],'crises':[],'blocked_signals':[],'clusters':[],'risk_episodes':[],'hs_events':[]}
        report,_=generate({'HIGH_FIRST':d,'LOW_FIRST':d},{'hs':[],'crises':[]})
        for token in ('2026-06','2026-07','2026-08','2026-09','2026-10','ALL','N/A',
                      'Não há replay sistêmico','BUL + BU−','somente diagnóstico futuro'):
            self.assertIn(token,report)


if __name__=='__main__':unittest.main()
