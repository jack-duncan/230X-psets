import unittest

import mm_algo2e as m
from mm_algo2e import BUY, SELL


class AsQuote(unittest.TestCase):
    def test_flat_no_drift_is_symmetric_about_s(self):
        r, spread, half = m.as_quote(20.0, 0, 100, 0.01, 0.0, 2e-5, 100.0, 0.0)
        self.assertAlmostEqual(r, 20.0)
        self.assertAlmostEqual(spread / 2, half)

    def test_long_inventory_lowers_reservation_price(self):
        r, *_ = m.as_quote(20.0, 10000, 100, 0.01, 0.0, 2e-5, 100.0, 0.0)
        self.assertAlmostEqual(r, 20.0 - 10000 * 2e-5 * 1e-4 * 100)

    def test_drift_uses_capped_horizon(self):
        r, *_ = m.as_quote(20.0, 0, 200, 0.01, 0.001, 2e-5, 100.0, 0.0)
        self.assertAlmostEqual(r, 20.0 + 0.001 * m.TAU_EFF_MAX)

    def test_terminal_spread_is_the_k_term_only(self):
        _, spread, _ = m.as_quote(20.0, 0, 0, 0.01, 0.0, 2e-5, 100.0, 0.0)
        self.assertAlmostEqual(spread, (2 / 2e-5) * m.math.log1p(2e-5 / 100.0))

    def test_negative_rho_widens_and_positive_rebate_tightens(self):
        _, _, fee = m.as_quote(20.0, 0, 100, 0.01, 0.0, 2e-5, 100.0, -0.002)
        _, _, reb = m.as_quote(20.0, 0, 100, 0.01, 0.0, 2e-5, 100.0, 0.002)
        self.assertGreater(fee, reb)

    def test_half_never_negative(self):
        _, _, half = m.as_quote(20.0, 0, 0, 0.01, 0.0, 2e-5, 100.0, 1.0)
        self.assertEqual(half, 0.0)


class Gate(unittest.TestCase):
    def test_hysteresis(self):
        self.assertFalse(m.update_gate(False, m.THETA_ON - 0.001))
        self.assertTrue(m.update_gate(False, m.THETA_ON))
        self.assertTrue(m.update_gate(True, m.THETA_OFF + 0.0001))
        self.assertFalse(m.update_gate(True, m.THETA_OFF - 0.0001))


class QuotePrices(unittest.TestCase):
    def test_rounds_outward(self):
        bid, ask = m.quote_prices(20.004, 0.0105, 1999, 2002)
        self.assertEqual((bid, ask), (1999, 2002))  # 1999.35 floored, 2001.45 ceiled

    def test_inside_touch_capped_at_one_tick(self):
        bid, ask = m.quote_prices(20.00, 0.0, 1990, 2010)
        self.assertEqual((bid, ask), (1991, 2009))

    def test_no_cross_in_one_tick_market(self):
        bid, ask = m.quote_prices(20.00, 0.0, 2000, 2001)
        self.assertEqual(bid, 2000)
        self.assertEqual(ask, 2001)

    def test_extras_widen_after_caps(self):
        bid, ask = m.quote_prices(20.00, 0.0, 1990, 2010, bid_extra=1, ask_extra=1)
        self.assertEqual((bid, ask), (1990, 2010))

    def test_min_one_tick_spread(self):
        bid, ask = m.quote_prices(20.00, 0.0, 2000, 2001)
        self.assertGreaterEqual(ask - bid, 1)


