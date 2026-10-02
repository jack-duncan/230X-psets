"""ALGO2e strategy and portfolio risk. Python 3.9+, standard library only."""
from __future__ import annotations
from collections import deque
from dataclasses import dataclass
import itertools
import math
import statistics

TICKERS = ('CNR', 'RY', 'AC')
BRIEF = {'CNR': (.0027, .0023), 'RY': (-.0014, -.0020), 'AC': (.0015, .0011)}


class SafetyError(Exception):
    pass


def finite(value, label='number'):
    if isinstance(value, bool):
        raise SafetyError('Invalid '+label)
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise SafetyError('Invalid '+label) from None
    if not math.isfinite(value):
        raise SafetyError('Nonfinite '+label)
    return value


def integer(value, label='integer'):
    x = finite(value, label)
    if x != int(x):
        raise SafetyError('Noninteger '+label)
    return int(x)


@dataclass
class Config:
    gross_cap: int = 20000
    net_cap: int = 20000
    asset_cap: int = 7000
    asset_dollars: float = 180000
    quote_size: int = 1000
    max_order: int = 5000
    trend_dollars: float = 60000
    trend_shares: int = 2500
    pair_dollars: float = 20000
    trend: bool = True
    pairs: bool = True
    market_make: bool = True
    loss_stop: float = 750
    drawdown_stop: float = 600
    snapshot_age: float = 1.5
    loop_seconds: float = .15
    stale_pause: float = 3
    pending_timeout: float = 8
    read_rate: float = 150
    write_rate: float = 50
    settlement: str = 'flatten'
    fee_mode: str = 'auto'
    warmup: int = 8

    def validate(self):
        for name in ('trend','pairs','market_make'):
            if not isinstance(getattr(self,name),bool): raise SafetyError(name+' must be Boolean')
        for name, v in vars(self).items():
            if name not in ('trend','pairs','market_make','settlement','fee_mode'):
                if not isinstance(v,(int,float)) or isinstance(v,bool):
                    raise SafetyError(name+' must be numeric')
                finite(v, name)
                if v <= 0:
                    raise SafetyError(name+' must be positive')
        for name in ('gross_cap','net_cap','asset_cap','quote_size','max_order','trend_shares','warmup'):
            integer(getattr(self,name), name)
        if not (self.asset_cap <= self.gross_cap < 25000 and self.net_cap < 25000
                and self.quote_size <= self.max_order <= 5000):
            raise SafetyError('Invalid share limits')
        if self.fee_mode not in ('auto','net','offset') or self.settlement not in ('flatten','net'):
            raise SafetyError('Unknown fee or settlement convention')
        if self.read_rate > 200 or self.write_rate > 100 or self.loop_seconds < .05:
            raise SafetyError('Request budgets exceed configured operating ceilings')


@dataclass(frozen=True)
class Economics:
    taker: float
    maker: float
    raw_rebate: float
    convention: str

    @classmethod
    def read(cls, ticker, security, mode='auto'):
        fee = finite(security.get('trading_fee'), 'trading fee')
        raw = finite(security.get('limit_order_rebate'), 'raw rebate')
        if mode == 'auto':
            # API documentation does not specify whether raw rebate offsets fee.
            # Recognize either encoding of this exact brief; otherwise require an explicit choice.
            expected_fee, expected_net = BRIEF[ticker]
            net_match = abs(fee-expected_fee)<1e-8 and abs(raw-expected_net)<1e-8
            offset_match = abs(fee-expected_fee)<1e-8 and abs(raw-fee-expected_net)<1e-8
            if not (net_match or offset_match):
                raise SafetyError('%s fee metadata differs: trading_fee=%s, raw_rebate=%s; select net/offset explicitly after checking settings'%(ticker,fee,raw))
            mode = 'net' if net_match else 'offset'
        return cls(fee, raw if mode=='net' else raw-fee, raw, mode)


