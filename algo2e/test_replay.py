import unittest

import mm_algo2e as mm
import replay
from mm_algo2e import BUY, SELL


def row(bid, ask, bids, asks, last, volume):
    return {"bid": bid, "ask": ask, "bid_size": bids[0][1], "ask_size": asks[0][1], "last": last,
            "volume": volume, "bids": bids, "asks": asks}


def book(volume=0, last=20.00, bids=((2000, 1000), (1999, 500)), asks=((2002, 1000), (2003, 500))):
    return {"CNR": row(20.00, 20.02, list(bids), list(asks), last, volume),
            "RY": row(10.00, 10.02, [(1000, 1)], [(1002, 1)], 10.0, 0),
            "AC": row(5.00, 5.02, [(500, 1)], [(502, 1)], 5.0, 0)}


def api_at(rows):
    api = replay.FakeApi()
    api.advance({"rows": rows, "tick": 1})
    return api


class FillModel(unittest.TestCase):
    def test_queue_ahead_drains_before_we_fill(self):
        api = api_at(book(volume=0))
        res = api.place_limit("CNR", BUY, 500, 2000)  # joins behind 1000 at the best bid
        self.assertEqual(res["quantity_filled"], 0)
        api.advance({"rows": book(volume=1200, last=20.00), "tick": 2})
        self.assertEqual(api.pos["CNR"], 200)  # 1200 traded: 1000 ahead, 200 to us

    def test_order_inside_spread_has_no_queue(self):
        api = api_at(book(volume=0))
        api.place_limit("CNR", BUY, 500, 2001)
        api.advance({"rows": book(volume=300, last=20.00), "tick": 2})
        self.assertEqual(api.pos["CNR"], 300)

    def test_trade_above_our_bid_does_not_fill_us(self):
        api = api_at(book(volume=0))
        api.place_limit("CNR", BUY, 500, 2001)
        api.advance({"rows": book(volume=300, last=20.02), "tick": 2})
        self.assertEqual(api.pos["CNR"], 0)

    def test_market_crossing_our_price_fills_at_our_price(self):
        api = api_at(book())
        api.place_limit("CNR", SELL, 800, 2002)
        crossed = book(bids=((2002, 600), (2001, 900)), asks=((2003, 500),))
        api.advance({"rows": crossed, "tick": 2})
        self.assertEqual(api.pos["CNR"], -600)
        self.assertAlmostEqual(api.cash["CNR"], 600 * 20.02)

    def test_marketable_limit_fills_at_book_price(self):
        api = api_at(book())
        res = api.place_limit("CNR", BUY, 1200, 2002)
        self.assertEqual(res["quantity_filled"], 1000)  # only the 2002 level is at or below our price
        self.assertEqual(api.pos["CNR"], 1000)

    def test_cancel_and_open_orders(self):
        api = api_at(book())
        oid = api.place_limit("CNR", BUY, 500, 2000)["order_id"]
        self.assertEqual(len(api.open_orders()["CNR"]), 1)
        self.assertEqual(api.cancel(oid), "ok")
        self.assertEqual(api.cancel(oid), "gone")
        self.assertEqual(api.open_orders()["CNR"], [])

    def test_book_hides_nothing_and_has_no_own_orders(self):
        raw = api_at(book()).book("CNR")
        bids, asks = mm.build_book_ex_own(raw["bids"], raw["asks"], set(), "ME")
        self.assertEqual(bids[0], (2000, 1000))
        self.assertEqual(asks[0], (2002, 1000))


if __name__ == "__main__":
    unittest.main()
