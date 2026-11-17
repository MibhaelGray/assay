"""Does the paper gateway give live bids and asks right now? Read-only: it asks for quotes and places nothing.

    uv run python deploy/quote_probe.py [SYMBOL ...]

Asks for each symbol twice, the way the order jobs do (intraday/extended.IBMarket): once on SMART, which is the
regular session, pre-market and after-hours, and once on IBKR's OVERNIGHT venue. It also asks for EURUSD, whose
data is free: a forex quote with no stock quotes means the stock subscription is what is missing (error 10089),
and no forex quote either means another session is holding the market data (error 10197) or the gateway is down.
Each run prints its result and adds it to data/quote-probe.log.
"""

import asyncio
import sys
from datetime import datetime
from pathlib import Path

from factor_rotation.config import CT
from factor_rotation.intraday.extended import IBMarket

SYMBOLS = ["SPY", "XLK", "AAPL", "CSCO", "CTVA"]
CLIENT_ID = 92  # its own, so it can run beside the order jobs
LOG = Path("data/quote-probe.log")


async def forex(ib) -> str:
    from ib_async import Forex
    errors = []
    on_error = lambda req_id, code, message, contract: errors.append(f"{code}: {message}")  # noqa: E731
    ib.errorEvent += on_error
    c = Forex("EURUSD")
    await ib.qualifyContractsAsync(c)
    t = ib.reqMktData(c, "", False, False)
    for _ in range(16):
        await asyncio.sleep(0.5)
        if t.bid and t.ask and t.bid > 0:
            break
    ib.cancelMktData(c)
    ib.errorEvent -= on_error
    if t.bid and t.ask and t.bid > 0:
        return f"quoted, bid {t.bid} ask {t.ask}"
    return "NO QUOTE" + (f" ({'; '.join(errors[-2:])})" if errors else "")


async def probe(symbols: list[str]) -> list[str]:
    lines = [f"Quote probe, {datetime.now(CT):%a %Y-%m-%d %-I:%M %p} CT"]
    try:
        async with IBMarket(client_id=CLIENT_ID) as m:
            lines.append(f"  EURUSD (free data): {await forex(m.ib)}")
            for label, overnight in (("SMART (regular, pre-market, after-hours)", False), ("OVERNIGHT venue", True)):
                await m.quotes(symbols, overnight, tries=2, need_books=True)
                lines.append(f"  {label}: {len(m.books)} of {len(symbols)} quoted")
                for s in symbols:
                    if s in m.books:
                        bid, ask = m.books[s]
                        lines.append(f"    {s:5} bid {bid:.2f}  ask {ask:.2f}  spread {(ask - bid) / ((ask + bid) / 2) * 100:.2f}%")
                    else:
                        lines.append(f"    {s:5} NO QUOTE: {m.missing.get(s, 'no reason given')}")
    except Exception as e:  # the gateway is down or refused the connection
        lines.append(f"  could not reach the paper gateway: {type(e).__name__}: {e}")
    return lines


if __name__ == "__main__":
    out = asyncio.run(probe(sys.argv[1:] or SYMBOLS))
    print("\n".join(out))
    LOG.parent.mkdir(exist_ok=True)
    with LOG.open("a") as f:
        f.write("\n".join(out) + "\n\n")
