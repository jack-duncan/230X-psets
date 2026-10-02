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
        spreads = {}
        for ticker in tickers:
            position = get_position(ticker)
            positions[ticker] = position
            spreads[ticker] = get_bid_ask_spread(ticker)
        total_position = 0
        for key in positions:
            total_position += positions[key]
        best_spread = max(spreads, key=spreads.get)
        print(f'the best spread is {best_spread} at {spreads[best_spread]}')
        bid, ask = get_bid_ask(best_spread)
        if spreads[best_spread] > 0.03:
            if abs(positions[best_spread]) <= 12000:
                if positions[best_spread] >= 0:
                    s.post('http://localhost:9999/v1/orders', params = {'ticker': best_spread, 'type': 'LIMIT', 'quantity': MAX_VOLUME/2, 'price': bid + 0.01, 'action': 'BUY'})
                    s.post('http://localhost:9999/v1/orders', params = {'ticker': best_spread, 'type': 'LIMIT', 'quantity': MAX_VOLUME, 'price': ask - 0.01, 'action': 'SELL'})
                    sleep(0.2)
                    s.post('http://localhost:9999/v1/commands/cancel', params = {'all': 1})
                elif positions[best_spread] < 0:
                    s.post('http://localhost:9999/v1/orders', params = {'ticker': best_spread, 'type': 'LIMIT', 'quantity': MAX_VOLUME, 'price': bid + 0.01, 'action': 'BUY'})
                    s.post('http://localhost:9999/v1/orders', params = {'ticker': best_spread, 'type': 'LIMIT', 'quantity': MAX_VOLUME/2, 'price': ask - 0.01, 'action': 'SELL'})
                    sleep(0.2)
                    s.post('http://localhost:9999/v1/commands/cancel', params = {'all': 1})

            elif abs(positions[best_spread]) > 0:
                s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'MARKET', 'quantity': 2000, 'price': ask - 0.01, 'action': 'SELL'})
            else:
                s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'MARKET', 'quantity': 2000, 'price': bid + 0.01, 'action': 'BUY'})


if __name__ == '__main__':  # convenience to make it easier to run the code
    main()
