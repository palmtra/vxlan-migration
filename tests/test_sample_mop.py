#!/usr/bin/env python3
"""The sample MOP for VLAN 3000 keeps to the MOP input contract (docs/MOP_INPUT.md)."""

import hashlib
import json
import os
import re
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.join(_HERE, "..")
_SAMPLE = os.path.join(_HERE, "mock_cli", "omaha", "sample_mop")
_INPUT = os.path.join(_SAMPLE, "ACME_APP_mop_input_20261102_210919_379.json")
_MOP = os.path.join(_SAMPLE, "MOP_VLAN3000_ACME_APP.md")

_CITED_ID_RE = re.compile(r"\[((?:[BDRTQI]|S|EP|A|SW)-[A-Za-z0-9._-]+)\]")
_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)


class SampleMopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_INPUT, "rb") as handle:
            cls.raw = handle.read()
        cls.doc = json.loads(cls.raw)
        with open(_MOP, encoding="utf-8") as handle:
            cls.mop = handle.read()
        cls.ids = (
            {f["id"] for f in cls.doc["findings"]}
            | {s["id"] for s in cls.doc["steps"]}
            | {e["id"] for e in cls.doc["endpoints"]["rows"]}
            | {a["id"] for a in cls.doc["acceptance"]}
            | {s["id"] for s in cls.doc["switches"]}
        )

    def test_input_is_valid(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        with open(os.path.join(_REPO, "schemas", "mop_input.schema.json"), encoding="utf-8") as handle:
            schema = json.load(handle)
        jsonschema.validate(self.doc, schema)

    def test_mop_names_the_exact_input(self):
        self.assertIn(hashlib.sha256(self.raw).hexdigest(), self.mop)
        self.assertIn(self.doc["files"]["mop_input"], self.mop)
        self.assertIn(self.doc["run"]["run_id"], self.mop)

    def test_every_cited_id_exists(self):
        cited = set(_CITED_ID_RE.findall(self.mop))
        self.assertTrue(cited)
        self.assertEqual(sorted(cited - self.ids), [])

    def test_every_step_high_risk_and_decision_is_covered(self):
        cited = set(_CITED_ID_RE.findall(self.mop))
        must = {s["id"] for s in self.doc["steps"]}
        must |= {f["id"] for f in self.doc["findings"] if f["kind"] in ("blocker", "decision", "task")}
        must |= {f["id"] for f in self.doc["findings"] if f["kind"] == "risk" and f["severity"] == "high"}
        must |= {a["id"] for a in self.doc["acceptance"]}
        self.assertEqual(sorted(must - cited), [])

    def test_steps_keep_their_order(self):
        positions = [self.mop.index("#### [%s]" % s["id"]) for s in self.doc["steps"] if "#### [%s]" % s["id"] in self.mop]
        self.assertEqual(positions, sorted(positions))

    def test_cli_is_verbatim(self):
        blocks = [block.strip("\n") for block in _FENCE_RE.findall(self.mop)]
        for step in self.doc["steps"]:
            for text in (step["cli"], step["rollback"]):
                if text and "\n" in text:
                    self.assertIn(text.strip("\n"), blocks, "%s: CLI not copied verbatim" % step["id"])
        # And nothing in a code block that the input does not carry.
        known = {t.strip("\n") for s in self.doc["steps"] for t in (s["cli"], s["rollback"]) if t}
        for block in blocks:
            self.assertIn(block, known, "code block not from steps[].cli / rollback:\n%s" % block)

    def test_status_and_gaps_are_stated(self):
        self.assertEqual(self.doc["vlan"]["status"], "needs_decisions")
        self.assertIn("Not approved for execution", self.mop)
        self.assertEqual(self.doc["vlan"]["l3_vni"], 822)
        self.assertIn("| L3 VNI | 822 ", self.mop)
        for step in self.doc["steps"]:
            if not step["cli"]:
                continue
            section = self.mop.split("#### [%s]" % step["id"], 1)[1].split("####", 1)[0]
            if step["rollback"] is None:
                self.assertIn("Rollback: NOT IN DISCOVERY", section, step["id"])
            if step.get("confirm"):
                self.assertIn("`%s`" % step["confirm"], section, step["id"])
            if step.get("save"):
                self.assertIn("`%s`" % step["save"], section, step["id"])


if __name__ == "__main__":
    unittest.main()
