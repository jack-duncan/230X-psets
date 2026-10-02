# -*- coding: utf-8 -*-
"""
Created on Sun Mar 21 14:34:14 2021

@author: Youssef
"""

# This is a python example RYorithm using REST API for the RIT RYO2 Case
import signal
import requests
from time import sleep
import sys

# This class definition allows us to print error messages and stop the program
class ApiException(Exception):
    pass

# This signal handler allows for a graceful shutdown when CTRL + C is pressed
def signal_handler(signum, frame):
    global shutdown
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    shutdown = True

# Set your API key to authenticate to the RTI client
API_KEY = {'X-API-Key': 'coolboy'}
shutdown = False

#SETTINGS
SPEEDBUMP = 0.5
# Maximum number of shares
MAX_VOLUME = 5000
# Maximum number of orders we can submit
MAX_ORDERS = 1
#  Max desirable order size
ORDER_LIMIT = 5000

# Attempt at limiting exposure
MAX_EXPOSURE = 20000
MAX_SHORT_EXPOSURE = -20000
MAX_LONG_EXPOSURE = 20000

# This helper method returns the current 'tick' of the running case
def get_tick(session):
    resp = session.get('http://localhost:9999/v1/case')
    if resp.ok:
        case = resp.json()
        return case['tick'], case['status']
    raise ApiException('Authorization error Please check API key.')

# This helper method returns the bid and ask first row for a given security
def ticker_bid_ask(session, ticker):
    payload = {'ticker': ticker}
    resp = session.get('http://localhost:9999/v1/securities/book', params=payload)
    if resp.ok:
        book = resp.json()
        return book['bids'][0]['price'],book['asks'][0]['price']
    raise ApiException('Authorization error Please check API key.')

def first_spread(session):
    payload = {'ticker': 'CNR'}
    resp = session.get('http://localhost:9999/v1/securities/book', params=payload)
    if resp.ok:
        book = resp.json()
        CNR_bid = book['bids'][0]['price']
        CNR_ask = book['asks'][0]['price']
        CNR_spread = CNR_ask - CNR_bid
        return CNR_spread
    raise ApiException('Authorization error Please check API key.')

def second_spread(session):
    payload = {'ticker': 'RY'}
    resp = session.get('http://localhost:9999/v1/securities/book', params=payload)
    if resp.ok:
        book = resp.json()
        RY_bid = book['bids'][0]['price']
        RY_ask = book['asks'][0]['price']
        RY_spread = RY_ask - RY_bid
        return RY_spread
    raise ApiException('Authorization error Please check API key.')

def third_spread(session):
    payload = {'ticker': 'AC'}
    resp = session.get('http://localhost:9999/v1/securities/book', params=payload)
    if resp.ok:
        book = resp.json()
        AC_bid = book['bids'][0]['price']
        AC_ask = book['asks'][0]['price']
        AC_spread = AC_ask - AC_bid
        return AC_spread
    raise ApiException('Authorization error Please check API key.')

def get_position(session):
    resp = session.get('http://localhost:9999/v1/securities')
    if resp.ok:
        book = resp.json()
        gross_position = abs(book[0]['position']) + abs(book[1]['position']) + abs(book[2]['position'])
        net_position = book[0]['position'] + book[1]['position'] + book[2]['position']
        CNR_position = book[0]['position']
        RY_position = book[1]['position']
        AC_position = book[2]['position']
        return gross_position, net_position, CNR_position, RY_position, AC_position
    raise ApiException('Authorization error Please check API key.')

# This helper method returns information about all the open sell orders
def open_sells(session):
    resp = session.get('http://localhost:9999/v1/orders?status=OPEN')
    if resp.ok:
        open_sells_volume = 0 # Total combined volume of all open sells
        ids = []             # All open sell ids
        prices = []          # All open sell volumes
        order_volumes = []   # All open sell volumes
        volume_filled = []   # Volume filled for each open sell order

        open_orders = resp.json()
        for order in open_orders:
            if order['action'] == 'SELL':
                volume_filled.append(order['quantity_filled'])
                order_volumes.append(order['quantity'])
                open_sells_volume = open_sells_volume + order['quantity']
                prices.append(order['price'])
                ids.append(order['order_id'])
    return volume_filled, open_sells_volume, ids, prices, order_volumes

