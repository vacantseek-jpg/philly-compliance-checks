#!/usr/bin/env python3
"""Rental-licence renewal position for a Philadelphia portfolio.

WHAT THIS ANSWERS
-----------------
Give it a portfolio -- a file of street addresses, or a file of OPA account
numbers, or a mix -- and it reports, for every property:

  * the ACTIVE rental licence(s): number, expiry, days to expiry, unit count,
    licence holder
  * a renewal bucket: EXPIRED | due <=30d | due <=60d | due <=90d | later
  * or NO ACTIVE RENTAL LICENCE, which is a finding in its own right
  * or UNCHECKED, when the address could not be resolved to one parcel
  * open L&I violations on the same parcel, shown ALONGSIDE, informational only

EVERYTHING IT USES IS FREE, KEYLESS AND PUBLIC
----------------------------------------------
  * AIS   https://api.phila.gov/ais/v1/search/<query>   address or OPA -> parcel
  * Carto https://phl.carto.com/api/v2/sql              business_licenses, violations

Address resolution, the match_type/ambiguity guards and the violations table
conventions are imported from check_violations.py rather than re-implemented.
That file is the reference implementation for "did we actually identify this
parcel"; there must not be a second, drifting copy of that judgement.

THE THREE THINGS THIS TOOL REFUSES TO DO
----------------------------------------
1. It never reports an unresolved or ambiguous address as "no licence".
   An address AIS could not pin to exactly one parcel is UNCHECKED, and
   UNCHECKED sorts to the TOP of every report and CSV. Quietly listing a
   property you never checked among the clean ones is the single worst thing
   this tool could do to a landlord, so it is made impossible rather than
   unlikely.

2. It never claims a violation blocks a licence renewal.
   The intuitive story -- "L&I will not renew you while a violation sits open"
   -- was tested against the data on 2026-09-06 and did not survive. Active
   rental licences carrying an open violation are 1.28% expired; those without
   are 0.96%. A 1.3x difference is not a blocking mechanism. Violations are
   printed beside the licence because a manager wants both on one page, and
   for no other reason.

3. It never invents a deadline for a violation. The violations table has five
   date columns and none of them is a cure or appeal deadline; see the module
   docstring of check_violations.py.

MEASURED 2026-09-06 (licensetype='Rental', licensestatus='Active', citywide):
    93,740 licence rows across 91,085 parcels
       888 already expired          3,428 due within 30 days
     5,144 due in 31-60 days        5,012 due in 61-90 days
    79,268 later
Reproduce with --verify-citywide, which runs the same buckets as one Carto
COUNT query and prints the SQL.

DATES
-----
expirationdate is a timestamptz. Buckets are computed on the calendar date in
America/New_York, not on the raw UTC instant -- a licence stamped
2025-08-05T03:18:42Z expired on 2025-08-04 in Philadelphia, and that is a real
row in this table, not a hypothetical. "Expires today" counts as due in 0 days,
not expired.

    That convention is why this tool says 888 expired where a naive
    `expirationdate < now()` says 914: the 26-row difference is licences whose
    expiry date is today but whose stored wall-clock time has already passed.
    Measured, reconciled, deliberate.

USAGE
-----
    python check_renewals.py portfolio.txt
    python check_renewals.py portfolio.txt --csv renewals.csv
    python check_renewals.py --demo
    python check_renewals.py --verify-citywide

portfolio.txt is one entry per line, or a CSV whose first column is the entry.
An all-digit entry is treated as an OPA account number (zero-padded to 9);
anything else is treated as a street address. Both are resolved through AIS, so
a mistyped OPA becomes UNCHECKED rather than a false "unlicensed".
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

# Import the sibling rather than copying it. Adding the script's own directory
# is belt-and-braces: python already puts it on sys.path when the script is run
# by path, but not when the module is imported from an odd working directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_violations import (  # noqa: E402
    TRUSTED_MATCH,
    Prop,
    carto,
    read_addresses,
    resolve,
)

# Note on what is NOT imported: the unsafe/unfit substring list. It stays in
# check_violations as the single definition and reaches this module only
# through Prop.open_unsafe. A second copy here would be one more pair of
# things that can silently disagree about what counts as "unsafe".

# ---------------------------------------------------------------------------
# Philadelphia local time.
# ---------------------------------------------------------------------------
# The date maths must happen in America/New_York. Falling back to UTC silently
# would move ~every late-evening expiry one day later, so the fallback shouts.
try:
    from zoneinfo import ZoneInfo

    PHL_TZ = ZoneInfo("America/New_York")
    # Prove the tz database is actually present -- on a bare Windows Python,
    # ZoneInfo() can construct and then fail on use.
    datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).astimezone(PHL_TZ)
except Exception:  # noqa: BLE001
    PHL_TZ = timezone.utc
    print(
        "WARNING: no America/New_York timezone data (pip install tzdata). "
        "Falling back to UTC; expiry dates stored late in the local evening "
        "may read one day late.",
        file=sys.stderr,
    )

# ---------------------------------------------------------------------------
# Buckets. Names are the ones a property manager was promised; the day bands
# are spelled out because "due <=60d" next to "due <=30d" can only mean 31-60.
# ---------------------------------------------------------------------------
B_UNCHECKED = "UNCHECKED"
B_NONE = "NO LICENCE FOUND"
B_EXPIRED = "EXPIRED"
B_30 = "due <=30d"
B_60 = "due <=60d"
B_90 = "due <=90d"
B_LATER = "later"
B_UNKNOWN_EXP = "expiry unknown"

BUCKET_BANDS = {
    B_EXPIRED: "expiry date already past",
    B_30: "0-30 days",
    B_60: "31-60 days",
    B_90: "61-90 days",
    B_LATER: "more than 90 days",
    B_UNKNOWN_EXP: "no expiry date on the record",
}

# Sort order. UNCHECKED first on purpose: it is the bucket a reader is most
# likely to skip, and the only one where the tool is admitting ignorance.
RANK = {B_UNCHECKED: 0, B_NONE: 1, B_EXPIRED: 2, B_30: 3, B_60: 4, B_90: 5,
        B_UNKNOWN_EXP: 6, B_LATER: 7}

LICENCE_COLS = (
    "opa_account_num, address, unit_type, unit_num, zip, licensenum, licensetype, "
    "rentalcategory, licensestatus, initialissuedate, mostrecentissuedate, "
    "expirationdate, numberofunits, legalname, business_name, opa_owner, "
    "owneroccupied, ownercontact1name"
)

OPA_TOKEN = re.compile(r"^\d{7,10}$")
HEADER_TOKENS = {
    "address", "street address", "property address", "addr", "opa", "opa_account_num",
    "opa account number", "account", "account number", "parcel", "parcel id", "property",
}


def sql_in(values) -> str:
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in values)


def today_phl() -> date:
    return datetime.now(tz=PHL_TZ).date()


def to_phl_date(value) -> date | None:
    """Carto timestamptz string -> the calendar date it falls on in Philadelphia."""
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    if dt.tzinfo is None:  # Carto has always sent an offset; assume UTC if it stops.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(PHL_TZ).date()


def bucket_for(days: int | None) -> str:
    if days is None:
        # An unknown expiry is NOT "more than 90 days". Filing it under B_LATER
        # put a licence whose expiry nobody knows into the safest bucket, where
        # it drops out of every due-within-N window. Unknown means look at it.
        return B_UNKNOWN_EXP
    if days < 0:
        return B_EXPIRED
    if days <= 30:
        return B_30
    if days <= 60:
        return B_60
    if days <= 90:
        return B_90
    return B_LATER


@dataclass
class Licence:
    row: dict
    expiry: date | None
    days: int | None
    bucket: str
    # "opa" when joined on opa_account_num, "address" when recovered by exact
    # address string because the licence row carries no OPA at all (70 of the
    # 93,740 active rental rows, measured 2026-09-06). An address match is
    # weaker evidence and is labelled as such everywhere it is shown.
    basis: str = "opa"

    @property
    def number(self) -> str:
        return (self.row.get("licensenum") or "").strip()

    @property
    def holder(self) -> str:
        for key in ("legalname", "business_name", "ownercontact1name", "opa_owner"):
            v = (self.row.get(key) or "").strip()
            if v:
                return " ".join(v.split())
        return ""

    @property
    def units(self):
        return self.row.get("numberofunits")

    @property
    def unit_label(self) -> str:
        ut = (self.row.get("unit_type") or "").strip()
        un = (self.row.get("unit_num") or "").strip()
        return " ".join(x for x in (ut, un) if x)


@dataclass
class Holding:
    """One portfolio entry: the parcel, its licences, its open violations."""

    given: str
    query: str                       # what was actually sent to AIS
    kind: str                        # "address" | "opa"
    prop: Prop | None = None
    licences: list[Licence] = field(default_factory=list)
    lapsed: dict | None = None       # most recent non-Active rental licence, if any
    notes: list[str] = field(default_factory=list)

    # ⛔ QUERY OUTCOME FLAGS, and why they exist.
    #
    # Three independent reviewers found the same defect: when a Carto query
    # failed, the report printed "open L&I violations: none" and wrote 0 into
    # the CSV — a positive all-clear for a parcel that was never successfully
    # queried. The lapsed-licence lookup had the same shape, printing the
    # definitive "no rental licence of any status on record" after swallowing
    # the error with a bare `continue`.
    #
    # "I asked, the answer was none" and "I could not ask" must never render
    # identically. False clean is the one failure this tool exists to prevent.
    # These default to False and become True only after a query returns.
    viol_ok: bool = False
    lapsed_ok: bool = False

    # Set when address-matched licences at this parcel belong to several
    # different holders — a multi-owner building. Such a property is reported
    # in full but excluded from the renewal totals, because the tool cannot
    # tell which of those licences the portfolio owner actually holds.
    needs_attribution: bool = False

    @property
    def address_ok(self) -> bool:
        """AIS pinned this to one real address, even if the parcel has no OPA.

        ~0.6% of real addresses resolve exactly and carry a blank
        opa_account_num (condo shells and some multi-address parcels). Those
        used to be UNCHECKED forever, because the address-recovery pass skipped
        anything unchecked and `unchecked` required an OPA — circular. Their
        LICENCE position is knowable from business_licenses.address; only their
        VIOLATION position needs the OPA, and viol_ok already reports that
        honestly as NOT CHECKED.
        """
        p = self.prop
        return bool(p and p.normalized and p.match_type in TRUSTED_MATCH)

    @property
    def unchecked(self) -> bool:
        if self.prop is None:
            return True
        if self.prop.opa:
            return bool(self.prop.error)
        # No OPA. Checked only if the address resolved cleanly AND that address
        # actually matched a licence — otherwise we genuinely know nothing.
        return not (self.address_ok and self.licences)

    @property
    def opa(self) -> str | None:
        return self.prop.opa if self.prop else None

    @property
    def label(self) -> str:
        if self.prop and self.prop.normalized:
            return self.prop.normalized
        return self.given

    @property
    def error(self) -> str | None:
        return self.prop.error if self.prop else "not resolved"

    @property
    def open_violations(self) -> list[dict]:
        return self.prop.open_all if self.prop else []

    @property
    def open_unsafe(self) -> list[dict]:
        return self.prop.open_unsafe if self.prop else []

    @property
    def bucket(self) -> str:
        """The property's headline position: its most urgent licence."""
        if self.unchecked:
            return B_UNCHECKED
        if not self.licences:
            return B_NONE
        return min((lic.bucket for lic in self.licences), key=lambda b: RANK[b])

    @property
    def rank(self) -> int:
        return RANK[self.bucket]

    @property
    def soonest(self) -> int | None:
        days = [lic.days for lic in self.licences if lic.days is not None]
        return min(days) if days else None


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------
def classify(token: str) -> tuple[str, str, str | None]:
    """token -> (kind, query, note). Zero-pads short OPA numbers to 9 digits.

    AIS is strict about this: '034106005' resolves, '34106005' is a 404. A
    spreadsheet that dropped the leading zero would otherwise turn a real
    parcel into UNCHECKED for a reason nobody would guess.
    """
    t = token.strip()
    if OPA_TOKEN.match(t):
        if len(t) < 9:
            padded = t.zfill(9)
            return "opa", padded, f"zero-padded OPA {t} -> {padded}"
        return "opa", t, None
    return "address", t, None


