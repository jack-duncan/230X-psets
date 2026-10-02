"""Local RIT Client runner. Never send a second insertion after an unknown outcome."""
from __future__ import annotations
import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import socket
import sys
import time
from urllib.error import HTTPError, URLError
from http.client import HTTPException
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from core import (TICKERS, Config, Economics, Book, Strategy, Quote, SafetyError,
                  allowed, exposure, finite, integer, leaves, marketable)


class HTTPFailure(Exception):
    def __init__(self,status,method,path):
        self.status=status
        super().__init__('HTTP %s for %s %s' % (status,method,path))


class Uncertain(Exception): pass
class Pending(Exception): pass
class RateLimit(Exception):
    def __init__(self,wait): self.wait=wait


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs): return None


class Client:
    def __init__(self,base,key,trade,c):
        u=urlsplit(base)
        if (u.scheme!='http' or u.hostname not in ('localhost','127.0.0.1','::1')
                or u.path.rstrip('/')!='/v1' or u.query or u.fragment or u.username or u.password):
            raise SafetyError('Use the local Client REST base URL, including /v1')
        self.base=base.rstrip('/'); self.key=key; self.trade=trade; self.c=c
        self.opener=build_opener(ProxyHandler({}),NoRedirect())
        self.deadlines={'read':0.0,'write':0.0}; self.order_deadlines={}
        self.order_rates={t:1 for t in TICKERS}; self.lookup_supported=True

    def request(self,method,path,**params):
        if method!='GET' and not self.trade: raise SafetyError('Read-only mode forbids writes')
        kind='read' if method=='GET' else 'write'
        now=time.monotonic()
        deadline=self.deadlines[kind]
        insertion=method=='POST' and path=='/orders'
        if insertion: deadline=max(deadline,self.order_deadlines.get(params['ticker'],0))
        if method!='GET' and now<deadline: raise RateLimit(deadline-now)
        if now<deadline: time.sleep(deadline-now)
        self.deadlines[kind]=time.monotonic()+1.05/(self.c.read_rate if kind=='read' else self.c.write_rate)
        url=self.base+path+('?' + urlencode(params) if params else '')
        req=Request(url,method=method,headers={'X-API-Key':self.key})
        try:
            with self.opener.open(req,timeout=.8) as response:
                data=json.loads(response.read())
                if insertion:
                    wait=max(1.05/self.order_rates[params['ticker']],finite(response.headers.get('X-Wait-Until',0)))
                    self.order_deadlines[params['ticker']]=time.monotonic()+wait
                return data
        except HTTPError as exc:
            try:
                if exc.code==429:
                    try:
                        body=json.loads(exc.read())
                        wait=max(.05,finite(body.get('wait',0)),finite(exc.headers.get('Retry-After',0)))
                    except (ValueError,SafetyError,AttributeError): wait=.5
                    if wait>10: raise SafetyError('Rate-limit delay exceeds ten seconds') from None
                    self.deadlines[kind]=time.monotonic()+wait
                    if insertion: self.order_deadlines[params['ticker']]=self.deadlines[kind]
                    raise RateLimit(wait) from None
                raise HTTPFailure(exc.code,method,path) from None
            finally: exc.close()
        except (URLError,TimeoutError,OSError,ValueError,HTTPException):
            raise Uncertain('%s %s outcome unavailable; no insertion retry' % (method,path)) from None

    def lookup(self,oid):
        if self.lookup_supported:
            try: return self.request('GET','/orders/'+str(oid))
            except HTTPFailure as exc:
                if exc.status!=404: raise
                self.lookup_supported=False
        for status in ('TRANSACTED','CANCELLED','OPEN'):
            for row in self.request('GET','/orders',status=status):
                if row.get('order_id')==oid: return row
        return None


