# scripts/

Helper scripts that don't run as part of the Aerodrome service. Everything
here is developer / maintainer tooling, safe to ignore at runtime.

## `screenshots.py`

Regenerates every PNG in `docs/` from the current HTML templates using
Playwright and synthetic mock data. No live receiver, no real aircraft
data, no PII.

**When to run:**

See `CONTRIBUTING.md` → *"Updating screenshots"* for the full policy. In
short: after any UI change that would make an existing screenshot look
wrong (new tab, new form field visible in an existing shot, reworked
layout, etc.), or when adding a feature big enough to warrant its own
screenshot.

**How to run** (from the repo root):

```bash
pip install playwright
playwright install chromium
python3 scripts/screenshots.py
```

Output goes to `docs/screenshot-*.png`, overwriting the existing files.
Review the results visually before committing — the harness prints JS
errors if the mock data has drifted from what the templates now expect,
but subtle layout regressions still need a human eye.

**Adding a new screenshot:**

1. Add a new `screenshot_foo(browser)` async function following the same
   pattern as the existing ones.
2. Append the function to the list in `main()`.
3. Add a corresponding entry to the Screenshots section in `README.md`.
4. Run the script and verify the new PNG looks right.

**When the harness crashes:**

If the script fails with errors like `Cannot read properties of undefined`,
it usually means a template now reads a field that the mock data doesn't
provide (or provides in the wrong shape). Look at the error path in the
template file, then update the corresponding payload constant near the
top of `screenshots.py`.
