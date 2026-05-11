# Contributing to Aerodrome

Aerodrome is a one-person hobby project. The short version of how to
work with the project from outside:

- **Bug reports:** welcome, best-effort response.
- **Feature suggestions:** welcome in issues, but the bar for "I'll
  build this" is "I want it for myself."
- **Pull requests:** disabled at the repository level.
- **Forks:** encouraged. The MIT license invites you to take Aerodrome
  somewhere different if it isn't going where you want.

The longer version is below.

## Why pull requests are disabled

Aerodrome is built the way one person wants it built. The codebase has
opinions — about formatting, about what features earn their place, about
when to add complexity and when to refuse it — and merging outside code
would either dilute those opinions or force me to relitigate them in
review. Neither is a good use of anyone's time.

The MIT license keeps the door open: fork freely, change whatever, ship
your own version. That's a real option, not a polite deflection. If you
want Aerodrome to do something it doesn't do today, and the answer to
"will the maintainer build this?" is no, your fork is the right path —
not a PR I'll close without merging.

## Reporting bugs

If something is broken, please open an issue. The more of the following
you can include, the faster I can act on it:

- **What you tried to do** — the user action, not the symptom.
- **What happened instead** — the observed symptom, ideally with a
  screenshot if it's UI-shaped.
- **Aerodrome version** — `cat VERSION` from your install directory, or
  the version shown in the gear menu.
- **Receiver type** — readsb, dump1090-fa, tar1090, PiAware, etc., and
  whether it's running on the same host as Aerodrome or a separate one.
- **Relevant log output** — the most useful 50–100 lines from
  `sudo journalctl -u aerodrome -n 200 --no-pager`. If the bug is
  reproducible, set the log level to `DEBUG` in `config.yaml`,
  reproduce, and paste those lines instead.
- **Performance report** (for slow-response or scale issues) — open
  the gear menu, choose Performance, click "Copy diagnostic report",
  and paste it into the issue. The report captures DB size, query
  timings with execution plans, disk-I/O baseline, and auto-generated
  hints — most of what I'd ask for in a follow-up.

If the bug is security-shaped (an auth bypass, a way to escape the
scoped sudoers rule, etc.), open the issue without exploit details and
ask me to follow up by another channel. Aerodrome assumes a trusted LAN
threat model — see the Remote Access section of `docs/INSTALL.md` — but
anything that breaks even that assumption is worth reporting carefully.

## Suggesting features

Feature requests are fine, with a few realities to set expectations:

- **The maintenance bar is high.** Aerodrome already has plenty of
  surface area. I'm conservative about adding more, especially for use
  cases I won't personally exercise. "I want it for myself" is a real
  filter, not a polite framing.
- **Roadmap is private.** I don't keep a public roadmap and won't
  commit to delivery dates. If a request lands in my own backlog, you
  may see it in a future release. If it doesn't, it doesn't.
- **A "no" isn't an indictment of the idea.** It usually means the
  idea is good but not aligned with what I want this codebase to be.
  That's exactly the case where forking makes sense.

## If you're forking

Go ahead. The MIT license has you covered. Two practical pointers:

- **`bump-version.sh`** drives the release process — VERSION,
  CHANGELOG, and version strings across source files. Read it before
  changing version strings by hand.
- **The Documentation page** (`/documentation` in the running app)
  surfaces these markdown files in-app via the `DOC_FILES` registry
  in `server.py` and the `DOCS` array in `templates/docs.html`. If
  you add or rename docs, update both registries to keep the in-app
  viewer correct.

Good luck, and have fun with it.
