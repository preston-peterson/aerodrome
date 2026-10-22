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

## `check_release_zip.py`

Proves a release zip **is** `git archive` of a given commit or tree, in the
canonical layout — and holds the one list of maintainer-only / local paths
that must never ship. Stdlib only; reads both in memory, extracts nothing.

```bash
python3 scripts/check_release_zip.py ZIP X.Y.Z REF [--repo DIR] [--label TEXT] [--no-hint]
python3 scripts/check_release_zip.py --deny-paths [X.Y.Z]  < paths
```

`REF` is any tree-ish: a commit (the release commit), or a tree id (a
snapshot of the working tree's tracked files). `--label` names it in the
findings ("only in working tree: …") instead of a raw id. It checks:

- **integrity** — every member's CRC;
- **shape** — exactly one top-level `aerodrome-vX.Y.Z/` folder; no absolute
  paths, `..`, backslashes or symlink entries;
- **caches** — no `__pycache__/`, `*.pyc` or `*.pyo`;
- **version** — the `VERSION` file inside says `X.Y.Z`;
- **content** — the same set of files as `git archive REF`, byte-identical,
  with the same executable bits. No exceptions list.

Exit `0` = clean, `1` = findings (listed, capped at 25), `2` = usage or git
error. Each kind of finding gets its own how-to-fix line: a file that
differs or is only in the zip → `unzip -p ZIP '<wrapper>/<path>' > <path>`
(plus `chmod +x` when the zip has it executable); a file only in `REF` →
remove it (`git rm`); an exec-bit difference → `chmod ±x`. `--no-hint` drops
those lines. `package-release.sh` runs it against a snapshot of the working
tree before it writes the `.sha256` (with `--no-hint`: that zip is deleted on
failure), and again in `--verify`; `publish-release.sh` runs it against
`--target` before publishing and against the tag on the downloaded zip
afterwards.

`--deny-paths` reads paths on stdin and prints every maintainer-only or
local one (`HANDOFF*.md`, `CLAUDE.md`, `AGENT_GUARDRAILS.md`, `audits/`,
`logs/`, `.claude/`, `graphify-out/`, `config.yaml`, `*.db`, …) — matched
by whole path component, so `dialogs/` or `templates/logs.html` are fine.
Exit `1` when there are any. `package-release.sh` runs it on the tracked
files before zipping; `publish-release.sh` on the `--target` tree and on the
downloaded zip. With `X.Y.Z`, a file an older release is known to have
carried (`docs/tech-debt-audit.md` before v3.4.109) is a note, not a hit.

## `check_installers.py`

Static consistency gate for the shell installers — a **required** step of
every `bump-version.sh` run (and so of every package), with no skip flag.
Stdlib only, a few hundredths of a second.

```bash
python3 scripts/check_installers.py [ROOT] [check,check,...]
python3 scripts/check_installers.py --help
```

- **bash-syntax** — every git-tracked `*.sh` parses (`bash -n`);
- **pkg-detect** — `pkg_detect()` is byte-identical in `install.sh` and
  `scripts/bootstrap.sh` (each script must work standalone, so the
  function is duplicated on purpose);
- **sudoers-rules** — the rule lines of the sudoers heredoc in `install.sh`
  match `scripts/recover-sudoers.sh`'s, both heredocs are opened unquoted
  (`<< 'SUDOEOF'` would install a literal `${USER}`), and both carry the
  same `SUDOERS_VERSION` marker (the first one in `install.sh`, which is the
  one the updater reads).

Prints one ✓/✗ line per check with indented details; exit `0` = clean,
`1` = findings, `2` = usage error (a `ROOT` that isn't a directory). A
`ROOT` with no shell scripts at all is a finding, never a silent pass.
`test_check_installers.py` imports its functions.
