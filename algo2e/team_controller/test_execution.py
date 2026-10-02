"""Execution regressions and synthetic stress, not a profit backtest."""
import contextlib
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import math
import random
import statistics
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
from core import *
from runner import Client, Engine, HTTPFailure, Pending, RateLimit, Uncertain, flatten_only, writer_lock_port
from test_algo2e import Fake, Log, books, make


class Clock:
    def __init__(self): self.now=100.0
    def monotonic(self): return self.now
    def sleep(self,seconds): self.now+=seconds


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.responses=[]; cls.requests=[]
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.0'
            def log_message(self,*args): pass
            def do_GET(self): self.respond()
            def do_POST(self): self.respond()
            def do_DELETE(self): self.respond()
            def respond(self):
                cls.requests.append((self.command,self.path))
                status,body,headers=cls.responses.pop(0)
                payload=json.dumps(body).encode()
                self.send_response(status)
                for k,v in headers.items(): self.send_header(k,str(v))
                self.end_headers(); self.wfile.write(payload)
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=lambda: cls.server.serve_forever(poll_interval=.05),daemon=True)
        cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()
    def setUp(self):
        self.responses.clear(); self.requests.clear()
        self.api=Client('http://localhost:%s/v1'%self.server.server_port,'test-fixture',True,Config())
        self.api.order_rates={t:500 for t in TICKERS}
    def test_success_header_is_duration_and_blocks_next_insert(self):
        self.responses.append((200,{'order_id':1},{'X-Wait-Until':.8}))
        self.api.request('POST','/orders',ticker='AC')
        with self.assertRaises(RateLimit) as ctx: self.api.request('POST','/orders',ticker='AC')
        self.assertGreater(ctx.exception.wait,.75); self.assertEqual(len(self.requests),1)
    def test_429_obeys_longer_header_without_immediate_retry(self):
        self.responses.append((429,{'wait':.1},{'Retry-After':.3}))
        with self.assertRaises(RateLimit) as ctx: self.api.request('POST','/orders',ticker='AC')
        self.assertAlmostEqual(ctx.exception.wait,.3)
        with self.assertRaises(RateLimit): self.api.request('POST','/orders',ticker='AC')
        self.assertEqual(len(self.requests),1)
    def test_auth_denial_not_retried(self):
        self.responses.append((401,{},{}))
        with self.assertRaises(HTTPFailure) as ctx: self.api.request('GET','/case')
        self.assertEqual(ctx.exception.status,401); self.assertEqual(len(self.requests),1)
    def test_truncated_post_response_is_uncertain(self):
        self.responses.append((200,{'order_id':1},{'Content-Length':999}))
        with self.assertRaises(Uncertain): self.api.request('POST','/orders',ticker='AC')
        self.assertEqual(len(self.requests),1)
    def test_order_lookup_404_falls_back_to_terminal_lists(self):
        self.responses.extend([(404,{},{}),(200,[],{}),(200,[{'order_id':123,'status':'CANCELLED'}],{})])
        self.assertEqual(self.api.lookup(123)['status'],'CANCELLED')
        self.assertFalse(self.api.lookup_supported)
        self.assertEqual(len(self.requests),3)
    def test_redirect_not_followed(self):
        self.responses.append((302,{}, {'Location':'http://example.com/'}))
        with self.assertRaises(HTTPFailure): self.api.request('GET','/case')
        self.assertEqual(len(self.requests),1)


def valid_pair():
    p=Pairs(); rng=random.Random(230); x=math.log(160); residual=0
    for tick in range(60):
        x+=rng.gauss(0,.003); residual=.6*residual+rng.gauss(0,.0008)
        p.rows.append((tick,{'CNR':x,'RY':math.log(100/160)+x+residual,'AC':math.log(20)+rng.gauss(0,.003)}))
    xs=[r[1]['CNR'] for r in p.rows]; ys=[r[1]['RY'] for r in p.rows]
    a,beta,_=fit(xs,ys)
    sd=statistics.stdev([y-a-beta*x for x,y in zip(xs,ys)])
    current={'CNR':x,'RY':a+beta*x+2.8*sd,'AC':math.log(20)}
    p.rows.append((60,current)); p.last_tick=60
    bs={t:Book(((math.exp(v)-.005,10000),),((math.exp(v)+.005,10000),)) for t,v in current.items()}
    es={t:Economics(*BRIEF[t],BRIEF[t][1],'net') for t in TICKERS}
    return p,bs,es