@dataclass(frozen=True)
class Book:
    bids: tuple
    asks: tuple

    @property
    def bid(self): return self.bids[0][0]
    @property
    def ask(self): return self.asks[0][0]
    @property
    def mid(self): return (self.bid+self.ask)/2
    @property
    def spread(self): return self.ask-self.bid

    @classmethod
    def parse(cls, raw, own_ids=()):
        if not isinstance(raw,dict): raise SafetyError('Book response must be an object')
        sides=[]
        for side in ('bids','asks'):
            if not isinstance(raw.get(side),list): raise SafetyError('Missing book side')
            levels={}
            for row in raw.get(side,[]):
                if not isinstance(row,dict): raise SafetyError('Invalid book order')
                if row.get('order_id') in own_ids: continue
                q=integer(row.get('quantity'))-integer(row.get('quantity_filled',0))
                p=finite(row.get('price'))
                if q<0 or p<=0: raise SafetyError('Invalid book level')
                if q: levels[p]=levels.get(p,0)+q
            sides.append(tuple(sorted(levels.items(), reverse=side=='bids')))
        if not all(sides) or sides[0][0][0]>=sides[1][0][0]:
            return None
        return cls(*sides)


def leaves(order):
    q=integer(order.get('quantity'))-integer(order.get('quantity_filled',0))
    if q<0: raise SafetyError('Negative order leaves')
    return q


def exposure(positions, orders, units=None):
    """Worst independent fills: buys and sells never offset reservations."""
    units=units or {t:1.0 for t in positions}
    lower=dict(positions); upper=dict(positions)
    for order in orders:
        t=order['ticker']
        if t not in positions: raise SafetyError('Unknown ticker in orders')
        if order['action']=='BUY': upper[t]+=leaves(order)
        elif order['action']=='SELL': lower[t]-=leaves(order)
        else: raise SafetyError('Unknown order side')
    gross=sum(max(abs(lower[t]),abs(upper[t]))*units[t] for t in positions)
    net=max(abs(sum(lower[t]*units[t] for t in positions)),abs(sum(upper[t]*units[t] for t in positions)))
    return gross,net,lower,upper


@dataclass(frozen=True)
class Quote:
    ticker: str
    action: str
    quantity: int
    price: float
    mode: str = 'maker'
    score: float = 0.0

    def order(self):
        return dict(ticker=self.ticker,action=self.action,quantity=self.quantity,
                    quantity_filled=0,price=self.price)


def allowed(c, positions, orders, proposed, books, units=None, bindings=()):
    """Check the complete batch as well as every venue-supplied limit binding."""
    if any(q.quantity<=0 or q.quantity>c.max_order for q in proposed): return False
    after=orders+[q.order() for q in proposed]
    reducing=bool(proposed) and all(
        positions[q.ticker]*(1 if q.action=='BUY' else -1)<0
        and sum(leaves(o) for o in after if o['ticker']==q.ticker and o['action']==q.action)<=abs(positions[q.ticker])
        for q in proposed)
    g,n,lo,hi=exposure(positions,after,units)
    old_g,old_n,_,_=exposure(positions,orders,units)
    if g>max(c.gross_cap,old_g if reducing else 0) or n>max(c.net_cap,old_n if reducing else 0): return False
    for t in positions:
        cap=min(c.asset_cap,int(c.asset_dollars/books[t].mid))
        # A bounded reducing order is still legal if inherited position exceeds our soft cap.
        if max(abs(lo[t]),abs(hi[t]))>max(cap,abs(positions[t])): return False
    for weights,gcap,ncap in bindings:
        g,n,_,_=exposure(positions,after,weights)
        old_g,old_n,_,_=exposure(positions,orders,weights)
        if g>max(gcap,old_g if reducing else 0) or n>max(ncap,old_n if reducing else 0): return False
    return True


class Signal:
    def __init__(self):
        self.history=deque(maxlen=90)
        self.fast=self.slow=0.0
        self.variance=.0001
        self.direction=0
        self.pending=0
        self.confirm=0
        self.last_tick=None
        self.gap=False

    def update(self,tick,mid):
        if self.last_tick is not None and tick<=self.last_tick: return
        self.gap=self.last_tick is not None and tick-self.last_tick>3
        if self.last_tick is None or self.gap:
            self.history.clear(); self.fast=self.slow=mid
            self.direction=self.pending=self.confirm=0
        else:
            dt=tick-self.last_tick
            change=mid-self.history[-1][1]
            self.variance=.85*self.variance+.15*change*change/dt
            self.fast+=(1-math.exp(-dt/2))*(mid-self.fast)
            self.slow+=(1-math.exp(-dt/8))*(mid-self.slow)
        self.last_tick=tick; self.history.append((tick,mid))
        threshold=max(.005,.65*self.sigma)
        signal=self.fast-self.slow
        candidate=1 if signal>threshold else -1 if signal<-threshold else self.direction
        if abs(signal)<threshold*.4: candidate=0
        if candidate==self.pending: self.confirm+=1
        else: self.pending=candidate; self.confirm=1
        if len(self.history)>=4 and self.confirm>=3: self.direction=candidate

    @property
    def sigma(self): return math.sqrt(max(.000001,self.variance))
    @property
    def alpha(self): return max(-3*self.sigma,min(3*self.sigma,.75*(self.fast-self.slow)))
    @property
    def efficiency(self):
        a=list(self.history)[-12:]
        total=sum(abs(y[1]-x[1]) for x,y in zip(a,a[1:]))
        return abs(a[-1][1]-a[0][1])/total if total else 0


