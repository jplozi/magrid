import unittest

from magrid import oar
from magrid.remote import Result
from tests.helpers import MON, T, cluster


class FakeRemote:
    def __init__(self, *results):
        self.results = list(results)
        self.sent = []

    def command(self, site, argv, timeout=0):
        self.sent.append((site, argv))
        return self.results.pop(0)


def request(**kw):
    base = dict(site="grenoble", cluster="yeti", nodes=1, queue="default", types=["deploy"], walltime=3600, start=T(MON, 19))
    base.update(kw)
    return oar.Request(**base)


class Commands(unittest.TestCase):
    def test_types(self):
        self.assertEqual(oar.job_types(cluster("yeti", 1, exotic=True), ["deploy", "destructive"]), ["deploy", "exotic", "destructive"])
        self.assertEqual(oar.job_types(cluster("dahu", 1), ["deploy", "exotic"]), ["deploy"])
        self.assertEqual(oar.job_types(cluster("yeti", 1, exotic=True), []), ["exotic"])

    def test_queued_on_named_hosts(self):
        r = request(queue="abaca", start=None, hosts=["grdix-3"], site="nancy", cluster="grdix", command="sleep infinity")
        self.assertEqual(r.argv()[-5:], ["-p", "host in ('grdix-3.nancy.grid5000.fr')", "-l", "host=1,walltime=1:00:00", "sleep infinity"])

    def test_name_and_hold(self):
        r = request(name="mpk", hold=True)
        self.assertEqual(r.argv()[:6], ["oarsub", "-q", "default", "--hold", "-t", "deploy"])
        self.assertIn("-n", r.argv())


class Submission(unittest.TestCase):
    def test_reservation_accepted(self):
        out = "# Include exotic resources\nOAR_JOB_ID=3134479\n# Advance reservation request: waiting for validation...\n# Reservation valid --> OK\n"
        o = oar.submit(FakeRemote(Result(0, out, "")), request())
        self.assertEqual((o.ok, o.job_id), (True, 3134479))

    def test_reservation_that_cannot_be_scheduled(self):
        out = "OAR_JOB_ID=42\n# Advance reservation request: waiting for validation...\n# Reservation valid --> KO\n"
        o = oar.submit(FakeRemote(Result(0, out, "")), request())
        self.assertEqual((o.ok, o.job_id), (False, 42))

    def test_refused_by_an_admission_rule(self):
        o = oar.submit(FakeRemote(Result(2, "", "# You cannot have more than 2 waiting reservations\n")), request())
        self.assertEqual((o.ok, o.job_id), (False, None))
        self.assertIn("more than 2", o.error)

    def test_read_back(self):
        doc = '{"7":{"state":"Waiting","queue":"default","submission_time":100,"start_time":0,"scheduled_start":200,' \
              '"walltime":"13:55:0","assigned_network_address":["yeti-2.grenoble.grid5000.fr"],"reservation":"Scheduled"}}'
        o = oar.Outcome(request(), ok=True, job_id=7)
        oar.read_back(FakeRemote(Result(0, doc, "")), o)
        self.assertEqual((o.state, o.start, o.walltime, o.hosts, o.submitted), ("Waiting", 200, 13 * 3600 + 55 * 60, ["yeti-2"], 100))

    def test_delete_and_extend(self):
        fr = FakeRemote(Result(0, "Deleting the job = 7 ...REGISTERED.\n", ""), Result(0, "Accepted: walltime change request updated\n", ""))
        self.assertTrue(oar.delete(fr, "grenoble", 7)[0])
        self.assertTrue(oar.extend(fr, "grenoble", 7, 5400)[0])
        self.assertEqual(fr.sent, [("grenoble", ["oardel", "7"]), ("grenoble", ["oarwalltime", "7", "+1:30:00"])])


if __name__ == "__main__":
    unittest.main()
