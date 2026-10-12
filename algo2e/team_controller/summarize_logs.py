"""Summarize each run separately; never merge practice and final rounds implicitly."""
import argparse
from collections import Counter
import json
from pathlib import Path


def summarize(path):
    records=[]
    for number,line in enumerate(Path(path).read_text(encoding='utf-8').splitlines(),1):
        try: records.append(json.loads(line))
        except json.JSONDecodeError: raise ValueError('%s: invalid JSON at line %d'%(Path(path).name,number)) from None
    starts=[r for r in records if r['event']=='round_start']
    if len(starts)>1: raise ValueError('Multiple round starts: split the run before analysis')
    snapshots=[r for r in records if r['event']=='snapshot']
    end=[r for r in records if r['event']=='final_account']
    baseline=starts[0]['account_nlv'] if starts else None
    points=[r['account_nlv'] for r in snapshots]+([end[-1]['account_nlv']] if end else [])
    peak=baseline; drawdown=0
    for value in points:
        peak=max(peak,value) if peak is not None else value
        drawdown=max(drawdown,peak-value)
    return dict(file=Path(path).name,events=dict(Counter(r['event'] for r in records)),
        round_start=starts[0] if starts else None,has_final_account=bool(end),
        last_recorded_nlv=points[-1] if points else None,
        recorded_pnl_change=points[-1]-baseline if points and baseline is not None else None,
        max_recorded_drawdown=drawdown,
        max_reserved_gross=max((r['risk_gross'] for r in snapshots),default=None),
        tick_range=[snapshots[0]['tick'],snapshots[-1]['tick']] if snapshots else None,
        filled_shares=sum(r.get('delta',0) for r in records if r['event']=='fill'),
        max_cycle_ms=max((r['elapsed_ms'] for r in records if r['event']=='cycle'),default=None),
        final_positions=end[-1]['positions'] if end else None,
        halts=[r.get('reason') for r in records if r['event'] in ('halt','recovery_halt')],
        note='Per-process record, not necessarily the entire official round. Missing intervals/fines/manual trades are not reconstructed.')


def main():
    p=argparse.ArgumentParser(); p.add_argument('logs',nargs='+'); p.add_argument('--output')
    args=p.parse_args(); result=json.dumps([summarize(x) for x in args.logs],indent=2,allow_nan=False)
    if args.output: Path(args.output).write_text(result+'\n',encoding='utf-8')
    else: print(result)


if __name__=='__main__': main()
