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
- **recover-delegates** (v3.4.145; replaced `sudoers-rules`) — the sudoers
  rules live in ONE place, `install.sh`'s `render_sudoers()`:
  `scripts/recover-sudoers.sh` has no rule line and no `SUDOERS_VERSION`
  marker and hands off to `install.sh --privileges-only`; `install.sh`'s
  first marker (the one the updater reads from a staged copy) sits inside
  `render_sudoers()`, whose `<< SUDOEOF` heredoc is unquoted
  (`<< 'SUDOEOF'` would install a literal `${u}`);
- **sudo-user** — in every `*.sh`, `SUDO_USER` (and `SUDO_UID`/`SUDO_GID`)
  is read only where the script runs as root. A non-root run can inherit it
  (`sudo -iu <account> bash …` leaves the admin's name there), so an
  unguarded `${SUDO_USER:-…}` picks the wrong account. A root test
  (`"$(id -u)"` or `$EUID` equal to 0) guards a read only when the read
  can't run without it passing: earlier on the read's line and joined to it
  by `&&` alone, or as the condition of an `if`/`elif` whose then-branch
  holds the read — on the same line up to that `if`'s own
  `else`/`elif`/`fi`, or on the lines below it, found by indentation (an
  `else` branch never counts). An `||`, a test's `-o`, a `!` before the
  test or a `;` between them means it doesn't. An unquoted heredoc's body
  counts where its `<<` is; `# sudo-user-ok: <why>` marks a line that is
  safe anyway. Cheap, not a parser: when unsure it reports (a root test
  made in another function, a `[ … ] || exit` line above the read, a
  `{ …; }` group); it trusts the indentation, and it sees only `$SUDO_*`
  expansions (not `printenv` or `${!name}`);
- **pipefail-readers** — in every `*.sh` that sets `pipefail`, no reader that
  can stop early (`grep -q`/`-m`/`-l`, `head`, `awk … exit`) sits on the
  right of a pipe: when it stops, the writer dies of SIGPIPE and pipefail
  turns that into a random failure (exit 141). Capture first, then match; or
  let `awk` read to the end with a found-flag. `# pipefail-ok: <why>` marks a
  line that is safe anyway; heredoc bodies are skipped; `sed …q` is not
  covered;
- **ntfy-cache** — the `cache-file:` path `ntfy_installer.py` writes into
  ntfy's `server.yml` is removed by `uninstall.sh` (on a code line, not only
  in a comment); a template it can't find is a finding, never a pass.

Prints one ✓/✗ line per check with indented details; exit `0` = clean,
`1` = findings, `2` = usage error (a `ROOT` that isn't a directory). A
`ROOT` with no shell scripts at all is a finding, never a silent pass.
`test_check_installers.py` imports its functions.

## `bootstrap.sh`

The curl installer (`bash <(curl -fsSL https://install.aerodromeadsb.com)`);
it goes live as soon as `main` is pushed, so treat every change to it as
public-facing. Everything is a function until its last line, which runs
`main` — a download cut short never runs a partial script. Read-only test
hooks: `AERODROME_BOOTSTRAP_OS_RELEASE`, `AERODROME_BOOTSTRAP_UNIT` and
`AERODROME_BOOTSTRAP_SOURCE_ONLY=1` (define the functions without running
`main`); `test_bootstrap.py` uses them in a stub sandbox.

## `recover-sudoers.sh`

Refreshes Aerodrome's system permissions (the sudoers rule) only. It has no
rules of its own: it runs the `install.sh` with the highest
`SUDOERS_VERSION` among its own tree, `<install>/update/`,
`<install>/update/aerodrome/` and `<install>/` (those that support
`--privileges-only`) as `install.sh --privileges-only --install-dir
<install>`. This is the command the Updates page shows:

```bash
sudo bash <install>/update/scripts/recover-sudoers.sh --install-dir <install>   # an update is staged
sudo bash <install>/scripts/recover-sudoers.sh                                 # the installed release
```

The older `sudo AERODROME_INSTALL_DIR=<install> bash …` form still works,
including piped (`curl … | sudo AERODROME_INSTALL_DIR=<install> bash`).
Piped with neither `--install-dir` nor `AERODROME_INSTALL_DIR`, it defaults to
`/opt/aerodrome`. A download cut short runs nothing (the script is one
function, called on its last line).
