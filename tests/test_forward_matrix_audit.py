import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tools import forward_experiment_report as report
from tools import forward_matrix_audit as audit

START=datetime(2026,10,3,12,tzinfo=timezone.utc)


def opportunity(source, pair=('SHO','BE+'), event='SIGNAL_OPPORTUNITY', at=START):
    return {'event':event,'ts':at.isoformat(),'source_candle_open_time':source,
            'ema_context':pair[0],'macd_context':pair[1],'price':100.}


def trade(source, net=1., reason='TRAILING', pair=('SHO','BE+'), at=START, closed=True):
    row={'pair_id':str(source),'source_candle_open_time':source,'opened_at':at.isoformat(),
         'entry_price':100.,'status':'CLOSED' if closed else 'OPEN',
         'position_notional_usdt':100.,'net_pnl_pct':net,
         'market_context_entry':{'tf_5m':{'ema_context':pair[0],'macd_context':pair[1]}}}
    if closed:
        row.update(closed_at=(at+timedelta(minutes=11)).isoformat(),exit_reason=reason,exit_price=100.+net)
        row['market_context_exit']={'tf_5m':{'ema_context':'BEA','macd_context':'BE-',
            'latest_closed_at_ms':int((at+timedelta(minutes=10)).timestamp()*1000)-1}}
    return row


