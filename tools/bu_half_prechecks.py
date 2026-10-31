"""Public Testnet size feasibility and exact frozen BU predicate checks."""
from pathlib import Path
from decimal import Decimal,ROUND_DOWN
from types import SimpleNamespace
from datetime import datetime,timezone
import sys,json,urllib.request,hashlib,bisect,inspect,argparse
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'));import replay12_engine as e
S=ROOT/'data/analysis/replay_bu_half_20261027';D=Decimal
def dump(n,x):e.dump(S/n,x)
def get(path):
 with urllib.request.urlopen('https://testnet.binance.vision'+path,timeout=30) as r:return json.load(r)
def capture():
 if (S/'TESTNET_EXCHANGE_INFO.json').exists():return
 for name,path in [('TESTNET_EXCHANGE_INFO.json','/api/v3/exchangeInfo?symbol=SOLUSDT'),('TESTNET_AVG_PRICE.json','/api/v3/avgPrice?symbol=SOLUSDT'),('TESTNET_TICKER.json','/api/v3/ticker/price?symbol=SOLUSDT')]:dump(name,{'captured_utc':datetime.now(timezone.utc).isoformat(),'endpoint':'https://testnet.binance.vision'+path,'data':get(path)})
def floor(q,step):return (q/step).to_integral_value(rounding=ROUND_DOWN)*step if step else q
def viability(notional,tag):
 info=json.loads((S/'TESTNET_EXCHANGE_INFO.json').read_bytes())['data'];sym=info['symbols'][0];fs={f['filterType']:f for f in sym['filters']};lot=fs['LOT_SIZE'];ml=fs['MARKET_LOT_SIZE'];nf=fs.get('NOTIONAL',fs.get('MIN_NOTIONAL'))
 assert sym['status']=='TRADING' and sym['isSpotTradingAllowed'] and 'MARKET' in sym['orderTypes'] and sym['quoteOrderQtyMarketAllowed']
 n=D(str(notional));quote=floor(n,D(1).scaleb(-sym['quoteAssetPrecision']));step=D(lot['stepSize']);minq=D(lot['minQty']);maxq=D(lot['maxQty']);mstep=D(ml['stepSize']);mmin=D(ml['minQty']);mmax=D(ml['maxQty']);minimum=D(nf['minNotional']);maximum=D(nf.get('maxNotional','0'));applymin=nf.get('applyMinToMarket',nf.get('applyToMarket',False));applymax=nf.get('applyMaxToMarket',False)
 raw=[json.loads(l) for l in (S/'market/1m.jsonl').open()];signals=json.loads((S/'signals.json').read_bytes())['REAL_A'];index={x['open_time_ms']:i for i,x in enumerate(raw)};cases=[]
 for sig in signals:
  i=index[sig['at']];bar=raw[i];price=D(str(bar['open']))*(D(1)+D('0.00035'));prior=raw[max(0,i-5):i]
  low=min([D(str(x['low'])) for x in prior]+[D(str(bar['open']))]);high=max([D(str(x['high'])) for x in prior]+[D(str(bar['open']))])
  cases.append((sig['at'],price,low,high))
 avg=json.loads((S/'TESTNET_AVG_PRICE.json').read_bytes())['data'];ticker=json.loads((S/'TESTNET_TICKER.json').read_bytes())['data'];price=D(ticker['price']);ap=D(avg['price']);cases.append(('CURRENT_TESTNET',price,min(price,ap),max(price,ap)))
 failures=[];minbuy=None;minsell=None;minqty=None;examples=[]
 for at,price,lo,hi in cases:
  q=floor(floor(quote/price,step),mstep);sell=floor(floor(q*(D(1)-D('0.001')),step),mstep)
  # Conservative size check: five-minute average within prior low/high bounds;
  # also allow a normal HS exit and sell-side friction, without changing replay.
  buyvalue=q*lo;sellvalue=sell*min(lo,price*D('.985')*D('.99965'))
  reasons=[]
  for label,qty in [('BUY',q),('SELL_AFTER_BASE_FEE',sell)]:
   if qty<minq or qty>maxq or qty%step:reasons.append(label+'_LOT_SIZE')
   if (mmin and qty<mmin) or (mmax and qty>mmax) or (mstep and qty%mstep):reasons.append(label+'_MARKET_LOT_SIZE')
  if applymin and (buyvalue<minimum or sellvalue<minimum):reasons.append('MIN_NOTIONAL')
  if applymax and (q*hi>maximum or sell*hi>maximum):reasons.append('MAX_NOTIONAL')
  if reasons:failures.append({'at':at,'reasons':reasons,'quantity':str(q),'sell_qty_after_fee':str(sell),'buy_notional_bound':str(buyvalue),'sell_notional_bound':str(sellvalue)})
  minbuy=buyvalue if minbuy is None else min(minbuy,buyvalue);minsell=sellvalue if minsell is None else min(minsell,sellvalue);minqty=q if minqty is None else min(minqty,q)
  if at=='CURRENT_TESTNET':examples.append({'at':at,'ticker':str(price),'avg_filter':str(ap),'rounded_buy_qty':str(q),'rounded_sell_qty_after_fee':str(sell),'rounded_buy_value_at_avg':str(q*ap),'rounded_sell_value_at_avg':str(sell*ap)})
 result={'passed':not failures,'tag':tag,'requested_notional':notional,'quote_budget_rounded_down_8dp':str(quote),'type':'MARKET_BUY_QUOTE_ORDER_QTY_AND_MARKET_SELL_QUANTITY','cases':len(cases),'LOT_SIZE':lot,'MARKET_LOT_SIZE':ml,'NOTIONAL':nf,'min_rounded_buy_notional_bound':str(minbuy),'min_rounded_sell_notional_bound':str(minsell),'min_qty':str(minqty),'current_examples':examples,'failures':failures,'interpretation':'arithmetic feasibility under captured filters and causal historical/current price cases; no live orders or historical Testnet execution certification','economics_quantization':'unchanged continuous quantities of frozen replay; size rounding checked separately'}
 dump('VIABILIDADE_'+tag+'.json',result)
 if failures:raise RuntimeError('SIZE_INFEASIBLE_STOP '+tag)
 return result
