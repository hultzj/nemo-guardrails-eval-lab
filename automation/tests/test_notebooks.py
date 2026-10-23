"""Structural self-checks for the lab notebooks: they refuse to measure without a guardrails server
and configs, and refuse to report a measurement in which prompts got no verdict.

Executing them needs pandas/matplotlib and a cluster (done in the live walk with papermill); these
checks catch a guard being dropped from the cells.   python3 -m unittest discover -s automation/tests
"""
import json
import os
import re
import unittest

NB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "notebooks")


def cells(name):
    with open(os.path.join(NB, name)) as f:
        return ["".join(c["source"]) for c in json.load(f)["cells"] if c["cell_type"] == "code"]


def sections(name):
    """Code cells grouped under their '## N.' markdown heading."""
    with open(os.path.join(NB, name)) as f:
        nb = json.load(f)["cells"]
    out, current = {}, "intro"
    for c in nb:
        src = "".join(c["source"])
        if c["cell_type"] == "markdown" and src.startswith("## "):
            current = src.split(".")[0][3:]
        elif c["cell_type"] == "code":
            out.setdefault(current, []).append(src)
    return out


class NotebookGuards(unittest.TestCase):
    def test_setup_requires_the_server_and_both_configs(self):
        for name in ("03-evaluate.ipynb", "04-tune-and-break.ipynb"):
            setup = "\n".join(sections(name)["1"])
            self.assertIn('lab.require_configs("regex-only", "content-safety")', setup, name)

    def test_every_measurement_asserts_it_got_verdicts(self):
        # every full-dataset evaluate() result is checked before it is compared or reported
        for name in ("03-evaluate.ipynb", "04-tune-and-break.ipynb"):
            for src in cells(name):
                for var in re.findall(r"^(\w+) = lab\.evaluate\([^)]*\brows\b", src, re.M):
                    self.assertRegex(src, rf'assert {var}\["errors"\] == 0', f"{name}: {var} unchecked")
        base = "\n".join(sections("04-tune-and-break.ipynb")["2"])
        self.assertIn('assert cs["errors"] == 0', base)
        self.assertIn('lab.require_configs("tuned")', base)

    def test_the_outage_section_alone_may_count_errors(self):
        outage = "\n".join(sections("04-tune-and-break.ipynb")["3"])
        self.assertIn('lab.require_configs("tuned")', outage)
        self.assertIn('errors_as="blocked"', outage)
        self.assertNotIn('["errors"] == 0', outage)  # errors are the point there


if __name__ == "__main__":
    unittest.main()
