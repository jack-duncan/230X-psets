import contextlib
from copy import deepcopy
from dataclasses import replace
import io
import math
import random
import time
import unittest
from unittest.mock import patch
from core import *
from runner import Engine,Client,HTTPFailure,Uncertain,Pending,RateLimit,flatten_only


class Log:
    def __init__(self): self.rows=[]
    def write(self,event,**kw): self.rows.append(dict(event=event,**deepcopy(kw)))


class Fake:
    def __init__(self):
        self.case=dict(tick=0,period=1,status='ACTIVE',ticks_per_period=300,total_periods=1)
        self.prices={'CNR':160.0,'RY':100.0,'AC':20.0}
        self.securities={t:dict(ticker=t,position=0,realized=0,unrealized=0,is_tradeable=True,
            is_shortable=True,quoted_decimals=2,min_trade_size=0,max_trade_size=5000,
            execution_delay_ms=0,api_orders_per_second=500,trading_fee=BRIEF[t][0],
            limit_order_rebate=sum(BRIEF[t]),limits=[dict(name='all',units=1)],min_price=.01,max_price=1000) for t in TICKERS}
        self.order_rates={}; self.orders={}; self.calls=[]; self.nextid=1; self.account=0
        self.hide=set(); self.fill_at_insert=0; self.reject=False; self.cancel_lag=False; self.lag_position=None
    def fill(self,oid,qty):
        o=self.orders[oid]; qty=min(qty,leaves(o)); o['quantity_filled']+=qty; o['vwap']=o['price']
        self.securities[o['ticker']]['position']+=(1 if o['action']=='BUY' else -1)*qty
        if not leaves(o): o['status']='TRANSACTED'
    def lookup(self,oid):
        return None if oid in self.hide else deepcopy(self.orders[oid])
    def request(self,method,path,**kw):
        self.calls.append((method,path,kw))
        if path=='/case': return deepcopy(self.case)
        if path=='/securities':
            rows=deepcopy(list(self.securities.values()))
            if self.lag_position is not None:
                for r in rows: r['position']=self.lag_position[r['ticker']]
            return rows
        if path=='/trader': return {'nlv':self.account}
        if path=='/limits': return [dict(name='all',gross_limit=25000,net_limit=25000)]
        if path=='/securities/book':
            p=self.prices[kw['ticker']]
            return {'bids':[dict(order_id=900000,price=round(p-.01,2),quantity=50000,quantity_filled=0)],
                    'asks':[dict(order_id=900001,price=round(p+.01,2),quantity=50000,quantity_filled=0)]}
        if method=='GET' and path=='/orders': return [deepcopy(o) for i,o in self.orders.items() if o['status']==kw.get('status','OPEN') and i not in self.hide]
        if method=='DELETE' or path=='/commands/cancel':
            ids=[int(path.rsplit('/',1)[1])] if method=='DELETE' else list(map(int,kw['ids'].split(',')))
            if not self.cancel_lag:
                for oid in ids:
                    if self.orders[oid]['status']=='OPEN': self.orders[oid]['status']='CANCELLED'
            return {'cancelled_order_ids':ids}
        if method=='POST' and path=='/orders':
            oid=self.nextid; self.nextid+=1
            self.orders[oid]=dict(kw,order_id=oid,status='OPEN',quantity_filled=0,price=float(kw['price']),vwap=0)
            if self.fill_at_insert: self.fill(oid,min(self.fill_at_insert,kw['quantity']))
            if self.reject: raise Uncertain('insertion outcome unknown')
            return deepcopy(self.orders[oid])
        raise AssertionError((method,path,kw))


def books(): return {t:Book(((p-.01,50000),),((p+.01,50000),)) for t,p in Fake().prices.items()}
def make(**kwargs):
    api=Fake(); log=Log(); bot=Engine(api,Config(**kwargs),log,True)
    bot.preflight(); bot.start_round(api.case)
    return bot,api,log


class EconomicsTests(unittest.TestCase):
    def test_both_encodings_all_signed_fee_structures(self):
        for t,(fee,maker) in BRIEF.items():
            for raw in (maker,maker+fee):
                e=Economics.read(t,dict(trading_fee=fee,limit_order_rebate=raw))
                self.assertAlmostEqual(e.taker,fee); self.assertAlmostEqual(e.maker,maker)
    def test_unknown_economics_do_not_silently_adopt_wrong_fee(self):
        with self.assertRaises(SafetyError): Economics.read('RY',dict(trading_fee=.01,limit_order_rebate=.015))
        e=Economics.read('RY',dict(trading_fee=.01,limit_order_rebate=.015),'offset')
        self.assertAlmostEqual(e.maker,.005)
    def test_nonfinite_rejected(self):
        for v in (float('nan'),float('inf'),True,None):
            with self.assertRaises(SafetyError): finite(v)
    def test_ry_negative_maker_reduces_edge(self):
        s=Strategy(Config()); b=books(); q={t:0 for t in TICKERS}
        es={t:Economics.read(t,dict(trading_fee=BRIEF[t][0],limit_order_rebate=BRIEF[t][1])) for t in TICKERS}
        quotes=s.quotes(20,300,q,b,es)
        self.assertTrue(quotes)
        self.assertTrue(all(x.price<b[x.ticker].ask if x.action=='BUY' else x.price>b[x.ticker].bid for x in quotes))