class MatrixAuditTests(unittest.TestCase):
    def fixture(self):
        pair=('BUL','BU+')
        erows=[trade(1,-2.,'HARD_STOP',pair),trade(2,1.,'PROFIT_LOCK',pair)]
        crows=[trade(1,-3.,'HARD_STOP',pair),trade(3,4.),trade(4,-1.,'HARD_STOP')]
        eopen=[trade(5,pair=pair,closed=False)]
        copen=[trade(6,closed=False)]
        ee=[opportunity(s,p) for s,p in ((1,pair),(2,pair),(3,('SHO','BE+')),(4,('SHO','BE+')),(5,pair),(6,('SHO','BE+')),(7,('SHO','BE+')))]
        ee += [opportunity(s,pair,'OPEN') for s in (1,2,5)]
        ee += [opportunity(s,event='ENTRY_BLOCKED_EMA_MACD_SHO_BE+') for s in (3,4,6,7)]
        ce=[opportunity(s,event='OPEN') for s in (1,3,4,6)]
        ce += [opportunity(7,event='ENTRY_BLOCKED_SPACING')]
        return erows,crows,eopen,copen,ee,ce

    def build(self, data=None):
        erows,crows,eopen,copen,ee,ce=data or self.fixture()
        exp=report.EXPERIMENTS['ema_macd']
        with patch.object(report,'_records',side_effect=lambda arm,since:erows if arm==exp else crows), \
             patch.object(report,'_state',side_effect=lambda arm:{'positions':eopen if arm==exp else copen}), \
             patch.object(report,'_events',side_effect=lambda arm,since:ee if arm==exp else ce):
            return audit.build_audit(report,START)

    def test_pair_decomposition_and_source_pairing_no_time_price_inference(self):
        result=self.build()
        accepted=result['pairs'][('BUL','BU+')]; blocked=result['pairs'][('SHO','BE+')]
        self.assertEqual(accepted['status'],'ACCEPTED')
        self.assertEqual((accepted['opened_experiment'],accepted['common'],accepted['experiment_only']),(3,1,2))
        self.assertEqual((blocked['opportunities'],blocked['blocked_by_matrix'],blocked['opened_control']),(4,4,3))
        self.assertEqual(blocked['not_opened_control'],{'ENTRY_BLOCKED_SPACING':1})
        self.assertEqual((blocked['metrics']['resolved'],blocked['metrics']['open']),(2,1))
        self.assertEqual(result['totals']['matrix_blocked_opened_control'],3)
        self.assertEqual(result['totals']['matrix_blocked_net_raw'],3.)
        self.assertEqual(sum(v['opportunities'] for v in result['pairs'].values()), result['totals']['opportunities'])
        self.assertEqual(sum(v['opened_experiment'] for v in result['pairs'].values()), 3)
        self.assertEqual(sum(v['opened_control'] for v in result['pairs'].values()), 4)

    def test_net_exit_counts_winners_losers_concentration_and_dd(self):
        m=self.build()['pairs'][('SHO','BE+')]['metrics']
        self.assertEqual((m['net'],m['net_trade'],m['PF']),(3.,1.5,4.))
        self.assertEqual((m['HS'],m['PL'],m['TRAIL']),(1,0,1))
        self.assertEqual((m['best'],m['worst'],m['top3_winners'],m['top3_losers']),(4.,-1.,4.,-1.))
        self.assertEqual((m['net_without_top1'],m['net_without_top3'],m['DD']),(-1.,-1.,1.))
        self.assertEqual(self.build()['pairs'][('BUL','BU+')]['metrics']['PL'],1)

    def test_duplicate_rows_events_do_not_double_count(self):
        data=list(self.fixture())
        data[0]=data[0]+[deepcopy(data[0][0])]
        data[4]=data[4]+[deepcopy(data[4][0])]
        data[4]+= [deepcopy(r) for r in data[4] if r.get('event') in ('OPEN','ENTRY_BLOCKED_EMA_MACD_SHO_BE+')]
        result=self.build(data)
        self.assertEqual(result['totals']['experiment_closed'],2)
        self.assertEqual(result['totals']['opportunities'],7)
        self.assertEqual(result['totals']['accepted_opportunities'],3)
        self.assertEqual(result['totals']['blocked_opportunities'],4)

    def test_conflicting_source_contexts_rejected(self):
        with self.assertRaisesRegex(ValueError,'conflicting'):
            audit.unique_sources(report,[opportunity(1),opportunity(1,('BUL','BU+'))],opportunities=True)

    def test_source_both_open_and_closed_rejected(self):
        with self.assertRaisesRegex(ValueError,'both closed and open'):
            audit.trade_index(report,[trade(1)],[trade(1,closed=False)],[])

    def test_candidate_uses_joint_distribution_and_n_not_pf_alone(self):
        index=audit.trade_index(report,[trade(i,1.,at=START+timedelta(days=i%2)) for i in range(10)],[],[])
        m=audit.metrics(report,index.values())
        self.assertEqual(audit.classify('BLOCKED',m)[0],'CANDIDATE_UNBLOCK')
        index=audit.trade_index(report,[trade(i,-1.,'HARD_STOP',pair=('BUL','BU+'),at=START+timedelta(days=i%2)) for i in range(10)],[],[])
        m=audit.metrics(report,index.values())
        self.assertEqual(audit.classify('ACCEPTED',m)[0],'CANDIDATE_BLOCK')

    def test_exit_stale_does_not_remove_observed_economics(self):
        data=list(self.fixture()); data[0][0]['market_context_exit']['tf_5m']['latest_closed_at_ms']=1
        result=self.build(data)
        self.assertEqual(result['pairs'][('BUL','BU+')]['metrics']['net'],-1.)
        output=io.StringIO()
        with redirect_stdout(output):audit.print_trades(report,list(result['experiment'].values()),exit_context=True)
        self.assertIn('STALE',output.getvalue())
        self.assertIn('OPEN',output.getvalue())

    def test_low_n_is_not_approval_even_if_only_large_winners(self):
        item=self.build()['pairs'][('SHO','BE+')]
        self.assertEqual(item['classification'],'INSUFFICIENT_SAMPLE')
        self.assertEqual(item['confidence'],'LOW_N / EXPLORATORY')
        self.assertIn('não há suporte suficiente',item['reason'])

    def test_concentration_prevents_automatic_keep_accepted(self):
        rows=audit.trade_index(report,[trade(i,1. if i<8 else -3.,pair=('BUL','BU+'),at=START+timedelta(days=i%2)) for i in range(10)],[],[])
        m=audit.metrics(report,rows.values())
        self.assertGreater(m['net'],0)
        self.assertLess(m['net_without_top3'],0)
        self.assertEqual(audit.classify('ACCEPTED',m)[0],'REVIEW_ACCEPTED')

    def test_freeze_is_raw_reconciled_but_not_classification_evidence(self):
        row=trade(9,at=audit.FREEZE_START-timedelta(seconds=1))
        row['closed_at']=(audit.FREEZE_END+timedelta(hours=1)).isoformat()
        index=audit.trade_index(report,[row],[],[])
        m=audit.metrics(report,index.values())
        self.assertEqual((m['closed'],m['resolved'],m['invalid']),(1,0,1))

    def test_warmup_excluded_from_classification_support(self):
        data=list(self.fixture())
        data[0][0]['opened_at']='2026-09-28T01:00:00Z'
        data[0][0]['closed_at']='2026-09-28T02:00:00Z'
        item=self.build(data)['pairs'][('BUL','BU+')]
        self.assertEqual(item['metrics']['resolved'],2)
        self.assertEqual(item['classification_metrics']['resolved'],1)
        self.assertEqual(item['warmup_metrics']['resolved'],1)

    def test_cli_pair_since_filter_reconciliation_and_normal_output_unchanged(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);exp=report.EXPERIMENTS['ema_macd']
            erows,crows,eopen,copen,ee,ce=self.fixture()
            # One older admission must disappear from both pair accounting and net.
            crows.append(trade(99,99.,at=START-timedelta(days=1)))
            for arm,rows,opened,events in ((exp,erows,eopen,ee),(report.CONTROL,crows,copen,ce)):
                for relative,content in ((arm.ledger,''.join(json.dumps(r)+'\n' for r in rows)),
                    (arm.events,''.join(json.dumps(r)+'\n' for r in events)),
                    (arm.state,json.dumps({'positions':opened}))):
                    path=root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(content)
            argv=['report','--experiment','ema_macd','--matrix-audit','--pair','SHO+BE+', '--since','03/10/2026 09:00','--top-n','1']
            output=io.StringIO()
            with patch.object(report,'ROOT',root),patch.object(sys,'argv',argv),redirect_stdout(output):report.main()
            text=output.getvalue()
            self.assertIn('control_closed | 3',text)
            self.assertIn('SHO+BE+',text)
            self.assertNotIn('\nBUL+BU+',text)
            self.assertIn('net | $+3.0000',text)
            self.assertIn('ALL SHO+BE+ CONTROL TRADES',text)
            self.assertIn('LOW_N / EXPLORATORY',text)
            old_argv=[x for x in argv[:3]]+['--since','03/10/2026 09:00']
            output=io.StringIO()
            with patch.object(report,'ROOT',root),patch.object(sys,'argv',old_argv),redirect_stdout(output):report.main()
            self.assertIn('EMA + MACD hypothesis',output.getvalue())
            self.assertNotIn('MATRIX PAIR SUMMARY',output.getvalue())

    def test_cli_rejects_invalid_pair_or_wrong_experiment(self):
        for argv in (['report','--matrix-audit'], ['report','--experiment','ema_macd','--matrix-audit','--pair','SHO+BOGUS'],['report','--pair','SHO+BE+']):
            with self.subTest(argv=argv),patch.object(sys,'argv',argv),redirect_stdout(io.StringIO()),patch('sys.stderr',io.StringIO()):
                with self.assertRaises(SystemExit):report.main()


if __name__=='__main__':unittest.main()
