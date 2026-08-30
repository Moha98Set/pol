"""
Paper wallet — replaying recorded windows with fake money.
==========================================================

Every other part of this system answers "did an edge exist". This one
answers the question that actually decides whether any of it was worth
building: **would it have made money.**

It replays `edge_windows` and `edge_ticks` rather than trading live,
because "is this trustworthy" is a statistical question. The same history
has to be runnable again with a different edge floor or a smaller wallet,
and the two results compared. A live run cannot be repeated.

Three rules keep the answer honest
----------------------------------

1. NEVER BUY AT THE BEST TICK. A backtest that enters at the bottom of the
   window is describing a trade nobody could place. Entry is the first
   tick at or after the window's open plus real execution latency: the
   signal-age gate, a book refetch, and one order per leg placed in
   sequence. On a five-leg basket that is well over a second, and the
   price has usually moved.

2. NEVER DEPLOY MORE THAN THE BOOK COULD TAKE. Each tick carries what the
   book actually absorbed at that instant. Ignoring it produces the
   fantasy profits every naive backtest reports.

3. RECORD THE REFUSALS. "We saw 40 windows and traded 3" says nothing
   without the other 37. An edge too thin and a wallet too small are
   different problems with different fixes, and only the reasons separate
   them.

What it does not model
----------------------

Selling the basket back. `edge_ticks` stores the side the basket is
bought from; selling means hitting the other side of the book, which is
not recorded. So the headline number is hold-to-resolution, where the
profit is fixed at purchase and no exit price is needed. An optimistic
figure — assuming every basket could be sold back at its own buy price —
is reported alongside, clearly labelled, as the ceiling it is.

Run:
    python paper.py                        # replay everything recorded
    python paper.py --cash 5000            # a bigger wallet
    python paper.py --min-window 10        # only windows over 10s
    python paper.py --compare              # against taking every window
    python paper.py --show 12              # the last run's trades
"""

import argparse
import json
import sqlite3
from datetime import datetime, timezone

import config
import db as dblib


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# Why a window was refused. Named rather than numbered so a run's summary
# reads as sentences.
SKIP_SHORT = "window_too_short"
SKIP_EDGE = "edge_too_thin"
SKIP_LEGS = "too_many_legs"
SKIP_NO_TICKS = "no_tick_after_latency"
SKIP_GONE = "edge_gone_by_entry"
SKIP_THIN = "book_too_thin"
SKIP_SMALL = "position_below_minimum"
SKIP_BROKE = "not_enough_cash"

SKIP_LABELS = {
    SKIP_SHORT: "پنجره کوتاه‌تر از حداقل",
    SKIP_EDGE: "لبه کمتر از آستانه",
    SKIP_LEGS: "پاهای بیش از حد",
    SKIP_NO_TICKS: "تیکی بعد از تأخیر اجرا نبود",
    SKIP_GONE: "لبه تا لحظه‌ی ورود از بین رفت",
    SKIP_THIN: "عمق دفتر کافی نبود",
    SKIP_SMALL: "اندازه‌ی موقعیت زیر حداقل",
    SKIP_BROKE: "پول کافی در کیف نبود",
    "taken": "خریداری شد",
}


