"""
Executor — turns a signal into orders. Dry-run by default.
==========================================================

This is the missing stage the project never had: db.py's header has said
"future tasks — executor" since the beginning. Everything upstream only
ever observed.

Design rules, in order of importance
------------------------------------
1. DRY RUN IS THE DEFAULT. Live trading requires BOTH the --live flag and
   the environment variable POLYMARKET_ALLOW_LIVE=yes. Two independent
   switches, because one flag is one typo away from real money.

2. REVALIDATE IMMEDIATELY BEFORE PLACING. A signal is evidence that an edge
   existed; it is not permission to trade. The books are re-fetched over
   REST and the edge recomputed from scratch. If it decayed past the
   threshold, abort — that is the normal outcome, not a failure.

3. EVERY LEG IS A LIMIT ORDER, NEVER A MARKET ORDER. The whole edge is
   often under 2%. A market order that walks one extra level erases it.
   Limit price = the worst price the slippage plan already assumed.

4. FILL-OR-KILL. A partially filled basket is not an arbitrage, it is an
   unhedged directional bet. FOK makes the exchange enforce that for us.

5. THINNEST LEG FIRST. The leg most likely to fail is placed first, so a
   failure costs nothing. Legs already filled when a later leg fails are
   unwound immediately and the execution is recorded as 'partial' —
   loudly, because that is the one state a human must look at.

6. WRITE TO THE DB BEFORE SENDING. If the process dies mid-basket, the row
   left in 'placing' is the only record that a position may exist.
   db.open_executions() surfaces these on the next start and refuses to
   trade until they are cleared.

Run:
    python executor.py --check                 # config + safety self-test
    python executor.py --from-db <opp_id>      # dry-run a stored opportunity
    python executor.py --watch                 # live engine -> dry-run
    python executor.py --watch --live          # real orders (needs env var)
"""

import argparse
import asyncio
import logging
import os
import time
from pathlib import Path
from typing import List, Optional

import requests

import arbmath
import config
import db as dblib
import fees

CLOB_URL = config.CLOB_URL

# ---------------------------------------------------------------------
# Risk limits — deliberately small. Raise them only after the signals
# table proves edges live long enough to be caught. Override per run with
# env vars or PROFILE=conservative; see config.py.
# ---------------------------------------------------------------------
MAX_CAPITAL_PER_TRADE = config.MAX_CAPITAL_PER_TRADE
MAX_TRADES_PER_DAY = config.MAX_TRADES_PER_DAY
MIN_NET_EDGE_TO_EXECUTE = config.MIN_NET_EDGE_TO_EXECUTE
MAX_SIGNAL_AGE_MS = config.MAX_SIGNAL_AGE_MS
REVALIDATE_TIMEOUT = config.REVALIDATE_TIMEOUT
LIMIT_BUFFER_TICKS = config.LIMIT_BUFFER_TICKS

# Touch this file to stop all live trading without killing the process.
KILL_SWITCH = Path(__file__).parent / "STOP"

log = logging.getLogger("executor")

SESSION = requests.Session()


class ExecutionAborted(Exception):
    """Raised when a safety check refuses the trade. Expected, not exceptional."""


# =====================================================================
# Safety gate
# =====================================================================


def live_trading_allowed(live_flag: bool) -> bool:
    """Both switches must agree before a single real order is sent."""
    if not live_flag:
        return False
    if os.getenv("POLYMARKET_ALLOW_LIVE", "").lower() not in ("yes", "true", "1"):
        log.error("--live given but POLYMARKET_ALLOW_LIVE is not set. "
                  "Staying in dry-run.")
        return False
    if KILL_SWITCH.exists():
        log.error("Kill switch present (%s). Staying in dry-run.", KILL_SWITCH)
        return False
    return True


def preflight(db, mode: str):
    """Refuse to start if a previous run left orders in an unknown state."""
    stuck = dblib.open_executions(db)
    if stuck:
        ids = ", ".join(str(r["id"]) for r in stuck)
        raise ExecutionAborted(
            f"{len(stuck)} execution(s) left unfinished (id: {ids}). "
            f"A position may be open. Inspect with view_db.py exec, resolve "
            f"them, then restart.")
    log.info("Preflight OK — mode=%s", mode)