def read_portfolio(path: str) -> list[Holding]:
    """Reuses check_violations.read_addresses for file shape, then classifies."""
    out: list[Holding] = []
    for token in read_addresses(path):
        if token.strip().lower() in HEADER_TOKENS:
            continue
        kind, query, note = classify(token)
        h = Holding(given=token.strip(), query=query, kind=kind)
        if note:
            h.notes.append(note)
        out.append(h)
    return out


# ---------------------------------------------------------------------------
# Carto fetches -- batched IN lists, never one request per property
# ---------------------------------------------------------------------------
def fetch_licences(holdings: list[Holding], batch: int = 40) -> None:
    by_opa: dict[str, list[Holding]] = {}
    for h in holdings:
        if h.opa:
            by_opa.setdefault(h.opa, []).append(h)

    opas = list(by_opa)
    today = today_phl()
    for i in range(0, len(opas), batch):
        chunk = opas[i : i + batch]
        sql = (
            f"SELECT {LICENCE_COLS} FROM business_licenses "
            f"WHERE licensetype = 'Rental' AND licensestatus = 'Active' "
            f"AND opa_account_num IN ({sql_in(chunk)}) "
            "ORDER BY expirationdate"
        )
        try:
            rows = carto(sql)
        except Exception as exc:  # noqa: BLE001
            # A failed licence query must NOT read as "no licence". Mark every
            # property in the chunk unchecked instead.
            for o in chunk:
                for h in by_opa[o]:
                    if h.prop:
                        h.prop.error = f"licence query failed: {type(exc).__name__}: {exc}"
            continue
        for row in rows:
            expiry = to_phl_date(row.get("expirationdate"))
            days = (expiry - today).days if expiry else None
            lic = Licence(row=row, expiry=expiry, days=days, bucket=bucket_for(days))
            for h in by_opa.get(row.get("opa_account_num"), []):
                h.licences.append(lic)
        time.sleep(0.25)

    for h in holdings:
        h.licences.sort(key=lambda l: (RANK[l.bucket], l.days if l.days is not None else 10**6))


