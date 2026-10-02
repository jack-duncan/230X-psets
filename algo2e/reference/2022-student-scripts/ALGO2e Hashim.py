import requests
from time import sleep
import signal
import sys

s = requests.Session()

s.headers.update({'X-API-key': 'coolboy770'})

MAX_VOLUME = 5000
tickers = ['AC', 'RY', 'CNR']


def get_status():
    resp = s.get('http://localhost:9999/v1/case')
    if resp.ok:
        case = resp.json()
        return case['status']


def get_bid_ask(ticker):
    payload = {'ticker': ticker}
    resp = s.get('http://localhost:9999/v1/securities/book', params=payload)
    if resp.ok:
        book = resp.json()
        bid_price_fn = book['bids'][0]['price']
        ask_price_fn = book['asks'][0]['price']
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
    status = get_status()
    while status == "ACTIVE":
        positions = {}
        spreads = {}
        for ticker in tickers:
            position = get_position(ticker)
            positions[ticker] = position
            spreads[ticker] = get_bid_ask_spread(ticker)
            bid, ask = get_bid_ask(ticker)
            spread = get_bid_ask_spread(ticker)
            if spread > 0.04:
                bid += 0.01
                ask -= 0.01
                loops = 2
                if spread > 0.06:
                    bid += 0.01
                    ask -= 0.01
                    loops = 2
                if spread > 0.08:
                    # bid += 0.01
                    # ask -= 0.01
                    loops = 3
                if abs(position) <= 12000:
                    if position >= 0:
                        for i in range(loops):
                            s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'quantity': MAX_VOLUME/2, 'price': bid, 'action': 'BUY'})
                            s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'quantity': MAX_VOLUME, 'price': ask, 'action': 'SELL'})
                    else:
                        for i in range(loops):
                            s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'quantity': MAX_VOLUME, 'price': bid, 'action': 'BUY'})
                            s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'LIMIT', 'quantity': MAX_VOLUME/2, 'price': ask, 'action': 'SELL'})
                elif position > 0:
                    s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'MARKET', 'quantity': 2000, 'price': ask - 0.01, 'action': 'SELL'})
                else:
                    s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'MARKET', 'quantity': 2000, 'price': bid + 0.01, 'action': 'BUY'})
        sleep(0.3)
        s.post('http://localhost:9999/v1/commands/cancel', params = {'all': 1})


if __name__ == '__main__':
    main()