def trades_today(db) -> int:
    row = db.execute("""
        SELECT COUNT(*) c FROM executions
        WHERE mode = 'live' AND date(started_at) = date('now')
    """).fetchone()
    return row["c"] if row else 0


# =====================================================================
# Revalidation — the single most important function here
# =====================================================================


def fetch_books_now(token_ids: List[str]) -> dict:
    """Fresh books straight from CLOB, no cache, short timeout."""
    payload = [{"token_id": t} for t in token_ids]
    r = SESSION.post(f"{CLOB_URL}/books", json=payload,
                     timeout=REVALIDATE_TIMEOUT)
    r.raise_for_status()
    out = {}
    for book in r.json():
        aid = book.get("asset_id")
        if aid:
            out[aid] = arbmath.normalize_asks(book.get("asks"))
    return out


def revalidate(signal: dict, budget: float) -> dict:
    """
    Re-derive the edge from books fetched *right now*.

    Nothing from the signal is trusted except which tokens to look at. If
    the numbers no longer clear the bar, ExecutionAborted is raised — and
    that will be the common case. An executor that rarely aborts is an
    executor that is trading on stale data.
    """
    legs_meta = signal.get("legs") or signal.get("legs_detail") or []
    token_ids = [l["token_id"] for l in legs_meta if l.get("token_id")]
    if len(token_ids) != len(legs_meta) or len(token_ids) < 2:
        raise ExecutionAborted("signal is missing token ids for some legs")

    age_ms = signal.get("age_ms")
    if age_ms is not None and age_ms > MAX_SIGNAL_AGE_MS:
        raise ExecutionAborted(f"signal is {age_ms:.0f}ms old (max "
                               f"{MAX_SIGNAL_AGE_MS}ms)")

    t0 = time.time()
    try:
        books = fetch_books_now(token_ids)
    except Exception as e:
        raise ExecutionAborted(f"could not refetch books: {e}")
    fetch_ms = (time.time() - t0) * 1000

    legs = []
    for meta in legs_meta:
        levels = books.get(meta["token_id"], [])
        if not levels:
            raise ExecutionAborted(f"leg '{meta.get('outcome')}' is dry now")
        legs.append((meta.get("outcome") or meta["token_id"][:10], levels))

    fee_rate = signal.get("fee_rate", fees.DEFAULT_FEE_RATE)

    # A NO basket pays N-1 per basket, not 1. Taking the payout from the
    # signal rather than assuming 1 is what keeps this check honest for
    # both kinds of trade — with the wrong payout, a NO basket looks
    # catastrophically unprofitable and would always abort.
    payout = float(signal.get("payout_per_basket") or 1.0)
    is_no_side = signal.get("side") == "no" or \
        signal.get("market_type") == "multi_no"
    if is_no_side and payout <= 1.0:
        raise ExecutionAborted(
            "NO-side signal did not carry its payout; refusing to guess")

    result = arbmath.evaluate_basket(legs, fee_rate, [budget],
                                     payout_per_basket=payout)

    # thresholded per dollar of capital: a NO basket's per-basket edge is
    # measured against an N-1 payout and would clear any bar set for YES
    edge = result["net_edge_per_dollar"]
    if edge is None or edge < MIN_NET_EDGE_TO_EXECUTE:
        raise ExecutionAborted(
            f"edge decayed to {(edge or 0)*100:.3f}%/$ "
            f"(need {MIN_NET_EDGE_TO_EXECUTE*100:.2f}%) after {fetch_ms:.0f}ms")

    sized = arbmath.optimal_k(legs, budget, fee_rate,
                              payout_per_basket=payout)
    if not sized:
        raise ExecutionAborted("no profitable size fits the budget")

    result["sized"] = sized
    result["legs"] = legs
    result["legs_meta"] = legs_meta
    result["fetch_ms"] = fetch_ms
    return result


# =====================================================================
# Planning
# =====================================================================


def round_to_tick(price: float, tick: float, up: bool = True) -> float:
    """Limit prices must sit on the tick grid or the exchange rejects them."""
    if tick <= 0:
        tick = 0.01
    steps = price / tick
    steps = int(steps) + 1 if up and steps % 1 else round(steps)
    return round(min(max(steps * tick, tick), 1.0 - tick), 4)


