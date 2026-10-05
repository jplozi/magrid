# The Grid'5000 rules magrid enforces, and where they come from

Verified on 2026-10-05. `magrid doctor` tells you whether any of the three
sources below has changed since; the fingerprints are in `magrid/verified.py`.

## The three sources

| Source | What it is | How to read it |
|---|---|---|
| Usage Policy | The written rules. Wiki revision 109283, last edited 16 September 2026. | <https://www.grid5000.fr/w/Grid5000:UsagePolicy>, with <https://www.grid5000.fr/w/Production> and <https://www.grid5000.fr/w/Advanced_OAR> |
| `usagepolicycheck` 4.4.6 | The script run every week on all frontends; it sends the "usage has been violating the Usage Policy" e-mail. It is the definition of a violation in practice. | On any frontend: `/usr/lib/ruby/vendor_ruby/usagepolicychecker/usagepolicychecker.rb` |
| OAR admission rules | Perl code OAR runs on every `oarsub`. What "the system enforces". | On any frontend: `PGPASSWORD=read psql -h oardb.<site>.grid5000.fr -U oarreader oar2 -c 'select rule from admission_rules order by priority, id'` (the read-only account is public, documented on the Advanced OAR page) |

Where the written policy and the script differ, magrid satisfies both.

## Default queue

