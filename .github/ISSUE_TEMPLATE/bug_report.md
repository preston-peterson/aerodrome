---
name: Bug report
about: Report something that's broken or behaving unexpectedly
title: ''
labels: bug
assignees: ''
---

<!--
Aerodrome is a one-person hobby project. Bug reports are welcome and
get a best-effort response. The more of the sections below you can
fill in, the faster the bug becomes actionable.

For security-shaped bugs (auth bypass, sudoers escape, etc.), please
use GitHub's private vulnerability reporting instead of a public
issue. See SECURITY.md.
-->

## What you tried to do

<!-- The user action, not the symptom. e.g. "Opened the Stats tab to see today's records." -->

## What happened instead

<!-- The observed symptom. Add a screenshot if it's UI-shaped. -->

## Aerodrome version

<!-- From `cat VERSION` in your install directory, or the version shown in the gear menu. -->

## Receiver type

<!-- readsb, dump1090-fa, tar1090, PiAware, etc. — and whether it's running on the same host as Aerodrome or a separate one. -->

## Relevant log output

<!--
The most useful 50–100 lines from:

    sudo journalctl -u aerodrome -n 200 --no-pager

If the bug is reproducible, set the log level to DEBUG in config.yaml,
reproduce it, and paste those lines here instead.
-->

```
<paste log output here>
```

## Performance report (only if the bug is performance-shaped)

<!--
Open the gear menu → Performance → "Copy diagnostic report" and paste
the full report here. Captures DB size, query timings with execution
plans, disk-I/O baseline, and auto-generated hints — most of what
would be asked for in a follow-up. Skip this section for non-performance
bugs.
-->