def build_plan(signal: dict, revalidated: dict, budget: float) -> dict:
    """
    Turn a revalidated edge into concrete per-leg orders.

    Legs are ordered thinnest-first: the leg with the least resting depth
    is the one most likely to disappear, so it is placed first while the
    cost of aborting is still zero.
    """
    sized = revalidated["sized"]
    k = sized["shares"]
    legs_meta = revalidated["legs_meta"]

    plan_legs = []
    for i, (name, levels) in enumerate(revalidated["legs"]):
        meta = legs_meta[i]
        tick = float(meta.get("tick_size") or 0.01)
        expected_avg = sized["avg_prices"][i]
        # worst price we are willing to pay: the average our own slippage
        # plan already priced in, plus one tick of tolerance
        limit_price = round_to_tick(expected_avg + LIMIT_BUFFER_TICKS * tick,
                                    tick, up=True)
        plan_legs.append({
            "outcome": name,
            "token_id": meta["token_id"],
            "shares": round(k, 2),
            "limit_price": limit_price,
            "expected_avg_price": round(expected_avg, 4),
            "tick_size": tick,
            "depth_usd": arbmath.depth_usd(levels),
        })

    plan_legs.sort(key=lambda l: l["depth_usd"])

    return {
        "signal_id": signal.get("signal_id"),
        "opportunity_id": signal.get("opportunity_id"),
        "event_slug": signal.get("event_slug"),
        "event_title": signal.get("event_title"),
        "url": signal.get("url"),
        "fee_rate": revalidated["fee_rate"],
        "net_edge": revalidated["net_edge"],
        "sum_best_asks": revalidated["sum_best_asks"],
        "budget": budget,
        "shares": round(k, 2),
        "cost": round(sized["real_cost"], 4),
        "fee": round(sized["fee"], 4),
        "profit": round(sized["profit"], 4),
        "roi": round(sized["roi"], 3),
        "legs": plan_legs,
        # worst case if every leg fills at its limit instead of its expected
        # average — the real amount of capital that must be available
        "max_outlay": round(sum(l["limit_price"] * l["shares"]
                                for l in plan_legs), 4),
    }


def describe_plan(plan: dict) -> str:
    lines = [
        "",
        "=" * 70,
        f"PLAN  {(plan.get('event_title') or '')[:58]}",
        "=" * 70,
        f"  net edge      : {plan['net_edge']*100:.3f}%  "
        f"(sum_asks={plan['sum_best_asks']:.4f}, fee={plan['fee_rate']*100:.0f}%)",
        f"  baskets (K)   : {plan['shares']:,.2f}",
        f"  expected cost : ${plan['cost']:,.2f} + ${plan['fee']:,.2f} fee",
        f"  max outlay    : ${plan['max_outlay']:,.2f} (all legs at limit)",
        f"  expected profit: ${plan['profit']:,.2f}  (ROI {plan['roi']:.2f}%)",
        "",
        f"  {'#':<3}{'Outcome':<28}{'Shares':>10}{'Limit':>8}{'Exp.avg':>9}{'Depth':>10}",
        "  " + "-" * 66,
    ]
    for i, leg in enumerate(plan["legs"], 1):
        lines.append(
            f"  {i:<3}{str(leg['outcome'])[:26]:<28}{leg['shares']:>10,.2f}"
            f"{leg['limit_price']:>8.3f}{leg['expected_avg_price']:>9.4f}"
            f"${leg['depth_usd']:>9,.0f}")
    lines.append("  (order shown is placement order: thinnest leg first)")
    lines.append(f"  {plan.get('url') or ''}")
    return "\n".join(lines)


# =====================================================================
# Order placement
# =====================================================================


