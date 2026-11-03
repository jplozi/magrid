"""What the rules in this version of magrid were checked against.

"magrid doctor" compares these fingerprints with the live platform. A
difference does not mean magrid is wrong, it means the Grid'5000 side changed
since the rules were last read, and that policy.py and checker.py should be
read again against the new version (docs/rules.md says how).
"""

VERIFIED_ON = "2026-10-05"

# The script that produces the weekly "usage has been violating the Usage Policy" e-mail.
CHECKER_PACKAGE = "usagepolicycheck 4.4.6"
CHECKER_PATH = "/usr/lib/ruby/vendor_ruby/usagepolicychecker/usagepolicychecker.rb"
CHECKER_SHA256 = "b6f48877db301e8c3fda996a7d346759cb33a8ca606e8cc13d6fda2b3cdd061c"

# https://www.grid5000.fr/w/Grid5000:UsagePolicy
WIKI_URL = "https://www.grid5000.fr/w/Grid5000:UsagePolicy"
WIKI_REVISION = 109283  # last edited on 16 September 2026

# md5 over the enabled OAR admission rules of each site, in order. Four
# variants exist, all read: they differ in the cap on waiting jobs, in the
# presence of the abaca rule, and in the encoding of one comment.
ADMISSION_RULES = {
    "ce33e22a9e159bc88eb4ee72013141b3": "sites with abaca clusters (grenoble, lille, rennes, sophia)",
    "12ca88439ab9baf317ecbe33de99a926": "nancy (150 waiting jobs instead of 100)",
    "4f219b6edd8eb1d9de9dc3b10a8e25c0": "sites without abaca clusters (luxembourg, lyon, nantes, strasbourg, toulouse)",
    "6fc72f7ff696f35e04fc5934e478ad78": "louvain (same as the previous one, one comment encoded differently)",
}
ADMISSION_RULES_SQL = (
    "select md5(string_agg(rule, chr(10) order by priority, id)) from admission_rules where enabled = 'YES'"
)

# OAR leaves SCHEDULER_JOB_SECURITY_TIME between two jobs on a resource, and its
# timeline counts whole seconds inclusively, so the next job may start that many
# seconds plus one after the previous one ends ("This reservation could run at
# 2026-11-03 12:50:01" at luxembourg for a job ending at 12:48:00). Measured on
# 2026-11-03 from each site's scheduler predictions, as the smallest distance
# between the end of a running job and the start of the waiting job that follows
# it on the same resources: 60 s at grenoble and nancy, 120 s at lille,
# luxembourg, lyon, nantes, rennes and sophia. louvain, strasbourg and toulouse
# had no waiting job to measure, so the common value is assumed there.
OAR_SECURITY_TIME = {"grenoble": 60, "nancy": 60, "lille": 120, "luxembourg": 120, "lyon": 120, "nantes": 120,
                     "rennes": 120, "sophia": 120}  # fmt: skip
OAR_SECURITY_DEFAULT = 120
OAR_GAP_SQL = (
    "select min(p.start_time - (r.start_time + m.moldable_walltime)) from gantt_jobs_predictions p"
    " join gantt_jobs_resources gr on gr.moldable_job_id = p.moldable_job_id"
    " join assigned_resources ar on ar.resource_id = gr.resource_id"
    " join jobs r on r.assigned_moldable_job = ar.moldable_job_id and r.state = 'Running'"
    " join moldable_job_descriptions m on m.moldable_id = r.assigned_moldable_job"
    " where p.start_time - (r.start_time + m.moldable_walltime) between 30 and 600"
)


def oar_security_time(site: str) -> int:
    return OAR_SECURITY_TIME.get(site, OAR_SECURITY_DEFAULT)


def oar_gap(site: str) -> int:
    """Seconds to keep on each side of a job at a site: the security time, plus the second OAR counts."""
    return oar_security_time(site) + 1
