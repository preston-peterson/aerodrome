# Update staging folder

This folder is used by Aerodrome's local update feature. Drop a new release
into this folder (or upload a release zip through the web UI's Updates page),
and Aerodrome will detect, stage, and apply it on request.

## How to stage an update

You have three ways to put a release in this folder:

1. **Web UI drag-and-drop.** Open the Aerodrome web UI, click the gear icon,
   then **Updates**. Drop an `aerodrome-vX.Y.Z.zip` onto the upload area. The
   server extracts it into this folder automatically, detects the staged
   version, and enables the **Apply & restart** button.

2. **SCP / rsync from your workstation.** Transfer the release contents into
   `/opt/aerodrome/update/` directly (or wherever your install lives — replace
   `/opt/aerodrome` with your install dir if you used `--prefix`). Either of
   these layouts works:

   - Flat: `update/VERSION`, `update/server.py`, `update/templates/…`, etc.
   - Nested: `update/aerodrome/VERSION`, `update/aerodrome/server.py`, etc.

   Both shapes are detected automatically. Then open the web UI's Updates
   page and click **Check again** to see the staged version, followed by
   **Apply & restart**.

3. **Copy files by hand.** Any tool that puts the release's files at one of
   the two layouts above will work.

## What happens when you apply

1. The release is copied to the side, into `.update-staging-<timestamp>/` —
   nothing of the running install changes yet. Your config.yaml, database,
   logs and venv are never part of it.
2. If its `requirements.txt` lists different packages, they are installed
   into Aerodrome's Python environment (they stay installed even if the
   update is then refused or rolled back).
3. The new version is test-loaded in a fresh Python process. If it can't
   load (a syntax error, a missing package), the update stops with the
   reason, Aerodrome's files are left as they were, and the release stays
   staged here — fix the cause and press Apply again.
4. The new files are switched in all at once and the service restarts. If
   Aerodrome can't restart itself, the switch is undone and the update is
   refused, with Aerodrome's files left as they were.
5. If the new version fails to start twice, Aerodrome puts the previous
   version (and its config.yaml) back on its own and records why in
   `.update-last-result.json` and the service log; the Updates page shows
   what was put back and why. About 30 seconds after
   the new version answers, the update is confirmed and the previous
   version is kept in `/opt/aerodrome/.backups/<timestamp>/`.

To go back to an earlier version by hand, see "Rolling back" in
docs/INSTALL.md.

## Sudoers version check

Aerodrome uses a `/etc/sudoers.d/aerodrome` rule for a small set of
privileged operations (installing/uninstalling the ntfy service, purging
data). When a release bumps the sudoers version, applying the update from
the web UI is blocked until you SSH to the server and run the command the
Updates page shows, for example:

    sudo bash /opt/aerodrome/update/scripts/recover-sudoers.sh --install-dir /opt/aerodrome

It refreshes Aerodrome's system permissions only — no reinstall, no
restart, same service account, and never older rules over newer ones.
This is a one-time step per sudoers-version bump and is shown prominently
on the Updates page when required.

## After a successful apply

This folder is cleared on a successful apply — everything except
`UPDATE_README.md` and `.gitkeep`, which are refreshed from the new release
so the in-app viewer always shows the current staging-folder help. If the
apply stops before that point (for example the sudoers check above), the
staged release stays here untouched.