class Sizes(unittest.TestCase):
    def test_flat_both_sides_full(self):
        q = m.compute_sizes(0, 10000)
        self.assertEqual(q, {BUY: m.CLIP, SELL: m.CLIP})

    def test_tiers_on_adding_side_reducing_stays_full(self):
        self.assertEqual(m.compute_sizes(6000, 10000)[BUY], m.CLIP // 2)
        self.assertEqual(m.compute_sizes(6000, 10000)[SELL], m.CLIP)
        self.assertEqual(m.compute_sizes(10000, 10000)[BUY], 0)
        self.assertEqual(m.compute_sizes(-10000, 10000)[SELL], 0)
        self.assertEqual(m.compute_sizes(-10000, 10000)[BUY], m.CLIP)

    def test_guard_level1_halves_facing_side_only(self):
        q = m.compute_sizes(0, 10000, guard_level=1, guard_dir="UP")
        self.assertEqual(q, {BUY: m.CLIP, SELL: m.CLIP // 2})

    def test_guard_level2_facing_off_and_reduce_only(self):
        q = m.compute_sizes(3000, 10000, guard_level=2, guard_dir="DOWN")
        self.assertEqual(q, {BUY: 0, SELL: m.CLIP})  # facing=BUY off; BUY is also the adding side
        q = m.compute_sizes(3000, 10000, guard_level=2, guard_dir="UP")
        self.assertEqual(q, {BUY: 0, SELL: 0})  # facing=SELL off; adding BUY off

    def test_guard_level2_flat_keeps_non_facing_side(self):
        q = m.compute_sizes(0, 10000, guard_level=2, guard_dir="UP")
        self.assertEqual(q, {BUY: m.CLIP, SELL: 0})

    def test_markout_level2_halves_that_side(self):
        q = m.compute_sizes(0, 10000, mk_level={BUY: 2, SELL: 1})
        self.assertEqual(q, {BUY: m.CLIP // 2, SELL: m.CLIP})

    def test_hard_position_limit(self):
        q = m.compute_sizes(24500, 100000, clip=2000)
        self.assertEqual(q[BUY], 500)
        q = m.compute_sizes(-24500, 100000, clip=2000)
        self.assertEqual(q[SELL], 500)


class Allocation(unittest.TestCase):
    def test_proportional_and_clamped(self):
        caps = m.allocate({"A": 3.0, "B": 1.0, "C": 0.0}, b_total=20000, floor=2000, ceil=12000)
        self.assertEqual(caps["A"], 12000)
        self.assertEqual(caps["B"], 5000)
        self.assertEqual(caps["C"], 2000)

    def test_zero_scores_split_evenly(self):
        caps = m.allocate({"A": 0, "B": 0, "C": 0}, b_total=30000, floor=0, ceil=30000)
        self.assertEqual(set(caps.values()), {10000})

    def test_score_decays_with_distance(self):
        self.assertGreater(m.flow_score(1, 100, 0.01, 0.01), m.flow_score(1, 100, 0.03, 0.01))
        self.assertEqual(m.flow_score(1, 100, 0.01, -0.5), 0.0)


class Markout(unittest.TestCase):
    def test_needs_min_fills_and_window(self):
        e = [(10, -0.01, 100)] * 2
        self.assertIsNone(m.weighted_markout(e, 11, 30, 3))
        e = [(10, -0.01, 100)] * 3
        self.assertAlmostEqual(m.weighted_markout(e, 11, 30, 3), -0.01)
        self.assertIsNone(m.weighted_markout(e, 100, 30, 3))  # stale entries age out

    def test_qty_weighted(self):
        self.assertAlmostEqual(m.weighted_markout([(1, 0.0, 100), (1, -0.03, 300)], 1, 30, 2), -0.0225)

    def test_levels(self):
        self.assertEqual(m.markout_level(None), 0)
        self.assertEqual(m.markout_level(-m.MARKOUT_TH / 2), 0)
        self.assertEqual(m.markout_level(-1.5 * m.MARKOUT_TH), 1)
        self.assertEqual(m.markout_level(-3 * m.MARKOUT_TH), 2)


class Guard(unittest.TestCase):
    fresh = (0, None, -1)

    def test_two_of_three_same_direction_is_level1(self):
        g = m.update_guard(self.fresh, [1.0, 0.9, 0.0], None, 10)
        self.assertEqual(g[:2], (1, "UP"))

    def test_one_asset_is_not_enough(self):
        self.assertEqual(m.update_guard(self.fresh, [1.0, 0.0, 0.0], None, 10)[0], 0)

    def test_opposite_drifts_do_not_trigger(self):
        self.assertEqual(m.update_guard(self.fresh, [1.0, -1.0, 0.0], None, 10)[0], 0)

    def test_drift_plus_bad_markout_is_level2(self):
        g = m.update_guard(self.fresh, [-1.0, -1.0, 0.0], -m.AGG_MARKOUT_TH * 2, 10)
        self.assertEqual(g[:2], (2, "DOWN"))

    def test_bad_markout_alone_uses_net_z_direction(self):
        g = m.update_guard(self.fresh, [0.3, 0.2, 0.1], -m.AGG_MARKOUT_TH * 2, 10)
        self.assertEqual(g[:2], (1, "UP"))

    def test_release_after_hold(self):
        g = m.update_guard(self.fresh, [1.0, 1.0, 1.0], None, 10)
        g = m.update_guard(g, [0.0, 0.0, 0.0], None, 10 + m.TREND_RELEASE_TICKS)
        self.assertEqual(g[0], 1)
        g = m.update_guard(g, [0.0, 0.0, 0.0], None, 10 + m.TREND_RELEASE_TICKS + 1)
        self.assertEqual(g[:2], (0, None))

    def test_level_does_not_drop_within_hold(self):
        g = m.update_guard(self.fresh, [1.0, 1.0, 0.0], -m.AGG_MARKOUT_TH * 2, 10)
        g = m.update_guard(g, [1.0, 1.0, 0.0], None, 12)
        self.assertEqual(g[0], 2)


class Stats(unittest.TestCase):
    def test_drift_shrunk_and_capped(self):
        self.assertAlmostEqual(m.shrunk_drift(0.002), 0.001)
        self.assertEqual(m.shrunk_drift(1.0), m.MU_MAX)
        self.assertEqual(m.shrunk_drift(-1.0), -m.MU_MAX)

    def test_ewma_moves_toward_observation_per_tick(self):
        mu, var = m.update_stats(0.0, 1e-4, 20.00, 1, 20.02)
        self.assertGreater(mu, 0)
        self.assertGreater(var, 1e-4)

    def test_gap_ticks_divide_the_move(self):
        mu1, _ = m.update_stats(0.0, 1e-4, 20.00, 1, 20.04)
        mu4, _ = m.update_stats(0.0, 1e-4, 20.00, 4, 20.04)
        self.assertAlmostEqual(mu1, 4 * mu4)


class Reconcile(unittest.TestCase):
    def test_matching_order_kept(self):
        p = m.diff_orders([{"id": 1, "price": 2000, "qty": 1500}], (2000, 1500))
        self.assertEqual((p["cancel"], p["place"]), ([], []))

    def test_reprice_cancels_and_places(self):
        p = m.diff_orders([{"id": 1, "price": 2000, "qty": 1500}], (2001, 1500))
        self.assertEqual((p["cancel"], p["place"]), ([1], [1500]))

    def test_zero_target_cancels_all(self):
        p = m.diff_orders([{"id": 1, "price": 2000, "qty": 1500}], None)
        self.assertEqual(p["cancel"], [1])

    def test_large_target_split(self):
        p = m.diff_orders([], (2000, 12000))
        self.assertEqual(p["place"], [5000, 5000, 2000])


class Fills(unittest.TestCase):
    def o(self, oid, action="BUY", price=2000, qty=1000, filled=0):
        return {"id": oid, "action": action, "price": price, "qty": qty, "filled": filled}

    def test_fill_growth_reports_order_id(self):
        tracked = {1: self.o(1)}
        fills, vanished, new = m.detect_fills(tracked, [self.o(1, qty=600, filled=400)])
        self.assertEqual(fills, [(1, "BUY", 400, 2000)])
        self.assertEqual(vanished, [])

    def test_vanished_not_assumed_filled(self):
        fills, vanished, _ = m.detect_fills({1: self.o(1)}, [])
        self.assertEqual(fills, [])
        self.assertEqual(len(vanished), 1)


class Flatten(unittest.TestCase):
    def test_long_sells_one_tick_inside_bid(self):
        self.assertEqual(m.flatten_targets(3000, 1999, 2001), {BUY: None, SELL: (2000, 3000)})

    def test_short_buys_one_tick_inside_ask(self):
        self.assertEqual(m.flatten_targets(-3000, 1999, 2001), {BUY: (2000, 3000), SELL: None})

    def test_flat_has_no_targets(self):
        self.assertEqual(m.flatten_targets(0, 1999, 2001), {BUY: None, SELL: None})


class CaseEnd(unittest.TestCase):
    def test_pause_is_not_the_end(self):
        self.assertFalse(m.case_over(38, "STOPPED", 300))

    def test_reset_clock_or_full_length_is_the_end(self):
        self.assertTrue(m.case_over(0, "STOPPED", 300))
        self.assertTrue(m.case_over(300, "ACTIVE", 300))


class Book(unittest.TestCase):
    def test_microprice_leans_to_thin_side(self):
        self.assertGreater(m.compute_fv(1000, 1002, 900, 100), 1001)
        self.assertLess(m.compute_fv(1000, 1002, 100, 900), 1001)

    def test_book_ex_own_drops_our_orders(self):
        bids = [{"order_id": 1, "price": 20.0, "quantity": 100, "quantity_filled": 0},
                {"order_id": 2, "price": 19.99, "quantity": 300, "quantity_filled": 100}]
        b, a = m.build_book_ex_own(bids, [], own_ids={1})
        self.assertEqual(b, [(1999, 200)])


if __name__ == "__main__":
    unittest.main()