All times are Europe/Paris. "Daytime" is 09:00 to 19:00 on working days:
Monday to Friday, except French public holidays (1 January, Easter Monday,
1 May, 8 May, Ascension Thursday, Pentecost Monday, 14 July, 15 August,
1 November, 11 November, 25 December; this is the script's own list).

**R1. Daily quota.** In daytime, a user may use at most 2 hours on all the
cores of a cluster, per cluster and per day. The script counts cores times
seconds of daytime for each (user, day, cluster) and compares with
`2 h x cores of the cluster`, where the cores are those of all the nodes that
are not retired, dead ones included. One node of an N-node cluster may
therefore be held 2 x N hours: 8 h on yeti or pyxis (4 nodes), 4 h on
sasquatch (2), 2 h on chicoree (1), 14 h on larochette (7), 60 h on dahu.

**R2. The 09:00 boundary.** A job must not cross 09:00 on a working day,
unless it was submitted that same day (from midnight on). The part after 09:00
counts in R1.

**R3. The 19:00 boundary.** A job must not cross 19:00 on a working day,
unless it was submitted at or after 17:00 that same day. The part before 19:00
then does not count in R1. There is no boundary at 17:00.

**R4. The free hour.** A job of at most 1 hour that starts less than 10
minutes after its submission does not count in R1. Neither does an extension
of at most 1 hour requested during the last 10 minutes of the job; it can be
repeated.

**R5. Extensions** (`oarwalltime`). OAR accepts any extension that fits before
the next reservation; the policy is checked afterwards. R3 applies with the
time of the request: an extension asked at or after 17:00 may carry a job
through the night. R2 does not: it looks at when the job was submitted, so no
extension can take a night or weekend job past 09:00.

**R6. Advance reservations** (`oarsub -r`). Depending on the level of the
user's group: gold, 2 waiting per site, any time ahead; silver, 2 per site, at
most 48 hours ahead; bronze, 1 per site, 24 hours ahead. Only waiting
reservations that start more than one hour from now are counted, and a new
reservation that starts within the hour is exempt from both limits. Enforced
by admission rule "Limit the number of advance reservations per user".

Consequences:

- A night is 19:00 to 09:00, 14 hours. A weekend is Friday 19:00 to Monday
  09:00: 62 hours, 61 or 63 when the clocks change, more when a holiday is
  attached.
- A job submitted at or after 17:00 can start at once and run until the next
  09:00 of a working day. On Friday that is Monday.
- A job submitted after midnight can start at once and continue into the
  day, as far as the quota allows and not beyond 19:00.
- The time of submission matters, not only the time of start. A reservation
  made in the morning for 17:30 to 08:30 is a violation; the same one made at
  17:00 is not.

## What the script does that the text does not say

Read from the code and reproduced in `magrid/checker.py`:

- A job counts as crossing a boundary only if it extends more than 5 minutes
  on both sides of it.
- The daily quota has a 5% tolerance.
- A job whose only daytime is the up to 6 minutes by which it overlaps a
  boundary is ignored.
- A job that crosses 19:00 gets the R3 exemption only if it ends more than
  5 minutes after 19:00. One that ends at 19:03 has its whole daytime part
  counted in the quota. This is the one case where the script is stricter
  than the text; magrid's double check exists for cases like it.
- What counts is what ran: a job deleted before a boundary did not cross it,
  whatever its walltime was. `magrid` uses this to tell you until when a
  doomed job can still be ended cleanly.
- Only jobs of the `default` queue are examined, and not those of type
  `besteffort`. Clusters that are not in the default queue are skipped.

magrid plans with none of these tolerances.

## Abaca (production) queues

From the Production page and the admission rule "Production queue rules":

- No day or night rule; the script does not look at these queues.
- Each group has a priority per nodeset: p1 (jobs up to 168 h), p2 (96 h),
  p3 (48 h), p4 (24 h), best-effort only, or no access. `oarsub -q abaca -p
  <cluster>` is routed to the group's priority queue for that cluster. Each
  node can further limit the walltime (`max_walltime` property: 24 h on
  grosminet, for example).
- No advance reservation, except one that starts within the hour, or with
  priority p1.
- Queues are scheduled by priority (p1 first), with fair sharing inside a
  queue. A waiting job can therefore be overtaken by later jobs of higher
  priority: the start time is an estimate.
- No walltime change, except with p1.
- The priorities are in the API: `https://api.grid5000.fr/stable/accesses`.

## Best-effort

Admission rule "Tie besteffort type and queue": not open to bronze groups;
never an advance reservation. A best-effort job runs only on idle resources
and is killed as soon as a regular job needs them. No day or night rule. At
most 168 hours, like any job outside abaca.

## Also enforced by OAR

- `oarsub -I` without `-r` is limited to 12 hours ("Limit the walltime for
  interactive jobs"). This is why "now" is booked as a reservation that starts
  in a minute.
- A job is at most 168 hours.
- With no `-q`, the queue depends on the user's group: `abaca` for groups
  attached to a production site, `default` otherwise. magrid always passes it.
- `-t exotic` is required for exotic clusters (yeti, pyxis, ...) and
  `destructive` requires `deploy`.
- For an advance reservation the end is fixed: if the nodes are late (booting,
  or being cleaned after the previous job), OAR shortens the walltime instead
  of moving the end. This is what makes a reservation safer than a queued job
  in the default queue, whose end moves with its start.

## How the port of the script was checked

`tests/oracle/make_oracle.py generate` writes 4,048 synthetic jobs, placed
around 09:00, 17:00 and 19:00, the tolerances, weekends, holidays and clock
changes, with and without extensions, in the input format the script accepts
through `INPUT_FILE_TIMES` and `INPUT_FILE_CORES`. The real script was run once
on them on a frontend; its answer is `tests/fixtures/oracle_expected.json.gz`.
`tests/test_checker_oracle.py` requires the port to give the same core-seconds
of daytime for every user, day and cluster, the same list of boundary-crossing
jobs, and the same violations.

## When a fingerprint changes

1. Usage Policy page: read the diff in the wiki history.
2. `usagepolicycheck`: diff the new Ruby file against the constants and the
   three functions ported in `checker.py` (`analyze_job`, `aggregate_by_user`,
   `qualify_users`), then rerun the oracle (steps in `make_oracle.py`).
3. Admission rules: dump them as above and diff with the previous dump.
4. Update `policy.py` if a rule moved, then `verified.py`.