class Wallet:
    """
    Cash, and baskets bought with it.

    A basket pays out when its market settles, and only then does the
    capital come back — with the profit on top, ready to be used again.
    Until it settles the money is real but unusable, which is why `locked`
    is tracked separately from `cash`: a wallet that traded once and one
    that recycled the same money twenty times end with the same profit
    per trade and completely different totals.

    Settlement needs a resolution date. Windows recorded before that was
    stored carry none, and their capital stays locked for the whole
    replay — pessimistic, but never invented.
    """

    def __init__(self, cash: float):
        self.start = cash
        self.cash = cash
        self.locked = 0.0
        self.realised = 0.0        # profit fixed at purchase, net of fees
        self.fees = 0.0            # what the exchange took, in total
        self.optimistic = 0.0
        self.settled = 0           # baskets that paid out during the run
        self.unsettled = 0         # still holding at the end
        self.wins = 0
        self.losses = 0
        # (settles_at, capital, fee, profit, window)
        self.open_positions = []
        # every movement of money, in order
        self.ledger = []

    @property
    def equity(self) -> float:
        """Cash plus what is inside open baskets — the real total."""
        return self.cash + self.locked

    def _record(self, kind, amount, at, window=None, capital=None,
                fee=None, profit=None):
        self.ledger.append({
            "seq": len(self.ledger) + 1, "kind": kind, "at": at,
            "amount": amount,
            "window_id": window["id"] if window is not None else None,
            "event_slug": window["event_slug"] if window is not None else None,
            "event_title": (window["event_title"]
                            if window is not None else None),
            "capital": capital, "fee": fee, "profit": profit,
            "balance_after": self.cash, "locked_after": self.locked,
            "equity_after": self.equity,
        })

    def buy(self, capital: float, fee: float, profit: float,
            optimistic: float, settles_at=None, at=None, window=None):
        """
        Cash out is the shares *and* the fee. Settlement later returns
        both plus the profit, so the net effect of a round trip is exactly
        the profit — but while the basket is held, the fee is money the
        wallet does not have. Deducting only the capital would let it
        commit to a trade it could not actually pay for.
        """
        self.cash -= capital + fee
        self.locked += capital + fee
        self.realised += profit
        self.fees += fee
        self.optimistic += optimistic
        self.open_positions.append((settles_at, capital, fee, profit, window))
        self._record("buy", -(capital + fee), at, window,
                     capital=capital, fee=fee, profit=profit)

    def settle_due(self, now) -> int:
        """
        Return capital and profit for every basket whose market has
        settled by `now`. Called as the replay walks forward in time, so
        money freed by an early trade is available to a later one — which
        is the whole difference between a wallet and a running total.
        """
        if now is None:
            return 0
        still_open, freed = [], 0
        for settles_at, capital, fee, profit, window in self.open_positions:
            if settles_at is not None and settles_at <= now:
                self.cash += capital + fee + profit
                self.locked -= capital + fee
                self.settled += 1
                if profit >= 0:
                    self.wins += 1
                else:
                    self.losses += 1
                freed += 1
                self._record("settle", capital + fee + profit, settles_at,
                             window, capital=capital, fee=fee, profit=profit)
            else:
                still_open.append((settles_at, capital, fee, profit, window))
        self.open_positions = still_open
        return freed

    def finish(self):
        self.unsettled = len(self.open_positions)


def entry_latency_ms(num_legs: int) -> int:
    """
    How long entering actually takes, and therefore how stale the price is
    by the time an order lands.

    Legs are placed one after another, not together, so a wide basket is
    slower — and a window that a two-leg basket can catch may already be
    gone for a ten-leg one.
    """
    return (config.PAPER_LATENCY_BASE_MS
            + config.PAPER_LATENCY_PER_LEG_MS * max(num_legs, 1))


