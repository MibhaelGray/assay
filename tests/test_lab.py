"""The research lab's engine: the timing is honest, the costs are charged, and the ledger's rules hold."""

import numpy as np
import pandas as pd
import pytest

from factor_rotation.lab import engine, panel, stats


@pytest.fixture(scope="module")
def p():
    return panel.synthetic()


def _stocks(p):
    return p.close[p.stocks]


def test_a_score_from_the_future_cannot_change_the_past(p):
    rng = np.random.default_rng(1)
    score = pd.DataFrame(rng.normal(size=_stocks(p).shape), index=p.dates, columns=p.stocks)
    later = score.copy()
    later.iloc[300:] = rng.normal(size=later.iloc[300:].shape)
    for entry, exit_ in (("open", "close"), ("open", "open"), ("close", "close"), ("close", "open")):
        a = engine.run(p, score, hold=3, entry=entry, exit=exit_)
        b = engine.run(p, later, hold=3, entry=entry, exit=exit_)
        cut = 301 if entry == "open" else 300      # a close entry pays its cost on the decision day itself
        assert np.allclose(a.gross.iloc[:301], b.gross.iloc[:301])
        assert np.allclose(a.net.iloc[:cut], b.net.iloc[:cut])
        assert not np.allclose(a.gross.iloc[301:], b.gross.iloc[301:])


def test_knowing_tomorrow_pays_and_noise_does_not(p):
    cheat = p.oc[p.stocks].shift(-1)     # tomorrow's open-to-close return: what a next-open, one-session hold earns
    r = engine.run(p, cheat, hold=1)
    assert stats.summary(r)["sharpe"] > 10
    rng = np.random.default_rng(2)
    noise = engine.run(p, pd.DataFrame(rng.normal(size=_stocks(p).shape), index=p.dates, columns=p.stocks), hold=1)
    s = stats.summary(noise)
    assert abs(s["sharpe_gross"]) < 2.5 and s["sharpe"] < s["sharpe_gross"]


def test_one_session_hold_earns_open_to_close(p):
    score = pd.DataFrame(np.nan, index=p.dates, columns=p.stocks)
    score.iloc[100, 0] = 1.0
    r = engine.run(p, score, hold=1, side="long", select="sign", max_w=1.0)
    assert r.gross.iloc[101] == pytest.approx(p.oc.iloc[101, 0])
    assert r.gross.drop(r.gross.index[101]).abs().sum() == 0
    both_ways = 2 * engine.cost_table(p).iloc[100, 0]
    assert r.cost.iloc[101] == pytest.approx(both_ways)


def test_three_session_hold_runs_open_to_third_close(p):
    score = pd.DataFrame(np.nan, index=p.dates, columns=p.stocks)
    score.iloc[100, 0] = 1.0
    r = engine.run(p, score, hold=3, side="long", select="sign", max_w=1.0)
    whole = p.close.iloc[103, 0] / p.open.iloc[101, 0] - 1
    assert (1 + 3 * r.gross.iloc[101:104]).prod() - 1 == pytest.approx(whole)   # one slice is a third of the book
    over = engine.run(p, score, hold=3, side="long", select="sign", max_w=1.0, exit="open")
    assert (1 + 3 * over.gross.iloc[101:105]).prod() - 1 == pytest.approx(p.open.iloc[104, 0] / p.open.iloc[101, 0] - 1)


def test_long_short_is_half_each_way_and_a_hedge_balances(p):
    rng = np.random.default_rng(3)
    score = pd.DataFrame(rng.normal(size=_stocks(p).shape), index=p.dates, columns=p.stocks)
    w = engine.pick(score, "ls", frac=0.2, max_w=1.0)
    assert w.clip(lower=0).sum(axis=1).iloc[50] == pytest.approx(1.0) and w.clip(upper=0).sum(axis=1).iloc[50] == pytest.approx(-1.0)
    alone = engine.run(p, score, hold=1, side="long", frac=0.2, max_w=1.0)
    hedged = engine.run(p, score, hold=1, side="long", hedge=True, frac=0.2, max_w=1.0)
    market = p.oc["SPY"].where(alone.names.shift(1) > 0, 0.0)     # the fund's open-to-close on the days a slice is on
    assert np.allclose(hedged.gross, 0.5 * alone.gross - 0.5 * market)


def test_doubling_costs_doubles_the_drag(p):
    rng = np.random.default_rng(4)
    score = pd.DataFrame(rng.normal(size=_stocks(p).shape), index=p.dates, columns=p.stocks)
    a, b = engine.run(p, score, hold=2), engine.run(p, score, hold=2, cost_mult=2.0)
    trade_a, trade_b = a.cost.sum(), b.cost.sum()
    assert trade_b > trade_a * 1.5 and np.allclose(a.gross, b.gross)


def test_deflated_sharpe_falls_as_more_is_tried():
    few, many = stats.deflated(0.08, 2500, 0.0, 3.0, 3), stats.deflated(0.08, 2500, 0.0, 3.0, 400)
    assert few > many > 0.5
    assert stats.deflated(0.03, 2500, 0.0, 3.0, 400) < 0.2     # a 0.5 Sharpe is nothing after 400 tries
