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

Aerodrome is built and maintained by one person, and I keep direct
merges to myself so the codebase stays internally consistent — in
formatting, in style, in which features earn their place and which add
complexity that isn't worth it. Reviewing and merging outside code
well takes real time, and for a project this size I'd rather spend
that time building.

That doesn't mean ideas aren't welcome. **They are — please file an
issue.** I want to know what people are running into, what's missing,
what could be better. I'll read every feature suggestion and bug
report, and the things that fit Aerodrome's direction will land in
future releases. The "I want it for myself" filter applies (see
[Suggesting features](#suggesting-features) below for what that means),
but a good idea well-explained genuinely moves the project.

The MIT license also keeps the door open the other way: fork freely,
change whatever, ship your own version. If you want Aerodrome to do
something it doesn't do today and the answer to "will the maintainer
build this?" turns out to be no, your fork is a real option — not a
polite deflection. Some of the most useful things people do with
open-source projects are fork them.

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
- **Demo mode** (v3.1.0) lives in `tools/synthetic_feeder/`. It's
  opt-in at install time (`./install.sh --demo` or the bootstrap's
  interactive prompt) and runs as a separate systemd service alongside
  `aerodrome.service`. The fleet seed is locked to `1903` so every
  demo install shows the same simulated aircraft — change the seed
  in `install.sh` and `tools/synthetic_feeder/seed_watchlist.py` if
  you want a different deterministic fleet for your fork. The data
  plane (collector, server, notifier) is intentionally ignorant of
  demo mode; only the UX layer (banner, notification prefix,
  Track-link guard) reads `demo.enabled`.

Good luck, and have fun with it.