def fit(x,y):
    mx=statistics.mean(x); my=statistics.mean(y)
    xx=sum((a-mx)**2 for a in x); yy=sum((b-my)**2 for b in y)
    xy=sum((a-mx)*(b-my) for a,b in zip(x,y))
    if min(xx,yy)<1e-16: return None
    b=xy/xx
    return my-b*mx,b,xy/math.sqrt(xx*yy)


class Pairs:
    """Preceding-window statistical screen, not an assumed arbitrage identity."""
    def __init__(self):
        self.rows=deque(maxlen=81)
        self.last_tick=None
        self.diagnostics=[]

    def update(self,tick,books):
        if tick==self.last_tick: return
        if self.last_tick is not None and tick-self.last_tick!=1: self.rows.clear()
        self.last_tick=tick
        self.rows.append((tick,{t:math.log(b.mid) for t,b in books.items()}))

    def candidates(self,books,economics,c):
        self.diagnostics=[]
        if len(self.rows)<61: return []
        # Current observation never enters the regression or variance estimate.
        prior=list(self.rows)[-61:-1]; current=self.rows[-1][1]
        choices=[]
        for x,y in itertools.combinations(TICKERS,2):
            xs=[r[1][x] for r in prior]; ys=[r[1][y] for r in prior]
            model=fit(xs,ys); early=fit(xs[:30],ys[:30]); late=fit(xs[30:],ys[30:])
            changes=fit([b-a for a,b in zip(xs,xs[1:])],[b-a for a,b in zip(ys,ys[1:])])
            if not all((model,early,late,changes)): continue
            a,beta,_=model
            if not (.2<beta<5 and changes[2]>.6 and abs(early[1]-late[1])<.3*beta): continue
            residual=[v-a-beta*u for u,v in zip(xs,ys)]
            sd=statistics.stdev(residual)
            ar=fit(residual[:-1],residual[1:])
            crosses=sum(u*v<0 for u,v in zip(residual,residual[1:]))
            if not ar or not (.05<ar[1]<.93) or crosses<5 or sd<1e-6: continue
            spread=current[y]-a-beta*current[x]
            z=spread/sd
            # Round-trip spread, signed fees, and an extra slippage allowance.
            cost=(books[y].spread+2*max(0,economics[y].taker)+.02)/books[y].mid
            cost+=beta*(books[x].spread+2*max(0,economics[x].taker)+.02)/books[x].mid
            self.diagnostics.append(dict(x=x,y=y,z=z,beta=beta,phi=ar[1],cost=cost))
            if not 2.25<=abs(z)<=3.5 or abs(spread)<1.8*cost: continue
            direction=-1 if z>0 else 1
            qy=max(1,int(c.pair_dollars/books[y].mid))
            qx=max(1,int(qy*beta*books[y].mid/books[x].mid))
            if max(qx,qy)>c.max_order: continue
            choices.append(dict(x=x,y=y,a=a,beta=beta,sd=sd,z=z,
                                targets={y:direction*qy,x:-direction*qx},score=abs(spread)/cost))
        return sorted(choices,key=lambda v:v['score'],reverse=True)


def marketable(ticker,position_delta,book,c,mode='reduce',score=1e6):
    action='BUY' if position_delta>0 else 'SELL'
    p=book.ask+.02 if position_delta>0 else book.bid-.02
    levels=book.asks if position_delta>0 else book.bids
    visible=sum(q for price,q in levels if (price<=p if position_delta>0 else price>=p))
    size=min(abs(position_delta),c.max_order,visible)
    return Quote(ticker,action,size,round(max(.01,p),2),mode,score) if size else None