def replay(db, *, cash=None, min_window_ms=None, min_edge=None,
           max_per_trade=None, min_capital=None, max_legs=None,
           take_everything=False, label=None) -> dict:
    """
    Walk every recorded window in order and decide what a wallet would
    have done. Returns the run summary; the detail is in paper_decisions.
    """
    cash = config.PAPER_START_CASH if cash is None else cash
    min_window_ms = (config.PAPER_MIN_WINDOW_MS if min_window_ms is None
                     else min_window_ms)
    min_edge = config.PAPER_MIN_EDGE if min_edge is None else min_edge
    max_per_trade = (config.PAPER_MAX_PER_TRADE if max_per_trade is None
                     else max_per_trade)
    min_capital = (config.PAPER_MIN_CAPITAL if min_capital is None
                   else min_capital)
    max_legs = config.PAPER_MAX_LEGS if max_legs is None else max_legs

    params = {
        "cash": cash, "min_window_ms": min_window_ms, "min_edge": min_edge,
        "max_per_trade": max_per_trade, "min_capital": min_capital,
        "max_legs": max_legs, "take_everything": take_everything,
        "latency_base_ms": config.PAPER_LATENCY_BASE_MS,
        "latency_per_leg_ms": config.PAPER_LATENCY_PER_LEG_MS,
    }

    cur = db.execute("""
        INSERT INTO paper_runs (started_at, label, params, start_cash)
        VALUES (?, ?, ?, ?)
    """, (utcnow(), label, json.dumps(params), cash))
    run_id = cur.lastrowid
    db.commit()

    wallet = Wallet(cash)
    decisions = []
    seen = taken = 0

    windows = db.execute("""
        SELECT * FROM edge_windows
        WHERE closed_at IS NOT NULL ORDER BY opened_at
    """).fetchall()

    for w in windows:
        seen += 1
        # Baskets settle as the replay walks forward, so money returned by
        # an earlier trade can fund a later one. Without this the wallet
        # would drain once and stay drained, which understates how many
        # trades the same capital could actually support.
        wallet.settle_due(w["opened_at"])
        row = {
            "run_id": run_id, "window_id": w["id"], "decided_at": utcnow(),
            "event_slug": w["event_slug"], "event_title": w["event_title"],
            "side": w["side"], "num_outcomes": w["num_outcomes"],
            "payout": w["payout"], "fee_rate": w["fee_rate"],
            "window_ms": w["duration_ms"], "best_edge": w["best_edge"],
            "best_sum_asks": w["best_sum_asks"],
            "taken": 0, "reason": None,
            "entry_ms": None, "entry_sum_asks": None, "entry_edge": None,
            "shares": None, "capital": None, "profit": None,
            "fee": None, "fillable_capital": None,
        }

        legs = w["num_outcomes"] or 2

        if not take_everything:
            if (w["duration_ms"] or 0) < min_window_ms:
                row["reason"] = SKIP_SHORT
                decisions.append(row)
                continue
            if legs > max_legs:
                row["reason"] = SKIP_LEGS
                decisions.append(row)
                continue

        # --- entry price: the first tick a real order could have reached
        latency = entry_latency_ms(legs)
        ticks = db.execute("""
            SELECT * FROM edge_ticks WHERE window_id = ? ORDER BY ts_ms
        """, (w["id"],)).fetchall()
        if not ticks:
            row["reason"] = SKIP_NO_TICKS
            decisions.append(row)
            continue

        opened_ms = ticks[0]["ts_ms"]
        entry = next((t for t in ticks
                      if t["ts_ms"] - opened_ms >= latency), None)
        if entry is None:
            # the window closed before an order could have landed
            row["reason"] = SKIP_NO_TICKS
            decisions.append(row)
            continue

        row["entry_ms"] = entry["ts_ms"] - opened_ms
        row["entry_sum_asks"] = entry["sum_best_asks"]
        row["entry_edge"] = entry["net_edge"]
        row["fillable_capital"] = entry["fillable_capital"]

        edge = entry["net_edge"]
        if edge is None:
            row["reason"] = SKIP_GONE
            decisions.append(row)
            continue

        if not take_everything and edge < min_edge:
            # it may have been good when the window opened; by the time an
            # order could land it was not
            row["reason"] = SKIP_GONE if (w["best_edge"] or 0) >= min_edge \
                else SKIP_EDGE
            decisions.append(row)
            continue

        sum_asks = entry["sum_best_asks"]
        if not sum_asks or sum_asks <= 0:
            row["reason"] = SKIP_GONE
            decisions.append(row)
            continue

        fillable = entry["fillable_capital"] or 0.0
        if fillable <= 0:
            row["reason"] = SKIP_THIN
            decisions.append(row)
            continue

        # net_edge = payout - sum_asks - fee_per_share, so the fee falls
        # out of numbers already recorded. Taken this way rather than
        # recomputed from fees.py, so this file cannot drift away from
        # what the engine actually measured.
        payout = w["payout"] or 1.0
        fee_per_share = max(payout - sum_asks - edge, 0.0)

        # --- size: capped by the book, the per-trade limit, and the cash
        #
        # The wallet pays for the shares *and* the fee, so the cash cap has
        # to be applied to both together. Sizing on the capital alone and
        # adding the fee afterwards overdraws the wallet by exactly the
        # fee — small per trade, and a negative balance in the ledger.
        cost_per_share = sum_asks + fee_per_share
        shares = min(fillable / sum_asks,
                     max_per_trade / sum_asks,
                     wallet.cash / cost_per_share if cost_per_share else 0)

        capital = shares * sum_asks
        fee = shares * fee_per_share
        profit = shares * edge

        if capital < min_capital:
            row["reason"] = (SKIP_BROKE
                             if wallet.cash < min_capital + fee
                             else SKIP_SMALL)
            decisions.append(row)
            continue

        # Selling the basket back would need the other side of the book,
        # which is not recorded. Buying and selling at the same price is
        # the ceiling, never the fill.
        optimistic = profit

        wallet.buy(capital, fee, profit, optimistic,
                   settles_at=w["end_date"] if "end_date" in w.keys() else None,
                   at=w["opened_at"], window=w)
        taken += 1
        row.update(taken=1, reason="taken", shares=shares,
                   capital=capital, profit=profit, fee=fee)
        decisions.append(row)

    wallet.finish()
    _save_decisions(db, decisions)
    _save_ledger(db, run_id, wallet.ledger)

    db.execute("""
        UPDATE paper_runs SET finished_at = ?, windows_seen = ?, trades = ?,
            skipped = ?, end_cash = ?, locked = ?, realised_profit = ?,
            optimistic_profit = ?, fees_paid = ?, wins = ?, losses = ?
        WHERE id = ?
    """, (utcnow(), seen, taken, seen - taken, wallet.cash, wallet.locked,
          wallet.realised, wallet.optimistic, wallet.fees,
          wallet.wins, wallet.losses, run_id))
    db.commit()

    return {
        "run_id": run_id, "windows": seen, "trades": taken,
        "skipped": seen - taken, "start_cash": wallet.start,
        "cash": wallet.cash, "locked": wallet.locked,
        "realised": wallet.realised, "optimistic": wallet.optimistic,
        "fees": wallet.fees,
        "settled": wallet.settled, "unsettled": wallet.unsettled,
        "wins": wallet.wins, "losses": wallet.losses,
        "equity": wallet.equity,
    }