PROBE=SimpleNamespace(policy='MACD_BU_MINUS')
def bu_predicate(context):return e.PolicyShadow._entry_policy(PROBE,context)[1]=='ENTRY_BLOCKED_MACD_BU_MINUS'
def predicates():
 man=json.loads((S/'market_manifest.json').read_bytes());cfg=json.loads((S/'config.json').read_bytes());obj=e.make('BE_OFF_CB_MACD_BU_MINUS_SHADOW',cfg,3.5,man['start_ms']);contexts=json.loads((S/'contexts_runtime_projection.json').read_bytes());ct=sorted(map(int,contexts));rows=[]
 for s in json.loads((S/'signals.json').read_bytes())['REAL_A']:
  at=s['at'];i=bisect.bisect_right(ct,at)-1;co=contexts[str(ct[i])] if i>=0 else {};obj.latest_market_context={'tf_5m':co};fields=obj._context_fields();allowed,reason=obj._entry_policy(fields);bu=bu_predicate(fields)
  assert bu==(reason=='ENTRY_BLOCKED_MACD_BU_MINUS')
  assert co.get('latest_closed_at_ms') is None or co['latest_closed_at_ms']<at
  rows.append({'at':at,'source':s['signal']['source_candle_open_time'],'macd_context':fields.get('macd_context'),'is_bu_minus':bu,'existing_filter_allowed':allowed,'existing_filter_reason':reason,'context_closed_at_ms':co.get('latest_closed_at_ms')})
 synthetic=[]
 for state in ['BU-','BU+','BE-','BE+','UNAVAILABLE',None]:
  ctx={'macd_context':state};a=obj._entry_policy(ctx);b=bu_predicate(ctx);assert b==(a[1]=='ENTRY_BLOCKED_MACD_BU_MINUS');synthetic.append({'state':state,'existing_return':a,'is_bu_minus':b,'half_notional':10 if b else 20})
 dump('PREDICADO_BU_MINUS.json',{'passed':True,'opportunities':len(rows),'bu_minus_N':sum(x['is_bu_minus'] for x in rows),'predicate_source':inspect.getsource(e.PolicyShadow._entry_policy),'predicate_file':inspect.getfile(e.PolicyShadow),'predicate_file_sha256':hashlib.sha256(Path(inspect.getfile(e.PolicyShadow)).read_bytes()).hexdigest(),'reuse_function':inspect.getsource(bu_predicate),'t_minus_1_vs_3':'NOT_REINTERPRETED; exact frozen context and predicate','rows':rows,'synthetic':synthetic})
 print('PREDICATE_EXACT_PASS',len(rows),flush=True)
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('stage',choices=['capture','initial','uniform']);a=p.parse_args()
 if a.stage=='capture':capture()
 elif a.stage=='initial':predicates();viability(10.,'10_USDT');print('PRECHECKS_10_AND_PREDICATE_PASS',flush=True)
 else:
  n=json.loads((S/'UNIFORME_CALIBRACAO.json').read_bytes())['notional_uniforme'];viability(n,'UNIFORME');print('UNIFORM_SIZE_PASS',n,flush=True)
