# magrid

magrid is a command-line tool for reserving compute nodes on
[Grid'5000](https://www.grid5000.fr) within the constraints of the platform's
Usage Policy. It reads the state of the platform, computes the reservation
windows that the policy allows on a set of clusters, and generates the
corresponding `oarsub` commands. Reservations are checked against the rules
before submission, at the time of submission, and once more after OAR has
recorded them.

![The interactive board](https://raw.githubusercontent.com/jplozi/magrid/main/docs/board.png)

## Motivation

The Usage Policy of Grid'5000 constrains reservations in the default queue in
several ways: a daily quota of core-hours per cluster during working hours, two
boundaries (09:00 and 19:00 on working days) that a job may cross only under
conditions on its submission time, and limits on the number and horizon of
advance reservations. Production (abaca) clusters follow a different regime,
with per-cluster priorities and maximum walltimes. Compliance is verified
weekly by a script that reports violations to the user and to the managers of
their group.

Computing start times and walltimes by hand against these rules is error-prone
(a walltime of 58:55 instead of 57:55 is enough to cross Monday 09:00), and
the information needed to choose a window, such as the availability of nodes,
the quota already consumed and the reservations already waiting, is spread
over several web pages and commands. magrid gathers it and applies the rules.

## Functions

- **Board.** For each cluster and each coming time slot (now, tonight,
  tomorrow, the coming weekend), the number of nodes free for the whole slot
  and the longest duration the rules allow. Production clusters show the
  priority queue, the maximum walltime and an estimated start.
- **Booking.** Given clusters and node counts, magrid selects the window,
  computes the start and the walltime, lists the rules it checked, prints the
  `oarsub` command and asks for confirmation. After submission it reads back
  what OAR recorded, checks it again, and runs the official policy check on
  the new job. `--wait` defers a reservation that is still beyond the 48-hour
  horizon and submits it when it becomes allowed.
- **Occupancy.** Who holds each node, with full name and group, until when,
  and who comes next; a per-node timeline of each cluster.
- **Own jobs.** The user's jobs on all sites, with a prediction of what the
  weekly check will report about each of them and, for a job that would be
  reported, the time before which ending it keeps it compliant. Extensions
  (`oarwalltime`) are checked against the same rules.
- **Cluster catalogue.** Every cluster of every site with its architecture,
  processor, memory, GPUs, the user's access level and the date of last use.
  The default board is the set of clusters the user ran jobs on in the last
  two months.

## Rules

The rules were taken from three sources: the Usage Policy page of the
Grid'5000 wiki, the `usagepolicycheck` script installed on the frontends
(version 4.4.6), which produces the weekly report, and the OAR admission rules
of each site, which `oarsub` applies. [docs/rules.md](docs/rules.md) gives
each rule with its source; `magrid rules` prints a summary. For the default
queue, in Europe/Paris time:

- Working days are Monday to Friday except French public holidays. Daytime is
  09:00 to 19:00 on working days.
- During daytime, a user may use at most 2 hours multiplied by the number of
  cores of a cluster, per cluster and per day. One node of an N-node cluster
  may therefore be held for 2N hours of daytime.
- A job may not cross 09:00 unless it was submitted on that same day; the part
  after 09:00 counts towards the quota.
- A job may not cross 19:00 unless it was submitted at or after 17:00 on that
  same day; the part before 19:00 is then exempt from the quota.
- Jobs of at most one hour starting within ten minutes of submission are
  exempt from the quota, as are extensions of at most one hour requested in the
  last ten minutes of a job.
- A user may have at most two advance reservations waiting per site, starting
  at most 48 hours ahead (silver level; the limits differ for gold and
  bronze). Reservations starting within the hour do not count.

Production clusters have no day or night rule and no advance reservations;
the group's priority on each cluster (p1 to p4) sets a maximum walltime, and
jobs of higher priority are scheduled first. Best-effort jobs run on idle
nodes and are killed when a regular job needs them.

The `usagepolicycheck` script applies tolerances that the written policy does
not mention (5 minutes around each boundary, 5% on the quota) and is stricter
in one case (a job crossing 19:00 but ending within 5 minutes of it does not
get the exemption). magrid plans without any tolerance and, by default, ends
reservations 5 minutes before a boundary.

## Method

**Data.** One ssh connection to a frontend provides, through the Grid'5000
REST API, the status of every node of the sites concerned, with the running
and scheduled jobs on each, and the user's jobs on all sites. The daytime
already consumed on each cluster is taken from `usagepolicycheck` itself.
Cluster descriptions, the user's group and privilege level, and the group's
priorities on production clusters are read once a week; the list of clusters
recently used, once a day from the OAR databases (public read-only account).
Each piece is used as soon as it arrives, so a slow site delays only its own
clusters.

**Windows.** For a cluster and a slot, the free intervals of each node are
computed from the recorded jobs (predicted slots of queued jobs count as
busy). The latest legal end of a window starting at a given time depends on
the submission time, the period (day, night, weekend) and the remaining
quota for the number of nodes requested; the window retained is the longest
one during which the requested number of nodes is simultaneously free. Every
candidate window is checked twice: against the written rules read without
tolerance (`magrid/policy.py`), and against a port of `usagepolicycheck`
(`magrid/checker.py`). A window is offered only if both accept it.

**Submission.** At the moment of submission the window is checked again with
the frontend's clock, with a few seconds of margin on the conditions that
depend on the submission time. Reservations use `oarsub -r` even when they
start immediately, because the end of a reservation is fixed by OAR (a late
start shortens the walltime), whereas a queued job's end moves with its start.
After submission, the start and walltime recorded by OAR are read back and
checked, and `usagepolicycheck` is run on the sites concerned.

**Drift.** `magrid doctor` compares fingerprints of the three rule sources
(wiki revision, script checksum, admission rules of each site) with those
magrid was verified against, and reports any change.

## Validation

- The port of `usagepolicycheck` was compared with the original on 4,048
  synthetic jobs placed around every boundary, tolerance, holiday and clock
  change, with and without extensions. The core-seconds counted per user, day
  and cluster, the boundary-crossing jobs and the resulting violations are
  identical (`tests/test_checker_oracle.py`).
- A randomized test builds platforms at random times and checks that every
  window offered passes both checks and overlaps no existing reservation on
  the nodes it names.
- The command line is tested end to end against a fake frontend built from a
  recorded state of the platform: the exact `oarsub`, `oardel` and
  `oarwalltime` commands are asserted, as is the absence of any command when
  a rule or the user refuses.
- The argument sets magrid generates were submitted to the live platform,
  read back and deleted (`tests/live/probe.py`).

The test suite (`python3 -m unittest discover -s tests -t .`, about 190
tests) runs offline.

## Usage

magrid needs Python 3.9 or later and nothing else (Linux or macOS, with ssh
and a terminal). It runs on the user's machine, or on a frontend.

```
pipx install git+https://github.com/jplozi/magrid.git
magrid doctor
```

pipx puts the `magrid` command in `~/.local/bin`; `pip install` of the same
URL works in a virtual environment. From a checkout, `pipx install -e .`
installs it in place, and `bin/magrid` runs it without installing anything.

### Reaching the frontends

magrid runs `ssh <alias>` for each site, with the alias pattern given in the
configuration (`{site}.g5k` by default). The configuration Grid'5000
recommends, in `~/.ssh/config`:

```
Host g5k
  User <login>
  Hostname access.grid5000.fr
  ForwardAgent no
  ControlMaster auto
  ControlPath ~/.ssh/cm-%C
  ControlPersist 4h

Host *.g5k
  User <login>
  ProxyCommand ssh g5k -W "$(basename %h .g5k):%p"
  ForwardAgent no
  ControlMaster auto
  ControlPath ~/.ssh/cm-%C
  ControlPersist 4h
```

The gateway only forwards the connection (`-W`); from 29 October 2026 it
runs no command at all, so an older `ProxyCommand ssh g5k nc ...` stops
working on that date. The `ControlMaster` lines keep one connection per host
open and reuse it, which the interactive board (one reading per minute)
needs: magrid goes through that connection when the configuration defines
one, and otherwise keeps one of its own for five minutes. `magrid doctor`
reports both points.

```
magrid                          interactive board
magrid board [what]             the board, printed; what: clusters, sites, groups (arm, gpu, ...)
magrid book yeti:2 --tonight    two yeti nodes for the night
magrid book dahu:max --weekend  every dahu node free for the whole weekend
magrid book yeti dahu:4         as early as possible, as long as the rules allow
magrid book yeti --from 14:00 --for 3h
magrid book yeti:2 --weekend --wait    submit when the 48-hour horizon allows it
magrid book grdix:2             two queued jobs on a production cluster
magrid book yeti-2 -n           dry run: the command only
magrid who roazhon15            holders, groups, until when, who is next
magrid gantt dahu               timeline of a cluster
magrid jobs                     own jobs on every site
magrid extend <job> --max       extend a running job as far as the rules allow
magrid del <job>
magrid check                    what the weekly check will report
magrid clusters [words]         the catalogue, filtered (arm, lyon, gpu, epyc, p3, ...)
magrid add, magrid hide         edit the board
magrid rules, magrid doctor, magrid config
```

In the interactive board: arrows move, `+` and `-` set the number of nodes
in the focused cell, `m` takes all of them, Enter reviews the commands and
books, `a` adds a cluster (searchable), `x` hides one, `g` shows the
timeline, `j` the user's jobs, `?` the help.

A booking plan, as shown before confirmation:

```
 yeti × 2   grenoble   likely yeti-2, yeti-1
   Mon 5 Oct 19:00 → Tue 6 Oct 08:55   13h55   starts in 5h50
     ✔ entirely inside the night (Mon 5 Oct 19:00 to Tue 6 Oct 09:00): no quota applies
     ✔ starts in 5h50 (silver: up to 48 h ahead)
     ✔ advance reservation 1 of 2 allowed at grenoble
   $ oarsub -q default -t deploy -t exotic -t destructive -r "2026-10-05 19:00:00" -l "{cluster='yeti'}/nodes=2,walltime=13:55:00"
```

A request outside the rules is refused with the rule and the nearest
allowed alternative:

```
 ✘ yeti: cannot run until Tue 6 Oct 18:55: 9h55 of daytime on Tue 6 Oct takes 9.9 node-hours, but only 8
   are left to you that day on yeti (quota: 2 h x 4 nodes): at most 8h. The latest possible end is
   Tue 6 Oct 17:00 (add --clip to take that)
```

### Configuration

`magrid config` creates and prints `~/.config/magrid/config.ini`: the board
(`auto`, or a list of clusters, plus clusters added and hidden), the ssh alias
pattern, the margin before boundaries, the job types per queue (`deploy
destructive` for the default queue; `exotic` is added where required), one
job per node or per cluster, teammates' logins, and the terminal theme.

### Operations performed on Grid'5000

Read-only, through one ssh connection to one frontend: `curl` on the
Grid'5000 API (node status, the user's jobs), `usagepolicycheck --json` for
the sites of the default-queue clusters shown (every ten minutes in the
interactive board, and after each action), one query per site on the OAR
databases once a day, and the reference API once a week. Nothing is run on a
node, and nothing on the access machine, which only forwards the connections.

Modifying, only after confirmation: `oarsub`, `oardel` and `oarwalltime` on
the frontend of the site concerned, with the exact command printed first.
`-n` prints the command without running it.

## Limitations

- Start times on production clusters are estimates from the current queue;
  jobs of higher priority submitted later are scheduled first.
- Nodes shown free may be taken between display and submission; OAR then
  refuses the reservation, which magrid reports.
- Predicted slots of queued jobs in the default queue are treated as busy,
  which may hide windows that OAR would accept.
- Jobs are assumed to occupy whole nodes. Moldable jobs, containers, VLANs,
  subnets and disk reservations are not handled.
- The rules encoded are those in force on 2026-10-05; `magrid doctor`
  reports a change of the sources but does not interpret it.

## Development

- `tests/oracle/make_oracle.py` regenerates the corpus for the comparison
  with `usagepolicycheck` (one run of the script on a frontend, with input
  files).
- `MAGRID_RECORD=file magrid board` records a state of the platform;
  `--replay file` replays it without a connection (submissions are disabled).
  `tests/oracle/sanitize_recording.py` turns a recording into a fixture with
  logins, groups and job numbers replaced.
- `tools/screenshot.py` regenerates `docs/board.png` from a synthetic state
  (requires matplotlib).
- `pip install -e .` in a virtual environment installs a checkout in place.
  The test suite runs on GitHub Actions for Python 3.9 to 3.14 at each push
  (`.github/workflows/tests.yml`), followed by a build of the package and a
  run of the installed command on a recorded state.
- The repository is developed on gitlab.inria.fr and mirrored to GitHub with
  `tools/mirror-github.py`.

The name refers to Magritte. A dry run prints *Ceci n'est pas une
réservation*.
