"""Destructive migration rehearsal ONLY on a newly created laboratory copy."""
import argparse
import io
import json
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.monitor.price_structure import WAVE
from tools.trend_structure_migrate import migrate,verify,rollback,files,file_digest
from tools import forward_experiment_report as report


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True);p.add_argument('--candles',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.output.mkdir(parents=True,exist_ok=False)
    lab=a.output/'bot';backup=a.output/'backup'
    before={str(p):file_digest(p) for p in files(a.source)}
    shutil.copytree(a.source,lab)
    report.ROOT=lab.resolve();cut=report.parse_time('08/10/2026 23:52')
    def summary():
        out=io.StringIO()
        with redirect_stdout(out):report.print_summary(cut)
        return out.getvalue()
    old=summary()
    m=migrate(lab,a.candles,backup,rule=WAVE)
    result=verify(lab,backup)
    again=migrate(lab,a.candles,backup,rule=WAVE)
    assert again==m,'Not idempotent'
    new=summary()
    # Compare only the unchanged common economic table, not new coverage.
    part=lambda s:s.split('TREND COVERAGE')[0]
    assert part(old)==part(new),'Economic summary changed'
    (a.output/'report_before.txt').write_text(old,encoding='utf-8')
    (a.output/'report_after.txt').write_text(new,encoding='utf-8')
    result.update(summary_identical=True,sqlite_files=sum(x['path'].endswith('.sqlite') for x in m['files']),
                  idempotent=True,mare_unchanged=True)
    rollback(lab,backup)
    for x in m['files']:assert file_digest(lab/x['path'])==x['before'],'Rollback not exact'
    result['rollback_exact']=True
    assert before=={str(p):file_digest(p) for p in files(a.source)},'Original copy changed'
    result['source_untouched']=True
    (a.output/'validation.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
