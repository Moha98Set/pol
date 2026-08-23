"""
A fake Polymarket API.
======================

Integration tests need the whole pipeline — fetch, filter, book, math,
storage — to run end to end. They must not need the internet, a VPN, or
Polymarket to be having a good day, and they must be able to produce
situations that are rare or impossible to catch live: a real arbitrage, a
crossed book, a page of events that 422s halfway through.

So this stands in for both HTTP endpoints the system talks to. It is
wired in by monkeypatching `scanner.SESSION`, which is the one seam every
request in the project goes through.

The builders below (`binary_event`, `multi_event`) generate payloads with
the same field names and the same string-encoded quirks as the real API —
`clobTokenIds` as a JSON *string*, prices and sizes as strings — because
those quirks are exactly what the parsing code exists to survive.
"""

import json
from datetime import datetime, timedelta, timezone


def iso_in(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


# =====================================================================
# Payload builders
# =====================================================================


def book(levels, bids=None) -> dict:
    """levels: [(price, size), ...] — encoded as strings, like the real API."""
    return {
        "asks": [{"price": str(p), "size": str(s)} for p, s in levels],
        "bids": [{"price": str(p), "size": str(s)} for p, s in (bids or [])],
    }


def binary_event(slug="binary-event", title="Will it rain?",
                 yes=(0.40, 100), no=(0.55, 100), volume=50_000,
                 days=30, tags=("Geopolitics",), extra_levels=True):
    """
    A binary event plus its two books.

    Defaults sum to $0.95 — a real 5% edge — because an integration test
    that cannot produce a positive result never exercises the interesting
    half of the code.
    """
    yes_token, no_token = f"{slug}-yes", f"{slug}-no"

    def levels(best):
        price, size = best
        if not extra_levels:
            return [(price, size)]
        return [(price, size), (round(price + 0.05, 4), size)]

    event = {
        "slug": slug,
        "title": title,
        "endDate": iso_in(days),
        "tags": [{"label": t} for t in tags],
        "markets": [{
            "question": title,
            "slug": slug,
            "closed": False,
            "enableOrderBook": True,
            "volume24hr": volume,
            "category": "Test",
            "clobTokenIds": json.dumps([yes_token, no_token]),
        }],
    }
    books = {yes_token: book(levels(yes)), no_token: book(levels(no))}
    return event, books


def multi_event(slug="multi-event", title="Who wins?",
                legs=((0.30, 500), (0.30, 500), (0.30, 500)),
                volume=80_000, days=45, tags=("Geopolitics",),
                neg_risk=True):
    """A negRisk multi-outcome event: one market per candidate."""
    markets, books = [], {}
    for i, (price, size) in enumerate(legs):
        token = f"{slug}-{i}"
        markets.append({
            "question": f"Will candidate {i} win?",
            "groupItemTitle": f"Candidate {i}",
            "slug": f"{slug}-{i}",
            "closed": False,
            "enableOrderBook": True,
            "volume24hr": volume / len(legs),
            "category": "Test",
            "clobTokenIds": json.dumps([token, f"{token}-no"]),
        })
        books[token] = book([(price, size),
                             (round(price + 0.02, 4), size)])

    event = {
        "slug": slug,
        "title": title,
        "endDate": iso_in(days),
        "negRisk": neg_risk,
        "tags": [{"label": t} for t in tags],
        "markets": markets,
    }
    return event, books


# =====================================================================
# The fake transport
# =====================================================================


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"{self.status_code}")


class FakeAPI:
    """
    Stands in for `requests.Session` for the two endpoints in use.

    Records every call, so tests can assert on request *behaviour* —
    batching, pagination, retry — and not only on the final numbers. The
    batching assertion matters: one call per token instead of one per
    hundred is a 100x cost increase that no unit test would ever notice.
    """

    def __init__(self):
        self.events = []
        self.books = {}
        self.get_calls = []
        self.post_calls = []
        self.fail_books_times = 0      # transient /books failures to inject
        self.page_size = 100

    # ---- setup -------------------------------------------------------

    def add(self, event_and_books):
        event, books = event_and_books
        self.events.append(event)
        self.books.update(books)
        return event

    def add_many(self, pairs):
        for pair in pairs:
            self.add(pair)

    # ---- transport ---------------------------------------------------

    def get(self, url, params=None, timeout=None):
        self.get_calls.append((url, dict(params or {})))

        if url.endswith("/events"):
            offset = int((params or {}).get("offset", 0))
            limit = int((params or {}).get("limit", self.page_size))
            return FakeResponse(self.events[offset:offset + limit])

        return FakeResponse([], 404)

    def post(self, url, json=None, timeout=None):
        self.post_calls.append((url, json))

        if not url.endswith("/books"):
            return FakeResponse([], 404)

        if self.fail_books_times > 0:
            self.fail_books_times -= 1
            raise ConnectionError("synthetic transient failure")

        out = []
        for item in json or []:
            token = item.get("token_id")
            if token in self.books:
                out.append(dict(self.books[token], asset_id=token))
        return FakeResponse(out)

    # ---- assertions --------------------------------------------------

    @property
    def book_requests(self) -> int:
        return len(self.post_calls)

    @property
    def tokens_requested(self) -> list:
        return [item["token_id"]
                for _url, payload in self.post_calls
                for item in (payload or [])]