class OrderPlacer:
    """
    Wraps py_clob_client. In dry mode nothing is imported and nothing is
    sent — the same code path runs, it just reports simulated fills, so a
    dry history is directly comparable to a live one.
    """

    def __init__(self, dry: bool = True):
        self.dry = dry
        self.client = None
        if not dry:
            self.client = self._build_client()

    @staticmethod
    def _build_client():
        from py_clob_client.client import ClobClient

        private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
        funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
        sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
        if not private_key or not funder:
            raise ExecutionAborted(
                "POLYMARKET_PRIVATE_KEY / POLYMARKET_FUNDER_ADDRESS not set")

        client = ClobClient(CLOB_URL, key=private_key, chain_id=137,
                            signature_type=sig_type, funder=funder)
        client.set_api_creds(client.create_or_derive_api_creds())
        return client

    def buy(self, token_id: str, price: float, shares: float) -> dict:
        """
        Place one fill-or-kill buy. Returns a normalized result dict:
        {ok, order_id, filled, avg_price, error}
        """
        if self.dry:
            # dry mode assumes the limit is marketable, which is optimistic
            # by design: if the strategy is not profitable under optimistic
            # fills it is certainly not profitable under real ones
            return {"ok": True, "order_id": f"DRY-{token_id[:8]}",
                    "filled": shares, "avg_price": price, "error": None}

        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY

        try:
            order = self.client.create_order(OrderArgs(
                token_id=token_id, price=price, size=shares, side=BUY))
            # FOK: all of it, at this price or better, or nothing at all
            resp = self.client.post_order(order, OrderType.FOK)
        except Exception as e:
            return {"ok": False, "order_id": None, "filled": 0.0,
                    "avg_price": None, "error": str(e)}

        filled = float(resp.get("size_matched") or 0)
        return {
            "ok": bool(resp.get("success")) and filled > 0,
            "order_id": resp.get("orderID") or resp.get("orderId"),
            "filled": filled,
            "avg_price": float(resp.get("price") or price),
            "error": None if resp.get("success") else str(resp.get("errorMsg")),
        }

    def sell(self, token_id: str, shares: float) -> dict:
        """
        Unwind a leg at whatever the book will pay. Used only on the abort
        path, where holding an unhedged leg is worse than a bad price.
        """
        if self.dry:
            return {"ok": True, "order_id": f"DRY-UNWIND-{token_id[:8]}",
                    "filled": shares, "avg_price": None, "error": None}

        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL

        try:
            # cross aggressively — the tick floor, so any resting bid takes it
            order = self.client.create_order(OrderArgs(
                token_id=token_id, price=0.01, size=shares, side=SELL))
            resp = self.client.post_order(order, OrderType.GTC)
        except Exception as e:
            return {"ok": False, "order_id": None, "filled": 0.0,
                    "avg_price": None, "error": str(e)}

        return {"ok": bool(resp.get("success")),
                "order_id": resp.get("orderID") or resp.get("orderId"),
                "filled": float(resp.get("size_matched") or 0),
                "avg_price": None, "error": None}


# =====================================================================
# The execution itself
# =====================================================================


