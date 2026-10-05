# magrid

Book Grid'5000 machines from the terminal, without ever breaking the Usage Policy.

magrid shows what you can book on your usual clusters and for how long, who
holds the machines you want, and builds the `oarsub` commands itself: the
start, the walltime, the queue and the job types are computed from the rules,
so the weekly "usage has been violating the Usage Policy" e-mail has nothing
to say.

```
                              NOW              TONIGHT          TOMORROW         WEEKEND
                              →18:55           19:00→08:55      Tue 09:00→18:55  Fri→Mon 08:55

 yeti  4 nodes, 1 down        3 × 5h43         3 × 13h55        3 × 8h           3 × 61h55
 grenoble · 8 node-h/day      →18:55           →Tue 08:55       →Tue 17:00       opens Wed 19:00

 dahu  31 nodes, 5 down       20 × 2h43        1 × 13h55        1 × 9h55         26 × 61h55
 grenoble · 55.9/60 node-h    →15:55           →Tue 08:55       →Tue 18:55       opens Wed 19:00

 grdix  16 nodes              queue p3 · up to 48h · est. start 16:17  ●2
 nancy · abaca p3             2 queued ahead · no day or night rule; higher priorities go first

 roazhon15  1 node            best-effort · up to 48h · est. start Wed 12:37  ●1
 rennes · best-effort only    held by you until Wed 12:37 · killed as soon as a regular job needs it
```

Read it as: on yeti, 3 nodes are free now until 18:55, 3 for the whole night,
3 tomorrow but only for 8 hours (the daily quota of a 4-node cluster), and the
weekend cannot be reserved before Wednesday 19:00 (48 hours ahead, silver
level).

## Install

Python 3.9 or later, nothing else. It runs on your own machine and reaches the
frontends with the ssh alias you already use (`ssh grenoble.g5k`).

```
git clone git@gitlab.inria.fr:whisper/magrid.git ~/work/magrid
ln -s ~/work/magrid/bin/magrid ~/.local/bin/magrid
magrid doctor        # checks the connection, your level, and that the rules have not changed
```

## Use

```
magrid                          the interactive board
magrid board                    the same picture, printed
magrid book yeti:2 --tonight    two yeti nodes for the night
magrid book dahu:max --weekend  every dahu node that is free for the whole weekend
magrid book yeti dahu:4         as early as possible, as long as the rules allow
magrid book yeti --from 14:00 --for 3h
magrid book yeti:2 --weekend --wait    too early to reserve? stay running and book the second it opens
magrid book grdix:2             two queued jobs on an abaca cluster
magrid book yeti-2 -n           dry run on a given node: show the command, submit nothing
magrid who roazhon15            who holds it, with full name and group, until when, and who is next
magrid gantt dahu               the timeline of a cluster
magrid jobs                     your jobs on every site
magrid extend 1234567 --max     extend a job as far as the rules allow
magrid del 1234567
magrid check                    what the weekly check will say about you, on all sites
magrid rules                    the rules, in one page
```

In the interactive board: arrows to move, `+` and `-` to choose how many nodes
in a cell, `m` for as many as possible, Enter to review the exact commands and
book, `g` for the timeline of the cluster, `j` for your jobs, `?` for help.

Booking always shows the plan first, with every rule that was checked and the
`oarsub` command, and asks before submitting:

```
 yeti × 2   grenoble   likely yeti-2, yeti-1
   Mon 5 Oct 19:00 → Tue 6 Oct 08:55   13h55   starts in 5h50
     ✔ entirely inside the night (Mon 5 Oct 19:00 to Tue 6 Oct 09:00): no quota applies
     ✔ starts in 5h50 (silver: up to 48 h ahead)
     ✔ advance reservation 1 of 2 allowed at grenoble
   $ oarsub -q default -t deploy -t exotic -t destructive -r "2026-10-05 19:00:00" -l "{cluster='yeti'}/nodes=2,walltime=13:55:00"
```

If you ask for something the rules do not allow, magrid refuses, says which
rule, and gives the nearest thing that is allowed:

```
 ✘ yeti: cannot run until Tue 6 Oct 18:55: 9h55 of daytime on Tue 6 Oct takes 9.9 node-hours, but only 8
   are left to you that day on yeti (quota: 2 h x 4 nodes): at most 8h. The latest possible end is
   Tue 6 Oct 17:00 (add --clip to take that)
```

