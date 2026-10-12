"""Chronological signal/risk checks. No invented historical passive fills or P&L."""
import argparse
from collections import Counter
import csv
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import statistics
from core import Book,BRIEF,Config,Economics,Strategy,Signal,TICKERS,allowed


def load_books(files):
    grouped={}; sources=[]
    for filename in files:
        data=Path(filename).read_bytes()
        sources.append({'file':str(filename),'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)})
        if not data or not any(data): raise ValueError('Empty/zero-filled CSV: '+str(filename))
        for row in csv.DictReader(data.decode('utf-8-sig').splitlines()):
            key=(row['timestamp'],int(row['period']),int(row['tick']))
            if row['case_status']!='ACTIVE': continue
            if row['ticker'] in grouped.setdefault(key,{}): raise ValueError('Duplicate timestamp/ticker')
            sides=[]
            for side in ('bid','ask'):
                levels=[]
                for i in range(1,4):
                    try: price=float(row[side+'_px_'+str(i)]); qty=float(row[side+'_depth_'+str(i)])
                    except ValueError: continue
                    if math.isfinite(price) and math.isfinite(qty) and price>0 and qty>0: levels.append((price,int(qty)))
                sides.append(tuple(levels))
            if all(sides) and sides[0][0][0]<sides[1][0][0]: grouped[key][row['ticker']]=Book(*sides)
    # Take the FIRST valid complete observation per tick to match the live update rule.
    ticks={}; previous=-1
    for key,books in sorted(grouped.items()):
        if len(books)!=3: continue
        if key[2]<previous: raise ValueError('Case reset: split runs before replay')
        previous=key[2]; ticks.setdefault(key[2],books)
    return ticks,sources


def evaluate(ticks,c):
    strategy=Strategy(c); es={t:Economics(BRIEF[t][0],BRIEF[t][1],BRIEF[t][1],'net') for t in TICKERS}
    counts={'development':Counter(),'holdout':Counter()}; forecasts=[]
    for tick,books in ticks.items():
        strategy.update(tick,books)
        part='development' if tick<250 else 'holdout'
        counts[part]['ticks']+=1
        candidates=strategy.pairs.candidates(books,es,c)
        counts[part]['pair_candidates']+=len(candidates)
        for t,s in strategy.signals.items():
            counts[part][t+'_trend_'+str(s.direction)]+=1
            if s.direction and tick+5 in ticks:
                future=ticks[tick+5][t].mid-books[t].mid
                forecasts.append({'tick':tick,'ticker':t,'direction':s.direction,'signed_5_tick_change':s.direction*future,'partition':part})
        q={t:0 for t in TICKERS}
        qs=strategy.quotes(tick,300,q,books,es)
        reserved=[]
        for quote in qs:
            if allowed(c,q,reserved,[quote],books): reserved.append(quote.order())
        counts[part]['proposed_quotes']+=len(reserved)
    return {'decision_counts':{k:dict(v) for k,v in counts.items()},
            'forward_diagnostics':{part:{t:{'n':len(vals),'mean_signed_5_tick_change':statistics.mean(vals) if vals else None}
                     for t in TICKERS for vals in [[x['signed_5_tick_change'] for x in forecasts if x['partition']==part and x['ticker']==t and (x['tick']+5<250 if part=='development' else True)]]}
                     for part in counts},
            'limitations':'Signal diagnostics only. No execution/queue simulation, no net-profit claim, one run, last50ticks chronological holdout rather than independent sample.'}


def old_round(archive):
    archive=Path(archive); manifest=json.loads((archive/'manifest.json').read_text()); result=[]
    signal=Signal(); reductions=0; first=None; worst=0; largest_cycle=0
    for source in manifest['analysis_event_files']:
        rows=[json.loads(line) for line in (archive/source).read_text().splitlines()]
        count=Counter(r['event'] for r in rows)
        snaps=[r for r in rows if r['event']=='snapshot']
        for r in rows:
            if r['event']=='cycle_timing': largest_cycle=max(largest_cycle,r.get('elapsed_ms',0))
        unique={}
        for r in snaps: unique.setdefault(r['tick'],r)
        for tick,r in unique.items():
            signal.update(tick,(r['bid']+r['ask'])/2)
            worst=max(worst,abs(r['position']))
            if signal.direction*r['position']<0:
                reductions+=1
                if first is None: first=tick
        accounts=[r for r in rows if r['event'] in ('account','final_account')]
        result.append({'source':source,'events':len(rows),'counts':dict(count),'snapshot_ticks':list(unique),
                       'first_account':accounts[0].get('account_nlv') if accounts else None,
                       'last_account':accounts[-1].get('account_nlv') if accounts else None})
    return dict(segments=result,max_observed_abs_position=worst,longest_cycle_ms=largest_cycle,
                new_guard_first_opposing_position_tick=first,new_guard_opposing_ticks=reductions,
                diagnostic_only='Actual old inventory path is used to check response; new strategy counterfactual fills/profit are unknown.')


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('csv',nargs='+'); ap.add_argument('--old-archive');ap.add_argument('--output',required=True)
    args=ap.parse_args();ticks,sources=load_books(args.csv);c=Config()
    result={'sources':sources,'config_frozen_before_replay':asdict(c),'new_case':evaluate(ticks,c)}
    if args.old_archive:result['old_final_round']=old_round(args.old_archive)
    Path(args.output).write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    print(json.dumps(result['new_case'],indent=2))


if __name__=='__main__':main()
