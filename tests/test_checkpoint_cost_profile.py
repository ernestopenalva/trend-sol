import unittest
from tools.checkpoint_cost_profile import composition,measure,quantile


class CheckpointProfileTests(unittest.TestCase):
    def test_composition_counts_history_separately(self):
        r=composition({'cb_schema':2,'positions':[{'pair_id':'p'}],
                       'closed_records':[{'pair_id':'c'}],'audit_events':[{'event':'OPEN'}]})
        self.assertEqual(r['positions'],1)
        self.assertEqual(r['closed_records'],1)
        self.assertEqual(r['audit_events'],1)
        self.assertGreater(r['historical_fraction'],0)
        self.assertLess(r['historical_fraction'],1)

    def test_serializer_measurement_does_not_mutate_state(self):
        state={'positions':[{'pnl':-.5}],'audit_events':[{'event':'OPEN'}]}
        before=repr(state);r=measure(state,3)
        self.assertEqual(repr(state),before)
        self.assertEqual(r['serialization_cpu_ms']['N'],3)
        self.assertGreaterEqual(r['serialization_wall_ms']['p50'],0)

    def test_quantiles(self):
        self.assertEqual(quantile([0,100],.5),50)
        self.assertIsNone(quantile([],.5))


if __name__=='__main__':unittest.main()