class Journal:
    def __init__(self,folder):
        folder.mkdir(parents=True,exist_ok=True)
        self.path=folder/(datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.jsonl')
        self.file=self.path.open('x',encoding='utf-8')
    def write(self,event,**data):
        self.file.write(json.dumps(dict(utc=datetime.now(timezone.utc).isoformat(),event=event,**data),allow_nan=False)+'\n')
        self.file.flush()
    def close(self): self.file.close()


class Engine:
    def __init__(self,api,c,log,trade):
        c.validate(); self.api=api; self.c=c; self.log=log; self.trade=trade
        self.strategy=Strategy(c); self.economics={}; self.bindings=[]
        self.owned={}; self.expected={t:0 for t in TICKERS}; self.pending_since=None
        self.cancel_command=False; self.active=False; self.last_tick=None
        self.duration=300; self.period=1; self.account_start=None; self.peak=None
        self.stopping=False; self.reduce_reason=None; self.pair=None; self.pair_cool=-1
        self.last_loop=None; self.last_console=0; self.last_books={}; self.last_positions=dict(self.expected)
        self.metadata={}; self.authorized=False; self.stats={'submissions':0,'fills':0}
        self.ambiguous_insertion=False

    def preflight(self):
        case=self.api.request('GET','/case')
        rows=self.api.request('GET','/securities')
        available={r['ticker']:r for r in rows if r.get('is_tradeable')}
        if set(available)!=set(TICKERS): raise SafetyError('Load ALGO2e with exactly CNR, RY, AC')
        self.duration=integer(case.get('ticks_per_period')); self.period=integer(case.get('period'))
        if self.duration<30 or integer(case.get('total_periods'))!=1: raise SafetyError('Unsupported round duration/periods')
        limits={r['name']:r for r in self.api.request('GET','/limits')}
        weights={}
        for t,s in available.items():
            if (s.get('is_shortable') is not True or integer(s.get('quoted_decimals'))!=2
                    or not 0<=finite(s.get('min_trade_size'))<=1
                    or finite(s.get('max_trade_size'))<self.c.max_order
                    or finite(s.get('execution_delay_ms'))!=0):
                raise SafetyError(t+' has incompatible security settings')
            self.economics[t]=Economics.read(t,s,self.c.fee_mode)
            self.api.order_rates[t]=finite(s.get('api_orders_per_second'))
            if self.api.order_rates[t]<=0: raise SafetyError(t+' API order entry disabled')
            if not s.get('limits'): raise SafetyError('Missing limit bindings for '+t)
            for binding in s['limits']:
                name=binding['name']; unit=finite(binding['units'])
                if name not in limits or unit<=0: raise SafetyError('Unknown risk limit binding')
                weights.setdefault(name,{z:0.0 for z in TICKERS})[t]=unit
            self.metadata[t]=s
        self.bindings=[]
        for name,w in weights.items():
            row=limits[name]; g=finite(row.get('gross_limit')); n=finite(row.get('net_limit'))
            if min(g,n)<=0: raise SafetyError('Invalid gross/net limit')
            self.bindings.append((w,.9*g,.9*n))
        self.log.write('preflight',case=case,config=asdict(self.c),economics={t:asdict(e) for t,e in self.economics.items()},
                       security_order_rates=self.api.order_rates,limit_bindings=self.bindings)
        return case,available

    def start_round(self,case):
        # Reset-dependent metadata and P&L must be read AFTER the actual start.
        verified,rows=self.preflight()
        if verified['status']!='ACTIVE': return False
        orders=self.api.request('GET','/orders',status='OPEN')
        positions={t:integer(rows[t].get('position')) for t in TICKERS}
        if self.trade and (orders or any(positions.values())):
            raise SafetyError('Cannot start: positions=%s; live orders=%s. Stop other bots, cancel orders, flatten or use --flatten.' % (positions,len(orders)))
        self.expected=positions.copy(); self.last_positions=positions.copy()
        self.account_start=finite(self.api.request('GET','/trader').get('nlv'))
        self.peak=self.account_start; self.last_tick=verified['tick']; self.active=True; self.authorized=self.trade
        self.log.write('round_start',tick=verified['tick'],period=verified['period'],account_nlv=self.account_start,positions=positions)
        return True

    def observe_order(self,row,tick):
        oid=integer(row.get('order_id'))
        state=self.owned[oid]
        if (row.get('ticker')!=state['ticker'] or row.get('action')!=state['action']
                or integer(row.get('quantity'))!=state['quantity']):
            raise SafetyError('Order identity changed')
        filled=integer(row.get('quantity_filled'))
        if not state['filled']<=filled<=state['quantity']: raise SafetyError('Cumulative fill moved backwards')
        delta=filled-state['filled']
        if delta:
            sign=1 if state['action']=='BUY' else -1
            self.expected[state['ticker']]+=sign*delta
            self.stats['fills']+=delta
            b=self.last_books.get(state['ticker'])
            if b: self.strategy.fill(state['ticker'],state['action'],delta,b.mid,tick)
            self.log.write('fill',tick=tick,order_id=oid,ticker=state['ticker'],action=state['action'],
                           delta=delta,cumulative=filled,vwap=row.get('vwap'),mode=state['mode'])
        state['filled']=filled; state['missing']=None
        if row.get('status') not in ('OPEN','CANCELLED','TRANSACTED'): raise SafetyError('Unknown order status')
        state['terminal']=row['status']!='OPEN'

    def snapshot(self,case):
        started=time.monotonic()
        open_orders=self.api.request('GET','/orders',status='OPEN')
        if self.trade and any(o.get('order_id') not in self.owned for o in open_orders):
            raise SafetyError('Unowned live order: another writer or an ambiguous insertion')
        seen={o['order_id']:o for o in open_orders}
        unresolved=False
        for oid,state in self.owned.items():
            row=seen.get(oid)
            if row is None: row=self.api.lookup(oid)
            if row is None:
                state['missing']=state['missing'] if state['missing'] is not None else time.monotonic()
                if time.monotonic()-state['missing']>self.c.pending_timeout:
                    raise SafetyError('Order %s remains unconfirmed; reservation retained' % oid)
                unresolved=True; continue
            self.observe_order(row,case['tick'])
            if row['status']=='OPEN': seen[oid]=row
            else: seen.pop(oid,None)
        rows=self.api.request('GET','/securities')
        by={r['ticker']:r for r in rows}
        positions={t:integer(by[t]['position']) for t in TICKERS}
        self.last_positions=positions
        account=finite(self.api.request('GET','/trader').get('nlv'))
        if self.trade and positions!=self.expected:
            unresolved=True
            if self.pending_since is None: self.pending_since=time.monotonic()
            if time.monotonic()-self.pending_since>self.c.pending_timeout:
                raise SafetyError('Position and cumulative fills disagree; possible manual trade or delayed state')
        else: self.pending_since=None
        if unresolved:
            self.log.write('state_pending',tick=case['tick'],positions=positions,expected=self.expected,account_nlv=account)
            raise Pending()
        for oid in [oid for oid,s in self.owned.items() if s['terminal']]: del self.owned[oid]
        books={t:Book.parse(self.api.request('GET','/securities/book',ticker=t,limit=40),set(self.owned)) for t in TICKERS}
        end=self.api.request('GET','/case')
        if end['period']!=case['period'] or end['tick']<case['tick']: raise SafetyError('Case reset during snapshot')
        if end['status']!='ACTIVE': raise Pending()
        if time.monotonic()-started>self.c.snapshot_age:
            self.cancel(list(seen.values())); raise Pending()
        if not all(books.values()):
            self.cancel(list(seen.values()))
            self.log.write('book_pending',tick=end['tick']); raise Pending()
        self.last_books=books
        return end,by,positions,list(seen.values()),books,account,started

    def cancel(self,orders):
        if not self.trade: return
        for row in orders:
            oid=row['order_id']; state=self.owned.get(oid)
            if not state: raise SafetyError('Cannot cancel unowned order in strategy loop')
            now=time.monotonic()
            if state['cancel'] is not None and now-state['cancel']>self.c.pending_timeout:
                raise SafetyError('Cancellation not reconciled')
            if now-state['last_cancel']<.5: continue
            if state['cancel'] is None: state['cancel']=now
            state['last_cancel']=now
            if self.cancel_command: self.api.request('POST','/commands/cancel',ids=str(oid))
            else:
                try: self.api.request('DELETE','/orders/'+str(oid))
                except HTTPFailure as exc:
                    if exc.status!=404: raise
                    self.cancel_command=True
                    self.api.request('POST','/commands/cancel',ids=str(oid))
            self.log.write('cancel_requested',order_id=oid)

    def submit(self,quote,positions,orders,books,rows,started):
        if not self.trade or time.monotonic()-started>self.c.snapshot_age: return False
        if any(s['cancel'] is not None or s['missing'] is not None for s in self.owned.values()): return False
        if not allowed(self.c,positions,orders,[quote],books,bindings=self.bindings): return False
        s=rows[quote.ticker]
        if not finite(s.get('min_price',.01))<=quote.price<=finite(s.get('max_price',1e9)):
            raise SafetyError('Order outside server price bounds')
        if quote.mode=='maker' and (quote.price>=books[quote.ticker].ask if quote.action=='BUY' else quote.price<=books[quote.ticker].bid):
            raise SafetyError('Maker quote crosses observed book')
        if any(o['ticker']==quote.ticker and o['action']!=quote.action and
               (quote.price>=float(o['price']) if quote.action=='BUY' else quote.price<=float(o['price'])) for o in orders): return False
        self.ambiguous_insertion=True
        try:
            row=self.api.request('POST','/orders',ticker=quote.ticker,type='LIMIT',action=quote.action,
                                 quantity=quote.quantity,price='%.2f'%quote.price)
        except RateLimit:
            self.ambiguous_insertion=False; raise
        oid=integer(row.get('order_id'))
        if oid in self.owned: raise SafetyError('Duplicate acknowledged order ID')
        self.owned[oid]=dict(ticker=quote.ticker,action=quote.action,quantity=quote.quantity,
                             filled=0,mode=quote.mode,terminal=False,created=time.monotonic(),
                             missing=None,cancel=None,last_cancel=-math.inf)
        self.observe_order(row,self.last_tick)
        self.ambiguous_insertion=False
        self.stats['submissions']+=1
        self.log.write('submitted',tick=self.last_tick,order_id=oid,**asdict(quote))
        # Send only one insertion per fresh snapshot; never reuse its position after an ack.
        return True

    def pair_quotes(self,tick,positions,orders,books):
        if self.pair is None: return None
        p=self.pair; x=p['x']; y=p['y']; targets=p['targets']
        z=(math.log(books[y].mid)-p['a']-p['beta']*math.log(books[x].mid))/p['sd']
        complete=all(positions[t]==targets[t] for t in targets)
        if complete: p['entered']=True
        # Fixed entry coefficients prevent a drifting fit from hiding a broken spread.
        if (abs(z)<.6 or abs(z)>4 or z*p['z']<=0 or tick-p['tick']>=20
                or self.reduce_reason or self.duration-tick<25
                or (not p['entered'] and time.monotonic()-p['created']>3)):
            p['closing']=True
        if p['closing']: targets={t:0 for t in targets}
        if p['closing'] and all(positions[t]==0 for t in targets) and not any(o['ticker'] in targets for o in orders):
            self.log.write('pair_closed',tick=tick,x=x,y=y)
            self.pair=None; self.pair_cool=tick+15; return []
        result=[]
        for t,target in targets.items():
            delta=target-positions[t]
            if delta:
                quote=marketable(t,delta,books[t],self.c,'pair_exit' if p['closing'] else 'pair_entry',1e5)
                if quote: result.append(quote)
        return result

    def step(self):
        now=time.monotonic()
        if self.last_loop is not None and now-self.last_loop>self.c.stale_pause and self.active:
            self.reduce_reason='long control-loop pause'
            self.log.write('circuit',reason=self.reduce_reason,elapsed=now-self.last_loop)
        self.last_loop=now
        case=self.api.request('GET','/case')
        if self.active and (case['period']!=self.period or case['tick']<self.last_tick or case['status']=='STOPPED' or case['tick']>=self.duration):
            return False
        if case['status'] not in ('ACTIVE','PAUSED','STOPPED'): raise SafetyError('Unknown case state')
        if case['status']!='ACTIVE':
            if self.active:
                rows=self.api.request('GET','/orders',status='OPEN')
                self.cancel([o for o in rows if o['order_id'] in self.owned])
            return not self.stopping
        if not self.active and not self.start_round(case): return True
        case,rows,positions,orders,books,account,started=self.snapshot(case)
        tick=integer(case['tick']); self.last_tick=tick
        if self.stopping: self.reduce_reason='operator stop'
        self.peak=max(self.peak,account)
        if account-self.account_start<=-self.c.loss_stop and self.reduce_reason is None: self.reduce_reason='account loss stop'
        if self.peak-account>=self.c.drawdown_stop and self.reduce_reason is None: self.reduce_reason='account drawdown stop'
        if any(s['cancel'] is not None for s in self.owned.values()):
            self.cancel([o for o in orders if self.owned[o['order_id']]['cancel'] is not None]); return True
        self.strategy.update(tick,books)
        worst=exposure(positions,orders)
        if worst[0]>self.c.gross_cap or worst[1]>self.c.net_cap: self.reduce_reason='portfolio exposure'
        if self.reduce_reason and not any(positions.values()) and not orders: return False
        candidates=self.strategy.pairs.candidates(books,self.economics,self.c) if self.c.pairs else []
        if (self.c.pairs and self.pair is None and candidates and tick>=self.pair_cool
                and not self.reduce_reason and self.duration-tick>40):
            for p in candidates:
                if all(positions[t]==0 for t in p['targets']):
                    proposals=[marketable(t,q,books[t],self.c,'pair_entry') for t,q in p['targets'].items()]
                    other_orders=[o for o in orders if o['ticker'] not in p['targets']]
                    if all(proposals) and all(q.quantity==abs(p['targets'][q.ticker]) for q in proposals) and allowed(self.c,positions,other_orders,proposals,books,bindings=self.bindings):
                        competing=[o for o in orders if o['ticker'] in p['targets']]
                        if competing:
                            self.cancel(competing); return True
                        self.pair=dict(p,tick=tick,created=time.monotonic(),entered=False,closing=False)
                        self.log.write('pair_open',tick=tick,**p)
                        break
        pair_quotes=self.pair_quotes(tick,positions,orders,books)
        blocked=tuple(self.pair['targets']) if self.pair else ()
        quotes=self.strategy.quotes(tick,self.duration,positions,books,self.economics,bool(self.reduce_reason),blocked)
        if pair_quotes is not None:
            # During leg entry/exit, give the paired transaction exclusive insertion priority.
            quotes=pair_quotes+([] if pair_quotes else quotes)
        if self.c.settlement=='net' and self.duration-tick<=2: quotes=[]
        self.log.write('snapshot',tick=tick,positions=positions,account_nlv=account,change_from_round_start=account-self.account_start,
                       risk_gross=worst[0],risk_net=worst[1],reduce_reason=self.reduce_reason,
                       books={t:asdict(b) for t,b in books.items()},
                       open_orders=[{k:o.get(k) for k in ('order_id','ticker','action','price','quantity','quantity_filled','status')} for o in orders],
                       security_accounts={t:{k:rows[t].get(k) for k in ('position','realized','unrealized','vwap','volume')} for t in TICKERS},
                       desired=[asdict(q) for q in quotes],signals=self.strategy.last_plan,
                       pairs_screen=self.strategy.pairs.diagnostics,snapshot_ms=1000*(time.monotonic()-started))
        if time.monotonic()-self.last_console>2:
            self.last_console=time.monotonic()
            print('tick=%s positions=%s round P&L=%.2f %s' % (tick,positions,account-self.account_start,self.reduce_reason or 'RUNNING'),flush=True)
        if not self.trade: return True
        targets={(q.ticker,q.action):q for q in quotes}
        cancel=[]
        for row in orders:
            target=targets.get((row['ticker'],row['action']))
            if (target is None or abs(float(row['price'])-target.price)>.005 or leaves(row)>target.quantity
                    or self.owned[row['order_id']]['mode']!=target.mode): cancel.append(row)
        if cancel:
            self.cancel(cancel); return True
        for quote in quotes:
            if any(o['ticker']==quote.ticker and (o['action']==quote.action or quote.mode!='maker') for o in orders): continue
            if self.submit(quote,positions,orders,books,rows,started): break
        return True

    def cleanup(self):
        if not self.authorized: return
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            try:
                case=self.api.request('GET','/case')
                if case['period']!=self.period or case['tick']<self.last_tick:
                    self.log.write('cleanup_reset_no_writes'); return
                orders=self.api.request('GET','/orders',status='OPEN')
                # Includes a possible accepted-but-unacknowledged insertion on this dedicated account.
                ids=[str(o['order_id']) for o in orders if o.get('ticker') in TICKERS]
                if ids:
                    self.api.request('POST','/commands/cancel',ids=','.join(ids))
                else:
                    unresolved=False
                    for oid in self.owned:
                        row=self.api.lookup(oid)
                        if row is None or row.get('status')=='OPEN': unresolved=True
                    if not unresolved and not self.ambiguous_insertion:
                        rows=self.api.request('GET','/securities')
                        positions={r['ticker']:r['position'] for r in rows if r['ticker'] in TICKERS}
                        account=finite(self.api.request('GET','/trader')['nlv'])
                        self.log.write('final_account',account_nlv=account,positions=positions,open_orders=0,
                                       change_from_round_start=account-self.account_start)
                        print('No live orders confirmed. Positions:',positions,'Account NLV:',account)
                        if any(positions.values()): print('POSITION REMAINS. Check RIT; order cancellation does not flatten inventory.')
                        return
            except RateLimit: pass
            except (HTTPFailure,Uncertain,SafetyError,KeyError,TypeError): break
            time.sleep(.2)
        self.log.write('cleanup_unconfirmed')
        print('Cleanup unconfirmed. Check RIT orders and positions now.')

    def run(self):
        try:
            while True:
                started=time.monotonic()
                try:
                    if not self.step(): break
                except Pending: pass
                except RateLimit as exc:
                    self.log.write('rate_limit',wait=exc.wait)
                self.log.write('cycle',elapsed_ms=1000*(time.monotonic()-started))
                time.sleep(max(0,self.c.loop_seconds-(time.monotonic()-started)))
            return 0
        except (SafetyError,HTTPFailure,Uncertain,KeyError,ValueError,TypeError) as exc:
            self.log.write('halt',reason=str(exc)); print('HALTED:',exc); return 1
        finally: self.cleanup()


def disable_quickedit():
    if os.name!='nt': return
    import ctypes
    kernel=ctypes.windll.kernel32; handle=kernel.GetStdHandle(-10); mode=ctypes.c_ulong()
    if kernel.GetConsoleMode(handle,ctypes.byref(mode)):
        kernel.SetConsoleMode(handle,(mode.value|0x80)&~0x40)


def writer_lock_port(base):
    # Localhost aliases and trailing slashes must share the same writer lock.
    return 30000+int(hashlib.sha256(str(urlsplit(base).port or 80).encode()).hexdigest()[:8],16)%20000


def source_hashes():
    result={}
    for name in ('core',__name__):
        module=sys.modules[name]
        digest=getattr(module,'SOURCE_SHA256',None)
        if digest is None: digest=hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        result['runner' if name==__name__ else name]=digest
    return result


def main(argv=None):
    disable_quickedit()
    parser=argparse.ArgumentParser(description='ALGO2e: one process per account. Defaults to read-only check.')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--trade',action='store_true'); mode.add_argument('--observe',action='store_true')
    mode.add_argument('--flatten',action='store_true',help='Cancel all CNR/RY/AC orders and flatten existing inventory only')
    parser.add_argument('--base-url',default='http://localhost:45325/v1')
    parser.add_argument('--profile',choices=('balanced','maker','trend','pairs','defensive'),default='balanced')
    parser.add_argument('--fee-mode',choices=('auto','net','offset'),default='auto')
    parser.add_argument('--settlement',choices=('flatten','net'),default='flatten')
    parser.add_argument('--config'); parser.add_argument('--logs',default='logs_algo2e')
    args=parser.parse_args(argv)
    c=Config(fee_mode=args.fee_mode,settlement=args.settlement)
    if args.profile=='maker': c.trend=False; c.pairs=False
    if args.profile=='trend': c.pairs=False
    if args.profile=='pairs': c.trend=False; c.market_make=False
    if args.profile=='defensive': c.quote_size=500; c.trend_dollars=30000; c.loss_stop=400; c.drawdown_stop=300
    if args.config:
        values=json.loads(Path(args.config).read_text())
        if not isinstance(values,dict) or set(values)-set(vars(c)): raise SafetyError('Unknown config setting')
        for k,v in values.items(): setattr(c,k,v)
    c.validate()
    # Shared team packages never embed personal credentials or log them.
    key=os.environ.get('RIT_API_KEY') or getpass.getpass('RIT Client API key (hidden): ')
    if not key: raise SafetyError('Missing API key')
    trade=args.trade or args.flatten
    api=Client(args.base_url,key,trade,c)
    lock=None
    if trade:
        lock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
        port=writer_lock_port(args.base_url)
        try: lock.bind(('127.0.0.1',port))
        except OSError: raise SafetyError('Another local writer is running for this API endpoint') from None
    log=Journal(Path(args.logs)); engine=Engine(api,c,log,trade)
    log.write('run_identity',version='ALGO2e-1',profile=args.profile,trade=trade,source_hashes=source_hashes())
    previous_handler=signal.signal(signal.SIGINT,lambda *_: setattr(engine,'stopping',True))
    print('ALGO2e 1 |',args.profile,'|','TRADING' if trade else 'READ ONLY')
    print('Log:',log.path)
    try:
        case,rows=engine.preflight()
        print('Economics:',{t:asdict(e) for t,e in engine.economics.items()})
        print('Gross/net caps:',c.gross_cap,c.net_cap,'Server order rates:',api.order_rates)
        if args.flatten: return flatten_only(engine)
        if args.trade or args.observe: return engine.run()
        print('Read-only check passed. Positions:',{t:rows[t]['position'] for t in TICKERS})
        return 0
    finally:
        signal.signal(signal.SIGINT,previous_handler)
        log.close()
        if lock: lock.close()


def flatten_only(engine):
    """Explicit recovery mode; never transitions into strategy trading."""
    api=engine.api; c=engine.c; deadline=time.monotonic()+30
    initial=api.request('GET','/case')
    engine.last_tick=initial['tick']; engine.period=initial['period']
    engine.account_start=finite(api.request('GET','/trader')['nlv'])
    original={o['order_id']:o for o in api.request('GET','/orders',status='OPEN')}
    if any(o['ticker'] not in TICKERS for o in original.values()):
        raise SafetyError('Recovery found an unexpected security')
    engine.owned.update({oid:{} for oid in original})
    engine.authorized=True
    expected=None; pending_order=None; last_filled=0; pending_identity=None
    stable_positions=None; stable_since=None
    try:
        while time.monotonic()<deadline:
            try:
                if engine.stopping:
                    print('Recovery interrupted; cancelling remaining orders.'); return 1
                case=api.request('GET','/case')
                if case['period']!=initial['period'] or case['tick']<initial['tick'] or case['status']!='ACTIVE':
                    print('Recovery stopped: round inactive/reset. Check positions in RIT.'); return 1
                engine.last_tick=case['tick']
                # Confirm initial cancellations in terminal history, not just an empty OPEN list.
                unresolved=[]
                for oid in list(original):
                    row=api.lookup(oid)
                    if row is None: unresolved.append(oid)
                    elif row['status']=='OPEN': unresolved.append(oid)
                    elif row['status'] in ('TRANSACTED','CANCELLED'): del original[oid]
                    else: raise SafetyError('Unknown initial recovery order status')
                if unresolved:
                    api.request('POST','/commands/cancel',ids=','.join(map(str,unresolved)))
                    time.sleep(.2); continue
                if pending_order is not None:
                    state=api.lookup(pending_order)
                    if state is None:
                        time.sleep(.2); continue
                    filled=integer(state['quantity_filled'])
                    if (state['ticker'],state['action'],integer(state['quantity']))!=pending_identity:
                        raise SafetyError('Recovery order identity changed')
                    if not last_filled<=filled<=pending_identity[2]: raise SafetyError('Invalid recovery cumulative fill')
                    expected[state['ticker']]+=(1 if state['action']=='BUY' else -1)*(filled-last_filled)
                    last_filled=filled
                    if state['status']=='OPEN':
                        api.request('POST','/commands/cancel',ids=str(pending_order)); time.sleep(.2); continue
                    if state['status'] not in ('TRANSACTED','CANCELLED'): raise SafetyError('Unknown recovery order status')
                    pending_order=None
                orders=api.request('GET','/orders',status='OPEN')
                if orders: raise SafetyError('Unexpected live order during recovery; stop all other writers')
                securities=api.request('GET','/securities')
                positions={s['ticker']:integer(s['position']) for s in securities if s['ticker'] in TICKERS}
                if set(positions)!=set(TICKERS): raise SafetyError('Incomplete recovery positions')
                if expected is None:
                    # Initial inventory has no local fill ledger. Allow cancellation-side position
                    # updates to settle before adopting a baseline; subsequent fills reconcile exactly.
                    if positions!=stable_positions:
                        stable_positions=positions.copy(); stable_since=time.monotonic()
                    if time.monotonic()-stable_since<1:
                        time.sleep(.2); continue
                    expected=positions.copy()
                if positions!=expected:
                    time.sleep(.2); continue
                nonflat=[s for s in securities if s['ticker'] in TICKERS and integer(s['position'])!=0]
                if not nonflat: print('Recovery complete: flat, no live orders.'); return 0
                s=nonflat[0]; b=Book.parse(api.request('GET','/securities/book',ticker=s['ticker'],limit=40))
                if b:
                    q=marketable(s['ticker'],-integer(s['position']),b,c)
                    if q:
                        if not finite(s.get('min_price',.01))<=q.price<=finite(s.get('max_price',1e9)):
                            raise SafetyError('Recovery price outside venue bounds')
                        engine.ambiguous_insertion=True
                        try:
                            row=api.request('POST','/orders',ticker=q.ticker,action=q.action,quantity=q.quantity,type='LIMIT',price='%.2f'%q.price)
                        except RateLimit:
                            engine.ambiguous_insertion=False; raise
                        # Reconcile the exact fill AND its position before a subsequent insertion.
                        pending_order=integer(row['order_id']); last_filled=0
                        pending_identity=(q.ticker,q.action,q.quantity)
                        engine.owned[pending_order]={}
                        engine.ambiguous_insertion=False
                        engine.log.write('recovery_submitted',tick=case['tick'],order_id=pending_order,**asdict(q))
                time.sleep(.25)
            except RateLimit as exc: time.sleep(min(.5,exc.wait))
        print('Recovery timed out. Check live orders and remaining positions.'); return 1
    except (SafetyError,HTTPFailure,Uncertain,KeyError,TypeError,ValueError) as exc:
        engine.log.write('recovery_halt',reason=str(exc))
        print('Recovery halted:',exc); return 1
    finally:
        engine.cleanup()


if __name__=='__main__':
    try: sys.exit(main())
    except (SafetyError,HTTPFailure,Uncertain,ValueError,OSError) as exc:
        print('Startup failed:',exc); sys.exit(1)