def execute(db, signal: dict, *, budget: float, dry: bool = True) -> dict:
    """
    Full pipeline for one signal: revalidate -> plan -> record -> place.

    Returns a summary dict. Raises nothing on a normal abort — aborts are
    recorded and returned, because they are the expected outcome.
    """
    mode = "dry" if dry else "live"
    budget = min(budget, MAX_CAPITAL_PER_TRADE)

    if not dry:
        if KILL_SWITCH.exists():
            return {"status": "aborted", "reason": "kill switch"}
        used = trades_today(db)
        if used >= MAX_TRADES_PER_DAY:
            return {"status": "aborted",
                    "reason": f"daily trade limit reached ({used})"}

    # ---- revalidate ------------------------------------------------
    try:
        fresh = revalidate(signal, budget)
    except ExecutionAborted as e:
        log.info("ABORT (revalidate): %s", e)
        return {"status": "aborted", "reason": str(e)}

    plan = build_plan(signal, fresh, budget)
    log.info(describe_plan(plan))

    # ---- record before sending anything ----------------------------
    execution_id = dblib.start_execution(db, plan, mode)
    dblib.set_execution_status(db, execution_id, "revalidated")

    if plan["max_outlay"] > budget * 1.05:
        dblib.finish_execution(db, execution_id, status="aborted",
                               abort_reason="max outlay exceeds budget")
        return {"status": "aborted", "reason": "max outlay exceeds budget",
                "execution_id": execution_id}

    placer = OrderPlacer(dry=dry)
    dblib.set_execution_status(db, execution_id, "placing")

    # ---- place leg by leg, thinnest first ---------------------------
    filled_legs = []
    total_cost = 0.0
    failure = None

    for i, leg in enumerate(plan["legs"]):
        result = placer.buy(leg["token_id"], leg["limit_price"], leg["shares"])
        dblib.update_execution_leg(
            db, execution_id, i,
            order_id=result["order_id"],
            status="filled" if result["ok"] else "rejected",
            filled_shares=result["filled"],
            avg_fill_price=result["avg_price"],
            error=result["error"])

        if not result["ok"]:
            failure = f"leg '{leg['outcome']}' failed: {result['error']}"
            log.error("LEG FAILED: %s", failure)
            break

        filled_legs.append((i, leg, result))
        total_cost += (result["avg_price"] or leg["limit_price"]) * result["filled"]
        log.info("  filled leg %d/%d: %s %.2f @ %.4f",
                 i + 1, len(plan["legs"]), leg["outcome"],
                 result["filled"], result["avg_price"] or leg["limit_price"])

    # ---- all legs filled: done -------------------------------------
    if failure is None:
        filled_shares = min(r["filled"] for _i, _l, r in filled_legs)
        actual_fee = fees.fee_for_legs(
            [r["avg_price"] or l["limit_price"] for _i, l, r in filled_legs],
            filled_shares, plan["fee_rate"])
        dblib.finish_execution(db, execution_id, status="filled",
                               filled_shares=filled_shares,
                               actual_cost=total_cost, actual_fee=actual_fee)
        profit = filled_shares - total_cost - actual_fee
        log.info("EXECUTED [%s] %d legs | %.2f baskets | cost $%.2f | "
                 "locked profit $%.2f", mode, len(filled_legs),
                 filled_shares, total_cost, profit)
        return {"status": "filled", "execution_id": execution_id,
                "shares": filled_shares, "cost": total_cost,
                "profit": profit, "mode": mode}

    # ---- a leg failed: unwind whatever filled ----------------------
    # This is the dangerous state. Every filled leg is now an unhedged
    # position, so we sell it back immediately and accept the loss rather
    # than hold a bet we never intended to make.
    log.error("UNWINDING %d filled leg(s)", len(filled_legs))
    unwind_ok = True
    for i, leg, result in filled_legs:
        undo = placer.sell(leg["token_id"], result["filled"])
        dblib.update_execution_leg(
            db, execution_id, i,
            status="unwound" if undo["ok"] else "STUCK",
            error=undo["error"])
        if not undo["ok"]:
            unwind_ok = False
            log.critical("UNWIND FAILED for %s (%s) — MANUAL ACTION REQUIRED",
                         leg["outcome"], leg["token_id"])

    dblib.finish_execution(
        db, execution_id,
        status="partial" if unwind_ok else "failed",
        filled_shares=0.0, actual_cost=total_cost,
        abort_reason=failure + ("" if unwind_ok else " | UNWIND FAILED"))

    return {"status": "partial" if unwind_ok else "failed",
            "execution_id": execution_id, "reason": failure,
            "unwound": unwind_ok, "mode": mode}


# =====================================================================
# Sources of signals
# =====================================================================


def signal_from_opportunity_row(row) -> dict:
    """Rehydrate a stored opportunity into the shape execute() expects."""
    import json as _json
    legs = _json.loads(row["legs_detail"] or "[]")
    return {
        "opportunity_id": row["id"],
        "event_slug": row["event_slug"],
        "event_title": row["event_title"],
        "fee_rate": row["fee_rate"],
        "url": row["url"],
        "legs": legs,
        # deliberately no age_ms: a stored opportunity has no freshness
        # claim, so revalidate() is the only thing standing between it and
        # a trade — which is exactly the intent
    }


def run_from_db(db, opportunity_id: int, budget: float, dry: bool):
    row = db.execute("SELECT * FROM opportunities WHERE id = ?",
                     (opportunity_id,)).fetchone()
    if row is None:
        log.error("No opportunity with id %d", opportunity_id)
        return
    result = execute(db, signal_from_opportunity_row(row),
                     budget=budget, dry=dry)
    log.info("Result: %s", result)


