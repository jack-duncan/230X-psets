# [Yesterday 5:31 p.m.] Lina Hijazi
import requests
from time import sleep
import signal
import sys

s = requests.Session()


s.headers.update({'X-API-key': 'coolboy'})


SIMULATION_LENGTH = 300
GROSS_LIMIT = 25000
MAX_VOLUME = 5000
MAX_ORDERS = int(GROSS_LIMIT / MAX_VOLUME)
TICKER_1 = 'AC'
TICKER_2 = 'RY'
TICKER_3 = 'CNR'
Sleep_one = 0.3
Sleep_two = 0.3

def get_tick_status():
    resp = s.get('http://localhost:9999/v1/case')
    if resp.ok:
        case = resp.json()
        return case['tick'], case['status']

def get_bid_ask(ticker):
    payload = {'ticker': ticker}
    resp = s.get('http://localhost:9999/v1/securities/book', params = payload)
    if resp.ok:
        book = resp.json()
        bid_book = book['bids']
        ask_book = book['asks']
        bid_price_fn = bid_book[0]['price']
        ask_price_fn = ask_book[0]['price']
        return bid_price_fn, ask_price_fn

def get_position(ticker):
    payload = {'ticker': ticker}
    resp = s.get('http://localhost:9999/v1/securities', params = payload)
    if resp.ok:
        securities = resp.json()
        current_position_fn = securities[0]['position']
        current_position_cost_fn = securities[0]['vwap']
        return current_position_fn, current_position_cost_fn

def live_orders():
    resp = s.get('http://localhost:9999/v1/orders?status=OPEN')
    if resp.ok:
        buys = {}
        sells = {}
        buyprice = []
        sellprice = []
        open_orders = resp.json()
        for order in open_orders:
            ticker = order['ticker']
            open_vol = order['quantity']
            buyprice = order['price']
            sellprice = order['price']
            order_id = order['order_id']
            if order['action'] == 'SELL':
                if ticker not in sells:
                    sells[ticker] = [sellprice, open_vol, order_id]
                else:
                    sells[ticker][1] +=  open_vol
                    sells[ticker] += [order_id]
            if order['action'] == 'BUY':
                if ticker not in buys:
                    buys[ticker] = [buyprice, open_vol, order_id]
                else:
                    buys[ticker][1] +=  open_vol
                    buys[ticker] += [order_id]
        return buys, sells, buyprice, sellprice

def get_live_ids(buys, sells):
    buy_ids = []
    sell_ids = []
    for open_tickers in buys:
        for i in range(len(buys[open_tickers]) - 2):
            buy_ids.append(buys[open_tickers][i + 2])
    for open_tickers in sells:
        for i in range(len(sells[open_tickers]) - 2):
            sell_ids.append(sells[open_tickers][i + 2])
    return buy_ids, sell_ids

def trade_shares(ticker, bid, ask):
    for i in range(MAX_ORDERS):
        s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'quantity': MAX_VOLUME, 'price': bid, 'action': 'BUY'})
        s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'quantity': MAX_VOLUME, 'price': ask, 'action': 'SELL'})

def sell_shares(ticker, bid, ask):
    for i in range(MAX_ORDERS):
        s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'quantity': MAX_VOLUME, 'price': ask, 'action': 'SELL'})

def buy_shares(ticker, bid, ask):
    for i in range(MAX_ORDERS):
        s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'quantity': MAX_VOLUME, 'price': bid, 'action': 'BUY'})

def reverse_position(ticker, direction):
    bid, ask = get_bid_ask(ticker)
    if direction == 'SELL':
        s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'quantity': MAX_VOLUME, 'price': ask, 'action': 'SELL'})
    elif direction == 'BUY':
        s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'quantity': MAX_VOLUME, 'price': bid, 'action': 'BUY'})

def cancel_orders():
    s.post('http://localhost:9999/v1/commands/cancel', params = {'ticker': TICKER_1})
    s.post('http://localhost:9999/v1/commands/cancel', params = {'ticker': TICKER_2})
    s.post('http://localhost:9999/v1/commands/cancel', params = {'ticker': TICKER_3})

def get_tick(session):
    resp = session.get('http://localhost:9999/v1/case')
    if resp.ok:
        case = resp.json()
        return case['tick']