# This helper method returns information about all open buy orders
def open_buys(session):
    resp = session.get('http://localhost:9999/v1/orders?status=OPEN')
    if resp.ok:
        open_buys_volume = 0 # Total combined volume of all open buys
        ids = []             # All open buy ids
        prices = []          # All open buy prices
        order_volumes = []   # All open buy volumes
        volume_filled = []   # Volume filled of each open buy order

        open_orders = resp.json()
        for order in open_orders:
            if order['action'] == 'BUY':
                open_buys_volume = open_buys_volume + order['quantity']
                volume_filled.append(order['quantity_filled'])
                order_volumes.append(order['quantity'])
                prices.append(order['price'])
                ids.append(order['order_id'])

    return volume_filled, open_buys_volume, ids, prices, order_volumes

# This helper method will buy and sell the maximum number of shares
def buy_sell(session, sell_price, buy_price, ticker):
    for i in range(MAX_ORDERS):
        session.post('http://localhost:9999/v1/orders', params = {'ticker': ticker,
        'type': 'LIMIT', 'quantity': ORDER_LIMIT, 'price': sell_price, 'action': 'SELL'})
        session.post('http://localhost:9999/v1/orders', params = {'ticker': ticker,
        'type': 'LIMIT', 'quantity': ORDER_LIMIT, 'price': buy_price, 'action': 'BUY'})

def SUKHCHARHAT():
    # Instantiate variables about all the open buy orders
    buy_ids = []                # Order ids
    buy_prices = []             # Order prices
    buy_volumes = []            # Order volumes
    volume_filled_buys = []     # Amount of volume filled for each order
    open_buys_volume = 0        # Combined volume from all open buy orders

    #Instatiate viarables about all the open sell orders
    sell_ids = []
    sell_prices = []
    sell_volumes = []
    volume_filled_sells = []
    open_sells_volume = 0

    # Instantiated variables when just one side of the book has been completely filled
    single_side_filled = False
    single_side_transaction_time = 0

    # Creates a session to manage connections and requests to the RIT Client
    with requests.Session() as s:
        s.headers.update(API_KEY)
        tick, status = get_tick(s)
        ticker_list = ['CNR','RY','AC']

        # While the case is active, do the following
        while status == "ACTIVE" and not shutdown:
            for i in range(3):
                # Update information about the case
                ticker = ticker_list[i]
                volume_filled_sells, open_sells_volume, sell_ids, sell_prices, sell_volumes = open_sells(s)
                volume_filled_buys, open_buys_volume, buy_ids, buy_prices, buy_volumes = open_buys(s)
                bid_price, ask_price = ticker_bid_ask(s, ticker)
                gross_position, net_position, CNR_position, RY_position, AC_position = get_position(s)
                CNR_spread = first_spread(s)
                RY_spread = second_spread(s)
                AC_spread = third_spread(s)

                # We're stuck with a position
            if(gross_position >= MAX_EXPOSURE):

                gross_position, net_position, CNR_position, RY_position, AC_position = get_position(s)

                if(CNR_position > 0):
                    s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': 'CNR', 'type': 'MARKET', 'action': 'SELL', 'quantity': MAX_VOLUME})
                if(CNR_position < 0):
                    s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': 'CNR', 'type': 'MARKET', 'action': 'BUY', 'quantity': MAX_VOLUME})
                if(RY_position > 0):
                    s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': 'RY', 'type': 'MARKET', 'action': 'SELL', 'quantity': MAX_VOLUME})
                if(RY_position < 0):
                    s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': 'RY', 'type': 'MARKET', 'action': 'BUY', 'quantity': MAX_VOLUME})
                if(AC_position > 0):
                    s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': 'AC', 'type': 'MARKET', 'action': 'SELL', 'quantity': MAX_VOLUME})
                if(AC_position < 0):
                    s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': 'AC', 'type': 'MARKET', 'action': 'BUY', 'quantity': MAX_VOLUME})

                sleep(SPEEDBUMP)