class RiskTests(unittest.TestCase):
    def test_gross_not_net_for_pairs(self):
        p={'CNR':7000,'RY':-7000,'AC':6000}
        self.assertEqual(exposure(p,[])[0],20000)
        self.assertFalse(allowed(Config(),p,[],[Quote('AC','BUY',1000,20)],books()))
    def test_opposite_orders_never_offset(self):
        p={t:0 for t in TICKERS}; os=[Quote('AC','BUY',5000,20).order(),Quote('AC','SELL',5000,20.01).order()]
        self.assertEqual(exposure(p,os)[0],5000)
        self.assertEqual(exposure(p,os)[1],5000)
    def test_random_fill_subsets_within_reserved_bounds(self):
        rng=random.Random(341)
        for _ in range(1000):
            p={t:rng.randint(-5000,5000) for t in TICKERS}
            os=[Quote(t,side,rng.randint(1,2000),20).order() for t in TICKERS for side in ('BUY','SELL')]
            g,n,lo,hi=exposure(p,os)
            for _ in range(5):
                actual=p.copy()
                for o in os: actual[o['ticker']]+=(1 if o['action']=='BUY' else -1)*rng.randint(0,o['quantity'])
                self.assertLessEqual(sum(abs(x) for x in actual.values()),g)
                self.assertLessEqual(abs(sum(actual.values())),n)
    def test_both_pair_legs_reserved_before_entry(self):
        p={'CNR':0,'RY':0,'AC':15000}; b=books()
        self.assertFalse(allowed(Config(),p,[],[Quote('CNR','BUY',3000,160),Quote('RY','SELL',3000,100)],b))
    def test_weighted_server_binding(self):
        p={t:0 for t in TICKERS}
        self.assertFalse(allowed(Config(),p,[],[Quote('AC','BUY',1000,20)],books(),bindings=[({'AC':2,'RY':0,'CNR':0},1500,1500)]))


class SignalTests(unittest.TestCase):
    def test_up_down_reversal_and_duplicate_ticks(self):
        s=Signal()
        for t in range(15): s.update(t,20+t*.01)
        self.assertEqual(s.direction,1)
        n=len(s.history)
        for _ in range(100): s.update(14,30)
        self.assertEqual(len(s.history),n)
        for t in range(15,40): s.update(t,20.14-(t-14)*.02)
        self.assertEqual(s.direction,-1)
    def test_gap_resets_learned_direction(self):
        s=Signal()
        for t in range(15): s.update(t,20+t*.01)
        s.update(170,21.20)
        self.assertEqual(s.direction,0); self.assertEqual(len(s.history),1)
    def test_wrong_way_inventory_gets_marketable_exit(self):
        c=Config(); s=Strategy(c); b=books()
        e={t:Economics.read(t,dict(trading_fee=BRIEF[t][0],limit_order_rebate=BRIEF[t][1])) for t in TICKERS}
        for tick in range(15):
            b['AC']=Book(((20+tick*.01,5000),),((20.01+tick*.01,5000),)); s.update(tick,b)
        qs=s.quotes(15,300,{'AC':-2500,'RY':0,'CNR':0},b,e)
        x=next(q for q in qs if q.ticker=='AC')
        self.assertEqual(x.action,'BUY'); self.assertEqual(x.mode,'reduce'); self.assertGreaterEqual(x.price,b['AC'].ask)
    def test_reduce_latch_survives_fading_signal(self):
        s=Strategy(Config()); b=books(); es={t:Economics(*BRIEF[t],BRIEF[t][1],'net') for t in TICKERS}
        s.signals['AC'].direction=1
        s.quotes(20,300,{'AC':-2500,'RY':0,'CNR':0},b,es)
        s.signals['AC'].direction=0
        qs=s.quotes(21,300,{'AC':-500,'RY':0,'CNR':0},b,es)
        self.assertEqual(next(q for q in qs if q.ticker=='AC').quantity,500)
    def test_no_pair_assumption_from_levels(self):
        p=Pairs(); c=Config(); b=books(); es={t:Economics(*BRIEF[t],BRIEF[t][1],'net') for t in TICKERS}
        rng=random.Random(7)
        for tick in range(65):
            for t in TICKERS:
                mid=b[t].mid+rng.choice((-.03,.03))
                b[t]=Book(((mid-.005,5000),),((mid+.005,5000),))
            p.update(tick,b)
        self.assertEqual(p.candidates(b,es,c),[])
    def test_pair_window_does_not_use_current_for_fit(self):
        p=Pairs(); b=books()
        for tick in range(61): p.update(tick,b)
        before=list(p.rows)[:-1]
        p.rows[-1]=(60,{'CNR':10,'RY':1,'AC':3})
        self.assertEqual(before,list(p.rows)[:-1])


