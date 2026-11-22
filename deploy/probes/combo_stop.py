"""Probe: does the IBKR paper account take native stop orders on the swing book's kinds of trade?

Run once by hand (or by the scheduled check) in regular hours, on the paper gateway only:

    uv run python deploy/probes/combo_stop.py            # whatIf checks only: nothing is placed
    uv run python deploy/probes/combo_stop.py --place    # also place each order, read its status, cancel it

Every test order is 1 share (or a 1-unit combo) with its trigger far from the market, so nothing can fill, and
every one is cancelled before the probe ends. They carry orderRef "probe|combo-stop", which no book job matches.
Names: KO and PEP (not held by the book). The result is written to data/probes/combo-stop-<date>.json.

Tests:
  outright   STP SELL 1 KO
  pair       STP SELL a KO/PEP combo, 1:1, non-guaranteed (sells KO, buys PEP: how a long pair exits)
  ratio      the same at 2:3, as a dollar-matched pair would need
  basket     STP SELL a 4-leg combo: KO, PG, CL long against XLP
  stop-limit STP LMT on the 1:1 pair
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from factor_rotation.config import NY  # noqa: E402
from factor_rotation.paper.broker import PaperSettings, check_settings  # noqa: E402

CLIENT_ID = 81
REF = "probe|combo-stop"


async def main(place: bool) -> dict:
    from ib_async import IB, ComboLeg, Contract, Order, Stock, TagValue

    s = PaperSettings()
    check_settings(s)
    ib = IB()
    errors = []
    ib.errorEvent += lambda req, code, msg, *a: errors.append({"req": req, "code": code, "msg": msg})
    await ib.connectAsync(s.host, s.port, clientId=CLIENT_ID, timeout=20, account=s.account)
    out = {"at": datetime.now(NY).isoformat(timespec="seconds"), "placed": place, "tests": []}
    try:
        syms = ["KO", "PEP", "PG", "CL", "XLP"]
        q = {c.symbol: c for c in await ib.qualifyContractsAsync(*[Stock(x, "SMART", "USD") for x in syms])}
        ib.reqMarketDataType(1)
        tickers = {x: ib.reqMktData(q[x], "", True, False) for x in syms}
        await asyncio.sleep(4)
        px = {x: (t.last if t.last == t.last and t.last else t.close) for x, t in tickers.items()}
        out["prices"] = px

        def combo(legs):   # [(symbol, ratio, "BUY"|"SELL")]
            c = Contract(symbol=",".join(x for x, _, _ in legs), secType="BAG", currency="USD", exchange="SMART")
            c.comboLegs = [ComboLeg(conId=q[x].conId, ratio=r, action=a, exchange="SMART") for x, r, a in legs]
            return c

        def net(legs):     # the combo's price: bought legs minus sold legs, per unit
            return sum((1 if a == "BUY" else -1) * r * px[x] for x, r, a in legs)

        def stop(aux, limit=None, combo_=False):
            o = Order(action="SELL", totalQuantity=1, orderType="STP LMT" if limit is not None else "STP",
                      auxPrice=round(aux, 2), tif="DAY", account=s.account, orderRef=REF, transmit=True, outsideRth=False)
            if limit is not None:
                o.lmtPrice = round(limit, 2)
            if combo_:
                o.smartComboRoutingParams = [TagValue("NonGuaranteed", "1")]
            return o

        pair = [("KO", 1, "BUY"), ("PEP", 1, "SELL")]
        ratio = [("KO", 2, "BUY"), ("PEP", 3, "SELL")]
        basket = [("KO", 1, "BUY"), ("PG", 1, "BUY"), ("CL", 1, "BUY"), ("XLP", 3, "SELL")]
        far = lambda legs: net(legs) - 0.5 * sum(r * px[x] for x, r, _ in legs)   # noqa: E731  a drop of half the gross
        tests = [
            ("outright", q["KO"], stop(px["KO"] * 0.5)),
            ("pair", combo(pair), stop(far(pair), combo_=True)),
            ("ratio", combo(ratio), stop(far(ratio), combo_=True)),
            ("basket", combo(basket), stop(far(basket), combo_=True)),
            ("stop-limit", combo(pair), stop(far(pair), far(pair) - 5, combo_=True)),
        ]
        for name, contract, order in tests:
            row = {"test": name, "type": order.orderType, "trigger": order.auxPrice}
            errors.clear()
            try:
                st = await asyncio.wait_for(ib.whatIfOrderAsync(contract, order), 20)
                row["whatif"] = {"ok": bool(st and st.status), "status": getattr(st, "status", None),
                                 "warning": getattr(st, "warningText", None)}
            except Exception as e:
                row["whatif"] = {"ok": False, "error": repr(e)}
            if place:
                trade = ib.placeOrder(contract, order)
                await asyncio.sleep(6)
                row["status"] = trade.orderStatus.status
                row["log"] = [f"{e.status}: {e.message}" for e in trade.log if e.message]
                if trade.orderStatus.status not in ("Cancelled", "ApiCancelled", "Inactive", "Filled"):
                    ib.cancelOrder(order)
                    await asyncio.sleep(3)
                row["after_cancel"] = trade.orderStatus.status
            row["errors"] = list(errors)
            out["tests"].append(row)
        # Nothing of ours may be left working.
        await ib.reqAllOpenOrdersAsync()
        left = [t for t in ib.openTrades() if t.order.orderRef == REF]
        for t in left:
            ib.cancelOrder(t.order)
        out["left_open"] = len(left)
    finally:
        ib.disconnect()
    path = Path("data/probes") / f"combo-stop-{datetime.now(NY):%Y-%m-%d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--place", action="store_true", help="place each test order, read its status and cancel it")
    print(json.dumps(asyncio.run(main(ap.parse_args().place)), indent=1, default=str))
