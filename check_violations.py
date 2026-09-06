#!/usr/bin/env python3
"""Check Philadelphia L&I violations for a list of addresses.

WHY THIS EXISTS
---------------
Philadelphia Bill 250329 takes effect 2026-11-01. From that date an unsafe or
unfit violation left uncorrected past 30 days forfeits the landlord's right to
collect rent and to recover possession, with the burden of proof on the owner.

This script answers, for a portfolio: which of these properties currently carry
an open violation, and which carry the unsafe/unfit kind that the new law has
teeth for.

EVERYTHING IT USES IS FREE, KEYLESS AND PUBLIC
----------------------------------------------
  * AIS   https://api.phila.gov/ais/v1/search/<address>   address -> OPA id
  * Carto https://phl.carto.com/api/v2/sql                the violations table

Verified live 2026-09-06:
  violations table          2,014,450 rows
  open violations              92,433 across 30,322 parcels
  open unsafe/unfit             4,176 across  3,336 parcels

⛔ WHAT THIS CANNOT TELL YOU, AND WHY IT MATTERS
------------------------------------------------
The table has FIVE date columns — casecreateddate, casecompleteddate,
violationdate, violationresolutiondate, mostrecentinvestigation — and NONE of
them is a deadline. There is no cure-deadline column and no appeal-deadline
column. The legally operative dates live inside the notice PDF referenced by
`publicnov`, not in the tabular data.

So this script reports EXPOSURE and AGE, never a deadline. Do not let a report
from it imply "you have N days left" — that number is not in the data and
inventing it would be the most dangerous thing this tool could do.

USAGE
-----
    python check_violations.py addresses.txt
    python check_violations.py addresses.txt --csv out.csv
    python check_violations.py --demo          # runs against known-bad parcels

`addresses.txt` is one street address per line, or a CSV whose first column is
the address (a header row is detected and skipped).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable

AIS = "https://api.phila.gov/ais/v1/search/"
CARTO = "https://phl.carto.com/api/v2/sql"

# Bill 250329 bites on unsafe/unfit. Established empirically from
# violationcodetitle rather than assumed: UNSAFE STRUCTURE, UNFIT STRUCTURE,
# EXTERIOR STRUCT UNSAFE COND n, INTERIOR UNSAFE, VACANT PROP UNSAFE,
# UNFIT PROP STANDARD and siblings all match on these two substrings.
UNSAFE_PAT = ("UNSAFE", "UNFIT")

# Statuses that mean "still live". Measured distinct values include COMPLIED,
# CLOSEDCASE, OPEN, CLOSED, DEMOLISH, RESOLVE, ERROR, COMPEXCP, STOP WORK.
OPEN_STATUSES = ("OPEN",)

TIMEOUT = 45


# Retry on transient failures. Measured 2026-09-06: Carto answers a batched
# licence query in ~800ms, 5/5 successes — yet a real run still hit a 45s
# TimeoutError on one batch. Both tools treat a failed query as UNCHECKED
# (correctly, never as "clean"), so without a retry a single blip silently
# converts real properties into "not known" on a 900-address portfolio.
#
# 404 is NOT retried: for AIS that means the address does not exist, and
# retrying it three times just makes a wrong address slow as well as wrong.
RETRIES = 3
BACKOFF = 1.5


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "philly-violations-check/1.0"})
    last: Exception | None = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 400, 401):     # definitive answers, not blips
                raise
            last = exc
        except Exception as exc:                # noqa: BLE001 - timeouts, resets, partial reads
            last = exc
        if attempt < RETRIES - 1:
            time.sleep(BACKOFF * (2 ** attempt))
    raise last if last else RuntimeError("unreachable")


def carto(sql: str) -> list[dict]:
    return _get(f"{CARTO}?q={urllib.parse.quote(sql)}").get("rows", [])


@dataclass
class Prop:
    given: str
    normalized: str | None = None
    opa: str | None = None
    li_key: str | None = None
    match_type: str | None = None
    error: str | None = None
    violations: list[dict] = field(default_factory=list)

    @property
    def open_all(self) -> list[dict]:
        return [v for v in self.violations if (v.get("violationstatus") or "").upper() in OPEN_STATUSES]

    @property
    def open_unsafe(self) -> list[dict]:
        return [v for v in self.open_all
                if any(p in (v.get("violationcodetitle") or "").upper() for p in UNSAFE_PAT)]


# match_type values that mean "AIS actually found the thing you asked for".
# ⛔ EVERYTHING ELSE STILL RETURNS HTTP 200. In particular "unmatched" means the
# address DOES NOT EXIST and AIS estimated a location from the query components.
# Gating on the HTTP status instead of this field is how a typo in a rent roll
# comes back as "no violations found" — a false clean bill of health on a
# property that was never checked. For a compliance tool that is the worst
# available failure, so it is an ERROR here, never a silent pass.
TRUSTED_MATCH = ("exact", "exact_key", "unit_child")


def resolve(addr: str) -> Prop:
    """Address -> OPA account number, via AIS."""
    p = Prop(given=addr)
    try:
        d = _get(AIS + urllib.parse.quote(addr.strip()))
    except urllib.error.HTTPError as exc:                       # noqa: BLE001
        p.error = "address not found in AIS" if exc.code == 404 else f"AIS HTTP {exc.code}"
        return p
    except Exception as exc:                                    # noqa: BLE001
        p.error = f"AIS: {type(exc).__name__}: {exc}"
        return p

    feats = d.get("features") or []
    if not feats:
        p.error = "no AIS match"
        return p

    # More than one parcel matched. Taking features[0] would pick one arbitrarily
    # and report its violations as if they were this address's.
    total = d.get("total_size") or len(feats)
    if total > 1:
        p.normalized = d.get("normalized")
        p.error = f"ambiguous — {total} parcels match; disambiguate before trusting a result"
        return p

    props = feats[0].get("properties", {})
    p.normalized = props.get("street_address") or d.get("normalized")
    p.match_type = feats[0].get("match_type")

    if p.match_type not in TRUSTED_MATCH:
        p.error = f"not an exact match (match_type={p.match_type!r}) — treat as UNCHECKED, not clean"
        return p

    p.opa = props.get("opa_account_num") or None
    # L&I's own key. Populated on every row measured, including the condo-shell
    # rows where opa_account_num is blank. li_parcel_id is NOT a usable fallback:
    # it is blank on the very rows that motivate one, and can be a synthetic
    # negative (the negated OPA).
    p.li_key = props.get("li_address_key") or None

    if not p.opa:
        p.error = ("no OPA account number"
                   + (f" (L&I key {p.li_key} exists — checkable by hand)" if p.li_key else ""))
    return p


def fetch_violations(props: list[Prop], batch: int = 40) -> None:
    """Fill in .violations for every prop that has an OPA id.

    Batched because one request per parcel is needless load on a free public
    API, and Carto happily takes an IN list.
    """
    have = [p for p in props if p.opa]
    by_opa: dict[str, Prop] = {}
    for p in have:
        by_opa.setdefault(p.opa, p)

    opas = list(by_opa)
    for i in range(0, len(opas), batch):
        chunk = opas[i:i + batch]
        quoted = ",".join("'" + o.replace("'", "''") + "'" for o in chunk)
        sql = (
            "SELECT opa_account_num, address, casenumber, casetype, casestatus, "
            "violationnumber, violationdate, violationcode, violationcodetitle, "
            "violationstatus, violationresolutiondate, underappeal, publicnov, "
            "caseprioritydesc, mostrecentinvestigation "
            f"FROM violations WHERE opa_account_num IN ({quoted}) "
            "ORDER BY violationdate DESC"
        )
        try:
            for row in carto(sql):
                tgt = by_opa.get(row.get("opa_account_num"))
                if tgt is not None:
                    tgt.violations.append(row)
        except Exception as exc:                                # noqa: BLE001
            for o in chunk:
                by_opa[o].error = f"carto: {type(exc).__name__}: {exc}"
        time.sleep(0.25)                                        # be a good citizen


def days_since(v: str | None) -> int | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00")).date()
    except ValueError:
        try:
            d = datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return (date.today() - d).days


def report(props: list[Prop], out_csv: str | None) -> None:
    exposed = [p for p in props if p.open_unsafe]
    any_open = [p for p in props if p.open_all]
    failed = [p for p in props if p.error]

    print()
    print("=" * 68)
    print(" PHILADELPHIA VIOLATION EXPOSURE")
    print("=" * 68)
    print(f"  addresses checked          {len(props)}")
    print(f"  resolved to a parcel       {len([p for p in props if p.opa])}")
    print(f"  could not resolve          {len(failed)}")
    print()
    print(f"  ANY open violation         {len(any_open)}")
    print(f"  OPEN UNSAFE / UNFIT        {len(exposed)}   <-- Bill 250329 exposure")
    print("=" * 68)

    if exposed:
        print()
        print(" Properties with open unsafe/unfit violations:")
        print()
        for p in exposed:
            print(f"  {p.normalized or p.given}   (OPA {p.opa})")
            # publicnov is one PDF per CASE, not per violation — several
            # violations on one case share a notice. Print each notice once.
            notices: dict[str, str] = {}
            for v in p.open_unsafe:
                age = days_since(v.get("violationdate"))
                age_s = f"{age}d old" if age is not None else "age unknown"
                appeal = " [UNDER APPEAL]" if str(v.get("underappeal") or "").strip().lower() in ("y", "yes", "true", "1") else ""
                print(f"      - {v.get('violationcodetitle')}  ({str(v.get('violationdate'))[:10]}, {age_s}){appeal}")
                if v.get("publicnov"):
                    notices.setdefault(v.get("casenumber") or v["publicnov"], v["publicnov"])
            for case, url in notices.items():
                print(f"        notice (case {case}): {url}")
            print()

    if failed:
        print(" Could not resolve:")
        for p in failed[:20]:
            print(f"   {p.given}  --  {p.error}")
        if len(failed) > 20:
            print(f"   ... and {len(failed) - 20} more")
        print()

    print(" NOTE: this data carries NO cure or appeal deadline. Those exist only")
    print(" inside the notice PDF. Ages above are days since the violation date,")
    print(" which is NOT the same thing as time remaining.")
    print()

    if out_csv:
        with open(out_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["given_address", "normalized_address", "opa_account_num",
                        "violation_date", "days_since", "code_title", "status",
                        "under_appeal", "is_unsafe_unfit", "case_number", "notice_url", "error"])
            for p in props:
                if p.error and not p.violations:
                    w.writerow([p.given, p.normalized or "", p.opa or "", "", "", "", "", "", "", "", "", p.error])
                    continue
                for v in p.open_all:
                    title = (v.get("violationcodetitle") or "")
                    w.writerow([
                        p.given, p.normalized or "", p.opa or "",
                        str(v.get("violationdate"))[:10], days_since(v.get("violationdate")) or "",
                        title, v.get("violationstatus") or "", v.get("underappeal") or "",
                        "YES" if any(x in title.upper() for x in UNSAFE_PAT) else "no",
                        v.get("casenumber") or "", v.get("publicnov") or "", "",
                    ])
        print(f" CSV written: {out_csv}")
        print()


def read_addresses(path: str) -> list[str]:
    out: list[str] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        if "," in sample:
            for i, row in enumerate(csv.reader(fh)):
                if not row:
                    continue
                cell = (row[0] or "").strip()
                if i == 0 and cell.lower() in ("address", "street address", "property address", "addr"):
                    continue
                if cell:
                    out.append(cell)
        else:
            out = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    return out


DEMO = [
    "2651 S Mildred St",      # known open UNSAFE STRUCTURE + INTERIOR UNSAFE
    "1327 S 4th St",          # known open INTERIOR UNSAFE GENERAL
    "535 E Penn St",          # known open UNSAFE STRUCTURE
    "5146 N 10th St",         # control - expected clean
    "1234 Market St",         # control - commercial
]


def main() -> int:
    ap = argparse.ArgumentParser(description="Check Philadelphia L&I violations for a list of addresses.")
    ap.add_argument("addresses", nargs="?", help="file with one address per line, or a CSV whose first column is the address")
    ap.add_argument("--csv", dest="out_csv", help="write per-violation rows to this CSV")
    ap.add_argument("--demo", action="store_true", help="run against known-bad parcels instead of a file")
    a = ap.parse_args()

    if a.demo:
        addrs = DEMO
    elif a.addresses:
        addrs = read_addresses(a.addresses)
    else:
        ap.error("give an address file, or --demo")
        return 2

    if not addrs:
        print("no addresses found", file=sys.stderr)
        return 1

    print(f"resolving {len(addrs)} address(es) via AIS ...", file=sys.stderr)
    props = []
    for i, addr in enumerate(addrs, 1):
        props.append(resolve(addr))
        if i % 25 == 0:
            print(f"  {i}/{len(addrs)}", file=sys.stderr)
        time.sleep(0.15)

    print("querying violations ...", file=sys.stderr)
    fetch_violations(props)
    report(props, a.out_csv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