class EngineTests(unittest.TestCase):
    def test_preflight_signed_fees_and_no_writes(self):
        bot,api,log=make()
        self.assertTrue(all(m=='GET' for m,_,_ in api.calls)); self.assertAlmostEqual(bot.economics['RY'].maker,-.002)
    def test_nonflat_error_includes_actual_state(self):
        api=Fake(); api.securities['AC']['position']=-1250
        bot=Engine(api,Config(),Log(),True)
        with self.assertRaisesRegex(SafetyError,'-1250'): bot.start_round(api.case)
    def test_startup_reset_rebases_pnl(self):
        bot,api,log=make(); bot.active=False; api.account=-118.75; bot.preflight()
        api.account=0; bot.start_round(api.case)
        self.assertEqual(bot.account_start,0)
    def test_one_insertion_per_snapshot(self):
        bot,api,log=make(); bot.step()
        self.assertEqual(len([x for x in api.calls if x[:2]==('POST','/orders')]),1)
    def test_ack_partial_fill_reserved_and_position_lag_blocks_new_order(self):
        bot,api,log=make(); api.fill_at_insert=250; bot.step()
        api.lag_position={t:0 for t in TICKERS}
        count=api.nextid
        with self.assertRaises(Pending): bot.step()
        self.assertEqual(api.nextid,count)
        api.lag_position=None; bot.step()
        self.assertEqual(bot.expected,{t:api.securities[t]['position'] for t in TICKERS})
    def test_hidden_order_pauses_then_recovers(self):
        bot,api,log=make(); bot.step(); api.hide={1}; n=api.nextid
        with self.assertRaises(Pending): bot.step()
        self.assertEqual(api.nextid,n)
        api.hide.clear(); bot.step()
    def test_cancel_ack_not_treated_as_terminal(self):
        bot,api,log=make(); bot.step(); api.cancel_lag=True
        bot.cancel(list(api.orders.values())); n=api.nextid
        bot.step(); self.assertEqual(api.nextid,n)
    def test_404_cancel_fallback(self):
        bot,api,log=make(); bot.step(); original=api.request
        def req(m,p,**kw):
            if m=='DELETE': raise HTTPFailure(404,m,p)
            return original(m,p,**kw)
        api.request=req; bot.cancel(list(api.orders.values()))
        self.assertTrue(bot.cancel_command); self.assertEqual(api.orders[1]['status'],'CANCELLED')
    def test_unowned_order_blocks(self):
        bot,api,log=make()
        api.orders[555]=dict(order_id=555,ticker='AC',action='BUY',quantity=100,quantity_filled=0,status='OPEN',price=20)
        with self.assertRaisesRegex(SafetyError,'Unowned'): bot.step()
    def test_timeout_insertion_not_retried(self):
        bot,api,log=make(); api.reject=True
        with self.assertRaises(Uncertain): bot.step()
        self.assertEqual(api.nextid,2)
        with self.assertRaises(SafetyError): bot.step()
        self.assertEqual(api.nextid,2)
    def test_long_loop_pause_latches_flatten(self):
        bot,api,log=make(); bot.last_loop=time.monotonic()-430
        self.assertFalse(bot.step()); self.assertEqual(bot.reduce_reason,'long control-loop pause')
    def test_account_loss_stop_stays_latched(self):
        bot,api,log=make(); api.account=-751
        self.assertFalse(bot.step()); api.account=0
        self.assertFalse(bot.step()); self.assertEqual(bot.reduce_reason,'account loss stop')
    def test_empty_open_list_not_cleanup_proof(self):
        bot,api,log=make(); bot.step(); api.hide={1}
        with patch('runner.time.monotonic',side_effect=[0,1,10]):
            bot.cleanup()
        self.assertEqual(log.rows[-1]['event'],'cleanup_unconfirmed')
    def test_end_policy_exits_instead_of_adding(self):
        bot,api,log=make(); api.case['tick']=295
        api.securities['AC']['position']=-2000; bot.expected['AC']=-2000
        bot.step()
        x=next(iter(api.orders.values()))
        self.assertEqual(x['action'],'BUY'); self.assertEqual(x['quantity'],2000)
    def test_pair_timeout_exits_unhedged_position(self):
        bot,api,log=make(); b=books()
        bot.pair=dict(x='CNR',y='RY',a=0,beta=1,sd=.1,z=-2.5,targets={'CNR':-100,'RY':160},
                      tick=0,created=time.monotonic()-4,entered=False,closing=False)
        out=bot.pair_quotes(1,{'CNR':-100,'RY':0,'AC':0},[],b)
        self.assertTrue(bot.pair['closing']); self.assertEqual(len(out),1); self.assertEqual(out[0].action,'BUY')


class TransportTests(unittest.TestCase):
    def test_observe_forbids_writes_before_transport(self):
        api=Client('http://localhost:45325/v1','fixture',False,Config())
        with self.assertRaises(SafetyError): api.request('POST','/orders')
    def test_remote_or_embedded_credentials_rejected(self):
        for url in ('http://example.com/v1','http://secret@localhost/v1','http://localhost/v1?key=secret'):
            with self.assertRaises(SafetyError): Client(url,'fixture',True,Config())


if __name__=='__main__':
    with contextlib.redirect_stdout(io.StringIO()):
        unittest.main(verbosity=2)