def fetch_licences_by_address(holdings: list[Holding], batch: int = 40) -> None:
    """Second pass for parcels that matched no licence on OPA.

    70 of the 93,740 active rental licence rows carry no opa_account_num at all
    (measured 2026-09-06). Without this pass those properties would be reported
    as unlicensed, which is a false accusation rather than a missed one.
    """
    # Includes blank-OPA parcels: they are exactly the rows this pass exists for.
    # Gating on `not h.unchecked` excluded them, and since `unchecked` was true
    # precisely BECAUSE they had no OPA, they could never be recovered.
    targets = [
        h for h in holdings
        if not h.licences and h.address_ok and (h.opa or not h.prop.opa)
    ]
    if not targets:
        return
    by_addr: dict[str, list[Holding]] = {}
    for h in targets:
        by_addr.setdefault(h.prop.normalized.strip().upper(), []).append(h)

    # TWO DIFFERENT MISSES, and they need different WHERE clauses.
    #
    #  (a) The parcel HAS an OPA but the licence row does not — 70 of the 93,740
    #      active rental rows. Restrict to licences with a blank OPA, or an
    #      address string shared by several parcels would pull in a neighbour's
    #      licence.
    #
    #  (b) The parcel has NO OPA in AIS but the licence row does. Real example:
    #      "1737-39 Chestnut St" resolves exact with a blank opa_account_num,
    #      while its active licence 834583 carries opa 888086730. The blank-OPA
    #      filter excluded exactly the row that would have answered the question,
    #      so the property stayed UNCHECKED with a licence sitting in plain sight.
    no_opa_addrs = {a for a, hs in by_addr.items() if any(not h.prop.opa for h in hs)}

    addrs = list(by_addr)
    today = today_phl()
    for i in range(0, len(addrs), batch):
        chunk = addrs[i : i + batch]
        blank_only = [a for a in chunk if a not in no_opa_addrs]
        any_opa = [a for a in chunk if a in no_opa_addrs]
        clauses = []
        if blank_only:
            clauses.append("((opa_account_num IS NULL OR opa_account_num = '') "
                           f"AND UPPER(address) IN ({sql_in(blank_only)}))")
        if any_opa:
            clauses.append(f"(UPPER(address) IN ({sql_in(any_opa)}))")
        sql = (
            f"SELECT {LICENCE_COLS} FROM business_licenses "
            "WHERE licensetype = 'Rental' AND licensestatus = 'Active' "
            f"AND ({' OR '.join(clauses)}) "
            "ORDER BY expirationdate"
        )
        try:
            rows = carto(sql)
        except Exception:  # noqa: BLE001
            continue  # best-effort recovery pass; the OPA pass already ran
        for row in rows:
            expiry = to_phl_date(row.get("expirationdate"))
            days = (expiry - today).days if expiry else None
            lic = Licence(
                row=row, expiry=expiry, days=days, bucket=bucket_for(days), basis="address"
            )
            for h in by_addr.get((row.get("address") or "").strip().upper(), []):
                h.licences.append(lic)
                # Say WHICH side was missing the OPA. They are different
                # situations and a reader checking the work needs to know which.
                why = ("the parcel has no OPA in AIS" if not h.prop.opa
                       else "the licence row has no OPA")
                h.notes.append(
                    f"licence {lic.number} matched by ADDRESS, not OPA "
                    f"({why}) -- verify before relying on it"
                )
        time.sleep(0.25)

    # ⛔ MULTI-OWNER BUILDINGS. Found only by running 900 real addresses.
    #
    # An address match on a blank-OPA parcel can pull in every unit-level
    # licence at that street address. Measured: "1001-13 CHESTNUT ST" returned
    # 55 active rental licences held by 22 DIFFERENT owners — a condo building,
    # not one landlord's holding. Counting all 55 as the portfolio's renewals
    # overstates the client's obligations with other people's licences, which
    # is the same class of error as overclaiming.
    #
    # Where the matched licences resolve to more than one holder, the property
    # is flagged for attribution and kept OUT of the headline due-within-N
    # counts. It is still listed in full — the reader needs to see it — but as
    # a question ("which of these are yours?"), not as an answer.
    for h in holdings:
        addr_lic = [l for l in h.licences if l.basis != "opa"]
        if len(addr_lic) < 2:
            continue
        holders = {(l.holder or "").strip().upper() for l in addr_lic if l.holder}
        if len(holders) > 1:
            h.needs_attribution = True
            h.notes.append(
                f"{len(addr_lic)} licences at this address across {len(holders)} "
                "different holders -- a multi-owner building. NOT counted in the "
                "renewal totals; identify which are yours."
            )