def _save_decisions(db, rows):
    if not rows:
        return
    db.executemany("""
        INSERT INTO paper_decisions (
            run_id, window_id, decided_at, event_slug, event_title, side,
            num_outcomes, payout, fee_rate, taken, reason, window_ms,
            best_edge, best_sum_asks, entry_ms, entry_sum_asks, entry_edge,
            shares, capital, fee, profit, fillable_capital
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [(r["run_id"], r["window_id"], r["decided_at"], r["event_slug"],
           r["event_title"], r["side"], r["num_outcomes"], r["payout"],
           r["fee_rate"], r["taken"], r["reason"], r["window_ms"],
           r["best_edge"], r["best_sum_asks"], r["entry_ms"],
           r["entry_sum_asks"], r["entry_edge"], r["shares"], r["capital"],
           r["fee"], r["profit"], r["fillable_capital"]) for r in rows])
    db.commit()


def _save_ledger(db, run_id: int, entries: list):
    if not entries:
        return
    db.executemany("""
        INSERT INTO paper_ledger (run_id, seq, at, kind, window_id,
            event_slug, event_title, amount, capital, fee, profit,
            balance_after, locked_after, equity_after)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [(run_id, e["seq"], e["at"], e["kind"], e["window_id"],
           e["event_slug"], e["event_title"], e["amount"], e["capital"],
           e["fee"], e["profit"], e["balance_after"], e["locked_after"],
           e["equity_after"]) for e in entries])
    db.commit()


def skip_breakdown(db, run_id: int) -> list:
    return db.execute("""
        SELECT reason, COUNT(*) n FROM paper_decisions
        WHERE run_id = ? GROUP BY reason ORDER BY n DESC
    """, (run_id,)).fetchall()


