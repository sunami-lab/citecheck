"""Live check against the real APIs, using tests/fixtures/planted.bib.

Skipped unless CITECHECK_LIVE=1, because it needs network access and takes a minute or two:

    CITECHECK_LIVE=1 python3 -m unittest tests.test_live -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "scripts", "citecheck.py")
FIXTURE = os.path.join(HERE, "fixtures", "planted.bib")

sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import citecheck as cc  # noqa: E402


@unittest.skipUnless(os.environ.get("CITECHECK_LIVE") == "1", "set CITECHECK_LIVE=1 to query the live APIs")
class PlantedFixture(unittest.TestCase):
    def test_every_entry_gets_an_expected_verdict(self):
        with open(FIXTURE, encoding="utf-8") as fh:
            fields, _ = cc.parse_bibtex(fh.read())
        expect = {f["key"]: {v.strip() for v in f["x-expect"].split(",")} for f in fields}
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.json")
            proc = subprocess.run([sys.executable, SCRIPT, FIXTURE, "--json", out],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
            with open(out, encoding="utf-8") as fh:
                results = {r["key"]: r for r in json.load(fh)["results"]}
        wrong = {k: (results[k]["verdict"], sorted(v), results[k]["issues"])
                 for k, v in expect.items() if results[k]["verdict"] not in v}
        self.assertEqual(wrong, {}, proc.stdout)
        self.assertEqual(proc.returncode, 1)  # planted errors must fail the run


if __name__ == "__main__":
    unittest.main()
