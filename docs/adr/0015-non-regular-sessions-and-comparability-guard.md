# ADR-0015: Non-regular sessions and the engine comparability guard

**Status:** Accepted · 2026-10-02 (decisions confirmed by Suba the same day)
**Extends:** ADR-0014 (weekly data product), ADR-0006 (point-in-time).

## 1. Non-regular sessions: keep as traded, flag, decide in the engine

Some weekly closes come from sessions that were not regular trading:

- a Muhurat session, about an hour on Diwali evening;
- a short DR switch-over drill;
- a Budget-day weekend session;
- a day the market halted.

**Decision: option (a).** The weekly data keeps every session exactly as traded and flags
it. A synthetic "regular" close would make the data less faithful to its source, so
none is substituted. The distinction belongs to the technical engine:

> A weekly technical confirmation occurring on a week whose terminal session is a
> non-regular session is **provisional** until the next regular trading week confirms or
> invalidates it.

The rule protects ChartLens's principle that the weekly state is authoritative and
should not be flipped by noise, here an hour of thin trading.

### Data

The trading calendar types every non-regular session. `special_types` in the provider's
calendar file uses a generic vocabulary that each exchange maps onto:

| Type | Meaning |
|---|---|
| `MUHURAT` | Diwali Muhurat session |
| `BUDGET` | A Union Budget weekend session, a full session |
| `DR_DRILL` | A short live session with a switch-over to the disaster-recovery site |
| `OTHER` | Anything else: a full make-up session, a halted or shortened day, an unidentified weekend file |

- **Weekend sessions:** every one is non-regular. The 26 sessions from 2006 to 2026 are all
  typed, and a test enforces it.
- **Weekday sessions:** they are typed only when they were not regular trading, which
  covers 13 weekday Muhurat sessions and 2 shortened days. These were **found from the
  data**: a read-only probe over all 5,146 sessions flagged every weekday whose market
  turnover was below 30% of the local median (±20 sessions). The flagged days are:
  - 13 Muhurat sessions, from 2007-11-09 (Fri) to 2025-10-21 (Tue), at 12–27% of the median;
  - 2009-05-18, a circuit-breaker halt at 0.9%;
  - 2017-07-10, the NSE outage with a late start, at 25%.

  Each carries its turnover ratio as evidence in the calendar's notes.
- **Correction:** 2024-01-20 was announced as a DR drill but held as a full session after
  22 January was declared a holiday (turnover 73% of the median). It is typed `OTHER`.

A weekday Muhurat session can set the weekly close. For example, Friday 2024-11-01 closed
2024-W44 for every security. The first M4 build flagged weekend sessions only, so it
missed these closes; they are flagged now.

Weekly bars (builder `weekly_v2`, schema 2) carry three fields:

- `special_sessions`: how many non-regular sessions the bar contains;
- `closes_on_special_session`: whether the close came from one;
- `closing_session_type`: the type of that closing session.

Prices are unchanged.

## 2. The comparability guard: one place, every analyzer

No technical relationship may compare two weekly bars unless all of the following hold:

```
same security_id AND same continuity_segment_id AND complete bars AND valid as_of
```

This covers HH/HL/LH/LL, BOS/CHoCH, trend transitions, pattern geometry, Fibonacci swings
and divergence. Each analyzer does not remember the rule; it is enforced once, in
`chartlens_engine.interfaces`:

| Condition | Where it is enforced |
|---|---|
| Valid `as_of` | `run_analyzer` → `ensure_as_of`: refuses any bar dated after `as_of` |
| Same security | Bar-frame contract: a `security_id` column holds one value. `run_analyzer` → `ensure_comparable`: it must equal `context.security_id` |
| Same continuity segment | Bar-frame contract: a `continuity_segment_id` column holds one value. `ensure_comparable`: it must equal `context.continuity_segment_id` |
| Weekly frames carry both | `ensure_comparable` refuses a weekly frame without `security_id` and `continuity_segment_id`, or a weekly context without a segment |
| Complete bars | `confirmable(bars)` returns complete bars only. Confirmation logic uses it; incomplete bars may be drawn but never confirm |
| Non-regular closes | `provisional(bars)` marks bars whose close came from a non-regular session, so a confirmation on them is provisional (section 1) |

Analyzers receive frames through `run_analyzer` only. `WeeklySeries.frame()` produces
frames with the identity columns attached, so a correctly loaded frame always passes
the guard. A frame mixing securities or segments, or one built by hand without its
identity, is refused before any analyzer sees it.

## Scope

M5 (API, Firestore, auth) exposes the data products and their metadata. It does not
compute technical analysis. The flow is:

```
GCS (adjusted daily, weekly) → WeeklyReader → API → frontend
```

The API never builds bars on request. The provisional-confirmation rule is applied by
the engine when confirmation logic exists. M4/M5 provide the flags and the guard it
relies on.
