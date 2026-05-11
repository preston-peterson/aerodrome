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
   `~/aerodrome/update/` directly. Either of these layouts works:

   - Flat: `update/VERSION`, `update/server.py`, `update/templates/…`, etc.
   - Nested: `update/aerodrome/VERSION`, `update/aerodrome/server.py`, etc.

   Both shapes are detected automatically. Then open the web UI's Updates
   page and click **Check again** to see the staged version, followed by
   **Apply & restart**.

3. **Copy files by hand.** Any tool that puts the release's files at one of
   the two layouts above will work.

## What happens when you apply

1. The current install is backed up to `~/aerodrome/.backups/<timestamp>/`
2. Files are copied from `update/` over the live install, preserving
   user-managed paths (config.yaml, database, logs, venv)
3. Python dependencies are reinstalled from `requirements.txt` (best-effort)
4. The `aerodrome` systemd service is restarted

If the apply step fails partway through, the `.backups/<timestamp>/` folder
has the previous install intact for manual recovery.

## Sudoers version check

Aerodrome uses a `/etc/sudoers.d/aerodrome` rule for a small set of
privileged operations (installing/uninstalling the ntfy service, purging
data). When a release bumps the sudoers version, applying the update from
the web UI is blocked until you SSH to the server and run `install.sh` to
refresh the sudoers file. This is a one-time step per sudoers-version bump
and is shown prominently on the Updates page when required.

## Preserving staged work

This folder (minus `UPDATE_README.md` and `.gitkeep`) is NOT cleared on
successful apply — anything you staged here survives, so mid-flow updates
aren't lost. The two docs files above are refreshed from each new release
so the in-app viewer always shows the current staging-folder help.