class PairTests(unittest.TestCase):
    def test_admissible_pair_beta_weighted_hedge_and_cost_gate(self):
        p,b,e=valid_pair(); candidates=p.candidates(b,e,Config())
        self.assertEqual(len(candidates),1); pair=candidates[0]
        self.assertAlmostEqual(pair['z'],2.8); self.assertLess(pair['targets']['RY'],0)
        ratio=abs(pair['targets']['CNR']*b['CNR'].mid/(pair['targets']['RY']*b['RY'].mid))
        self.assertAlmostEqual(ratio,pair['beta'],delta=.02)
        expensive={t:Economics(1,0,0,'net') for t in TICKERS}
        self.assertEqual(p.candidates(b,expensive,Config()),[])
    def test_current_spread_changes_z_not_training_coefficients(self):
        p,b,e=valid_pair(); original=p.candidates(b,e,Config())[0]
        tick,cur=p.rows[-1]; cur=cur.copy(); cur['RY']+=original['sd']*.1; p.rows[-1]=(tick,cur)
        updated=p.candidates(b,e,Config())[0]
        self.assertEqual(original['a'],updated['a']); self.assertEqual(original['beta'],updated['beta'])
        self.assertAlmostEqual(updated['z'],2.9)
    def test_two_legs_then_reversion_exit(self):
        bot,api,log=make(market_make=False,trend=False)
        p,b,e=valid_pair(); candidate=p.candidates(b,e,Config())[0]
        api.prices={t:v.mid for t,v in b.items()}; api.case['tick']=60; api.fill_at_insert=5000
        bot.strategy.pairs.candidates=lambda *args:[candidate]
        bot.step(); self.assertEqual(len(api.orders),1)
        bot.step(); self.assertEqual(len(api.orders),2)
        bot.step(); self.assertTrue(bot.pair['entered'])
        for t,q in candidate['targets'].items(): self.assertEqual(api.securities[t]['position'],q)
        api.prices['RY']=math.exp(candidate['a']+candidate['beta']*math.log(api.prices['CNR']))
        api.case['tick']=61
        bot.step(); bot.step(); bot.step()
        self.assertIsNone(bot.pair)
        self.assertTrue(all(s['position']==0 for s in api.securities.values()))
    def test_broken_relation_unwinds_instead_of_averaging_down(self):
        bot,api,log=make(); p,b,e=valid_pair(); pair=p.candidates(b,e,Config())[0]
        bot.pair=dict(pair,tick=60,created=100,entered=True,closing=False)
        b['RY']=Book(((b['RY'].mid+2,5000),),((b['RY'].mid+2.01,5000),))
        pos=dict.fromkeys(TICKERS,0);pos.update(pair['targets'])
        quotes=bot.pair_quotes(61,pos,[],b)
        self.assertTrue(bot.pair['closing'])
        for q in quotes: self.assertEqual(q.action,'BUY' if pos[q.ticker]<0 else 'SELL')


class RecoveryTests(unittest.TestCase):
    def test_partial_fill_position_lag_does_not_duplicate_reduction(self):
        bot,api,log=make(); api.securities['AC']['position']=600; api.fill_at_insert=250
        clock=Clock(); original=api.request; delay={'reads':0,'position':600}
        def request(method,path,**kw):
            if method=='POST' and path=='/orders':
                delay['position']=api.securities['AC']['position']; delay['reads']=3
            rows=original(method,path,**kw)
            if path=='/securities' and delay['reads']:
                delay['reads']-=1
                for row in rows:
                    if row['ticker']=='AC': row['position']=delay['position']
            return rows
        api.request=request
        with patch('runner.time.monotonic',clock.monotonic),patch('runner.time.sleep',clock.sleep):
            self.assertEqual(flatten_only(bot),0)
        self.assertEqual(api.securities['AC']['position'],0)
        self.assertEqual([o['quantity'] for o in api.orders.values()],[600,350,100])
        self.assertTrue(all(o['action']=='SELL' for o in api.orders.values()))
    def test_missing_initial_order_not_assumed_cancelled(self):
        bot,api,log=make(); api.securities['AC']['position']=500
        api.orders[9]=dict(order_id=9,ticker='AC',action='BUY',quantity=100,quantity_filled=0,price=19,status='OPEN')
        api.lookup=lambda oid:None
        clock=Clock()
        with patch('runner.time.monotonic',clock.monotonic),patch('runner.time.sleep',clock.sleep):
            self.assertEqual(flatten_only(bot),1)
        self.assertFalse(any(m=='POST' and p=='/orders' for m,p,k in api.calls))
    def test_ambiguous_insertion_never_claims_verified_cleanup(self):
        bot,api,log=make(); api.reject=True
        with self.assertRaises(Uncertain): bot.step()
        clock=Clock()
        with patch('runner.time.monotonic',clock.monotonic),patch('runner.time.sleep',clock.sleep): bot.cleanup()
        self.assertEqual(log.rows[-1]['event'],'cleanup_unconfirmed')
    def test_local_aliases_share_writer_lock(self):
        urls=['http://localhost:45325/v1','http://127.0.0.1:45325/v1/','http://[::1]:45325/v1']
        self.assertEqual(len({writer_lock_port(u) for u in urls}),1)