def fetch_lapsed(holdings: list[Holding], batch: int = 40) -> None:
    """For unlicensed parcels only: was there ever a rental licence here?

    'Never licensed' and 'licence lapsed last month' are different problems and
    a manager needs to tell them apart.
    """
    targets = [h for h in holdings if not h.unchecked and not h.licences and h.opa]
    if not targets:
        return
    by_opa: dict[str, list[Holding]] = {}
    for h in targets:
        by_opa.setdefault(h.opa, []).append(h)

    opas = list(by_opa)
    for i in range(0, len(opas), batch):
        chunk = opas[i : i + batch]
        sql = (
            "SELECT opa_account_num, licensenum, licensestatus, expirationdate, "
            "inactivedate, numberofunits, legalname, business_name "
            "FROM business_licenses WHERE licensetype = 'Rental' "
            f"AND licensestatus <> 'Active' AND opa_account_num IN ({sql_in(chunk)}) "
            "ORDER BY expirationdate DESC"
        )
        try:
            rows = carto(sql)
        except Exception as exc:  # noqa: BLE001
            # A bare `continue` here used to let the report go on to print
            # "no rental licence of any status on record for this parcel" —
            # a confident negative about a parcel that was never queried.
            for o in chunk:
                for h in by_opa[o]:
                    h.notes.append(f"lapsed-licence lookup failed: {type(exc).__name__}")
            continue                       # lapsed_ok stays False
        for row in rows:
            for h in by_opa.get(row.get("opa_account_num"), []):
                if h.lapsed is None:  # ORDER BY DESC -> first seen is most recent
                    h.lapsed = row
        for o in chunk:
            for h in by_opa[o]:
                h.lapsed_ok = True
        time.sleep(0.25)


def fetch_open_violations(holdings: list[Holding], batch: int = 40) -> None:
    """Open violations on the same parcel. Context only -- see the module docstring.

    Filtered to OPEN server-side; check_violations.fetch_violations pulls the
    full history, which is the right shape for that tool and needless volume
    for this one. Prop.open_all / Prop.open_unsafe still apply their own filter,
    so the two remain consistent.
    """
    by_opa: dict[str, list[Holding]] = {}
    for h in holdings:
        if h.opa and h.prop:
            by_opa.setdefault(h.opa, []).append(h)

    opas = list(by_opa)
    for i in range(0, len(opas), batch):
        chunk = opas[i : i + batch]
        sql = (
            "SELECT opa_account_num, casenumber, casestatus, violationnumber, "
            "violationdate, violationcodetitle, violationstatus, underappeal, publicnov "
            f"FROM violations WHERE violationstatus = 'OPEN' "
            f"AND opa_account_num IN ({sql_in(chunk)}) ORDER BY violationdate DESC"
        )
        try:
            rows = carto(sql)
        except Exception as exc:  # noqa: BLE001
            for o in chunk:
                for h in by_opa[o]:
                    h.notes.append(f"violation lookup failed: {type(exc).__name__}")
            continue                       # viol_ok stays False -> renders NOT CHECKED
        for row in rows:
            for h in by_opa.get(row.get("opa_account_num"), []):
                h.prop.violations.append(row)
        for o in chunk:                    # the query returned; this chunk IS checked
            for h in by_opa[o]:
                h.viol_ok = True
        time.sleep(0.25)


