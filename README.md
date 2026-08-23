# Polymarket Arbitrage Pipeline

Finds baskets on Polymarket whose legs are mutually exclusive and sum to
less than $1. Buying every leg then pays exactly $1 per share regardless of
outcome, so the difference is risk-free — before fees, slippage, and the
several ways the data can lie to you, which is what most of this code is
about.

```
profit per share = 1 - sum(best asks) - fees - slippage
```

Multi-outcome events are checked from both sides. Buying YES on every leg
pays $1 per basket, so it is arbitrage when the asks sum to less than one.
Buying NO on every leg pays $(N-1) — exactly one outcome wins, so N-1 of
the NOs pay out — which is arbitrage when the NO asks sum to less than
N-1. Since a NO ask at `p` is a YES bid at `1-p`, the second condition is
just "the basket can be sold for more than a dollar".

The two can never both be true (bids never exceed asks), and they cover
different halves of the price space, which is the point of checking both.
The NO side needs no extra API call — the NO book is the YES book's bid
side. Its returns are worse for the same dollar edge, though, because a NO
basket ties up about `N-1` dollars to earn it; everything is therefore
ranked by edge **per dollar**, never per basket.

## Run it

```bash
python arb_monitor.py           # scan every 15 minutes, store to SQLite
python query.py health          # is it working?
python query.py funnel          # where did all the events go?
```

## The pieces

| File | Does |
|---|---|
| `arb_monitor.py` | The loop. Scan, store, sleep, repeat. |
| `scanner.py` | Fetching, filtering, and assembling baskets. |
| `arbmath.py` | Order-book walking, sizing, profit. The money math. |
| `fees.py` | Polymarket's real per-leg fee model. |
| `validate.py` | Reasons to distrust data, each with a stable code. |
| `patterns.py` | Title patterns meaning the legs are not exclusive, with the reason for each. |
| `config.py` | Every tunable number, overridable from the environment. |
| `db.py` | SQLite schema and writes. |
| `metrics.py` | Funnel counters — why events were rejected. |
| `logging_setup.py` | Structured logs (text for humans, JSON for queries). |
| `recorder.py` | Freezes the raw input of a scan to disk. |
| `replay.py` | Re-runs the analysis against frozen input, offline. |
| `query.py` | Asks the database questions without SQL. |
| `live_engine.py` | WebSocket book tracking, sub-second edges. |
| `executor.py` | Places the trade. Dry-run by default. |
| `findmarket.py` | One-shot scan to a CSV. |
| `view_db.py` | Browse stored history. |

## Configuration

Nothing needs editing to change behaviour:

```bash
MIN_NET_EDGE=0.01 MIN_VOLUME_24H=5000 python arb_monitor.py
PROFILE=conservative python arb_monitor.py
python config.py                    # what am I actually running with?
```

On Windows, `VAR=value command` is not valid syntax — the settings must be
set first, as their own statement. In `cmd.exe`:

```
set PROFILE=smoke
python arb_monitor.py
```

In PowerShell:

```
$env:PROFILE = "smoke"
python arb_monitor.py
```

Settings persist for the rest of that terminal session; `set PROFILE=`
(cmd) or `Remove-Item Env:PROFILE` (PowerShell) clears one. Run
`python config.py` to see what is actually in effect — every value is
labelled with where it came from.

Profiles are coherent sets, not single knobs: `conservative` (higher edge
floor, more liquidity, smaller size), `research` (catch everything, execute
nothing), `smoke` (fast and small, for checking the plumbing).

The full resolved configuration is logged at startup, so any run can be
reproduced from its own output.

## Debugging

The hard part of this project is that the input is gone by the time you
want to look at it. An order book that produced a suspicious signal at
14:32 does not exist at 14:33. So record it:

```bash
RECORD=1 python arb_monitor.py                     # freeze every book seen
python replay.py                                   # replay the newest recording
python replay.py rec.jsonl.gz --slug who-wins      # one event, step by step
```

`--slug` prints every leg's depth and each stage of the arithmetic
separately, so a wrong answer can be traced to the stage that produced it.

Before and after any change to the filters or the math:

```bash
python replay.py rec.jsonl.gz --verdicts before.json
# ... make the change ...
python replay.py rec.jsonl.gz --compare before.json
```

"No behaviour change" is what a refactor should print, and this is the only
way to prove it. When a real bug turns up, keep it forever:

```bash
python replay.py rec.jsonl.gz --promote who-wins bug_dry_leg
```

That writes `tests/fixtures/bug_dry_leg.json`; write a test asserting the
correct verdict against the book that actually caused the problem.

## Asking the data questions

```bash
python query.py health      # shape-based: analysed nothing? all filtered out?
python query.py funnel      # rejection reasons, ranked
python query.py drift       # this window vs the last — silent breakage
python query.py opps        # opportunities, best first
python query.py suspects    # flagged but not rejected — review these by hand
python query.py fees        # which fee categories actually produce edges
python query.py timings     # where the time goes
python query.py sql "SELECT ..."
```

`drift` is the one that catches breakage nothing else reports: no errors,
no warnings, and the dry-leg rate quietly goes from 3% to 40% because an
endpoint changed shape. The only other symptom is an absence of signals,
which looks exactly like a quiet market.

## Logs

```bash
LOG_FORMAT=json LOG_FILE=arb.log python arb_monitor.py
jq -s 'group_by(.stage) | map({stage: .[0].stage, n: length})' arb.log
```

Text on the console, JSON in the file. Numbers stay numbers, so
`select(.net_edge > 0.01)` works.

## Tests

```bash
python -m pytest tests/ -q
```

208 tests, no network, about 20 seconds. Unit tests for the math and the
fee model; integration tests that run the whole pipeline against a fake
Polymarket (`tests/fakeapi.py`) — including a test that records a scan,
replays it, and asserts the verdicts match.

## Executing

Dry run by default. It re-fetches the books and re-checks the edge
immediately before sending anything, because a signal is a memory of a book,
not the book.

```bash
python executor.py                  # dry run
python executor.py --live           # real orders, real money
```

Risk limits live in `config.py` and are deliberately small. Raise them only
after the `signals` table shows edges surviving long enough to be caught.

## Notes

- Trading requires a funded Polygon wallet and credentials in `.env`.
- Polymarket restricts trading for users in some jurisdictions, including
  the US. Check the terms before going live.
- Fee rates are category-dependent (0% geopolitics, 7% crypto, 3% sports,
  4% politics/finance, 5% default) and are matched from event tags.