class StressTests(unittest.TestCase):
    def test_over_cap_circuit_can_reduce_but_cannot_reverse_or_add(self):
        p={'CNR':8000,'RY':8000,'AC':8000}; b=books(); c=Config()
        self.assertTrue(allowed(c,p,[],[Quote('AC','SELL',4000,20)],b))
        self.assertFalse(allowed(c,p,[],[Quote('AC','BUY',100,20)],b))
        pending=[Quote('AC','SELL',5000,20).order()]
        self.assertFalse(allowed(c,p,pending,[Quote('AC','SELL',4000,20)],b))
    def test_full_rounds_limits_and_terminal_flatten(self):
        # Arbitrary fills exercise risk invariants; they are not calibrated queue fills.
        for scenario in range(8):
            bot,api,log=make(); clock=Clock(); rng=random.Random(8100+scenario)
            with patch('runner.time.monotonic',clock.monotonic),patch('runner.time.sleep',clock.sleep):
                bot.last_loop=None
                for tick in range(300):
                    api.case['tick']=tick
                    if scenario%4==0: drift=.015*tick
                    elif scenario%4==1: drift=-.015*tick
                    elif scenario%4==2: drift=.015*(tick if tick<150 else 300-tick)
                    else: drift=.1*math.sin(tick/8)
                    for t,base in (('CNR',160),('RY',100),('AC',20)):
                        api.prices[t]=base+drift+(rng.gauss(0,.01) if scenario>=4 else 0)
                    for micro in range(4):
                        for oid,o in list(api.orders.items()):
                            if o['status']!='OPEN': continue
                            mid=api.prices[o['ticker']]
                            cross=o['price']>=round(mid+.01,2) if o['action']=='BUY' else o['price']<=round(mid-.01,2)
                            if cross or (tick<285 and rng.random()<.2):
                                api.fill(oid,leaves(o) if cross else max(1,leaves(o)//2))
                        try: bot.step()
                        except Pending: pass
                        positions={t:s['position'] for t,s in api.securities.items()}
                        open_orders=[o for o in api.orders.values() if o['status']=='OPEN']
                        gross,net,_,_=exposure(positions,open_orders)
                        self.assertLessEqual(gross,bot.c.gross_cap,(scenario,tick))
                        self.assertLessEqual(net,bot.c.net_cap,(scenario,tick))
                        clock.sleep(.2)
                self.assertTrue(all(s['position']==0 for s in api.securities.values()),scenario)
                self.assertFalse(any(o['status']=='OPEN' for o in api.orders.values()),scenario)
    def test_round_reset_ends_without_touching_new_round(self):
        bot,api,log=make(); bot.last_tick=100; api.case['tick']=0
        self.assertFalse(bot.step()); bot.cleanup()
        self.assertFalse(any(m!='GET' for m,p,k in api.calls))
    def test_empty_startup_book_waits_then_recovers(self):
        bot,api,log=make(); original=api.request
        def request(m,p,**kw):
            if p=='/securities/book': return {'bids':[],'asks':[]}
            return original(m,p,**kw)
        api.request=request
        with self.assertRaises(Pending): bot.step()
        self.assertFalse(api.orders)
        api.request=original; self.assertTrue(bot.step()); self.assertTrue(api.orders)
    def test_invalid_config_types_rejected(self):
        for field,value in (('loss_stop','750'),('snapshot_age',True),('pair_dollars',float('nan'))):
            c=Config(); setattr(c,field,value)
            with self.assertRaises(SafetyError): c.validate()


if __name__=='__main__':
    with contextlib.redirect_stdout(io.StringIO()): unittest.main(verbosity=2)