class Strategy:
    def __init__(self,c):
        self.c=c; self.signals={t:Signal() for t in TICKERS}; self.pairs=Pairs()
        self.exit_sign={t:0 for t in TICKERS}
        self.markouts={t:deque(maxlen=40) for t in TICKERS}
        self.pending_marks=[]
        self.last_plan={}

    def fill(self,ticker,action,quantity,mid,tick):
        self.pending_marks.append((tick,ticker,1 if action=='BUY' else -1,quantity,mid))

    def update(self,tick,books):
        for t,b in books.items(): self.signals[t].update(tick,b.mid)
        self.pairs.update(tick,books)
        remain=[]
        for when,t,sign,q,mid in self.pending_marks:
            if tick-when>=2:
                if tick-when<=4: self.markouts[t].append((sign*(books[t].mid-mid),q))
            else: remain.append((when,t,sign,q,mid))
        self.pending_marks=remain

    def quotes(self,tick,duration,positions,books,economics,reduce_all=False,blocked=()):
        c=self.c; output=[]; self.last_plan={}
        left=duration-tick
        for t in TICKERS:
            if t in blocked: continue
            b=books[t]; q=positions[t]; s=self.signals[t]; e=economics[t]
            if q*self.exit_sign[t]<=0: self.exit_sign[t]=0
            against=s.direction*q<0
            if q and (against or reduce_all or (c.settlement=='flatten' and left<=10)):
                self.exit_sign[t]=1 if q>0 else -1
            if self.exit_sign[t]:
                quote=marketable(t,-q,b,c)
                if quote: output.append(quote)
                self.last_plan[t]={'mode':'reduce','direction':s.direction,'target':0}
                continue
            if reduce_all or left<=2: continue
            cap=min(c.asset_cap,int(c.asset_dollars/b.mid))
            target=0
            warm=len(s.history)>=c.warmup
            if c.trend and warm and s.efficiency>.35 and s.direction:
                strength=min(1,abs(s.alpha)/max(.01,s.sigma))
                target=int(s.direction*min(c.trend_shares,c.trend_dollars/b.mid)*strength)
            if c.settlement=='flatten': target=int(target*min(1,max(0,left-10)/30))
            self.last_plan[t]={'mode':'trend' if target else 'maker','direction':s.direction,'target':target,'alpha':s.alpha}
            delta=target-q
            if target and delta*s.direction>0 and abs(delta)>=100:
                hurdle=b.spread/2+2*max(0,e.taker)+.01+s.sigma*.3
                if abs(s.alpha)>hurdle:
                    quote=marketable(t,(1 if delta>0 else -1)*min(abs(delta),c.quote_size),b,c,'trend',1000+abs(s.alpha))
                    if quote: output.append(quote)
                    continue
            if not c.market_make: continue
            observed=self.markouts[t]
            adverse=max(0,-sum(v*n for v,n in observed)/sum(n for _,n in observed)) if observed else .3*s.sigma
            skew=(q-target)/max(1,min(cap,2500))*max(.015,s.sigma*2)
            center=b.mid+max(-b.spread,min(b.spread,.3*s.alpha))-skew
            for side,sign,best in (('BUY',1,b.bid),('SELL',-1,b.ask)):
                capacity=min(cap-sign*q,c.quote_size)
                if s.direction*sign<0 or (c.settlement=='flatten' and left<30): capacity=min(capacity,max(0,-sign*q))
                if capacity<=0: continue
                candidates=[]
                for distance in (-1,0,1,2):
                    p=round(best-sign*distance*.01,2)
                    if p<=0 or (p>=b.ask if sign>0 else p<=b.bid): continue
                    edge=sign*(center-p)+e.maker-adverse
                    if edge<.001: continue
                    levels=b.bids if sign>0 else b.asks
                    ahead=sum(v for px,v in levels if (px>=p if sign>0 else px<=p))
                    speed=math.exp(-.7*max(0,distance))/(1+ahead/5000)
                    size=min(capacity,max(100,int(c.quote_size*min(1,edge/.02))))
                    candidates.append(Quote(t,side,size,p,'maker',edge*size*speed))
                if candidates: output.append(max(candidates,key=lambda z:z.score))
        return sorted(output,key=lambda z:z.score,reverse=True)
