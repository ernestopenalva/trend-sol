from datetime import datetime, timezone
import unittest
from tools.real_a_rsi_exposure_study import Trade, _attach_exposure, _quantile_groups

class RsiExposureStudyTests(unittest.TestCase):
    def test_exposure_counts_only_positions_open_at_new_entry(self):
        a=Trade({'exit_reason':'HARD_STOP'},datetime(2026,1,1,tzinfo=timezone.utc),datetime(2026,1,1,2,tzinfo=timezone.utc),1,1,0,0)
        b=Trade({'exit_reason':'BREAKEVEN','be_armed_at':'2026-01-01T00:30:00+00:00'},datetime(2026,1,1,1,tzinfo=timezone.utc),datetime(2026,1,1,3,tzinfo=timezone.utc),1,1,0,0)
        _attach_exposure([a,b]); self.assertEqual((b.open_count,b.unprotected_count),(1,1))
    def test_quantiles_do_not_drop_records(self):
        rows=[Trade({},datetime.now(timezone.utc),datetime.now(timezone.utc),1,1,0,float(i),rsi=float(i)) for i in range(5)]
        self.assertEqual(sum(len(x) for _,x in _quantile_groups(rows,'rsi')),5)