# =====================================================================
# Reporting
# =====================================================================


def print_run(db, summary: dict):
    s = summary
    ret = (s["realised"] / s["start_cash"] * 100) if s["start_cash"] else 0

    print()
    print("=" * 62)
    print(f"  کیف پول کاغذی — اجرای #{s['run_id']}")
    print("=" * 62)
    print(f"  سرمایه‌ی اولیه      ${s['start_cash']:,.2f}")
    print(f"  پنجره‌های بررسی‌شده  {s['windows']:,}")
    print(f"  معامله‌شده           {s['trades']:,}")
    print(f"  رد شده              {s['skipped']:,}")
    print()
    print(f"  سرمایه‌ی قفل‌شده     ${s['locked']:,.2f}"
          f"   ({s.get('unsettled', 0)} سبد تسویه‌نشده)")
    print(f"  نقد باقی‌مانده      ${s['cash']:,.2f}")
    print(f"  سود قفل‌شده         ${s['realised']:,.2f}   ({ret:+.2f}٪)")
    if s.get("fees"):
        gross = s["realised"] + s["fees"]
        share = s["fees"] / gross * 100 if gross else 0
        print(f"  کارمزد پرداختی      ${s['fees']:,.2f}"
              f"   ({share:.1f}٪ از سود ناخالص)")
    if s.get("settled"):
        print(f"  تسویه‌شده           {s['settled']} سبد — "
              f"سرمایه‌شان دوباره قابل استفاده شد")
        wins, losses = s.get("wins", 0), s.get("losses", 0)
        if wins + losses:
            rate = wins / (wins + losses) * 100
            print(f"  سودده / زیان‌ده     {wins} / {losses}"
                  f"   (نرخ موفقیت {rate:.0f}٪)")
    print()

    if s["trades"] == 0:
        print("  هیچ معامله‌ای انجام نشد. دلایل زیر را ببینید — اگر بیشترشان")
        print("  «پنجره کوتاه» است، مشکل بازار است؛ اگر «پول کافی نبود»،")
        print("  مشکل اندازه‌ی کیف است.")
        print()

    print("  چرا پنجره‌ها معامله نشدند:")
    for r in skip_breakdown(db, s["run_id"]):
        label = SKIP_LABELS.get(r["reason"], r["reason"])
        share = r["n"] / s["windows"] * 100 if s["windows"] else 0
        print(f"      {label:<34} {r['n']:>6}  {share:>5.1f}٪")
    print()
    if s.get("wins") and not s.get("losses"):
        print("  نرخ موفقیت ۱۰۰٪ است چون قاعده‌ی ورود فقط لبه‌ی مثبت می‌خرد و")
        print("  سود در همان لحظه قفل می‌شود. زیان واقعی از جایی می‌آید که این")
        print("  شبیه‌سازی نمی‌بیند: پر نشدن یکی از پاها و ماندن با موقعیت")
        print("  پوشش‌نداده، یا بازاری که واقعاً انحصار متقابل نداشته. تا وقتی")
        print("  آن‌ها مدل نشوند، این عدد را نباید نشانه‌ی بی‌خطر بودن گرفت.")
        print()
    print("  توجه: سود در لحظه‌ی خرید تعیین می‌شود، ولی پول تا تسویه‌ی بازار")
    print("  برنمی‌گردد. پنجره‌هایی که پیش از ثبت تاریخ تسویه ضبط شده‌اند تاریخ")
    print("  ندارند و سرمایه‌شان تا پایان اجرا قفل می‌ماند — بدبینانه، ولی")
    print("  ساختگی نیست. فروش زودهنگام هم شبیه‌سازی نشده، چون سمت فروشِ")
    print("  دفتر ثبت نمی‌شود.")
    print("=" * 62)