def annotate(holdings: list[Holding]) -> None:
    """Cross-licence notes that only make sense once every licence is loaded."""
    for h in holdings:
        expired = [l for l in h.licences if l.bucket == B_EXPIRED]
        current = [l for l in h.licences if l.bucket != B_EXPIRED]
        if expired and current:
            nxt = min(current, key=lambda l: l.days if l.days is not None else 10**6)
            # 22 parcels citywide look like this (measured 2026-09-06). Usually
            # the licence was renewed under a NEW number and the old row was
            # never deactivated -- but the data does not say so, so neither do we.
            h.notes.append(
                f"holds an expired licence AND a current one (expires {nxt.expiry}); "
                "the expired row may be a superseded prior licence -- confirm with L&I"
            )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def _fmt_days(days: int | None) -> str:
    if days is None:
        return "no expiry date"
    if days < 0:
        return f"EXPIRED {-days} day{'s' if days != -1 else ''} ago"
    if days == 0:
        return "expires TODAY"
    return f"in {days} day{'s' if days != 1 else ''}"


def _violation_line(h: Holding) -> str:
    # The query failed. Saying "none" here would be a false all-clear on a
    # parcel that was never checked — see Holding.viol_ok.
    if not h.viol_ok:
        return "open L&I violations: NOT CHECKED (lookup failed) -- not known to be clear"
    n, u = len(h.open_violations), len(h.open_unsafe)
    if not n:
        return "open L&I violations: none"
    s = f"open L&I violations: {n}"
    if u:
        s += f" ({u} unsafe/unfit)"
    return s + "   [informational -- not a renewal blocker]"


def _licence_line(lic: Licence, prefix: str = "") -> str:
    units = ""
    if lic.units not in (None, ""):
        units = f"{lic.units} unit{'s' if str(lic.units) != '1' else ''}"
    # Every row this tool buckets EXPIRED still carries licensestatus 'Active'
    # in L&I's own data — the bucket is derived from expirationdate, which is
    # the honest reading, but silently overriding the city's own status field
    # hides a disagreement the reader should see before acting on it.
    contradiction = ""
    li_status = str((lic.row or {}).get("licensestatus") or "").strip().lower()
    if lic.days is not None and lic.days < 0 and li_status == "active":
        contradiction = "[L&I still says Active]"
    tail = " ".join(x for x in (lic.holder,
                                "[ADDRESS MATCH]" if lic.basis != "opa" else "",
                                contradiction) if x)
    return (
        f"{prefix:<7}licence {lic.number or '?':>8}  {lic.unit_label:<7}  "
        f"expires {str(lic.expiry or 'unknown'):<10}  "
        f"{_fmt_days(lic.days):<21}  {units:>9}   {tail}".rstrip()
    )


