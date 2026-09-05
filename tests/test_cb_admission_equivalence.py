import unittest
from tools.cb_admission_equivalence_audit import AdmissionCounter, isolated_runner


class AdmissionTests(unittest.TestCase):
    def test_runtime_source_buckets_and_persistent_count(self):
        counter = AdmissionCounter({'entry':{'admission_candle_interval':'5m'}})
        counter.record(4*60_000)
        self.assertEqual(counter.count(0), 1)
        self.assertEqual(counter.count(4*60_000), 1)
        self.assertEqual(counter.count(5*60_000), 0)
        # Closing a position does not free its admission bucket.
        self.assertEqual(counter.count(3*60_000), 1)

    def test_one_minute_buckets(self):
        counter = AdmissionCounter({'entry':{'admission_candle_interval':'1m'}})
        counter.record(60_000)
        self.assertEqual(counter.count(60_000), 1)
        self.assertEqual(counter.count(120_000), 0)

    def test_runner_is_private(self):
        from tools import ge_replay_study
        original = ge_replay_study.run_universe
        runner = isolated_runner()
        self.assertIsNot(original, runner)
        self.assertIs(original, ge_replay_study.run_universe)


if __name__ == '__main__':
    unittest.main()
