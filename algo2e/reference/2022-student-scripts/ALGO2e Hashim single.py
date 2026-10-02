import requests
from time import sleep
import signal
import sys
from collections import Counter

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

def dump(ticker):
    """
    Sets <ticker> position back to 0 no matter where it was before.
    """
    position = get_position(ticker)
    if position > 0:
        whole = int(position // 5000)
        remainder = int(position - (whole * 5000))
        for i in range(whole):
            resp = s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'MARKET', 'quantity': 5000, 'action': 'SELL'})
        resp = s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'MARKET', 'quantity': remainder, 'action': 'SELL'})
    elif position < 0:
        whole = int(-position // 10000)
        remainder = int(-position - (whole * 10000))
        for i in range(whole):
            resp = s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'MARKET', 'quantity': 5000, 'action': 'BUY'})
        resp = s.post('http://localhost:9999/v1/orders', params = {'ticker': ticker, 'type': 'MARKET', 'quantity': remainder, 'action': 'BUY'})


def main():
    status = get_status()
    accumulator = []
    winners = []
    length = 20
    while status == "ACTIVE":
        positions = {}
        spreads = {}
        for ticker in tickers:
            position = get_position(ticker)
            positions[ticker] = position
            spreads[ticker] = get_bid_ask_spread(ticker)
        best_spread = max(spreads, key=spreads.get)
        if len(accumulator) > 0:
           winner_old = winner
        if len(accumulator) < length:
            accumulator.append(best_spread)
        else:
            del accumulator[0]
            accumulator.append(best_spread)
        c = Counter(accumulator)
        print(c)
        winner = max(c, key=c.get)
        # winners.append(winner)
        if len(accumulator) > 1:
            if winner != winner_old:
                dump(winner_old)
        print(f'{winner} is best over last {length}')
        position = positions[winner]
        bid, ask = get_bid_ask(winner)
        if get_bid_ask_spread(winner) > 0.04:
            if abs(position) <= 18000:
                if position >= 0:
                    for i in range(3):
                        s.post('http://localhost:9999/v1/orders', params = {'ticker': winner, 'type': 'LIMIT', 'quantity': MAX_VOLUME/2, 'price': bid + 0.01, 'action': 'BUY'})
                        s.post('http://localhost:9999/v1/orders', params = {'ticker': winner, 'type': 'LIMIT', 'quantity': MAX_VOLUME, 'price': ask - 0.01, 'action': 'SELL'})
                else:
                    for i in range(3):
                        s.post('http://localhost:9999/v1/orders', params = {'ticker': winner, 'type': 'LIMIT', 'quantity': MAX_VOLUME, 'price': bid + 0.01, 'action': 'BUY'})
                        s.post('http://localhost:9999/v1/orders', params = {'ticker': winner, 'type': 'LIMIT', 'quantity': MAX_VOLUME/2, 'price': ask - 0.01, 'action': 'SELL'})
            elif position > 0:
                s.post('http://localhost:9999/v1/orders', params = {'ticker': winner, 'type': 'MARKET', 'quantity': 4000, 'price': ask - 0.01, 'action': 'SELL'})
            else:
                s.post('http://localhost:9999/v1/orders', params = {'ticker': winner, 'type': 'MARKET', 'quantity': 4000, 'price': bid + 0.01, 'action': 'BUY'})

        # print(f'the best spread is {best_spread} at {spreads[best_spread]}')
        sleep(0.5)
        s.post('http://localhost:9999/v1/commands/cancel', params = {'all': 1})


if __name__ == '__main__':
    main()