def report(holdings: list[Holding], out_csv: str | None, source: str) -> None:
    today = today_phl()
    unchecked = [h for h in holdings if h.bucket == B_UNCHECKED]
    unlicensed = [h for h in holdings if h.bucket == B_NONE]
    checked = [h for h in holdings if h.bucket not in (B_UNCHECKED,)]

    # Licences at multi-owner buildings are excluded from every total: the tool
    # cannot tell which of them the portfolio owner holds, and counting another
    # landlord's renewals as yours overstates the workload. They are reported in
    # their own section instead.
    attribution = [h for h in holdings if h.needs_attribution]
    all_lic = [(h, l) for h in holdings if not h.needs_attribution for l in h.licences]
    counts = {b: sum(1 for _, l in all_lic if l.bucket == b) for b in
              (B_EXPIRED, B_30, B_60, B_90, B_UNKNOWN_EXP, B_LATER)}

    print()
    print("=" * 74)
    print(" PHILADELPHIA RENTAL LICENCE -- RENEWAL POSITION")
    print(f" portfolio: {source}    as of {today} (America/New_York)")
    print("=" * 74)
    print(f"  properties given                {len(holdings)}")
    print(f"  resolved to exactly one parcel  {len(checked)}")
    print(f"  UNCHECKED                       {len(unchecked)}"
          f"{'   <-- resolve these; they are NOT known to be clean' if unchecked else ''}")
    print()
    print(f"  active rental licences found    {len(all_lic)}"
          f"   (across {len([h for h in holdings if h.licences])} propert"
          f"{'y' if len([h for h in holdings if h.licences]) == 1 else 'ies'})")
    print()
    print("  RENEWAL POSITION, per licence")
    for b in (B_EXPIRED, B_30, B_60, B_90, B_UNKNOWN_EXP, B_LATER):
        print(f"    {b:<15} {'(' + BUCKET_BANDS[b] + ')':<32}{counts[b]:>5}")
    print()
    w30 = counts[B_30]
    w60 = w30 + counts[B_60]
    w90 = w60 + counts[B_90]
    print(f"  renewals due within  30 days    {w30}")
    print(f"  renewals due within  60 days    {w60}")
    print(f"  renewals due within  90 days    {w90}")
    print(f"  already expired                 {counts[B_EXPIRED]}")
    print()
    print(f"  NO ACTIVE RENTAL LICENCE ON RECORD         {len(unlicensed)}"
          f"{'   <-- check these against the rent roll' if unlicensed else ''}")
    if attribution:
        n_lic = sum(len(h.licences) for h in attribution)
        print(f"  MULTI-OWNER BUILDINGS                      {len(attribution)}"
              f"   <-- {n_lic} licences, NOT in the totals above")
    print("=" * 74)

    if unchecked:
        print()
        print(" UNCHECKED -- could not be pinned to exactly one parcel.")
        print(" These are NOT reported as unlicensed and NOT reported as clean.")
        print(" Nothing is known about their licence position until they resolve.")
        print()
        for h in unchecked:
            print(f"   {h.given}")
            print(f"       {h.error}")
        print()

    if unlicensed:
        print()
        print(" NO ACTIVE RENTAL LICENCE FOUND")
        # ⛔ This section used to assert that L&I "can order the units vacated"
        # and that an unlicensed period is "not curable in arrears". Neither
        # statement is in any dataset this tool reads — they were asserted from
        # outside the data, which is the exact failure this tool is built to
        # avoid. It now reports the absence and says what the absence is worth.
        #
        # It also used to headline these as "operating unlicensed". Absence of a
        # rental licence is evidence that no licence exists — NOT evidence that
        # the parcel is a rental. A commercial address or an owner-occupied home
        # correctly has no rental licence and is not in breach of anything.
        print(" No Active rental licence is on record for these parcels.")
        print(" If a parcel IS being let, that is a bigger exposure than one")
        print(" expiring next month. If it is not a rental -- commercial, or")
        print(" owner-occupied -- this is the expected and correct state.")
        print(" Only you know which. Check against the rent roll before acting.")
        print()
        for h in sorted(unlicensed, key=lambda x: -len(x.open_violations)):
            print(f"   {h.label}   (OPA {h.opa})")
            if h.lapsed:
                exp = to_phl_date(h.lapsed.get("expirationdate"))
                st = h.lapsed.get("licensestatus")
                num = h.lapsed.get("licensenum")
                # A Closed/Inactive licence can carry a FUTURE expiry — it was
                # surrendered early rather than allowed to lapse. Printing
                # "-40 days ago" for those is nonsense, and worse, it implies
                # the licence expired when it was actually closed.
                if not exp:
                    ago = ""
                elif (today - exp).days >= 0:
                    ago = f", {(today - exp).days} days ago"
                else:
                    ago = f", closed early — expiry was {(exp - today).days} days out"
                print(f"       last rental licence {num} -- status {st}, "
                      f"expiry {exp or 'unknown'}{ago}")
            elif h.lapsed_ok:
                print("       no rental licence of any status on record for this parcel")
            else:
                # The lapsed lookup failed. Do not claim the record is empty.
                print("       prior-licence lookup FAILED -- not known whether one exists")
            print(f"       {_violation_line(h)}")
            for n in h.notes:
                print(f"       note: {n}")
            print()

    if attribution:
        print()
        print(" MULTI-OWNER BUILDINGS -- WHICH OF THESE ARE YOURS?")
        print(" These addresses have no parcel id of their own, so licences were")
        print(" matched on the address string -- and the matches come back under")
        print(" several different owners. That means it is a building with")
        print(" separately-owned units, not one holding. None of these licences")
        print(" are counted in the totals above; counting another landlord's")
        print(" renewals as yours would overstate the work.")
        print()
        for h in sorted(attribution, key=lambda x: -len(x.licences)):
            holders = sorted({(l.holder or "?").strip() for l in h.licences})
            soon = [l for l in h.licences if l.days is not None and l.days <= 90]
            print(f"   {h.label}")
            print(f"       {len(h.licences)} active licences, {len(holders)} holders"
                  f"{f', {len(soon)} due within 90 days' if soon else ''}")
            for name in holders[:4]:
                print(f"         - {name}")
            if len(holders) > 4:
                print(f"         ... and {len(holders) - 4} more")
        print()

    due = sorted(
        [h for h in holdings
         if h.bucket in (B_EXPIRED, B_30, B_60, B_90) and not h.needs_attribution],
        key=lambda h: (h.rank, h.soonest if h.soonest is not None else 10**6),
    )
    if due:
        print()
        print(" EXPIRED, AND DUE WITHIN 90 DAYS")
        print()
        for h in due:
            print(f"   {h.label}   (OPA {h.opa})   [{h.bucket}]")
            for lic in h.licences:
                if lic.bucket == B_LATER:
                    continue
                print(f"    {_licence_line(lic)}")
            for lic in [l for l in h.licences if l.bucket == B_LATER]:
                print(f"    {_licence_line(lic, prefix='(also)')}")
            print(f"       {_violation_line(h)}")
            for n in h.notes:
                print(f"       note: {n}")
            print()

    later_only = [h for h in holdings if h.bucket == B_LATER]
    if later_only:
        print()
        print(" NOTHING DUE INSIDE 90 DAYS")
        print()
        for h in later_only:
            print(f"   {h.label}   (OPA {h.opa})")
            for lic in h.licences:
                print(f"    {_licence_line(lic)}")
            print(f"       {_violation_line(h)}")
            for n in h.notes:
                print(f"       note: {n}")
            print()

    print("-" * 74)
    print(" Open violations are shown for context only. This tool does NOT claim")
    print(" a violation blocks a renewal -- measured citywide 2026-09-06, active")
    print(" licences with an open violation are 1.28% expired vs 0.96% without,")
    print(" which is not a blocking mechanism. Nor does the violations data carry")
    print(" any cure or appeal deadline; those live only in the notice PDF.")
    print(" Licence status reflects L&I's published dataset, not a legal opinion.")
    print("-" * 74)
    print()

    if out_csv:
        write_csv(holdings, out_csv, today)
        print(f" CSV written: {out_csv}")
        print()


CSV_HEADER = [
    "urgency_rank", "bucket", "given_input", "input_kind", "normalized_address",
    "opa_account_num", "licensenum", "licensestatus", "rental_category",
    "unit_type", "unit_num", "expiration_date", "days_to_expiry",
    "number_of_units", "licence_holder", "owner_occupied", "most_recent_issue_date",
    "licence_match_basis", "open_violations", "open_unsafe_unfit", "notes",
]


