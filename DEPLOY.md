# Running this on a server

Observe-only: the monitor scans and stores, the live engine watches
sockets, and nothing places an order. No wallet, no private key, no
`py_clob_client` — see "Going live" at the bottom for what changes if that
is ever the goal.

## The one thing that surprises people

**`load_dotenv()` is never called by anything that runs as a service.**
Only `polly.py`, a standalone demo, calls it. `config.py` and `executor.py`
read `os.getenv` directly, so the *process environment* is the only channel
that works. Putting a `.env` file next to the code on the server does
nothing at all.

Under systemd that channel is `EnvironmentFile=/etc/polly/polly.env`.

(The `.env` currently in the repo root is not an env file either — it is a
206-line Python script with zero `KEY=VALUE` lines. It is in `.gitignore`
and must not be copied to the server.)

## Layout

| Path | Contains | Writable by service |
|---|---|---|
| `/opt/polly` | code + venv | no |
| `/var/lib/polly` | `arb_monitor.db` | yes |
| `/etc/polly/polly.env` | settings | no |

Code read-only is deliberate. The application never writes beside itself —
every path in the pipeline is built from `Path(__file__).parent` or from
`DB_PATH`, so it does not care what the working directory is, and the only
thing it needs to write is the database.

## Install

```bash
sudo bash deploy/install.sh
```

Creates the `polly` system user, syncs the code, builds a fresh virtualenv
(the macOS one in `venv/` must not be copied — wrong platform, wrong
Python, 269MB), installs both units, and writes a starting config.

Requires Python 3.10+. The code is pure Python with no platform-specific
branches, so any current distro works.

## Before starting: prove the config applies

```bash
sudo -u polly bash -c 'set -a; . /etc/polly/polly.env; set +a; \
    /opt/polly/venv/bin/python /opt/polly/config.py'
```

Every value is printed with its source — `env`, `profile`, or `default`.
If `DB_PATH` still says `default`, the environment file is not reaching the
process and the service would silently write to `/opt/polly/arb_monitor.db`,
which is read-only under this unit.

## Start

```bash
sudo systemctl enable --now polly-monitor
sudo journalctl -u polly-monitor -f
```

A healthy first minute prints `monitor starting` with the whole resolved
config on one line, then `events retrieved`, then `pre-filter complete`
with a breakdown of why events were dropped.

The live engine is independent and optional:

```bash
sudo systemctl enable --now polly-live
```

Both write to the same SQLite file. That is safe — `db.connect()` opens in
WAL mode, which is built for one writer and concurrent readers.

## Scan pacing — the setting that actually matters

Measured against the live API: about **2100 active events** are fetchable,
the pre-filter keeps roughly a quarter of them, and each survivor costs a
few seconds of order-book round trips.

Uncapped, that is a **~30 minute scan** against a `SCAN_INTERVAL` of 15
minutes. Nothing breaks — `main()` computes `max(0, interval - elapsed)`
and simply starts the next scan immediately — but the interval becomes
fiction, and you lose the ability to reason about how fresh a stored
opportunity is.

`MAX_EVENTS_SCAN=800` in the shipped config keeps a scan near ten minutes,
comfortably inside the window. Capping costs less than it sounds: events
are fetched ordered by 24h volume, so the cap keeps the most liquid slice
rather than an arbitrary sample.

Measure it on your own host before changing it — a server near the API will
be considerably faster than a laptop:

```bash
sudo journalctl -u polly-monitor | grep cycle_complete | tail -5
```

Then raise `MAX_EVENTS_SCAN` (or set it to `0` for uncapped) until
`duration_ms` approaches `SCAN_INTERVAL`, and stop there.

## Is it working?

`query.py` takes the database as a flag, so it needs no environment:

```bash
DB=/var/lib/polly/arb_monitor.db
sudo -u polly /opt/polly/venv/bin/python /opt/polly/query.py health --db $DB
sudo -u polly /opt/polly/venv/bin/python /opt/polly/query.py funnel --db $DB
sudo -u polly /opt/polly/venv/bin/python /opt/polly/query.py drift  --db $DB
```

`health` is shape-based: it catches "analysed nothing" and "filtered
everything out", the two failures that produce no errors at all. `drift`
compares this window against the last and is the only check that catches
an endpoint quietly changing shape — no errors, no warnings, just a
dry-leg rate that went from 3% to 40%.

Run `drift` after any Polymarket-side change and after every deploy.

## Stopping and restarting

`arb_monitor` traps SIGTERM. It stops analysing, writes the results it
already has, marks the scan `done`, and exits in about a second. An
interrupted scan is recorded as a short scan rather than vanishing, so
`systemctl restart` does not leave a row stuck in `running` that the next
start has to clean up.

```bash
sudo systemctl restart polly-monitor
```

## Updating

```bash
sudo systemctl stop polly-monitor polly-live
sudo bash deploy/install.sh          # re-syncs code, keeps the database
sudo systemctl start polly-monitor polly-live
```

`install.sh` excludes `*.db` from the sync, so history survives updates.

## Backups

The database is the entire product of this system. Back it up with SQLite's
own command rather than `cp` — a plain copy of a WAL-mode database while a
writer is running produces a file that may not open:

```bash
sudo -u polly sqlite3 /var/lib/polly/arb_monitor.db \
    ".backup '/var/backups/polly-$(date +%F).db'"
```

## Going live (not configured here)

Three things change, and each is deliberately separate from the others:

1. `pip install py_clob_client` — the observe-only venv does not have it.
   `executor.py` imports it lazily inside the signing function, which is
   why a dry run never needs it.
2. `POLYMARKET_PRIVATE_KEY` and `POLYMARKET_FUNDER_ADDRESS` in
   `/etc/polly/polly.env`. A key on a server is a key that can be stolen
   from that server; use a wallet funded with only what you would accept
   losing.
3. `POLYMARKET_ALLOW_LIVE=yes` **and** the `--live` flag. Two switches,
   because one flag is one typo away from real money.

`touch /opt/polly/STOP` halts live trading without stopping the process.

Check the jurisdiction rules first — Polymarket restricts trading for users
in several countries, including the US.
