# -*- coding: utf-8 -*-
"""
Created on Sun Feb  7 00:46:52 2021

@author: tariq
"""

import signal
import requests
from time import sleep
import sys
s = requests.session()
s.headers.update({'X-API-key': 'GFLMBM2C'}) 
class APiException(Exception):
    pass

def signal_handler(signum, frame):
    global shutdown
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    shutdown = True
 
API_KEY = {'X-API-Key': 'GFLMBM2C'}
shutdown = False
SPEEDBUMP = 0.5
MAX_VOLUME = 5000
MAX_ORDERS = 5
SPREAD = 0.05
MAX_LONG_EXPOSURE = 20000
MAX_SHORT_EXPOSURE = -20000
ORDER_LIMIT = 200

def get_tick(session):
    resp = session.get('http://localhost:9999/v1/case')
    if resp.ok:
        case = resp.json()
        return case['tick']
    raise APiException('Authorization error Please check API key.')
    
def ticker_bid_ask(session, ticker):
    payload = {'ticker': ticker}
    resp = session.get('http://localhost:9999/v1/securities/book', params=payload)
    if resp.ok:
        book = resp.json()
        return book['bids'][0]['price'], book['asks'][0]['price']
    raise APiException(' Authorization error Please check API key.')
    
def get_position():
    resp = s.get ('http://localhost:9999/v1/securities')
    if resp.ok:
        book = resp.json()
        return abs(book[0]['position']) + abs(book[1]['position']) + abs(book[2]['position'])
    
def open_sells(session):
    resp = session.get('http://localhost:9999/v1/orders?status=OPEN')
    if resp.ok:
        open_sells_volume = 0
        ids = []
        prices = []
        order_volumes = []
        volume_filled = []
        
        open_orders = resp.json()
        for order in open_orders:
            if order['action'] == 'SELL':
                volume_filled.append(order['quantity_filled'])
                order_volumes.append(order['quantity'])
                open_sells_volume = open_sells_volume + order['quantity']
                prices.append(order['price'])
                ids.append(order['order_id'])
    return volume_filled, open_sells_volume, ids, prices, order_volumes

def open_buys(session):
    resp = session.get('http://localhost:9999/v1/orders?status=OPEN')
    if resp.ok:
        open_buys_volume = 0
        ids = []
        prices = []
        order_volumes = []
        volume_filled = []
        
        open_orders = resp.json()
        for order in open_orders:
            if order['action'] == 'BUY':
                open_buys_volume = open_buys_volume + order['quantity']
                volume_filled.append(order['quantity_filled'])
                order_volumes.append(order['quantity'])
                prices.append(order['price'])
                ids.append(order['order_id'])
                
    return volume_filled, open_buys_volume, ids, prices, order_volumes

def get_bid_ask(ticker):
    payload = {'ticker': ticker}
    resp = s.get ('http://localhost:9999/v1/securities/book', params = payload)
    if resp.ok:
        book = resp.json()
        bid_side_book = book['bids']
        ask_side_book = book['asks']
        
        bid_prices_book = [item["price"] for item in bid_side_book]
        ask_prices_book = [item['price'] for item in ask_side_book]
        
        best_bid_price = bid_prices_book[0]
        best_ask_price = ask_prices_book[0]
  
        return best_bid_price, best_ask_price

def buy_sell(session, sell_price, buy_price):
    for i in range(MAX_ORDERS):
        session.post('http://local.9999/v1/orders', params = {'ticker': 'ALGO' , 'type': 'LIMIT', 'quantity': MAX_VOLUME, 'price': sell_price, 'action': 'SELL'})
        session.post('http://localhost:9999/v1/orders', params = {'ticker': 'ALGO', 'type': 'LIMIT', 'quantity': MAX_VOLUME, 'price': buy_price, 'action': 'BUY'})
        
def re_order(session, number_of_orders, ids, volumes_filled, volumes, price, action):
    for i in range(number_of_orders):
        id = ids[i]
        volume = volumes[i]
        volume_filled = volumes_filled[i]
        if(volume_filled !=0):
            volume = MAX_VOLUME - volume_filled

        deleted = session.delete('http://localhost:9999/v1/orders/{}'.format(id))
        if(deleted.ok):
            session.post('http://localhost:9999/v1/orders', params = {'ticker': 'ALGO', 'type': 'LIMIT', 'quantity': volume, 'price': price, 'action': action})
