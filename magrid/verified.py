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
