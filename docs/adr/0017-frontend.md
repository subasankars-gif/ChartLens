# ADR-0017: The frontend: a faithful, untrusted visualization layer

**Status:** Accepted · 2026-10-02 (decisions confirmed by Suba the same day)

## Invariants

1. **The frontend is untrusted.** Every rule that matters is enforced by the API (ADR-0016):

   ```
   Google sign-in → Firebase ID token → API → verify token → approved user? → data
   ```

   The pending screen is UX, not authorization. The admin page calls admin endpoints
   that the API authorizes, and the UI never decides who is an admin. The static
   site holds only public identifiers: the Firebase web config and the API URL. It
   never receives GCS credentials and never touches the bucket.
2. **Faithful visualization.** If the API returns it, the chart may draw it. If the API
   does not return it, M6 does not infer or calculate it. Only drawing concerns are
   computed in the browser: pixel positions, scaling, colours, layout. There are no
   indicators (not even a moving average), swings, trendlines, patterns, adjustment,
   or inferred continuity.

## Decisions (confirmed 2026-10-02)

| # | Decision |
|---|---|
| 1 | **Firebase Hosting**: a static export at `https://chartlenslab.web.app` (Hosting site `chartlenslab` in the project `chartlens-lake-13934`), listed in the API's CORS origins. It deploys keylessly from GitHub, as `chartlens-deployer` |
| 2 | **Google sign-in** (Firebase, popup). A *pending* screen for users not yet approved. An Admin → Users page backed by the admin API |
| 3 | **TradingView Lightweight Charts**: weekly candles and a volume pane. The chart component is analysis-agnostic |
| 4 | **The valid segment is shown by default.** "Show earlier history" adds older segments, separated by a break band. No synthetic bridging bar, no connecting line, no implication that the move across a break was a market move |
| 5 | **Flags:** the forming week (hollow body), `CONTINUITY_BREAK` partial bars, and the special-session type on bars that close on one. Hovering shows the exact decimal text from the API |
| 6 | **Security page:** identity and its symbol and ISIN history, status, `usable_from`, segments, findings, and the full provenance set (`meta_version`; weekly, data, adjustment, identity, dq and calendar versions; methodology hash; data `as_of`) |
| 7 | **Watchlists deferred.** The API already supports them |

**Production URL** (amended 2026-10-02). The product URL is `https://chartlenslab.web.app`,
a dedicated Hosting site in the same project. The project, Cloud Run, GCS and Firestore are
unchanged. Sign-in uses that domain as its `authDomain`, so the popup runs on the site's own
origin. During the move, the API also accepts the project's default Hosting domains
(`chartlens-lake-13934.web.app` and `.firebaseapp.com`). They are removed once the new site
is verified end to end.

**Routing.** Pages are `/` (sign-in, then search), `/security/?id=…` and
`/admin/users/`. A static export cannot pre-render 4,061 security pages, so the
`security_id` travels in the query string.

**Security ids are opaque.** Search returns the id, and the UI passes it back to the API
unchanged. The UI never builds or parses one.

**Exact decimals.** Prices and volumes arrive as decimal strings and are displayed from
those strings. Display trims trailing zeros; it does not round through binary floats.
Numbers are converted to floats only to place a candle on the canvas.

**Types come from the contract.** `docs/api/openapi.json` is generated from the API
(`scripts/export_openapi.py`), and the frontend's types are generated from that file
(`pnpm gen:api`). CI fails if either is stale.

## Acceptance criteria

1. An unauthenticated user is sent to sign in.
2. A signed-in user who is not approved sees the pending screen. The API refuses that
   user's data requests too.
3. An approved user can search.
4. Search covers current and historical symbols, ISINs and names.
5. The security page is keyed by the immutable `security_id`.
6. The weekly chart reconciles exactly to the API data.
7. The forming week is visually distinguished.
8. Continuity breaks cannot be visually bridged.
9. Earlier segments are opt-in.
10. Weeks that close on a special session are flagged with their type.
11. Decimal prices display without binary-float artifacts.
12. The security page shows `usable_from`, status, findings and provenance.
13. No M6 component computes technical analysis.
14. Admin approval is authorized by the server, not only by the UI.
15. The static frontend never receives GCS credentials and never accesses the bucket.

**Verification.** Unit tests cover the chart-data mapping: segments, breaks, flags and
exact text. Playwright drives the built site against the real API over a test lake. In
that end-to-end build only, a test sign-in replaces Google. The production build refuses
that mode, and CI checks the deployed bundle for it. A probe also renders real securities
from a copy of the lake for screenshots.

## Amendment (2026-10-02, M8): drawing analysis

The faithful-visualization invariant extends to technical analysis (ADR-0023). The chart
draws the swings, structure, zones, Fibonacci levels, divergences and pattern geometry
that the API returns, behind toggles that are off by default. It never fits, detects,
tests or decides any of them.
