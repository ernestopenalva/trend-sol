"""Operator-run VPS evidence. Reads production; writes only a fresh evidence folder.

resources: Linux RSS/PSS + host memory/swap after 30-minute warmup.
observe: streaming LIVE wave coverage and gap-repair evidence since restart.
public-probe: public candles only, isolated buffer with a deliberately missing slot.
No Monitor, execution client, orders, service control, Git, or production writes.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
NEW = ('be_off_cb_ema_macd_cluster_act10_shadow', 'chaveado_vivo_shadow',
       'chaveado_vivo_verde_shadow')
K1_ARM = 'ema_macd_cluster_act10_k1_shadow'
ACTIVE = ('be_off_cb_shadow', 'be_off_cb_act20_gap5_shadow', 'be_off_cb_act10_gap13_shadow',
          'be_off_cb_fast_drop_ema_shadow', 'hs_bear_cluster_exit_shadow', 'cb_exit_all_shadow',
          'be_off_cb_macd_bu_minus_shadow', 'be_off_cb_ema_macd_shadow', 'ema_macd_hist_1m_shadow', *NEW,
          K1_ARM)


def stamp(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Timestamp must include timezone')
    return int(dt.timestamp()*1000)


def dump(path, value):
    with path.open('x', encoding='utf8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)


def numeric_proc(path):
    values = {}
    for line in path.read_text(encoding='ascii').splitlines():
        parts = line.replace(':', '').split()
        if len(parts) > 1:
            try:
                values[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return values


def process_sample(pid, root, proc=Path('/proc')):
    folder = proc/str(pid)
    if folder.joinpath('cwd').resolve() != root.resolve():
        raise ValueError(f'PID {pid} does not have the selected project cwd')
    command = folder.joinpath('cmdline').read_bytes().split(b'\0')
    if not any(Path(arg.decode(errors='replace')).name == 'main.py' for arg in command):
        raise ValueError(f'PID {pid} is not the selected main.py process')
    fields = folder.joinpath('stat').read_text().rsplit(')', 1)[1].split()
    start_ticks = int(fields[19])
    uptime = float((proc/'uptime').read_text().split()[0])
    age = uptime - start_ticks/os.sysconf('SC_CLK_TCK')
    status = numeric_proc(folder/'status')
    smaps = numeric_proc(folder/'smaps_rollup')
    return {'pid':pid, 'start_ticks':start_ticks, 'uptime_seconds':age,
            'rss_kib':status['VmRSS'], 'pss_kib':smaps['Pss'],
            'rss_peak_kib':status.get('VmHWM'), 'swap_kib':status.get('VmSwap'),
            'swap_pss_kib':smaps.get('SwapPss'), 'threads':status.get('Threads')}


def resource_sample(pids, root, proc=Path('/proc')):
    mem = numeric_proc(proc/'meminfo')
    vm = numeric_proc(proc/'vmstat')
    return {'at_ms':int(time.time()*1000), 'memory_available_kib':mem['MemAvailable'],
            'memory_total_kib':mem['MemTotal'],
            'swap_used_kib':mem['SwapTotal']-mem['SwapFree'],
            'swap_total_kib':mem['SwapTotal'], 'swap_cached_kib':mem.get('SwapCached'),
            'pswpin_pages':vm['pswpin'], 'pswpout_pages':vm['pswpout'],
            'oom_kill_count':vm.get('oom_kill'),
            'processes':[process_sample(pid, root, proc) for pid in pids]}


def resource_verdict(samples, settings, expected_samples=None):
    run_growth = run_io = longest_growth = longest_io = 0
    for left, right in zip(samples, samples[1:]):
        growth = right['swap_used_kib'] > left['swap_used_kib']
        io = (right['pswpin_pages'] > left['pswpin_pages'] or
              right['pswpout_pages'] > left['pswpout_pages'])
        run_growth = run_growth+1 if growth else 0
        run_io = run_io+1 if io else 0
        longest_growth, longest_io = max(longest_growth, run_growth), max(longest_io, run_io)
    minimum = min(s['memory_available_kib'] for s in samples)/1024
    warm = all(p['uptime_seconds'] >= settings['minimum_uptime_seconds']
               for s in samples for p in s['processes'])
    stable = len({tuple((p['pid'], p['start_ticks']) for p in s['processes']) for s in samples}) == 1
    enough = len(samples) >= (expected_samples or 2)
    consecutive = settings['continuous_swap_intervals']
    oom = any(s['oom_kill_count'] is not None and samples[0]['oom_kill_count'] is not None
              and s['oom_kill_count'] > samples[0]['oom_kill_count'] for s in samples)
    passed = (enough and stable and warm and minimum >= settings['minimum_available_mib']
              and longest_growth < consecutive and longest_io < consecutive and not oom)
    return {'passed':passed, 'minimum_memory_available_mib':minimum,
            'minimum_required_mib':settings['minimum_available_mib'], 'warmed_up':warm,
            'same_processes_throughout':stable, 'sufficient_samples':enough,
            'longest_swap_growth_intervals':longest_growth,
            'longest_paging_intervals':longest_io, 'new_oom_kills':oom,
            'swap_used_delta_kib':samples[-1]['swap_used_kib']-samples[0]['swap_used_kib'],
            'pswpin_delta_pages':samples[-1]['pswpin_pages']-samples[0]['pswpin_pages'],
            'pswpout_delta_pages':samples[-1]['pswpout_pages']-samples[0]['pswpout_pages'],
            'criterion':f'FAIL on {consecutive} consecutive intervals with growing swap or paging activity; host-wide counters'}


def resources(args, config):
    if sys.platform != 'linux':
        raise ValueError('resources must run on Linux VPS; local Windows is not VPS evidence')
    settings = config['instrumentation']['keyed_validation']
    duration = args.duration if args.duration is not None else settings['observation_seconds']
    interval = settings['sample_interval_seconds']
    if duration < interval*settings['continuous_swap_intervals'] or interval <= 0:
        raise ValueError('Observation window is too short for swap criterion')
    expected = int(duration//interval)+1
    samples = []
    with (args.output/'samples.jsonl').open('x', encoding='utf8') as stream:
        started = time.monotonic()
        for i in range(expected):
            remaining = started+i*interval-time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            sample = resource_sample(args.pid, args.root)
            samples.append(sample)
            stream.write(json.dumps(sample)+'\n');stream.flush()
    return {'mode':'resources', 'phase':args.phase, 'samples':len(samples),
            'settings':settings, **resource_verdict(samples, settings, expected),
            'per_process_first':samples[0]['processes'], 'per_process_last':samples[-1]['processes'],
            'operational_deploy_authorization':False}


def rows(path, errors):
    try:
        handle = path.open(encoding='utf8')
    except FileNotFoundError:
        errors['missing_files'] += 1
        return
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError('Non-object record')
                yield value
            except (ValueError, json.JSONDecodeError):
                errors['partial_tail' if not line.endswith('\n') else 'malformed_complete_lines'] += 1


def coverage(counter):
    total = sum(counter.values())
    labeled = total-counter['MISSING_FIELD']
    return {'total':total, 'labeled':labeled, 'counts':dict(counter),
            'unavailable_pct_of_labeled':100*counter['UNAVAILABLE']/labeled if labeled else None,
            'coverage_pct':100*labeled/total if total else None}


def observe(args, config):
    since = stamp(args.since);until = stamp(args.until) if args.until else int(time.time()*1000)
    if since > until:
        raise ValueError('since is after until')
    errors = Counter();health = Counter();reasons = Counter();events = Counter();repairs=[];boot=None;latest_health=None
    log = args.root/config.get('logging', {}).get('system_log', 'logs/system.log')
    for row in rows(log, errors):
        at = row.get('sampled_at_ms') or row.get('received_ms')
        if at is None:
            try:
                at = stamp(row.get('enabled_at') or row['ts'])
            except (ValueError, KeyError, TypeError):
                continue
        if not since <= at <= until:
            continue
        event = row.get('event', '')
        if event == 'structure_v2_enabled':
            boot = row
        if event == 'structure_health':
            latest_health = row
            wave = row.get('wave') or {}
            health[wave.get('label') or 'MISSING_FIELD'] += 1
            if wave.get('undefined_reason'):
                reasons[wave['undefined_reason']] += 1
        if event == 'structure_gap_repair':
            repairs.append({'interval':row.get('interval'),'received_ms':at,
                            'missing_before':row.get('missing_before'),
                            'missing_after':row.get('missing_after'), 'status':row.get('status')})
        if event.endswith(('_failed', '_error')):
            events[event] += 1
    arms={}
    for key in ACTIVE:
        settings=config['instrumentation'][key];counts={'open':Counter(),'close':Counter()}
        marola_counts={'open':Counter(),'close':Counter()}
        versions=Counter();seen=set();checkpoint_open=None
        def include(row, phases):
            for phase in phases:
                at=row.get(f'wave_{phase}_at') or row.get('opened_at' if phase=='open' else 'closed_at') or row.get('open_ts' if phase=='open' else 'close_ts')
                if not at:
                    continue
                try:
                    in_window=since <= stamp(at) <= until
                except ValueError:
                    errors['invalid_trade_timestamps'] += 1
                    continue
                identity=(row.get('pair_id'),phase)
                if in_window and identity not in seen:
                    seen.add(identity)
                    counts[phase][row.get(f'wave_{phase}') or 'MISSING_FIELD'] += 1
                    marola_counts[phase][row.get(f'marola_90_{phase}') or 'MISSING_FIELD'] += 1
                    details=row.get(f'wave_{phase}_details') or {}
                    versions[details.get('rule_version') or details.get('version') or 'MISSING_VERSION'] += 1
        for row in rows(args.root/settings['ledger_file'], errors):
            include(row, ('open','close'))
        try:
            with (args.root/settings['state_file']).open(encoding='utf8') as handle:
                state=json.load(handle)
            checkpoint_open=sum(p.get('status')=='OPEN' for p in state.get('positions',[]))
            for row in state.get('positions', []):
                include(row, ('open',))
        except FileNotFoundError:
            errors['missing_state_files'] += 1
        arms[key]={phase:coverage(counts[phase]) for phase in counts}
        arms[key]['marola_90']={phase:coverage(marola_counts[phase]) for phase in marola_counts}
        arms[key]['versions']=dict(versions)
        arms[key]['open_positions_in_checkpoint']=checkpoint_open
    configured = (boot or {}).get('new_shadows') or {}
    return {'mode':'observe','since_ms':since,'until_ms':until,
        'passed':None, 'status':'EVIDENCE_REQUIRES_REVIEW',
        'health_wave':coverage(health),'health_unavailable_reasons':dict(reasons),
        'repairs':repairs,'errors':dict(events),'read_quality':dict(errors),
        'new_shadows_confirmed_enabled':all(configured.get(k,{}).get('enabled') is True and
            configured.get(k,{}).get('accept_new_entries') is True for k in NEW),
        'k1_shadow_confirmed_enabled':configured.get(K1_ARM,{}).get('enabled') is True and
            configured.get(K1_ARM,{}).get('accept_new_entries') is True,
        'boot_marker':boot,'latest_health':latest_health,
        'latest_health_age_seconds':(until-latest_health['sampled_at_ms'])/1000
            if latest_health and latest_health.get('sampled_at_ms') else None,
        'arms':arms,'non_transactional_read':True,
        'notes':['No trades means unavailable percentage is null, not zero.',
                 'Time-sampled health and trade phases are separate denominators.',
                 'Only records since the supplied restart time; historical fields are not backfilled.']}


def public_probe(args, config):
    from src.exchange.binance_market_data import BinanceMarketDataClient
    from src.monitor.price_structure import LiveStructure
    from src.monitor.structure_repair import StructureRepair
    base=config['market_data']['rest_url']
    if base.rstrip('/') != 'https://api.binance.com':
        raise ValueError('Probe requires the configured real public Binance market endpoint')
    client=BinanceMarketDataClient(base, timeout_seconds=8)
    now=lambda:int(time.time()*1000)
    provider=LiveStructure(config['symbol'], 0)
    log=[]
    class Logger:
        def system(self,event,**fields):log.append({'event':event,**fields})
    try:
        for tf,buffer in [('15m',provider.wave_buffer),('5m',provider.marola_buffers['marola_90'])]:
            candles=client.klines(config['symbol'],tf,buffer.rule.count+2)
            received=now()
            for row in candles:
                if tf=='15m':buffer.add_kline(row,received)
                else:provider.add_marola_kline(row,received)
        interval = getattr(args, 'interval', '15m')
        target = provider.wave_buffer if interval == '15m' else provider.marola_buffers['marola_90']
        before=target.snapshot(now(),'PROBE')
        if before['label']=='UNAVAILABLE':
            raise ValueError(f'Public {interval} warmup incomplete')
        missing_slot=before['window_start_ms']+target.rule.interval_ms
        del target.hours[missing_slot];target._cache.clear()
        gap_at=now();gap=target.snapshot(gap_at,'PROBE')
        # Respect configured close grace if invocation happens on a boundary.
        wait=max(0,(gap['window_end_ms']+config['instrumentation']['structure_repair']['close_grace_ms']-now())/1000)
        if wait:time.sleep(wait+.01)
        repair=StructureRepair(provider,client,Logger(),now,config['instrumentation']['structure_repair'])
        repair.repair_once();recovered_at=now()
        after=target.snapshot(recovered_at,'PROBE')
        historical=target.snapshot(gap_at,'PROBE')
        passed=(gap['label']=='UNAVAILABLE' and after['label']!='UNAVAILABLE'
                and missing_slot in historical['missing_candles']
                and missing_slot not in after['missing_candles'] and repair.ready)
        return {'mode':'public-probe','passed':passed,'public_endpoint':base,
                'symbol':config['symbol'],'interval':interval,'missing_slot_ms':missing_slot,
                'before':before,'gap':gap,'after':after,
                'past_snapshot_after_repair':historical,'repair_events':log,
                'production_buffers_modified':False,'orders_sent':0}
    finally:
        client.session.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['resources','observe','public-probe'])
    p.add_argument('--root',type=Path,default=SOURCE)
    p.add_argument('--config',type=Path)
    p.add_argument('--output',type=Path,required=True,help='New evidence directory; refuses overwrite')
    p.add_argument('--pid',type=int,action='append')
    p.add_argument('--phase',choices=['before','after','fallback'],default='before')
    p.add_argument('--duration',type=float)
    p.add_argument('--since',help='Actual restart timestamp with timezone')
    p.add_argument('--until')
    p.add_argument('--interval', choices=['15m', '5m'], default='15m', help='Gap to simulate in public-probe')
    a=p.parse_args();a.root=a.root.resolve();a.output=a.output.resolve()
    if a.output==a.root or any(a.output.is_relative_to(a.root/name) for name in ('src','config','logs','data')):
        p.error('Evidence must be outside production sources, config, logs and data (use backups/)')
    if a.mode=='resources' and not a.pid:p.error('--pid is required')
    if a.mode=='observe' and not a.since:p.error('--since is required')
    import yaml
    config=yaml.safe_load((a.config or a.root/'config/config.yaml').read_text(encoding='utf8'))
    a.output.mkdir(parents=True,exist_ok=False)
    try:
        result={'resources':resources,'observe':observe,'public-probe':public_probe}[a.mode](a,config)
    except Exception as exc:
        result={'mode':a.mode,'passed':False,'status':'CHECK_FAILED',
                'error_type':type(exc).__name__,'error':str(exc)}
    dump(a.output/'RESULT.json',result)
    print(json.dumps({'output':str(a.output/'RESULT.json'),'passed':result.get('passed'),
                      'status':result.get('status')},ensure_ascii=False))
    return 2 if result.get('passed') is False else 0


if __name__=='__main__':raise SystemExit(main())