#                s.post('http://localhost:9999/v1/commands/cancel', params = {'ticker': ticker})
#                sleep(SPEEDBUMP)

                gross_position, net_position, CNR_position, RY_position, AC_position = get_position(s)

            if(gross_position < MAX_EXPOSURE and net_position < MAX_LONG_EXPOSURE and net_position > MAX_SHORT_EXPOSURE):

                # Check if you have 0 open orders
                if(open_sells_volume == 0 and open_buys_volume == 0):
                    # Both sides are filled now
                    single_side_filled = False

                    # Calculate the spread between the bid and ask prices
                    bid_ask_spread = ask_price - bid_price

                    # Set the prices
                    sell_price = ask_price - 0.01 # Trying to be more competitive
                    buy_price = bid_price + 0.01 # Trying to be more competitive

                    # The calculated spread is greater or equal to our set spread
                    if CNR_spread > RY_spread and CNR_spread > AC_spread:
                        if (bid_ask_spread >= 0.30):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.20):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.10):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.05):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.04):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.03):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.02):
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': bid_price + 0.005, 'action': 'BUY', 'quantity': ORDER_LIMIT})
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': ask_price - 0.005, 'action': 'SELL', 'quantity': ORDER_LIMIT})
                        if (bid_ask_spread >= 0.01):
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': bid_price + 0.0025, 'action': 'BUY', 'quantity': ORDER_LIMIT})
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': ask_price - 0.0025, 'action': 'SELL', 'quantity': ORDER_LIMIT})
                        if (bid_ask_spread >= 0.002): #Minimum possible point at which it's worth trading
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': bid_price + 0.0005, 'action': 'BUY', 'quantity': ORDER_LIMIT})
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': ask_price - 0.0005, 'action': 'SELL', 'quantity': ORDER_LIMIT})

                        sleep(SPEEDBUMP)

                    if RY_spread > CNR_spread and RY_spread > AC_spread:
                        if (bid_ask_spread >= 0.30):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.20):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.10):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.05):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.04):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.03):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.08):
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': bid_price + 0.005, 'action': 'BUY', 'quantity': ORDER_LIMIT})
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': ask_price - 0.005, 'action': 'SELL', 'quantity': ORDER_LIMIT})
                        if (bid_ask_spread >= 0.06):
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': bid_price + 0.0025, 'action': 'BUY', 'quantity': ORDER_LIMIT})
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': ask_price - 0.0025, 'action': 'SELL', 'quantity': ORDER_LIMIT})
                        if (bid_ask_spread >= 0.02): #Minimum possible point at which it's worth trading
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': bid_price + 0.000275, 'action': 'BUY', 'quantity': ORDER_LIMIT})
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': ask_price - 0.000275, 'action': 'SELL', 'quantity': ORDER_LIMIT})

                        sleep(SPEEDBUMP)

                    if AC_spread > CNR_spread and AC_spread > RY_spread:
                        if (bid_ask_spread >= 0.30):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.20):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.10):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.05):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.04):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.03):
                            buy_sell(s, sell_price, buy_price, ticker)
                        if (bid_ask_spread >= 0.08):
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': bid_price + 0.005, 'action': 'BUY', 'quantity': ORDER_LIMIT})
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': ask_price - 0.005, 'action': 'SELL', 'quantity': ORDER_LIMIT})
                        if (bid_ask_spread >= 0.06):
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': bid_price + 0.0025, 'action': 'BUY', 'quantity': ORDER_LIMIT})
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': ask_price - 0.0025, 'action': 'SELL', 'quantity': ORDER_LIMIT})
                        if (bid_ask_spread >= 0.02): #Minimum possible point at which it's worth trading
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': bid_price + 0.000025, 'action': 'BUY', 'quantity': ORDER_LIMIT})
                            s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'price': ask_price - 0.000025, 'action': 'SELL', 'quantity': ORDER_LIMIT})

                        sleep(SPEEDBUMP)

                # There are outstanding open orders
                else:
                    if(not single_side_filled and (open_buys_volume == 0 or open_sells_volume == 0)):
                        single_side_filled = True
                        single_side_transaction_time = tick

                    # Ask side has been completely filled and;
                    # It's been more than 1.5 seconds since a single side has been completely filled
                    if(open_sells_volume == 0 and (tick - single_side_transaction_time >= 1.5)):
#                        s.post('http://localhost:9999/v1/commands/cancel', params = {'ticker': ticker})
                        s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'MARKET', 'action': 'BUY', 'quantity': ORDER_LIMIT})
                        sleep(SPEEDBUMP)

                    # Bid side has been completely filled and;
                    # It's been more than 1.5 seconds since a single side has been completely filled
                    if(open_buys_volume == 0 and (tick - single_side_transaction_time >= 1.5)):
#                        s.post('http://localhost:9999/v1/commands/cancel', params = {'ticker': ticker})
                        s.post('http://localhost:9999/v1/commands/orders', params = {'ticker': ticker, 'type': 'MARKET', 'action': 'SELL', 'quantity': ORDER_LIMIT})
                        sleep(SPEEDBUMP)

                    s.post('http://localhost:9999/v1/commands/cancel', params = {'ticker': ticker})
                    sleep(SPEEDBUMP)

            # Refresh the case time. THIS IS IMPORTANT FOR THE WHILE LOOP
            tick, status = get_tick(s)

if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    SUKHCHARHAT()