def run_watch(db, budget: float, dry: bool, top: int, min_edge: float):
    """Attach the executor to the live engine as its on_signal callback."""
    import live_engine

    def on_signal(signal: dict):
        log.info("Signal received (%.0fms old) — evaluating for execution",
                 signal.get("age_ms", 0))
        result = execute(db, signal, budget=budget, dry=dry)
        log.info("Execution result: %s", result.get("status"))
        if result.get("reason"):
            log.info("  reason: %s", result["reason"])

    engine = live_engine.LiveEngine(
        top_n=top,
        min_edge=max(min_edge, MIN_NET_EDGE_TO_EXECUTE),
        on_signal=on_signal,
        store=True,
    )
    asyncio.run(engine.run())


def self_check(dry: bool):
    """Print exactly what would happen, without touching the network."""
    print("=" * 60)
    print("Executor self-check")
    print("=" * 60)
    print(f"  mode                : {'DRY RUN' if dry else 'LIVE TRADING'}")
    print(f"  --live flag         : {'yes' if not dry else 'no'}")
    print(f"  POLYMARKET_ALLOW_LIVE: "
          f"{os.getenv('POLYMARKET_ALLOW_LIVE') or '(unset)'}")
    print(f"  private key set     : "
          f"{'yes' if os.getenv('POLYMARKET_PRIVATE_KEY') else 'no'}")
    print(f"  funder set          : "
          f"{'yes' if os.getenv('POLYMARKET_FUNDER_ADDRESS') else 'no'}")
    print(f"  kill switch ({KILL_SWITCH.name})   : "
          f"{'PRESENT — trading blocked' if KILL_SWITCH.exists() else 'absent'}")
    print(f"  max capital / trade : ${MAX_CAPITAL_PER_TRADE:,.2f}")
    print(f"  max trades / day    : {MAX_TRADES_PER_DAY}")
    print(f"  min net edge        : {MIN_NET_EDGE_TO_EXECUTE*100:.2f}%")
    print(f"  max signal age      : {MAX_SIGNAL_AGE_MS} ms")
    print(f"  config profile      : {config.PROFILE}")
    print(f"  database            : {dblib.DB_PATH}")

    db = dblib.connect()
    stuck = dblib.open_executions(db)
    print(f"  unfinished runs     : "
          f"{len(stuck)}{' — MUST BE RESOLVED' if stuck else ''}")


def main():
    parser = argparse.ArgumentParser(description="Polymarket arb executor")
    parser.add_argument("--live", action="store_true",
                        help="place REAL orders (also needs "
                             "POLYMARKET_ALLOW_LIVE=yes)")
    parser.add_argument("--budget", type=float, default=MAX_CAPITAL_PER_TRADE)
    parser.add_argument("--from-db", type=int, metavar="OPP_ID",
                        help="execute a stored opportunity by id")
    parser.add_argument("--watch", action="store_true",
                        help="drive from the live WebSocket engine")
    parser.add_argument("--top", type=int, default=config.LIVE_TOP_N)
    parser.add_argument("--min-edge", type=float, default=MIN_NET_EDGE_TO_EXECUTE)
    parser.add_argument("--check", action="store_true",
                        help="print config and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    dry = not live_trading_allowed(args.live)

    if args.check:
        self_check(dry)
        return

    if not dry:
        log.warning("!" * 60)
        log.warning("LIVE TRADING ENABLED — real orders, real money.")
        log.warning("Budget per trade: $%.2f | daily cap: %d trades",
                    min(args.budget, MAX_CAPITAL_PER_TRADE), MAX_TRADES_PER_DAY)
        log.warning("Create the file '%s' to stop immediately.", KILL_SWITCH.name)
        log.warning("!" * 60)
    else:
        log.info("DRY RUN — no orders will be sent.")

    db = dblib.connect()
    try:
        preflight(db, "live" if not dry else "dry")
    except ExecutionAborted as e:
        log.error("Preflight failed: %s", e)
        return

    if args.from_db is not None:
        run_from_db(db, args.from_db, args.budget, dry)
    elif args.watch:
        run_watch(db, args.budget, dry, args.top, args.min_edge)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