def get_curr_position(i):
    resp = s.get ('http://localhost:9999/v1/securities')
    if resp.ok:
        book = resp.json()
        info = book[i]
        ret = info['position']
        return ret
def market_order(ticker, qty, buy_sell):
    resp = s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'MARKET', 'quantity': abs(qty), 'action': buy_sell})
    return resp

def main():
    tick, status = get_tick()
    ticker_list = ['CNR','AC','ALG']
    buy_ids = []
    buy_prices = []
    buy_volumes = []
    volume_filled_buys = []
    open_buys_volume = 0
    sell_ids = []
    sell_prices = []
    sell_volumes = []
    volume_filled_sells = []
    open_sells_volume = 0
    single_side_filled = False
    single_side_transaction_time = 0
    while status == 'ACTIVE':        

        for i in range(3):
            #i=1
            ticker_symbol = ticker_list[i]
            position = get_position()
            best_bid_price, best_ask_price = get_bid_ask(ticker_symbol)
            if position < MAX_LONG_EXPOSURE:
                resp = s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker_symbol, 'type': 'LIMIT', 'quantity': ORDER_LIMIT, 'price': best_bid_price*0.99, 'action': 'BUY'})
                #resp_fake = s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker_symbol, 'type': 'LIMIT', 'quantity': 1, 'price': best_bid_price*(1.5), 'action': 'BUY'})
            if position > MAX_SHORT_EXPOSURE:
                resp = s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker_symbol, 'type': 'LIMIT', 'quantity': ORDER_LIMIT, 'price': best_ask_price*1.01, 'action': 'SELL'})
                #resp_fake = s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker_symbol, 'type': 'LIMIT', 'quantity': 1, 'price': best_ask_price*(0.5), 'action': 'SELL'})
                sleep(0.011)
  
            indiv_exposure = get_curr_position(i)
            if indiv_exposure > 10000:
                s.post('http://localhost:9999/v1/commands/cancel', params = {'ticker': ticker_symbol})
                market_order(ticker_symbol, ORDER_LIMIT, "SELL")
            elif indiv_exposure < -10000:
                s.post('http://localhost:9999/v1/commands/cancel', params = {'ticker': ticker_symbol})
                market_order(ticker_symbol, ORDER_LIMIT, "BUY")
    with requests.Session() as s:
        s.headers.update(API_KEY)
        tick = get_tick(s)
        while tick > 5 and tick < 295 and not shutdown:
            volume_filled_sells, open_sells_volume, sell_ids, sell_prices, sell_volumes = open_sells(s)
            volume_filled_buys, open_buys_volume, buy_ids, buy_prices, buy_volumes = open_buys(s)
            bid_price, ask_price = ticker_bid_ask(s, 'ALGO')
            if(open_sells_volume == 0 and open_buys_volume == 0):
                single_side_filled = False
                bid_ask_spread = ask_price - bid_price
                sell_price = ask_price
                buy_price = bid_price
                if(bid_ask_spread >= SPREAD):
                    buy_sell(s, sell_price, buy_price)
                    sleep(SPEEDBUMP)
            else:
                if(not single_side_filled and (open_buys_volume == 0 or open_sells_volume == 0)):
                    single_side_filled = True
                    single_side_transaction_time = tick
                if(open_sells_volume == 0):
                    if(buy_price == bid_price):
                        continue
                    elif(tick - single_side_transaction_time >=3):
                        next_buy_price = bid_price + 0.01
                        potential_profit = sell_price - next_buy_price - 0.02
                        if(potential_profit >= 0.01 or tick - single_side_transaction_time >= 6):
                            action = 'BUY'
                            number_of_orders = len(buy_ids)
                            buy_price = bid_price + 0.01
                            price = buy_price
                            ids = buy_ids
                            volumes = buy_volumes
                            volume_filled = volume_filled_buys
                            re_order(s, number_of_orders, ids, volume_filled, volumes, price, action)
                            sleep(SPEEDBUMP)
        
                elif(open_buys_volume == 0):
                    if(sell_price == ask_price):
                
                        continue
                
                    elif(tick - single_side_transaction_time >=3):
                        next_sell_price = ask_price - 0.01
                        potential_profit = next_sell_price - buy_price - 0.02
                    while status == 'ACTIVE':        

                        tick = get_tick(s)    
    
if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    main()
    
                    