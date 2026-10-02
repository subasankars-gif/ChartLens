# ADR-0004: Weekly candle construction and week boundaries

**Status:** Accepted · 2026-09-30 · field names superseded by [ADR-0014](0014-weekly-data-product.md): the label is `last_session_date`; `week_start_date`/`week_end_date` are the ISO week's Monday/Sunday; bars split at continuity breaks

## Decision

Weekly candles are built from the canonical daily dataset (never taken from an
external weekly feed), deterministically:

| Field | Rule |
|---|---|
| Grouping | Sessions bucketed by **ISO week (Monday–Sunday)** |
| `open` | Open of the first session in the week |
| `high` / `low` | Max high / min low across the week's sessions |
| `close` | Close of the last session in the week |
| `volume` | Sum of session volumes |
| `week_start_date` | First *actual* session of the week |
| `week_end_date` (`bar_date`) | Last *actual* session of the week, not the calendar Friday |
| `trading_days` | Number of sessions in the bucket |
| `is_complete` | True once the week's last *scheduled* session (from the trading calendar) has closed |

### Edge cases

* **Friday holiday:** the week ends on Thursday; `week_end_date` is Thursday.
* **Special weekend sessions** (e.g. a Saturday Budget-day session): belong to the ISO
  week they fall in.
* **Muhurat session:** belongs to its ISO week like any other session. If it falls on
  a Sunday it becomes that week's closing print on very thin volume; data quality
  flags the week so this is visible rather than silently shaping the close.
* **A week with no sessions** produces no bar (not a zero-volume bar).

### The forming week

The current week's bar is built and shown, with `is_complete = false`. **No
confirmation, breakout or pattern status may be decided on an incomplete bar**
(spec §45: confirmation only after the candle closes). The UI marks it visibly.

## Consequences

* The builder needs the trading calendar to decide `is_complete`, so the calendar
  lives in the exchange layer and is stored under `metadata/calendars/`.
* For historical (point-in-time) runs, weekly bars are rebuilt from daily bars
  `<= as_of` — see ADR-0006.
* The same builder generalises to monthly bars by changing the grouping key.
