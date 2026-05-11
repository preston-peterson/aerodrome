# Tech-debt audit report

_Generated 2026-04-23 16:45 UTC against Aerodrome v2.47.0 by `scripts/tech_debt_audit.py`._

This report is a static-analysis snapshot. It finds suspicious patterns; it does not modify code. Each finding is a candidate for manual review — some will be real debt, others will be false positives from dynamic dispatch, external callers, or framework-provided lifecycle hooks.

## Summary

| Category | Count |
|---|---:|
| Dead Python functions | 0 |
| Orphan FastAPI endpoints | 0 |
| Dead JavaScript | 0 |
| Stale version comments | 1 |
| TODO / FIXME / HACK markers | 0 |
| **Total** | **1** |

## Dead Python functions

Functions defined but with no references found in the Python codebase. Verify no dynamic dispatch (getattr, plugin registries) before removing.

_No findings in this category._

## Orphan FastAPI endpoints

Routes declared in server.py whose path prefix doesn't appear in any template, script, or other Python source. May be called by external tools or left over from a removed feature.

_No findings in this category._

## Dead JavaScript

Top-level JS functions or let/const declarations with no references in the same template. Event-handler attributes (onclick, onsubmit) are counted as real references.

_No findings in this category._

## Stale version comments

In-source version references many minor releases old. Comments tied to these versions may describe behavior that has since changed.

### `collector.py`

- **Line 256** — `v2.36.5`
  - Version reference is 2.47 - 2.36 = 11 minor releases old. Comment may describe behavior that has since changed.

## TODO / FIXME / HACK markers

Inventory of self-flagged notes in the code. Not inherently debt — useful for tracking unresolved items.

_No findings in this category._

---

_To regenerate: `python3 scripts/tech_debt_audit.py`_
