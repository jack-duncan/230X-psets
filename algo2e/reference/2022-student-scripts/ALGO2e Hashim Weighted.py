import requests
from time import sleep
import signal
import sys

s = requests.Session()


s.headers.update({'X-API-key': 'coolboy770'})


SIMULATION_LENGTH = 300
GROSS_LIMIT = 25000
MAX_VOLUME = 5000
MAX_ORDERS = int(GROSS_LIMIT / MAX_VOLUME)
tickers = ['AC', 'RY', 'CNR']


def get_tick_status():
    resp = s.get('http://localhost:9999/v1/case')
    if resp.ok:
        case = resp.json()
        return case['tick'], case['status']


def get_bid_ask(ticker):
    payload = {'ticker': ticker}
    resp = s.get('http://localhost:9999/v1/securities/book', params=payload)
    if resp.ok:
        book = resp.json()
        bid_book = book['bids']
        ask_book = book['asks']
        bid_price_fn = bid_book[0]['price']
        ask_price_fn = ask_book[0]['price']
        return bid_price_fn, ask_price_fn


def get_bid_ask_spread(ticker):
    bid, ask = get_bid_ask(ticker)
    return ask - bid


def get_position(ticker):
    payload = {'ticker': ticker}
    resp = s.get('http://localhost:9999/v1/securities', params=payload)
    if resp.ok:
        securities = resp.json()
        current_position_fn = securities[0]['position']
        return current_position_fn


def main():
    tick, status = get_tick_status()
    while status == "ACTIVE":
        positions = {}
        positive_spreads = {}
        for ticker in tickers:
            position = get_position(ticker)
            positions[ticker] = position
            positive_spreads[ticker] = max(get_bid_ask_spread(ticker) - 0.01, 0)
        total_position = 0
        for key in positions:
            total_position += positions[key]
        total_spreads = 0
        for key in positive_spreads:
            total_spreads += positive_spreads[key]
        for ticker in tickers:
            weight = positive_spreads[ticker]/total_spreads
            bid, ask = get_bid_ask(ticker)
            if positive_spreads[ticker] > 0.01:
                if abs(positions[ticker]) <= 11000:
                    if positions[ticker] >= 0:
                        s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'quantity': (weight * MAX_VOLUME), 'price': bid, 'action': 'BUY'})
                        s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'quantity': 2*(weight * MAX_VOLUME), 'price': ask, 'action': 'SELL'})
                    elif positions[ticker] < 0:
                        s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'quantity': 2*(weight * MAX_VOLUME), 'price': bid, 'action': 'BUY'})
                        s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'quantity': (weight * MAX_VOLUME), 'price': ask, 'action': 'SELL'})
                elif positions[ticker] > 0:
                    s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'MARKET', 'quantity': 2000, 'price': ask - 0.01, 'action': 'SELL'})
                elif positions[ticker] < 0:
                    s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'MARKET', 'quantity': 2000, 'price': bid + 0.01, 'action': 'BUY'})
        sleep(0.2)
        s.post('http://localhost:9999/v1/commands/cancel', params = {'all': 1})


if __name__ == '__main__':  # convenience to make it easier to run the code
    main()
