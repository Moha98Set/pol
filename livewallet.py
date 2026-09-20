"""
The live paper wallet.
======================

`paper.py` replays recorded windows. It is fast and repeatable, which is
what makes parameter tuning possible, but it has one weakness it cannot
fix: it decides *when* an order would have landed using a formula —

    400ms + 300ms per leg

— and then looks up the tick nearest that moment. If the formula is
optimistic, every number it produces is optimistic with it, and nothing
in the replay can tell you so.

This module removes the formula. It runs inside the live engine, and when
a signal fires it does not buy. It waits — really waits, on the clock —
for as long as placing the orders would take, and then prices the basket
against the book *as it is at that moment*. If the edge evaporated during
the wait, that is recorded as a refusal with the real elapsed time on it.

What that buys us, and what it does not
---------------------------------------

It answers "was that tick actionable", which the replay only assumes. It
measures the real distance between seeing an edge and being able to act,
so the replay's latency constants can be checked against something.

It does NOT answer the question that matters most: whether all the legs
would actually fill. No orders are placed, so a basket that would have
half-filled and left an unhedged position looks here exactly like one
that filled cleanly. Only real money answers that.

And it is slow. Roughly three trades a day on this market. It is the
validation, never the search.

Money
-----

Identical to the replay's wallet, deliberately, so the two are
comparable: cash out is capital *plus* fee, both come back at settlement
with the profit on top, and `locked` is tracked apart from `cash` because
a wallet that recycled the same dollar twenty times and one that spent it
once are not the same wallet.

State lives in the database rather than in memory, because a service
restart must not silently reset the experiment.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone

import config

log = logging.getLogger("livewallet")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# Why a signal did not become a trade. Same vocabulary as the replay so
# the two runs' breakdowns can be read side by side.
SKIP_EDGE_GONE = "edge_gone_by_entry"
SKIP_LEGS_GONE = "legs_unavailable"
SKIP_THIN = "book_too_thin"
SKIP_SMALL = "position_below_minimum"
SKIP_BROKE = "not_enough_cash"
SKIP_LEGS = "too_many_legs"
SKIP_DUPLICATE = "already_holding"
SKIP_SLOW = "return_too_slow"

SKIP_LABELS = {
    SKIP_SLOW: "بازده به‌ازای مدت قفل‌شدن پول کم بود",
    SKIP_EDGE_GONE: "لبه تا لحظه‌ی ورود از بین رفت",
    SKIP_LEGS_GONE: "پاها تا لحظه‌ی ورود در دسترس نبودند",
    SKIP_THIN: "عمق دفتر کافی نبود",
    SKIP_SMALL: "اندازه‌ی موقعیت زیر حداقل",
    SKIP_BROKE: "پول کافی در کیف نبود",
    SKIP_LEGS: "پاهای بیش از حد",
    SKIP_DUPLICATE: "همین بازار از قبل در سبد بود",
    "taken": "خریداری شد",
}


def execution_delay_ms(num_legs: int) -> float:
    """
    How long placing the orders would take, and therefore how long to wait
    before pricing the entry.

    The same constants the replay uses — on purpose. This module exists to
    test them, and it can only do that by applying them and reporting what
    the market did during the wait.
    """
    return float(config.PAPER_LATENCY_BASE_MS
                 + config.PAPER_LATENCY_PER_LEG_MS * max(num_legs, 1))


def days_until(end_date, now=None) -> float:
    """
    Days from now until the market settles, or None if it does not say.

    Floored at a day: a market resolving in an hour would otherwise divide
    a small profit into an enormous annual rate and wave through every
    trade the test exists to catch.
    """
    if not end_date:
        return None
    try:
        end = datetime.fromisoformat(str(end_date).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max((end - now).total_seconds() / 86400.0, 1.0)


def annual_pct(profit: float, capital: float, days: float):
    """
    The trade's return restated as a rate per year.

    Profit alone cannot be compared across trades: $0.94 on $250 for seven
    days and $2.25 on $47 for eight are not close, and the raw numbers say
    the opposite of the truth.
    """
    if not capital or not days or days <= 0:
        return None
    return profit / capital * 365.0 / days * 100.0


def leg_prices(watched, result, side: str) -> list:
    """
    The individual legs behind a basket, at the price entry would have paid.

    Taken from the re-priced entry rather than from the signal: a basket is
    bought a second or two after the edge appears, and quoting the signal's
    prices here would describe orders that were never placed. A NO basket's
    legs are named for the side actually bought, because "Yes" against a
    position that is short it is the one label that could mislead.
    """
    asks = (result or {}).get("leg_best_asks") or []
    legs = []
    for i, (name, _token_id) in enumerate(getattr(watched, "legs", []) or []):
        legs.append({
            "outcome": f"NO {name}" if side == "no" else name,
            "price": asks[i] if i < len(asks) else None,
        })
    return legs


class LiveWallet:
    """
    One wallet, persisted, fed by the live engine's signals.

    `price_now(slug)` is supplied by the engine: it re-prices an event
    against the current books and returns (side, result, edge), or
    (None, None, None) if the legs are no longer all available.
    """

    def __init__(self, db, price_now, *, start_cash=None, max_per_trade=None,
                 min_capital=None, min_edge=None, max_legs=None,
                 min_annual_pct=None, exit_min_fraction=None,
                 delay_ms=execution_delay_ms):
        self.db = db
        self.price_now = price_now
        self.delay_ms = delay_ms
        # One re-entrant lock around everything that touches the database
        # or the balance.
        #
        # Two reasons, and both are load-bearing. Decisions are independent
        # timers, so two can land at once, and sizing against a cash figure
        # that another thread is about to spend is how a wallet overdraws.
        # And the connection is opened with check_same_thread=False,
        # because asyncio hands each decision to whatever worker thread is
        # free — which makes this lock the only thing keeping two threads
        # out of SQLite at the same moment.
        self.lock = threading.RLock()

        self.start_cash = (config.PAPER_START_CASH if start_cash is None
                           else start_cash)
        self.max_per_trade = (config.PAPER_MAX_PER_TRADE
                              if max_per_trade is None else max_per_trade)
        self.min_capital = (config.PAPER_MIN_CAPITAL if min_capital is None
                            else min_capital)
        self.min_edge = config.PAPER_MIN_EDGE if min_edge is None else min_edge
        self.max_legs = config.PAPER_MAX_LEGS if max_legs is None else max_legs
        self.min_annual_pct = (config.PAPER_MIN_ANNUAL_PCT
                               if min_annual_pct is None else min_annual_pct)
        self.exit_min_fraction = (config.PAPER_EXIT_MIN_FRACTION
                                  if exit_min_fraction is None
                                  else exit_min_fraction)

        self.state = self._load_or_create()

    # -----------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------

    def _load_or_create(self) -> dict:
        row = self.db.execute("SELECT * FROM live_wallet WHERE id = 1"
                              ).fetchone()
        if row is not None:
            state = dict(row)
            log.info("Live wallet resumed: $%.2f cash, $%.2f locked, "
                     "%d trade(s) so far",
                     state["cash"], state["locked"], state["trades"])
            return state

        params = {
            "start_cash": self.start_cash,
            "max_per_trade": self.max_per_trade,
            "min_capital": self.min_capital,
            "min_edge": self.min_edge,
            "max_legs": self.max_legs,
            "latency_base_ms": config.PAPER_LATENCY_BASE_MS,
            "latency_per_leg_ms": config.PAPER_LATENCY_PER_LEG_MS,
        }
        self.db.execute("""
            INSERT INTO live_wallet (id, created_at, updated_at, start_cash,
                cash, locked, params)
            VALUES (1, ?, ?, ?, ?, 0, ?)
        """, (utcnow(), utcnow(), self.start_cash, self.start_cash,
              json.dumps(params)))
        self.db.commit()
        log.info("Live wallet created with $%.2f", self.start_cash)
        return dict(self.db.execute(
            "SELECT * FROM live_wallet WHERE id = 1").fetchone())

    def _save(self):
        s = self.state
        s["updated_at"] = utcnow()
        self.db.execute("""
            UPDATE live_wallet SET updated_at = ?, cash = ?, locked = ?,
                realised_profit = ?, fees_paid = ?, wins = ?, losses = ?,
                gross_profit = ?, gross_loss = ?, trades = ?, settled = ?
            WHERE id = 1
        """, (s["updated_at"], s["cash"], s["locked"], s["realised_profit"],
              s["fees_paid"], s["wins"], s["losses"], s["gross_profit"],
              s["gross_loss"], s["trades"], s["settled"]))
        self.db.commit()

    @property
    def cash(self) -> float:
        return self.state["cash"]

    @property
    def locked(self) -> float:
        return self.state["locked"]

    @property
    def equity(self) -> float:
        return self.state["cash"] + self.state["locked"]

    def _ledger(self, kind, amount, position_id, slug, title,
                capital, fee, profit, num_outcomes=None):
        self.db.execute("""
            INSERT INTO live_ledger (at, kind, position_id, event_slug,
                event_title, amount, capital, fee, profit,
                balance_after, locked_after, equity_after, num_outcomes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (utcnow(), kind, position_id, slug, title, amount,
              capital, fee, profit, self.state["cash"], self.state["locked"],
              self.equity, num_outcomes))
        self.db.commit()

    def _record_decision(self, row):
        cur = self.db.execute("""
            INSERT INTO live_decisions (at, event_slug, event_title, side,
                num_outcomes, payout, fee_rate, taken, reason, position_id,
                signal_age_ms, planned_delay_ms, total_ms, signal_edge,
                entry_edge, signal_sum_asks, entry_sum_asks,
                fillable_capital, shares, capital, fee, profit,
                hold_days, annual_pct, signal_leg_skew_ms, entry_leg_skew_ms,
                capped_by, uncapped_capital, uncapped_profit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (row.get("at") or utcnow(), row.get("event_slug"),
              row.get("event_title"), row.get("side"),
              row.get("num_outcomes"), row.get("payout"), row.get("fee_rate"),
              row.get("taken", 0), row.get("reason"), row.get("position_id"),
              row.get("signal_age_ms"), row.get("planned_delay_ms"),
              row.get("total_ms"), row.get("signal_edge"),
              row.get("entry_edge"), row.get("signal_sum_asks"),
              row.get("entry_sum_asks"), row.get("fillable_capital"),
              row.get("shares"), row.get("capital"), row.get("fee"),
              row.get("profit"), row.get("hold_days"),
              row.get("annual_pct"), row.get("signal_leg_skew_ms"),
              row.get("entry_leg_skew_ms"), row.get("capped_by"),
              row.get("uncapped_capital"), row.get("uncapped_profit")))
        self.db.commit()
        return cur.lastrowid

    # -----------------------------------------------------------------
    # Holding
    # -----------------------------------------------------------------

    def holds(self, slug: str) -> bool:
        """
        Already holding an unsettled basket on this event.

        Buying a second one is not diversification — it is the same bet
        twice, and it would let one market absorb the whole wallet while
        the signal flickers.
        """
        with self.lock:
            return self.db.execute("""
                SELECT 1 FROM live_positions
                WHERE event_slug = ? AND settled_at IS NULL LIMIT 1
            """, (slug,)).fetchone() is not None

    def open_positions(self) -> list:
        with self.lock:
            return self.db.execute("""
                SELECT * FROM live_positions WHERE settled_at IS NULL
                ORDER BY opened_at
            """).fetchall()

    # -----------------------------------------------------------------
    # Deciding
    # -----------------------------------------------------------------

    def consider(self, signal: dict, watched) -> dict:
        """
        Price the entry and buy if it still stands.

        Call this *after* waiting out the execution delay — the caller owns
        the wait so the engine's event loop is never blocked by it. `signal`
        is the payload the engine emitted; `watched` is the event to
        re-price.
        """
        with self.lock:
            return self._consider(signal, watched)

    def _consider(self, signal: dict, watched) -> dict:
        legs = signal.get("num_outcomes") or 2
        row = {
            "taken": 0,
            "event_slug": signal.get("event_slug"),
            "event_title": signal.get("event_title"),
            "side": signal.get("side"),
            "num_outcomes": legs,
            "payout": signal.get("payout_per_basket", 1.0),
            "fee_rate": signal.get("fee_rate"),
            "signal_age_ms": signal.get("age_ms"),
            "planned_delay_ms": signal.get("planned_delay_ms"),
            "signal_edge": signal.get("best_net_edge"),
            "signal_sum_asks": signal.get("best_sum_asks"),
            "signal_leg_skew_ms": signal.get("leg_skew_ms"),
        }
        skew_now = getattr(self, "skew_now", None)
        if skew_now is not None:
            try:
                row["entry_leg_skew_ms"] = skew_now(watched)
            except Exception:
                row["entry_leg_skew_ms"] = None
        opened_ts = signal.get("first_seen_ts")
        row["total_ms"] = ((time.time() - opened_ts) * 1000
                           if opened_ts else None)

        if legs > self.max_legs:
            row["reason"] = SKIP_LEGS
            self._record_decision(row)
            return row

        if self.holds(row["event_slug"]):
            row["reason"] = SKIP_DUPLICATE
            self._record_decision(row)
            return row

        # --- the entry price: the book as it is now, not as it was
        side, result, edge = self.price_now(watched)
        if result is None or edge is None:
            row["reason"] = SKIP_LEGS_GONE
            self._record_decision(row)
            return row

        best = result.get("best") or {}
        row["entry_edge"] = edge
        row["entry_sum_asks"] = result.get("sum_best_asks")
        row["fillable_capital"] = best.get("real_cost")

        if side != row["side"]:
            # the profitable side flipped during the wait; that is a
            # different trade from the one the signal described
            row["reason"] = SKIP_EDGE_GONE
            self._record_decision(row)
            return row

        if edge < self.min_edge or not best:
            row["reason"] = SKIP_EDGE_GONE
            self._record_decision(row)
            return row

        sum_asks = result.get("sum_best_asks")
        if not sum_asks or sum_asks <= 0:
            row["reason"] = SKIP_EDGE_GONE
            self._record_decision(row)
            return row

        # real_cost, not the requested rung: the ladder returns the capital
        # asked for whether or not the book could absorb it
        fillable = best.get("real_cost") or 0.0
        if fillable <= 0:
            row["reason"] = SKIP_THIN
            self._record_decision(row)
            return row

        payout = row["payout"] or 1.0
        # Derived from what was measured rather than recomputed, so this
        # file cannot drift away from the engine's own arithmetic.
        fee_per_share = max(payout - sum_asks - edge, 0.0)

        # Three ceilings, and which one binds is worth recording. A trade
        # the book or the per-trade cap limited is the system working; one
        # the balance limited is an opportunity the wallet was too small to
        # take, and those are invisible otherwise — the refusals carry no
        # size, and a position quietly shrunk to fit the cash still logs as
        # a plain purchase.
        cost_per_share = sum_asks + fee_per_share
        by_book = fillable / sum_asks
        by_cap = self.max_per_trade / sum_asks
        by_cash = self.cash / cost_per_share if cost_per_share else 0.0

        shares = min(by_book, by_cap, by_cash)
        affordable = min(by_book, by_cap)      # had the cash been there
        row["capped_by"] = ("cash" if by_cash < affordable else
                            "book" if by_book <= by_cap else "max_per_trade")
        row["uncapped_capital"] = affordable * sum_asks
        row["uncapped_profit"] = affordable * edge

        capital = shares * sum_asks
        fee = shares * fee_per_share
        profit = shares * edge

        if capital < self.min_capital:
            row["reason"] = (SKIP_BROKE
                             if self.cash < self.min_capital + fee
                             else SKIP_SMALL)
            self._record_decision(row)
            return row

        # Is the profit worth the wait? A basket pays nothing until its
        # market resolves, so a trade is really a loan of its capital for
        # however long that takes, and edge says nothing about the term.
        # Skipped when the event has no end date rather than guessed at:
        # refusing on missing data would silently drop whole categories.
        end_date = getattr(watched, "end_date", None)
        days = days_until(end_date)
        rate = annual_pct(profit, capital, days) if days else None
        row["hold_days"] = days
        row["annual_pct"] = rate
        if (self.min_annual_pct > 0 and rate is not None
                and rate < self.min_annual_pct):
            row["reason"] = SKIP_SLOW
            self._record_decision(row)
            return row

        position_id = self._buy(row, shares, capital, fee, profit,
                                signal.get("url"),
                                getattr(watched, "end_date", None),
                                leg_prices(watched, result, side))

        row.update(taken=1, reason="taken", shares=shares, capital=capital,
                   fee=fee, profit=profit, position_id=position_id)
        self._record_decision(row)

        log.info("PAPER BUY | %-3s | %s | $%.2f + $%.2f fee -> $%.2f profit "
                 "| edge %.3f%% (was %.3f%%) after %.0fms | cash $%.2f",
                 (row["side"] or "?").upper(),
                 (row["event_title"] or "")[:38], capital, fee, profit,
                 edge * 100, (row["signal_edge"] or 0) * 100,
                 row["total_ms"] or 0, self.cash)
        return row

    def _buy(self, row, shares, capital, fee, profit, url, end_date,
             legs=None) -> int:
        """Caller holds the lock."""
        cur = self.db.execute("""
            INSERT INTO live_positions (opened_at, event_slug, event_title,
                side, num_outcomes, payout, fee_rate, shares, capital, fee,
                profit, entry_sum_asks, end_date, url, legs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (utcnow(), row["event_slug"], row["event_title"], row["side"],
              row["num_outcomes"], row["payout"], row["fee_rate"], shares,
              capital, fee, profit, row["entry_sum_asks"], end_date, url,
              json.dumps(legs or [], ensure_ascii=False)))
        position_id = cur.lastrowid

        s = self.state
        # Cash out is the shares and the fee together. Settlement returns
        # both plus the profit, so a round trip nets exactly the profit —
        # but while the basket is held, the fee is money the wallet does
        # not have, and sizing as though it did overdraws by that much.
        s["cash"] -= capital + fee
        s["locked"] += capital + fee
        s["realised_profit"] += profit
        s["fees_paid"] += fee
        s["trades"] += 1
        if profit >= 0:
            s["wins"] += 1
            s["gross_profit"] += profit
        else:
            s["losses"] += 1
            s["gross_loss"] += profit
        self._save()
        self._ledger("buy", -(capital + fee), position_id, row["event_slug"],
                     row["event_title"], capital, fee, profit,
                     row.get("num_outcomes"))
        return position_id

    # -----------------------------------------------------------------
    # Settlement
    # -----------------------------------------------------------------

    # -----------------------------------------------------------------
    # Exiting early
    # -----------------------------------------------------------------

    def consider_exits(self, sell_now) -> int:
        """
        Sell back any basket the book is now willing to pay enough for.

        `sell_now(slug, side)` returns what one basket would fetch at the
        current bids, or None if it cannot be priced.

        The bar is deliberately set where an exit cannot cost anything: the
        proceeds, after the exit's own fee, must cover the whole purchase
        *and* at least `exit_min_fraction` of the profit that holding to
        resolution would have paid. At the default of 1.0 that means the
        same profit, sooner — never less profit for the convenience.

        Which makes this a liquidity tool, not a risk tool. Holding is
        already risk-free: we own every outcome and the payout is certain.
        What holding is not, is quick — the first live run had capital tied
        up for as long as 57 days while the wallet sat too empty to take
        the next opportunity. This turns the same dollar over faster on the
        rare occasions the market offers to let it.
        """
        with self.lock:
            sold = 0
            for p in self.open_positions():
                if self._maybe_exit(p, sell_now):
                    sold += 1
            return sold

    def _maybe_exit(self, p, sell_now) -> bool:
        """Caller holds the lock."""
        try:
            sum_bids = sell_now(p["event_slug"], p["side"])
        except Exception as e:
            log.error("Exit pricing failed for %s: %s", p["event_slug"], e)
            return False
        if not sum_bids or sum_bids <= 0:
            return False

        shares = p["shares"] or 0.0
        cost = (p["capital"] or 0.0) + (p["fee"] or 0.0)
        booked = p["profit"] or 0.0

        # The exit pays a fee of its own. Estimated as the entry's, because
        # the fee is a function of price and the price has not moved far —
        # an approximation, but one that errs against selling, which is the
        # safe direction for a test that is allowed to say no.
        exit_fee = p["fee"] or 0.0
        net = shares * sum_bids - exit_fee

        if net < cost + booked * self.exit_min_fraction:
            return False

        actual = net - cost
        s = self.state

        # Unwind what purchase booked before booking what happened. The
        # profit was recorded as certain the moment the basket was bought,
        # and selling early replaces that number rather than adding to it.
        if booked >= 0:
            s["wins"] -= 1
            s["gross_profit"] -= booked
        else:
            s["losses"] -= 1
            s["gross_loss"] -= booked
        s["realised_profit"] -= booked

        if actual >= 0:
            s["wins"] += 1
            s["gross_profit"] += actual
        else:
            s["losses"] += 1
            s["gross_loss"] += actual
        s["realised_profit"] += actual

        s["fees_paid"] += exit_fee
        s["cash"] += net
        s["locked"] -= cost
        s["settled"] += 1

        self.db.execute("""
            UPDATE live_positions SET settled_at = ?, settle_reason = 'sold',
                exit_proceeds = ?, exit_profit = ?, exit_sum_bids = ?
            WHERE id = ?
        """, (utcnow(), net, actual, sum_bids, p["id"]))
        self._save()
        self._ledger("sell", net, p["id"], p["event_slug"], p["event_title"],
                     p["capital"], exit_fee, actual, p["num_outcomes"])

        log.info("PAPER SELL | %s | $%.2f back for a $%.2f cost | "
                 "profit $%.2f (booked $%.2f) | cash $%.2f",
                 (p["event_title"] or "")[:38], net, cost, actual, booked,
                 s["cash"])
        return True

    def settle_event(self, slug: str, reason: str = "resolved") -> int:
        """
        The market resolved. Because we hold the whole basket, *which*
        outcome won does not matter — only that it is over and the payout
        is due.
        """
        with self.lock:
            return self._settle(self.db.execute("""
                SELECT * FROM live_positions
                WHERE event_slug = ? AND settled_at IS NULL
            """, (slug,)).fetchall(), reason)

    def settle_due(self, now: str = None) -> int:
        """
        Fall back to the scheduled end date.

        The resolution message is authoritative but only arrives while we
        are connected and still watching the market. A basket whose event
        left the watchlist would otherwise stay locked forever.
        """
        now = now or utcnow()
        with self.lock:
            return self._settle(self.db.execute("""
                SELECT * FROM live_positions
                WHERE settled_at IS NULL AND end_date IS NOT NULL
                  AND end_date <= ?
            """, (now,)).fetchall(), "end_date")

    def _settle(self, positions, reason: str) -> int:
        if not positions:
            return 0
        with self.lock:
            s = self.state
            for p in positions:
                capital = p["capital"] or 0.0
                fee = p["fee"] or 0.0
                profit = p["profit"] or 0.0
                s["cash"] += capital + fee + profit
                s["locked"] -= capital + fee
                s["settled"] += 1
                self.db.execute("""
                    UPDATE live_positions SET settled_at = ?, settle_reason = ?
                    WHERE id = ?
                """, (utcnow(), reason, p["id"]))
                self._save()
                self._ledger("settle", capital + fee + profit, p["id"],
                             p["event_slug"], p["event_title"],
                             capital, fee, profit, p["num_outcomes"])
                log.info("PAPER SETTLE | %s | +$%.2f (%s) | cash $%.2f",
                         (p["event_title"] or "")[:40],
                         capital + fee + profit, reason, self.cash)
        return len(positions)

    # -----------------------------------------------------------------
    # Reporting
    # -----------------------------------------------------------------

    def summary(self) -> dict:
        s = dict(self.state)
        s["equity"] = self.equity
        s["open"] = len(self.open_positions())
        return s