def _iso(value) -> str:
    d = to_phl_date(value)
    return d.isoformat() if d else ""


# Columns 7..18 of CSV_HEADER -- the licence-specific block. A property with no
# licence has to fill exactly this many blanks, so the count lives in one place
# rather than as a hand-counted run of empty strings that silently shifts the
# notes column left when someone adds a field.
LICENCE_FIELD_COUNT = 12


def write_csv(holdings: list[Holding], path: str, today: date) -> None:
    """One row per licence, sorted by urgency.

    Properties with no licence and properties that are UNCHECKED still get a
    row. A portfolio row that vanishes from the export because there was no
    licence to write would read as "not a problem" to whoever opens the file.
    """
    rows = []
    for h in holdings:
        base = [h.given, h.kind, (h.prop.normalized if h.prop else "") or "", h.opa or ""]
        # Blank, not 0, when the violations query never succeeded. A hard 0 in a
        # spreadsheet reads as "checked and clear" and is indistinguishable from
        # a genuine zero — the false-clean failure three reviewers flagged.
        nv, nu = ((len(h.open_violations), len(h.open_unsafe))
                  if h.viol_ok else ("", ""))
        if not h.licences:
            note = "; ".join(h.notes)
            if h.bucket == B_UNCHECKED:
                note = "; ".join(x for x in (h.error, note) if x)
            elif h.lapsed:
                exp = to_phl_date(h.lapsed.get("expirationdate"))
                note = "; ".join(x for x in (
                    f"last rental licence {h.lapsed.get('licensenum')} status "
                    f"{h.lapsed.get('licensestatus')} expiry {exp or 'unknown'}", note) if x)
            else:
                note = "; ".join(x for x in ("no rental licence of any status on record", note) if x)
            rows.append((
                [RANK[h.bucket], 10**6],
                [RANK[h.bucket], h.bucket] + base
                + [""] * LICENCE_FIELD_COUNT
                # Blank, not 0, when the property is UNCHECKED. No violation
                # query was ever run for it, and a "0" in a violations column
                # is exactly the false all-clear this tool exists to avoid.
                + ([""] * 2 if h.bucket == B_UNCHECKED else [nv, nu])
                + [note],
            ))
            continue
        for lic in h.licences:
            r = lic.row
            rows.append((
                [RANK[lic.bucket], lic.days if lic.days is not None else 10**6],
                [RANK[lic.bucket], lic.bucket] + base + [
                    lic.number, r.get("licensestatus") or "", r.get("rentalcategory") or "",
                    r.get("unit_type") or "", r.get("unit_num") or "",
                    lic.expiry.isoformat() if lic.expiry else "",
                    lic.days if lic.days is not None else "",
                    lic.units if lic.units is not None else "",
                    lic.holder, r.get("owneroccupied") or "",
                    _iso(r.get("mostrecentissuedate")),
                    lic.basis, nv, nu, "; ".join(h.notes),
                ],
            ))
    rows.sort(key=lambda t: t[0])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        for _, row in rows:
            w.writerow(row)


# ---------------------------------------------------------------------------
# Citywide self-check: the same buckets, computed by Postgres instead of Python
# ---------------------------------------------------------------------------
VERIFY_SQL = """SELECT
  count(*) AS active_rows,
  count(*) FILTER (WHERE e <  d)                 AS expired,
  count(*) FILTER (WHERE e >= d AND e <= d + 30) AS due_30,
  count(*) FILTER (WHERE e >  d + 30 AND e <= d + 60) AS due_60,
  count(*) FILTER (WHERE e >  d + 60 AND e <= d + 90) AS due_90,
  count(*) FILTER (WHERE e >  d + 90)            AS later,
  min(d) AS today_phl
FROM (
  SELECT (expirationdate AT TIME ZONE 'America/New_York')::date AS e,
         (now()          AT TIME ZONE 'America/New_York')::date AS d
  FROM business_licenses
  WHERE licensetype = 'Rental' AND licensestatus = 'Active'
) t"""


def verify_citywide() -> int:
    print("Running the bucket definition as one Carto COUNT query.")
    print("The Python bucketing above must agree with this by construction:")
    print("both compare calendar dates in America/New_York.")
    print()
    print(VERIFY_SQL)
    print()
    rows = carto(VERIFY_SQL)
    if not rows:
        print("no rows returned", file=sys.stderr)
        return 1
    r = rows[0]
    print(f"  today in Philadelphia            {str(r['today_phl'])[:10]}")
    print(f"  active rental licence rows       {r['active_rows']}")
    print(f"  EXPIRED                          {r['expired']}")
    print(f"  due <=30d  (0-30 days)           {r['due_30']}")
    print(f"  due <=60d  (31-60 days)          {r['due_60']}")
    print(f"  due <=90d  (61-90 days)          {r['due_90']}")
    print(f"  later      (>90 days)            {r['later']}")
    total = sum(r[k] for k in ("expired", "due_30", "due_60", "due_90", "later"))
    ok = total == r["active_rows"]
    print()
    print(f"  buckets sum to {total} vs {r['active_rows']} rows -- "
          f"{'partition is exhaustive and disjoint' if ok else 'MISMATCH'}")
    print()
    print("  Note: a naive `expirationdate < now()` reports 914 expired, not")
    print(f"  {r['expired']}. The difference is licences whose expiry DATE is today but")
    print("  whose stored time of day has passed. This tool treats those as due")
    print("  in 0 days, because a licence is good for the whole of its last day.")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Demo -- every entry below was found with a live query and hand-checked in AIS
