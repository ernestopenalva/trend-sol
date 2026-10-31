"""Check human decision boundaries using synthetic aggregates only."""
from pathlib import Path
from copy import deepcopy
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'));import multi16_analyze as a
def main():
 def case(gain=.005):return [{'delta_mean':gain,'halves_primary':[{'delta_mean':.001},{'delta_mean':.002}],'excluding_top3':{'delta_mean':.001}} for _ in range(2)]
 act=[{'N':1,'delta_conditional':.01} for _ in range(2)];tests=[]
 def check(name,two,ac,expected):assert a.a.classify(two,ac)[0]==expected,name;tests.append(name)
 check('inclusive .005 both paths',case(),act,'CANDIDATA_A_FORWARD')
 check('below materiality but positive',case(.0049),act,'INCONCLUSIVA')
 x=case();x[0]['halves_primary'][0]['delta_mean']=-.01;check('one negative half is inconclusive not rejection',x,act,'INCONCLUSIVA')
 x=case();x[0]['delta_mean']=0;check('zero whole-path mean gives no evidence',x,act,'SEM_EVIDENCIA_DE_BENEFICIO')
 x=case();x[1]['excluding_top3']['delta_mean']=-.01;check('concentration fail gives inconclusive',x,act,'INCONCLUSIVA')
 ac=deepcopy(act);ac[0]['delta_conditional']=0;check('no favorable intervention effect',case(),ac,'INCONCLUSIVA')
 ac=deepcopy(act);ac[1]['N']=0;check('no actual intervention one path',case(),ac,'INCONCLUSIVA')
 a.dump('CLASSIFICATION_CHECKS.json',{'passed':True,'checks':tests,'new_economic_results_consulted':False});print('CLASSIFIER_GATES_PASS',len(tests))
if __name__=='__main__':main()