## The rules

`magrid rules` prints them; [docs/rules.md](docs/rules.md) says where each one
comes from and how it was verified. In short, for the default queue (all times
Europe/Paris):

- Daytime is 09:00 to 19:00 on working days. In daytime you may use 2 hours
  times all the cores of a cluster, per day: one node of an N-node cluster for
  2 x N hours.
- A job may not cross 09:00, unless it was submitted that same day (after
  midnight).
- A job may not cross 19:00, unless it was submitted at or after 17:00 that
  same day. There is no 17:00 boundary: 17:00 is only the time from which a
  new job may run on through the night.
- So a night is 19:00 to 09:00, a weekend is Friday 19:00 to Monday 09:00, and
  nothing can extend a night or weekend job past 09:00.
- At most 2 advance reservations waiting per site, at most 48 hours ahead
  (silver). Reservations starting within the hour are exempt.

The abaca queues have no day or night rule, no advance reservation, and a
maximum walltime that depends on your group's priority on each cluster. Jobs
there are queued, and higher priorities are scheduled first, so the start time
magrid shows is an estimate. Best-effort jobs have no rule either and are
killed when a regular job needs the node.

## Why it can be trusted

- The rules are not from memory. They were read from the three places that
  define them: the Usage Policy page, the `usagepolicycheck` script that sends
  the weekly e-mail, and the OAR admission rules that `oarsub` runs.
- `magrid/checker.py` is a port of that script. It is compared with the real
  one on 4,048 synthetic jobs placed around every boundary, tolerance, holiday
  and clock change: the figures and the verdicts are identical
  (`tests/test_checker_oracle.py`).
- Every window is checked twice before it is proposed: against the written
  rules read without any tolerance, and against the port. A randomized test
  builds platforms at random times and verifies that everything magrid offers
  passes both.
- The quota already used today comes from `usagepolicycheck` itself.
- At the second a job is submitted, it is checked again with the frontend's
  clock. After submission, magrid reads back what OAR recorded, checks that,
  and runs the official check on the new job.
- `magrid doctor` compares fingerprints of the three sources with the versions
  magrid was verified against, and says so if Grid'5000 changed its rules.

By default reservations end 5 minutes before a boundary (08:55, 18:55), and
none of the tolerances of the official check are used.

## What it runs on Grid'5000

Reading (every refresh, one ssh connection to one frontend): `curl` on the
Grid'5000 API (node status, your jobs), and `usagepolicycheck --json` for the
sites of your default-queue clusters. Once a week: the cluster descriptions,
your group and your abaca priorities.

Changing something, only after you confirm: `oarsub`, `oardel` and
`oarwalltime`, on the frontend of the site concerned, with the exact command
shown first. `-n` shows it without running it.

Nothing is ever run on a node, and nothing heavier than those commands on a
frontend.

## Configuration

`magrid config` creates and shows `~/.config/magrid/config.ini`: the clusters
on the board, the ssh alias pattern, the safety margin, the job types per
queue (`deploy destructive` by default for the default queue; `exotic` is
added where needed), one job per node or per cluster, your teammates' logins.

## Limits

- Start times in the abaca queues are estimates: later jobs of higher priority
  go first.
- Between the moment a window is shown and the moment it is submitted, someone
  else can take the nodes. OAR then refuses the reservation and magrid says
  so. A refused reservation never breaks a rule.
- Predicted slots of queued jobs in the default queue are treated as busy,
  which can hide a window that OAR would accept.
- Jobs are assumed to take whole nodes. Moldable jobs, containers, VLANs,
  subnets and disk reservations are not handled.

## Development

```
python3 -m unittest discover -s tests -t .     # about 150 tests, offline
```

- `tests/oracle/make_oracle.py` regenerates the corpus for the comparison with
  the real `usagepolicycheck` (one short run of the official tool on a
  frontend).
- `tests/live/probe.py` submits what magrid would submit, reads it back and
  deletes it at once. It creates real jobs for a few seconds: read its header
  before using it.
- `MAGRID_RECORD=file magrid board` records a state of the platform;
  `--replay file` plays it back without any connection (and without being
  able to submit). `tests/oracle/sanitize_recording.py` makes a fixture of it.

The name is for Magritte: in a dry run, *ceci n'est pas une réservation*.
