"""Differential test: the Python port against the real usagepolicycheck.

tests/fixtures/oracle_expected.json.gz is what usagepolicycheck 4.4.6 answered
on a Grid'5000 frontend for the 4,048 synthetic jobs of oracle_jobs.json.gz
(see tests/oracle/make_oracle.py for how both were produced). Every number the
real script computed, per user, day and cluster, must come out identical.
"""

import datetime as dt
import gzip
import json
import unittest
from pathlib import Path

from magrid.checker import Job, aggregate, violations

FIXTURES = Path(__file__).parent / "fixtures"


def load():
    with gzip.open(FIXTURES / "oracle_jobs.json.gz", "rt") as f:
        corpus = json.load(f)
    with gzip.open(FIXTURES / "oracle_expected.json.gz", "rt") as f:
        expected = json.load(f)
    jobs = [
        Job(
            id=j["id"],
            user=j["user"],
            queue=j["queue"],
            submission=j["submission"],
            start=j["start"],
            stop=j["stop"],
            walltime=j["walltime"],
            resources={j["cluster"]: j["cores"]},
            extensions=[(req, b - a) for req, a, b in j["parts"]],
        )
        for j in corpus["jobs"]
    ]
    return corpus["cores"], jobs, expected


class Oracle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cores, cls.jobs, cls.expected = load()
        cls.usage = aggregate(cls.jobs)

    def test_corpus_is_what_was_recorded(self):
        self.assertEqual(len(self.jobs), 4048)

    def test_same_usage_per_user_day_cluster(self):
        want = {}
        for user, days in self.expected["total_jobs"].items():
            for day, sites in days.items():
                for clusters in sites.values():
                    for cluster, u in clusters.items():
                        if not u["jobs"]:
                            continue  # bookkeeping entries of the walltime statistic
                        want[(user, dt.date.fromisoformat(day), cluster)] = u
        self.assertEqual(set(self.usage), set(want))
        self.assertGreater(len(want), 5000)
        for key, u in want.items():
            mine = self.usage[key]
            self.assertEqual(mine.restricted_cputime, u["restricted_cputime"], key)
            self.assertEqual(mine.extra_cputime, u["extra_cputime"], key)
            self.assertEqual(mine.total_cputime, u["total_cputime"], key)
            self.assertEqual(sorted(mine.jobs), sorted(u["jobs"]), key)
            self.assertEqual(sorted(mine.cross_boundaries_jobs), sorted(u["cross_boundaries_jobs"]), key)
            if "rate" in u:
                self.assertAlmostEqual(mine.rate(self.cores[key[2]]), u["rate"], places=6, msg=key)

    def test_same_violations(self):
        want = set()
        for user, days in self.expected["flagged"].items():
            for day, clusters in days.items():
                for cluster in clusters:
                    want.add((user, dt.date.fromisoformat(day), cluster))
        got = {(v.user, v.day, v.cluster) for v in violations(self.usage, self.cores)}
        self.assertEqual(got, want)
        self.assertGreater(len(want), 1000)


if __name__ == "__main__":
    unittest.main()
