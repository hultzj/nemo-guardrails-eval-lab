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


class Monitor(BaseHTTPRequestHandler):
    headers_seen = []

    def log_message(self, *args):
        pass

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


if __name__ == "__main__":
    unittest.main()
