import unittest
from tools.protection_curve_prerequisite import protection,maximum_giveback

class CurveTests(unittest.TestCase):
    def test_nominal_boundaries(self):
        self.assertEqual(protection(4,20,0),-20)
        self.assertEqual(protection(5,20,0),1.5)
        self.assertEqual(protection(8,20,0),3)
        self.assertEqual(protection(10,20,0),5)
        self.assertEqual(protection(12,20,0),7)
    def test_shifted_trigger(self):
        self.assertEqual(protection(5,24,4),-24)
        self.assertEqual(protection(7.5,24,4),4)
        self.assertEqual(protection(9,24,4),4)
        self.assertEqual(protection(10,24,4),5)
    def test_global_and_post_first_are_distinct(self):
        self.assertEqual(maximum_giveback(20,0)['giveback_atr'],25)
        self.assertEqual(maximum_giveback(20,0,True)['giveback_atr'],7)
        self.assertEqual(maximum_giveback(24,4,True)['giveback_atr'],6)
    def test_trail_can_protect_before_pl(self):
        self.assertEqual(protection(10,60,10),5)
        self.assertEqual(protection(13.5,60,10),10)
        self.assertEqual(maximum_giveback(60,10)['giveback_atr'],70)