def print_trades(db, run_id: int, limit: int = 20):
    rows = db.execute("""
        SELECT * FROM paper_decisions WHERE run_id = ? AND taken = 1
        ORDER BY profit DESC LIMIT ?
    """, (run_id, limit)).fetchall()
    if not rows:
        print("  این اجرا معامله‌ای نداشت.")
        return

    print(f"\n  {'بازار':<38} {'سمت':<5} {'ورود ms':>8} "
          f"{'قیمت':>8} {'سرمایه':>10} {'سود':>9}")
    print("  " + "-" * 84)
    for r in rows:
        title = (r["event_title"] or r["event_slug"] or "")[:36]
        print(f"  {title:<38} {r['side'] or '?':<5} {r['entry_ms'] or 0:>8} "
              f"{r['entry_sum_asks'] or 0:>8.4f} "
              f"${r['capital'] or 0:>9,.2f} ${r['profit'] or 0:>8,.2f}")


def print_ledger(db, run_id: int, limit: int = 40):
    rows = db.execute("""
        SELECT * FROM paper_ledger WHERE run_id = ? ORDER BY seq LIMIT ?
    """, (run_id, limit)).fetchall()
    if not rows:
        print("  این اجرا حرکتی در کیف نداشت.")
        return

    print(f"\n  {'#':>3} {'تاریخ':<12} {'رویداد':<8} {'بازار':<26} "
          f"{'تغییر':>11} {'نقد':>11} {'کل دارایی':>11}")
    print("  " + "-" * 92)
    for r in rows:
        kind = "خرید" if r["kind"] == "buy" else "تسویه"
        when = (r["at"] or "")[:10]
        title = (r["event_title"] or r["event_slug"] or "")[:24]
        print(f"  {r['seq']:>3} {when:<12} {kind:<8} {title:<26} "
              f"{r['amount']:>+11,.2f} {r['balance_after']:>11,.2f} "
              f"{r['equity_after']:>11,.2f}")


def main():
    parser = argparse.ArgumentParser(
        description="Paper wallet over recorded windows")
    parser.add_argument("--cash", type=float)
    parser.add_argument("--min-window", type=float, metavar="SECONDS",
                        help="minimum window length to enter")
    parser.add_argument("--min-edge", type=float, metavar="PERCENT",
                        help="minimum net edge, in percent")
    parser.add_argument("--max-per-trade", type=float)
    parser.add_argument("--label")
    parser.add_argument("--compare", action="store_true",
                        help="also replay taking every window, as a control")
    parser.add_argument("--show", type=int, nargs="?", const=20,
                        metavar="N", help="print the best N trades")
    parser.add_argument("--ledger", type=int, nargs="?", const=40,
                        metavar="N", help="print the first N wallet movements")
    args = parser.parse_args()

    db = dblib.connect()
    db.row_factory = sqlite3.Row

    kwargs = {}
    if args.cash is not None:
        kwargs["cash"] = args.cash
    if args.min_window is not None:
        kwargs["min_window_ms"] = args.min_window * 1000
    if args.min_edge is not None:
        kwargs["min_edge"] = args.min_edge / 100.0
    if args.max_per_trade is not None:
        kwargs["max_per_trade"] = args.max_per_trade

    summary = replay(db, label=args.label or "filtered", **kwargs)
    print_run(db, summary)
    if args.show:
        print_trades(db, summary["run_id"], args.show)
    if args.ledger:
        print_ledger(db, summary["run_id"], args.ledger)

    if args.compare:
        # The control: no filters at all. If the filtered run does not beat
        # it, the filters are costing more than they save.
        control = replay(db, label="control (every window)",
                         take_everything=True, **kwargs)
        print_run(db, control)
        print()
        print(f"  فیلترشده  ${summary['realised']:>9,.2f} "
              f"در {summary['trades']} معامله")
        print(f"  بدون فیلتر ${control['realised']:>9,.2f} "
              f"در {control['trades']} معامله")
        better = summary["realised"] > control["realised"]
        print(f"\n  {'فیلترها ارزش داشتند.' if better else 'فیلترها سود را بیشتر نکردند — بازنگری کنید.'}")

    db.close()


if __name__ == "__main__":
    main()
