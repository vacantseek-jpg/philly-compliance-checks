# Philadelphia compliance checks

Two tools that answer, for a rental portfolio, using only free public City of
Philadelphia data:

| tool | question |
|---|---|
| `check_violations.py` | Which of these properties carry open L&I violations, and which are the unsafe/unfit kind Bill 250329 has teeth for? |
| `check_renewals.py` | Which rental licences expire when, which have lapsed, and which parcels have no licence at all? |

No API key. No AppFolio access. No permission needed from the property owner.

```bash
python check_violations.py --demo
python check_renewals.py  --demo

python check_renewals.py rentroll.csv --csv renewals.csv
```

`rentroll.csv` is one entry per line, or a CSV whose first column is the entry.
An all-digit entry is treated as an OPA account number; anything else as a street
address. Both go through AIS.

**Exit codes.** `0` every property fully checked · `3` something could not be
checked · `1` bad input. A scheduled run can tell the difference, which it could
not when the tool always returned 0.

---

## Why this exists

Philadelphia **Bill 250329** takes effect **2026-11-01**. From that date, an
unsafe or unfit violation left uncorrected past 30 days forfeits the landlord's
right to collect rent and to recover possession, with the burden of proof on the
owner. Across a few hundred units that is a rent-roll problem, not a fines
problem.

---

## Data sources

Both keyless, both public, both verified live on 2026-09-06.

```
AIS    https://api.phila.gov/ais/v1/search/<address or OPA>
Carto  https://phl.carto.com/api/v2/sql?q=<url-encoded SQL>
         tables: violations, business_licenses
```

⛔ **Never send an API key.** A bogus `gatekeeperKey` turns a working AIS `200`
into a `401`. Absent is correct; wrong is worse than nothing.

---

## Measured, 2026-09-06

Everything below was taken by query, not read from a document.

### Violations

```
violations table                    2,014,450 rows
open (violationstatus='OPEN')          92,433 across 30,322 parcels
open UNSAFE/UNFIT                       4,176 across  3,336 parcels
carry a notice PDF (open unsafe)         84.8%
data freshness                        ~1 day
```

Unsafe/unfit is expressible from `violationcodetitle`: `UNSAFE STRUCTURE`,
`UNFIT STRUCTURE`, `EXTERIOR STRUCT UNSAFE COND n`, `INTERIOR UNSAFE`,
`VACANT PROP UNSAFE`, `UNFIT PROP STANDARD` and siblings.

⛔ **There is no deadline column.** Five date columns —
`casecreateddate`, `casecompleteddate`, `violationdate`,
`violationresolutiondate`, `mostrecentinvestigation` — and none is a cure or
appeal deadline. Those exist only inside the notice PDF. The tools report
**exposure and age, never a deadline.** In 7 of 10 sampled notices the appeal
deadline *preceded* the cure deadline, so one cannot be derived from the other.

`publicnov` is one PDF per **case**, not per violation.

### Licences

```
active rental licences                 93,740
expiring within 30 days                 3,313
expiring within 60 days                 8,408
already expired, still 'Active'           914
```

`rentalcategory` is pipe-delimited and multi-valued (`Hotel|Residential Dwellings`).
`Expired` is a distinct `licensestatus` (4,005) separate from Active-with-a-past-date.

### Volume: renewals are ~60x violations

On a 900-unit portfolio, roughly **32 renewals a month** against **~5 unsafe/unfit
violations in total**. Violations are a risk story; renewals are a labour story.

---

## A claim that did not survive measurement

> "L&I withholds licence renewal while violations sit on record past 30 days."

Not visible in the data:

```
active rental licences WITHOUT an open violation   90,218   0.96% expired
active rental licences WITH    an open violation    3,522   1.28% expired
```

A 1.3x difference is not a blocking mechanism. `check_renewals.py` prints
violations beside each licence tagged **"informational — not a renewal blocker"**
and carries this measurement in its own footer, so the claim cannot be repeated
by someone reading its output.

---

## Scale test — 900 real addresses

Run against 900 real Philadelphia rental addresses, heavy on hyphenated
multi-unit buildings.

```
900 properties · 1,049 s (~17 min) · exit 3

resolved 857 · UNCHECKED 43 (40 ambiguous, 3 not exact)
862 attributable licences across 854 properties
  27 due within 30 days · 66 within 60 · 110 within 90 · 6 expired
   3 with no active licence
  14 multi-owner buildings (143 licences) held back for attribution
```

27 due in 30 days, against a citywide extrapolation of ~32. Two independent
methods, same answer.

**The scale test earned its keep.** At 15 addresses everything looked right. At
900, `1001-13 CHESTNUT ST` returned **55 active licences across 22 different
owners** — a condo building, not one landlord's holding — and every one was being
counted as the portfolio's. Fourteen such buildings inflated the 90-day figure
from a true 110 to a claimed 130, an **18% overstatement built from other
landlords' licences**. They are now reported separately as a question rather than
counted as an answer.

---

## What these tools refuse to do

1. **Never report an unchecked property as clean.** An address AIS cannot pin to
   exactly one parcel is `UNCHECKED`, and a failed database query renders as
   `NOT CHECKED`, never as `none` or `0`. AIS returns **HTTP 200 for addresses
   that do not exist** (`match_type: "unmatched"`), so resolution gates on
   `match_type`, never on the status code. On a real rent roll ~4.4% come back
   ambiguous and need a human — that is the honest cost of not guessing.

2. **Never invent a deadline.** See above.

3. **Never assert a legal consequence.** An earlier draft claimed L&I "can order
   the units vacated" and that an unlicensed period is "not curable in arrears".
   Neither statement is in any dataset here. Removed.

4. **Never call a parcel a rental because it lacks a rental licence.** Absence of
   a licence is evidence that no licence exists, not that the property is being
   let. A commercial address correctly has none.

5. **Never attribute another owner's licences to the portfolio.** See the scale
   test.

---

## Layout

```
check_violations.py   violations + the shared HTTP layer, AIS resolution,
                      match_type/ambiguity guards, retry with backoff
check_renewals.py     licences; imports the above rather than copying it
```

There is deliberately **one** definition of "did we actually identify this
parcel" and **one** of what counts as unsafe/unfit. A second copy is a second
thing that can drift.

---

## Status

Both tools are built, adversarially reviewed and scale-tested. Neither has run
against a real client portfolio, because that needs a rent roll.