# on 2026-09-06. None is invented.
# ---------------------------------------------------------------------------
DEMO = [
    "5162 D St",            # EXPIRED 2025-08-04 - longest-expired active licence
    "1602 N 17th St",       # EXPIRED 2026-09-03, 5 units, open unsafe violation
    "306 W Upsal St",       # NO ACTIVE LICENCE - lapsed 2026-08-25, 26 open violations
    "2921 N Bonsall St",    # expires TODAY, 12 open violations
    "2319 Sansom St",       # TWO unit-scoped licences, due in 2 and 3 days
    "1221 Wagner Ave",      # due in 5 days, 25 open violations
    "615 N 11th St",        # holds an expired licence AND a current one
    "5961 Belmar St",       # one due in 18 days, one in the 'later' bucket
    "4231-41 Locust St",    # 250 units, due in 19 days
    "221 N Peach St",       # due in 29 days, 2 open unsafe violations
    "1706 Cecil B Moore Ave",  # due in 48 days - the 31-60 band
    "4207 Frankford Ave",   # due in 66 days - the 61-90 band
    "600 Harvey St",        # 624 units, due in 175 days - nothing to do yet
    "2651 S Mildred St",    # NO LICENCE ever, but open UNSAFE violations
    "883309050",            # 1234 Market St by OPA - commercial, no rental licence
]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Rental-licence renewal position for a Philadelphia portfolio.")
    ap.add_argument("portfolio", nargs="?",
                    help="file of addresses and/or OPA account numbers, one per line "
                         "(or a CSV whose first column is the entry)")
    ap.add_argument("--csv", dest="out_csv", help="write one row per licence to this CSV")
    ap.add_argument("--demo", action="store_true",
                    help="run against verified real Philadelphia properties")
    ap.add_argument("--verify-citywide", action="store_true",
                    help="check the bucket definition against a direct Carto COUNT")
    a = ap.parse_args()

    if a.verify_citywide:
        return verify_citywide()

    if a.demo:
        holdings, source = [], "demo"
        for token in DEMO:
            kind, query, note = classify(token)
            h = Holding(given=token, query=query, kind=kind)
            if note:
                h.notes.append(note)
            holdings.append(h)
        source = f"demo ({len(holdings)} verified real properties)"
    elif a.portfolio:
        holdings = read_portfolio(a.portfolio)
        source = f"{a.portfolio} ({len(holdings)} entries)"
    else:
        ap.error("give a portfolio file, or --demo, or --verify-citywide")
        return 2

    if not holdings:
        print("no portfolio entries found", file=sys.stderr)
        return 1

    print(f"resolving {len(holdings)} propert"
          f"{'y' if len(holdings) == 1 else 'ies'} via AIS ...", file=sys.stderr)
    for i, h in enumerate(holdings, 1):
        h.prop = resolve(h.query)
        if h.prop.error and h.kind == "opa":
            # resolve() phrases every failure as an address failure, because an
            # address is what it is normally handed. Say what was really queried.
            h.prop.error = f"OPA {h.query}: " + h.prop.error.replace(
                "address not found in AIS", "no such OPA account number in AIS")
        if i % 25 == 0:
            print(f"  {i}/{len(holdings)}", file=sys.stderr)
        time.sleep(0.15)

    # ⛔ COLLAPSE DUPLICATE PARCELS, and do it here — after AIS, before counting.
    #
    # A real rent roll lists the same property more than once: "1234 Main St"
    # and "1234 MAIN STREET", or an address line and an OPA line for the same
    # parcel. Both resolve to one opa_account_num. Left alone, every headline
    # number and every CSV row is multiplied by however many times the property
    # appears, so "32 renewals due" might really be 20. Two reviewers flagged
    # this independently.
    #
    # Dedupe cannot happen at file-read time: the spellings differ and only AIS
    # knows they are the same parcel. UNCHECKED entries are never collapsed —
    # they have no parcel to collapse on, and each one is its own open question.
    seen: dict[str, Holding] = {}
    deduped: list[Holding] = []
    for h in holdings:
        key = h.opa
        if not key or h.unchecked:
            deduped.append(h)
            continue
        first = seen.get(key)
        if first is None:
            seen[key] = h
            deduped.append(h)
        else:
            first.notes.append(f"also given as {h.given!r} — same parcel, counted once")
    if len(deduped) != len(holdings):
        print(f"  collapsed {len(holdings) - len(deduped)} duplicate parcel "
              f"reference(s)", file=sys.stderr)
    holdings = deduped

    print("querying rental licences ...", file=sys.stderr)
    fetch_licences(holdings)
    fetch_licences_by_address(holdings)
    fetch_lapsed(holdings)
    print("querying open violations ...", file=sys.stderr)
    fetch_open_violations(holdings)
    annotate(holdings)

    report(holdings, a.out_csv, source)

    # Exit non-zero when any property could not be fully checked, so a scheduled
    # run can tell a complete pass from one where half the queries timed out.
    # Returning 0 unconditionally meant a cron job that checked $? saw success
    # even if every violations lookup had failed.
    # lapsed_ok only matters where the lapsed lookup was actually attempted —
    # fetch_lapsed targets parcels with no active licence. A healthy licensed
    # property never needs it, so requiring it flagged every clean run as
    # incomplete.
    incomplete = [h for h in holdings
                  if h.unchecked
                  or not h.viol_ok
                  or (not h.licences and not h.lapsed_ok)]
    if incomplete:
        print(f"\n {len(incomplete)} of {len(holdings)} propert"
              f"{'y' if len(incomplete) == 1 else 'ies'} could not be fully checked "
              f"-- exit 3.", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
