import tempfile
import time
import unittest
from pathlib import Path

from magrid import config, fetch, selection
from magrid.config import Config


def node(nodeset, cores=64):
    return {"cores": cores, "nodeset": nodeset, "max_walltime": 0}


def cluster(site, n, *, queues=("admin", "default"), arch="x86_64", cpu="Intel Xeon Gold 6130", sockets=2, gpus=0,
            gpu_model="", exotic=False, nodeset=None, microarch="Skylake-SP", vendor="Intel"):
    name = nodeset or "x"
    return {"site": site, "queues": list(queues), "exotic": exotic, "cpu": cpu, "sockets": sockets, "cores": 64, "threads": 128,
            "ram_gib": 192, "vendor": vendor, "microarch": microarch, "arch": arch, "gpus": gpus, "gpu_model": gpu_model,
            "nodes": {f"{name}-{i}": node(name) for i in range(1, n + 1)}}


CLUSTERS = {
    "yeti": cluster("grenoble", 4, exotic=True, sockets=4, nodeset="yeti"),
    "dahu": cluster("grenoble", 31, nodeset="dahu"),
    "sasquatch": cluster("grenoble", 2, arch="aarch64", cpu="Ampere Neoverse-N1", sockets=1, exotic=True, nodeset="sasquatch",
                         microarch="Ares", vendor="Ampere/ARM"),
    "hydra": cluster("lyon", 4, arch="aarch64", cpu="NVIDIA Grace A02 CPU", sockets=1, gpus=1, gpu_model="GH200 480GB",
                     exotic=True, nodeset="hydra", microarch="Grace", vendor="NVIDIA/ARM"),
    "pyxis": cluster("lyon", 4, arch="aarch64", cpu="Cavium ThunderX2 99xx", exotic=True, nodeset="pyxis", vendor="Cavium"),
    "drac": cluster("grenoble", 8, arch="ppc64le", cpu="IBM POWER8NVL 1.0", gpus=4, gpu_model="Tesla P100", nodeset="drac"),
    "grdix": cluster("nancy", 16, queues=("abaca", "admin", "production"), cpu="AMD EPYC 9754", nodeset="grdix", vendor="AMD"),
    "roazhon15": cluster("rennes", 1, queues=("abaca", "admin", "production"), nodeset="roazhon15"),
    "secret": cluster("nancy", 2, queues=("abaca", "admin", "production"), nodeset="secret"),
}
ACCESS = {"grdix": "p3", "roazhon15": "besteffort"}
SITES = ["grenoble", "lyon", "nancy", "rennes"]


def resolve(*words, recent=()):
    return selection.resolve(words, CLUSTERS, ACCESS, sites=SITES, recent=recent)


class Access(unittest.TestCase):
    def test_access(self):
        self.assertEqual(selection.access_of(CLUSTERS["yeti"], ACCESS), "default")
        self.assertEqual(selection.access_of(CLUSTERS["grdix"], ACCESS), "p3")
        self.assertEqual(selection.access_of(CLUSTERS["roazhon15"], ACCESS), "besteffort")
        self.assertEqual(selection.access_of(CLUSTERS["secret"], ACCESS), "none")
        self.assertFalse(selection.usable(CLUSTERS["secret"], ACCESS))

    def test_order(self):
        self.assertEqual(selection.order(["roazhon15", "grdix", "pyxis", "yeti", "dahu", "hydra"], CLUSTERS, ACCESS),
                         ["dahu", "yeti", "hydra", "pyxis", "grdix", "roazhon15"])


class Board(unittest.TestCase):
    def test_auto_is_what_was_used(self):
        cfg = Config()
        self.assertEqual(selection.board(cfg, CLUSTERS, ACCESS, ["roazhon15", "yeti", "hydra"]), ["yeti", "hydra", "roazhon15"])

    def test_add_and_hide_on_top_of_auto(self):
        cfg = Config(add=["pyxis", "gone"], hide=["yeti"])
        self.assertEqual(selection.board(cfg, CLUSTERS, ACCESS, ["yeti", "hydra"]), ["hydra", "pyxis"])

    def test_an_explicit_list_keeps_its_order(self):
        cfg = Config(clusters=["yeti", "dahu"], add=["hydra"], hide=["dahu"])
        self.assertEqual(selection.board(cfg, CLUSTERS, ACCESS, ["pyxis"]), ["yeti", "hydra"])


