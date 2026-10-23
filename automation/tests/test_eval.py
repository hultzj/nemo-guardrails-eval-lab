"""Self-checks for notebooks/labeval.py: tallies for every kind of answer, in both ERRORS_AS modes,
as a script and as the library the notebooks import.

Runs the real script against a stub monitor:  python3 -m unittest discover -s automation/tests
"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
EVAL = os.path.join(HERE, "..", "..", "notebooks", "labeval.py")

ALLOWED = {"guardrails": {"output_data": {"triggered_input_rail": None, "allowed": True}}}
BLOCKED = {"guardrails": {"output_data": {"triggered_input_rail": "regex check input"}}}
NO_VERDICT = {"choices": [{"message": {"content": "Internal server error"}}], "guardrails": {"output_data": {}}}
# prompt -> (label, how the stub answers)
ROWS = {"s-allow": ("safe", "allowed"), "s-block": ("safe", "blocked"), "s-miss": ("safe", "no-verdict"),
        "s-trunc": ("safe", "truncated"), "u-allow": ("unsafe", "allowed"), "u-block": ("unsafe", "blocked"),
        "u-miss": ("unsafe", "no-verdict"), "u-drop": ("unsafe", "dropped")}


RUNS = [  # what GET /api/runs returns: two complete content-safety runs, one partial, one other config
    {"run": "cs-old", "config": "content-safety", "n": 8, "started": 1, "tp": 1, "fp": 1, "fn": 1, "tn": 5, "errors": 0},
    {"run": "cs-new", "config": "content-safety", "n": 8, "started": 3, "tp": 2, "fp": 1, "fn": 0, "tn": 5, "errors": 0, "errors_as": "allowed"},
    {"run": "cs-partial", "config": "content-safety", "n": 3, "started": 4, "tp": 1, "fp": 0, "fn": 0, "tn": 2, "errors": 0},
    {"run": "rx", "config": "regex-only", "n": 8, "started": 5, "tp": 0, "fp": 1, "fn": 2, "tn": 5, "errors": 0},
    # newest, 8 prompts counted so far, but a run of 16: not a complete 8-prompt baseline
    {"run": "cs-bigger", "config": "content-safety", "n": 8, "total": 16, "started": 6, "tp": 2, "fp": 0, "fn": 0, "tn": 6, "errors": 0},
]


class Monitor(BaseHTTPRequestHandler):
    headers_seen = []

    def log_message(self, *args):
        pass

    def do_GET(self):  # /api/runs, or /v1/rails/configs as the guardrails server
        data = json.dumps([{"id": "regex-only"}] if self.path == "/v1/rails/configs" else RUNS).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Monitor.headers_seen.append(dict(self.headers))
        how = ROWS[body["messages"][0]["content"]][1]
        if how == "dropped":  # close before any response: RemoteDisconnected in the client
            self.close_connection = True
            return
        data = json.dumps({"allowed": ALLOWED, "blocked": BLOCKED}.get(how, NO_VERDICT)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data) + (500 if how == "truncated" else 0)))
        self.end_headers()
        self.wfile.write(data)
        if how == "truncated":  # promised more than sent: IncompleteRead in the client
            self.close_connection = True


class EvalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Monitor)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.dir = tempfile.mkdtemp()
        for name, value in (("namespace", "test-ns"), ("token", "test-token")):
            with open(os.path.join(cls.dir, name), "w") as f:
                f.write(value)
        cls.dataset = os.path.join(cls.dir, "dataset.jsonl")
        with open(cls.dataset, "w") as f:
            for prompt, (label, _) in ROWS.items():
                f.write(json.dumps({"id": prompt, "label": label, "prompt": prompt}) + "\n")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def run_eval(self, errors_as):
        env = dict(os.environ, SA_DIR=self.dir, DATASET=self.dataset, CONFIG_ID="t", ERRORS_AS=errors_as,
                   MONITOR_URL=f"http://127.0.0.1:{self.srv.server_port}")
        out = subprocess.run([sys.executable, EVAL], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        m = re.search(r"^TP=(\d+) FN=(\d+) FP=(\d+) TN=(\d+) errors=(\d+)$", out.stdout, re.M)
        self.assertIsNotNone(m, out.stdout)
        return tuple(int(x) for x in m.groups())

    def test_errors_counted_as_allowed_fail_open(self):
        # errors (no verdict, truncated, dropped) are not blocks: unsafe ones become FN, safe ones TN
        self.assertEqual(self.run_eval("allowed"), (1, 3, 1, 3, 4))
        self.assertEqual(Monitor.headers_seen[-1]["X-Eval-Errors-As"], "allowed")

    def test_errors_counted_as_blocked_fail_closed(self):
        self.assertEqual(self.run_eval("blocked"), (3, 1, 3, 1, 4))
        self.assertEqual(Monitor.headers_seen[-1]["X-Eval-Errors-As"], "blocked")


    def test_runs_started_together_get_distinct_ids(self):
        env = dict(os.environ, SA_DIR=self.dir, DATASET=self.dataset, CONFIG_ID="same",
                   MONITOR_URL=f"http://127.0.0.1:{self.srv.server_port}")
        procs = [subprocess.Popen([sys.executable, EVAL], env=env, stdout=subprocess.PIPE, text=True) for _ in range(2)]
        runs = {re.search(r"^run=(\S+)", p.communicate(timeout=60)[0], re.M).group(1) for p in procs}
        self.assertEqual(len(runs), 2, runs)


    def test_library_call_used_by_the_notebooks(self):
        os.environ["SA_DIR"] = self.dir
        spec = importlib.util.spec_from_file_location("labeval", EVAL)
        lab = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lab)
        res = lab.evaluate("t", lab.load_rows(self.dataset), "allowed",
                           url=f"http://127.0.0.1:{self.srv.server_port}", progress=None)
        self.assertEqual(res["counts"], {"TP": 1, "FN": 3, "FP": 1, "TN": 3})
        self.assertEqual(res["errors"], 4)
        self.assertAlmostEqual(res["metrics"]["recall"], 0.25)
        self.assertEqual({r["outcome"] for r in res["results"] if r["id"] == "u-drop"}, {"FN"})
        self.assertEqual(len(lab.load_rows(self.dataset, stride=4)), 2)


    def lab(self):
        os.environ["SA_DIR"] = self.dir
        spec = importlib.util.spec_from_file_location("labeval", EVAL)
        lab = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lab)
        return lab

    def test_baseline_is_the_newest_complete_run(self):
        lab = self.lab()
        runs = lab.monitor_runs(f"http://127.0.0.1:{self.srv.server_port}")
        self.assertEqual(set(runs), {"cs-old", "cs-new", "cs-partial", "rx", "cs-bigger"})
        base = lab.latest_run(runs, "content-safety", 8)
        self.assertEqual(base["run"], "cs-new")  # newest complete: not partial, not a larger run, not another config
        self.assertEqual(base["counts"], {"TP": 2, "FP": 1, "FN": 0, "TN": 5})
        self.assertAlmostEqual(base["metrics"]["precision"], 2 / 3)

    def test_no_baseline_after_a_monitor_restart(self):
        lab = self.lab()
        self.assertIsNone(lab.latest_run({}, "content-safety", 8))  # the notebook then re-measures
        self.assertIsNone(lab.latest_run({"rx": RUNS[3]}, "content-safety", 8))


    def test_require_configs_stops_before_an_evaluation_that_would_measure_nothing(self):
        lab = self.lab()
        url = f"http://127.0.0.1:{self.srv.server_port}"
        self.assertEqual(lab.require_configs("regex-only", url=url), ["regex-only"])
        with self.assertRaisesRegex(RuntimeError, r"not \['content-safety'\].*Module 2"):
            lab.require_configs("regex-only", "content-safety", url=url)
        with self.assertRaisesRegex(RuntimeError, "No guardrails server answers.*Module 1"):
            lab.require_configs("regex-only", url="http://127.0.0.1:9")  # nothing listens there

    def test_summary_warns_when_prompts_got_no_verdict(self):
        lab = self.lab()
        counts = {"TP": 0, "FP": 0, "FN": 20, "TN": 30}
        res = {"run": "r", "config": "c", "errors_as": "allowed", "counts": counts, "errors": 50,
               "metrics": lab.metrics(counts)}
        self.assertIn("WARNING: 50 of 50 prompts got no verdict", lab.summary(res))
        self.assertNotIn("WARNING", lab.summary({**res, "errors": 0}))


if __name__ == "__main__":
    unittest.main()
