import unittest
from tools.price_structure_study import label,aggregate
from tools.market_selection_study import MarketCandle

class StructureTests(unittest.TestCase):
    def window(self,highs=(12,14),lows=(0,2)):
        cs=[MarketCandle(i*3600000,(i+1)*3600000-1,5,10,3,5,1,1) for i in range(72)]
        for i,v in zip([10,30],highs):cs[i]=MarketCandle(i*3600000,(i+1)*3600000-1,5,v,3,5,1,1)
        for i,v in zip([20,40],lows):cs[i]=MarketCandle(i*3600000,(i+1)*3600000-1,5,10,v,5,1,1)
        return cs
    def test_taxonomy_and_strict_ties(self):
        self.assertEqual(label(self.window())[0],'BULL')
        self.assertEqual(label(self.window((14,12),(2,0)))[0],'BEAR')
        self.assertEqual(label(self.window((12,12),(0,2)))[0],'MIXED')
        self.assertEqual(label(self.window((12,14),(2,0)))[0],'MIXED')
    def test_unconfirmed_right_edge_ignored(self):
        cs=self.window();cs[70]=MarketCandle(70*3600000,71*3600000-1,5,100,-100,5,1,1)
        self.assertEqual(label(cs),label(self.window()))
    def test_insufficient_structure_separate(self):
        self.assertEqual(label(self.window()[:71])[0],'UNDEFINED')
        self.assertEqual(label([self.window()[0]]*72)[0],'UNDEFINED')
    def test_complete_hour_only_no_gap_fill(self):
        cs=[MarketCandle(i*60000,(i+1)*60000-1,5,10+i,3,6,1,1) for i in range(61)]
        out=aggregate(cs,60);self.assertEqual(len(out),1);self.assertEqual(out[0].high,69)
        self.assertEqual(out[0].quote_volume,60);self.assertEqual(out[0].close_time_ms,3599999)
        self.assertEqual(aggregate(cs[:30]+cs[31:],60),[])

if __name__=='__main__':unittest.main()