class Words(unittest.TestCase):
    def test_clusters_and_nodes(self):
        p = resolve("yeti-2", "hydra")
        self.assertEqual((p.clusters, p.nodes), (["yeti", "hydra"], ["yeti-2"]))
        self.assertEqual(resolve("YETI,dahu").clusters, ["yeti", "dahu"])
        with self.assertRaises(selection.SelectionError):
            resolve("yeti-9")
        with self.assertRaises(selection.SelectionError):
            resolve("narnia")

    def test_groups(self):
        self.assertEqual(resolve("arm").clusters, ["sasquatch", "hydra", "pyxis"])
        self.assertEqual(resolve("power").clusters, ["drac"])
        self.assertEqual(resolve("gpu").clusters, ["drac", "hydra"])
        self.assertEqual(resolve("lyon").clusters, ["hydra", "pyxis"])
        self.assertEqual(resolve("nancy").clusters, ["grdix"])  # no access to secret
        self.assertEqual(resolve("abaca").clusters, ["grdix", "roazhon15"])
        self.assertEqual(resolve("besteffort").clusters, ["roazhon15"])
        self.assertEqual(resolve("exotic").clusters, ["sasquatch", "yeti", "hydra", "pyxis"])
        self.assertEqual(resolve("recent", recent=["pyxis", "yeti"]).clusters, ["yeti", "pyxis"])
        self.assertEqual(len(resolve("all").clusters), 8)
        self.assertEqual(resolve("lyon", "arm").clusters, ["hydra", "pyxis", "sasquatch"])  # words add up


class Search(unittest.TestCase):
    def find(self, text, **kw):
        return selection.search(text, CLUSTERS, ACCESS, **kw)

    def test_every_word_must_match(self):
        self.assertEqual(self.find("arm lyon"), ["hydra", "pyxis"])
        self.assertEqual(self.find("grace"), ["hydra"])
        self.assertEqual(self.find("gpu"), ["drac", "hydra"])
        self.assertEqual(self.find("epyc"), ["grdix"])
        self.assertEqual(self.find("p3"), ["grdix"])
        self.assertEqual(self.find("4-socket"), ["yeti"])
        self.assertEqual(self.find("power"), ["drac"])
        self.assertEqual(self.find("arm gpu"), ["hydra"])

    def test_ranking_and_scope(self):
        self.assertEqual(self.find("ya")[:1], [])  # nothing starts with ya, nothing contains it
        self.assertEqual(self.find("y")[0], "yeti")  # names starting with the word come first
        self.assertEqual(self.find("", first=["pyxis"])[0], "pyxis")
        self.assertNotIn("secret", self.find(""))
        self.assertIn("secret", self.find("", include_unusable=True))

    def test_ago(self):
        now = time.time()
        self.assertEqual(selection.ago(0, now), "")
        self.assertEqual(selection.ago(now - 3600, now), "today")
        self.assertEqual(selection.ago(now - 86400 * 1.5, now), "yesterday")
        self.assertEqual(selection.ago(now - 86400 * 9, now), "9 days ago")
        self.assertEqual(selection.ago(now - 86400 * 50, now), "7 weeks ago")


class History(unittest.TestCase):
    def test_parse(self):
        h = fetch.parse_history({"hist_grenoble": b"yeti 1790960737 5\ndahu 1790983210 9\n", "hist_lyon": b"", "other": b"x"})
        self.assertEqual(h.sites, ["grenoble", "lyon"])
        self.assertEqual(h.used, {"yeti": [1790960737, 5], "dahu": [1790983210, 9]})
        self.assertEqual(sorted(h.recent(1790983210 + 86400, 60)), ["dahu", "yeti"])
        self.assertEqual(h.recent(1790983210 + 61 * 86400, 60), [])

    def test_script_quotes(self):
        s = fetch.history_script(["grenoble", "nancy"], days=90)
        self.assertIn("job_user = '$me'", s)
        self.assertIn("interval '90 days'", s)
        self.assertIn('> "$d/hist_$s"', s)


class ConfigFile(unittest.TestCase):
    def test_lines_are_set_in_place(self):
        d = Path(tempfile.mkdtemp())
        p = d / "config.ini"
        config.set_lines({"add": "hydra pyxis", "hide": "yeti"}, p)
        c = config.load(p)
        self.assertEqual((c.clusters, c.add, c.hide), (["auto"], ["hydra", "pyxis"], ["yeti"]))
        self.assertIn("# The clusters on the board", p.read_text())
        p.write_text("[magrid]\nmargin = 10m\n# mine\nclusters = yeti dahu\n")
        config.set_lines({"add": "hydra", "hide": ""}, p)
        self.assertEqual(p.read_text(), "[magrid]\nmargin = 10m\n# mine\nclusters = yeti dahu\nadd = hydra\nhide =\n")
        c = config.load(p)
        self.assertEqual((c.clusters, c.add, c.hide, c.margin), (["yeti", "dahu"], ["hydra"], [], 600))


if __name__ == "__main__":
    unittest.main()