def main():

    tick, status = get_tick_status()
    while status == 'ACTIVE':
        ticker_1_position, ticker_1_cost = get_position(TICKER_1)
        ticker_2_position, ticker_2_cost = get_position(TICKER_2)
        ticker_3_position, ticker_3_cost = get_position(TICKER_3)

        gross_position = abs(ticker_1_position) + abs(ticker_2_position) + abs(ticker_3_position)
        buys, sells, buyprice, sellprice = live_orders()

        ticker_1_bid, ticker_1_ask = get_bid_ask(TICKER_1)
        ticker_2_bid, ticker_2_ask = get_bid_ask(TICKER_2)
        ticker_3_bid, ticker_3_ask = get_bid_ask(TICKER_3)

        ticker_1_spread = ticker_1_ask - ticker_1_bid
        ticker_2_spread = ticker_2_ask - ticker_2_bid
        ticker_3_spread = ticker_3_ask - ticker_3_bid
        fattest_spread = max(ticker_1_spread, ticker_2_spread, ticker_3_spread)

        if ticker_1_spread == fattest_spread:
            trade_ticker, trade_bid, trade_ask = TICKER_1, ticker_1_bid, ticker_1_ask
        elif ticker_2_spread == fattest_spread:
            trade_ticker, trade_bid, trade_ask = TICKER_2, ticker_2_bid, ticker_2_ask
        elif ticker_3_spread == fattest_spread:
            trade_ticker, trade_bid, trade_ask = TICKER_3, ticker_3_bid, ticker_3_ask

        print(buys)
        print(sells)
        print(buyprice)
        print(sellprice)
        if(len(sells) == 0 and len(buys) == 0 and gross_position == 0):
            trade_shares(trade_ticker, trade_bid, trade_ask)
            sleep(Sleep_one)

        else:
            buy_ids, sell_ids = get_live_ids(buys, sells)


            ticker_1_passive_buy = False
            ticker_2_passive_buy = False
            ticker_3_passive_buy = False
            ticker_1_passive_sell = False
            ticker_2_passive_sell = False
            ticker_3_passive_sell = False
            print(buy_ids)
            print(sell_ids)

            if (len(buys) != 0):
                if TICKER_1 in buys:
                    ticker_1_passive_buy = True
                if TICKER_2 in buys:
                    ticker_2_passive_buy = True
                if TICKER_3 in buys:
                    ticker_3_passive_buy = True

            if (len(sells) != 0):
                if TICKER_1 in sells:
                    ticker_1_passive_sell = True
                if TICKER_2 in sells:
                    ticker_2_passive_sell = True
                if TICKER_3 in sells:
                    ticker_3_passive_sell = True


        position_held_time = []
        resp = s.get('http://localhost:9999/v1/orders?status=OPEN')
        if resp.ok:
            open_orders = resp.json()
            for order in open_orders:
                position_held_time.append(order['tick'])

        #successful practice run with no position limit violations or stalls. p&l = 150k saved performance report in email.
        if len(sells) != 0 and len(position_held_time) > 1 and tick - max(position_held_time) > 10 \
                and ((ticker_1_position == 25000 and ticker_1_ask < (sellprice + 0.15)) \
                     or (ticker_2_position == 25000 and ticker_2_ask < (sellprice +  0.1)) \
                     or (ticker_3_position == 25000 and ticker_3_ask < (sellprice + 0.03))):
            cancel_orders()
            sleep(Sleep_two)

            print("Cancelled Sells")

            if ticker_1_position == 25000:
                sell_shares(TICKER_1, ticker_1_bid, ticker_1_ask)
                sleep(Sleep_one)
            elif ticker_2_position == 25000:
                sell_shares(TICKER_2, ticker_2_bid, ticker_2_ask)
                sleep(Sleep_one)
            elif ticker_3_position == 25000:
                sell_shares(TICKER_3, ticker_3_bid, ticker_3_ask)
                sleep(Sleep_one)

        if len(buys) != 0 and len(position_held_time) > 1 and tick - max(position_held_time) > 10 \
                and ((ticker_1_position == -25000 and ticker_1_bid > (buyprice + 0.15)) \
                     or (ticker_2_position == -25000 and ticker_2_bid > (buyprice +  0.1)) \
                     or (ticker_3_position == -25000 and ticker_3_bid > (buyprice + 0.03))):
            cancel_orders()
            sleep(Sleep_two)

            print("Cancelled Buys")

            if ticker_1_position == -25000:
                buy_shares(TICKER_1, ticker_1_bid, ticker_1_ask)
                sleep(Sleep_one)
            elif ticker_2_position == -25000:
                buy_shares(TICKER_2, ticker_2_bid, ticker_2_ask)
                sleep(Sleep_one)
            elif ticker_3_position == -25000:
                buy_shares(TICKER_3, ticker_3_bid, ticker_3_ask)
                sleep(Sleep_one)

        #print(len(buys), len(sells))
        print_out(tick, ticker_1_bid, ticker_1_ask, ticker_2_bid, ticker_2_ask, ticker_3_bid, ticker_3_ask, ticker_1_position, ticker_2_position, ticker_3_position, gross_position)
        #cancel_orders()
        tick, status = get_tick_status()


def print_out(tick, ticker_1_bid, ticker_1_ask, ticker_2_bid, ticker_2_ask, ticker_3_bid, ticker_3_ask, ticker_1_position, ticker_2_position, ticker_3_position, total_position):
    print('-------------------------------------------------')
    print('Tick:                     ', tick, '- (', round((tick/SIMULATION_LENGTH)*100, 1), '% Complete)')
    print('AC Bid/Ask/Spread:        ', ticker_1_bid, '/', ticker_1_ask, '/', round(ticker_1_ask - ticker_1_bid, 2))
    print('RY Bid/Ask/Spread:       ', ticker_2_bid, '/', ticker_2_ask, '/', round(ticker_2_ask - ticker_2_bid, 2))
    print('CNR Bid/Ask/Spread:       ', ticker_3_bid, '/', ticker_3_ask, '/', round(ticker_3_ask - ticker_3_bid, 2))
    print('Shares - AC/RY/CNR/Gross:', round(ticker_1_position), '/', round(ticker_2_position), '/', round(ticker_3_position), '/', round(total_position))
    print('-------------------------------------------------')
    print('')

if __name__ == '__main__': # convenience to make it easier to run the code
    main()

