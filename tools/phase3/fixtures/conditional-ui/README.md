# ui-conditions scratch

Development fixtures and offline checks for report page 6 (`条件 Buff` /
`tools/phase3/src/le_calc/report_ui/pages/conditions.py`).

Two payload sources are used:

* `fixture_view.json` — hand-built from `<author-archive>/conditional-interface-spec.md`
  §8.5/§9 examples (Craterborn, Throne of Ambition, Chilled Touch, Conflagrate,
  Eternal Flames, Supermassive, Crescendoing Rage, mana-threshold idol).
* `real-view.json` — the real payload produced by the backend package:
  `le_calc.conditional.build_context(snapshot, ruleset=..., calc_input_path=...,
  build_path=..., repo_root=.)["view"]` (17 effects / 17 states / 4 presets,
  status=ready).  Regenerate it when the registry/backend changes.

## Files

| File | Purpose |
|---|---|
| `build_fixture.py` | Builds `fixture_view.json` (full §9 page payload) and `fixture_unavailable.json` (legacy/degraded payload). |
| `fixture_view.json` | Fixture `view["conditions"]` payload: 8 effects, 6 shared states, presets, checked ledger, dependency modules, client rules + baseline scenario. |
| `fixture_unavailable.json` | `available=false` payload used for the explanatory empty page. |
| `check_render.py` | Fixture render checks: root `<section>`, zero `<script>/<style>`, no external URLs/handlers, every button `type="button"`, required anchors, escaping, unavailable/degraded payloads, `node --check app.js`, `.cond-*` CSS appendix, offline JS tokens. |
| `check_real.py` | Same structural checks against `real-view.json`, plus: one control per declared control kind, proposal-note visibility per initial status, 检查分母 content, preset options, shared-state controls, effect name coverage, collapsed baseline-included records, hostile-payload escaping. |
| `conditions_module.js` | Copy of the appended app.js module (source of truth lives in `report_ui/assets/app.js`; kept for diffing). |
| `build_full.py` | Renders the real 5-page report with the **fixture** view injected as `result.conditional.view` → `full-report.html`; also renders the no-conditional report → `full-report-old.html`. |
| `build_real.py` | Renders the real report with the **backend** view injected → `real-report.html` (and asserts the embedded view-data round-trips). |
| `cdp_smoke.mjs` | CDP page tests against the fixture payload: tab switch, §8.4 status precedence, shared-state/effect sync, filters, presets, reset, export Blob, import (valid + invalid), changed notice, other tabs, print media emulation. |
| `cdp_smoke_real.mjs` | CDP smoke against the real backend payload: compares client §8.4 statuses with backend `status.initial` for every effect, badges per card, proposal-note visibility, 检查分母, toggle/cast-state/stacks/shared-state controls, filters, preset apply/reset, export→import round trip, invalid import, other tabs, print media emulation. |
| `run_headless.py` | Launches headless Chrome with a CDP port (real async timers), runs both smoke suites, then screenshots + print-to-PDF probes, and checks the legacy report's empty page. |

## Commands (repository root)

```powershell
$env:PYTHONPATH='tools\phase3\src'
.\.venv\Scripts\python.exe <author-archive>\ui-conditions\build_fixture.py
.\.venv\Scripts\python.exe <author-archive>\ui-conditions\check_render.py
.\.venv\Scripts\python.exe <author-archive>\ui-conditions\check_real.py
.\.venv\Scripts\python.exe <author-archive>\ui-conditions\build_full.py
.\.venv\Scripts\python.exe <author-archive>\ui-conditions\build_real.py
.\.venv\Scripts\python.exe <author-archive>\ui-conditions\run_headless.py
```

Expected: `check_render.py` / `check_real.py` print "all ... checks passed";
`run_headless.py` prints fixture 40/40 interaction checks + 7 print checks and
real 39/39 interaction checks + 5 print checks, then "headless smoke OK".

Screenshots: `conditions-top-1440.png`, `conditions-1440x900.png`,
`conditions-full-1440.png`, `conditions-top-900.png`, `real-conditions-*`.
Print probes: `conditions-print.pdf`, `real-conditions-print.pdf`.

## Status semantics

The page JS implements contract §8.4 exactly like the backend
`le_calc.conditional.evaluate._status_for` (including `cast_state` controls,
`already_in_baseline` before auto states, `has_unknown` → `auto_unknown`,
and "any" condition groups).  `cdp_smoke_real.mjs` asserts that the client
statuses after page init equal the backend `status.initial` for all 17 effects;
keep those two implementations in sync if the contract changes.
