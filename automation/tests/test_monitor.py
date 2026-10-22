"""Self-checks for rails-monitor: validation, proxy + recording, ordering, and the HTTP event stream.

Runs the real handler on localhost against a stub guardrails server. Standard library only:
    python3 -m unittest discover -s automation/tests
"""
import http.client
import importlib.util
import json
import os
import queue
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
MONITOR = os.environ.get("MONITOR_PY") or os.path.join(HERE, "..", "argocd", "components", "tenants", "files", "monitor.py")

BLOCKED = {"choices": [{"message": {"content": "I'm sorry, I can't respond to that."}}],
           "guardrails": {"output_data": {"triggered_input_rail": "content safety check input $model=content_safety",
                                          "policy_violations": ["Criminal Planning/Confessions"], "allowed": False},
                          "log": {"stats": {"input_rails_duration": 0.5}}}}


NO_VERDICT = {"choices": [{"message": {"content": "Internal server error"}}], "guardrails": {"output_data": {}}}


class Upstream(BaseHTTPRequestHandler):
    """Stub guardrails server: the prompt picks the answer (default: blocked); remembers each body."""
    bodies = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Upstream.bodies.append(body)
        prompt = ((body.get("messages") or [{}])[-1]).get("content", "")
        status, data, extra = 200, json.dumps(BLOCKED).encode(), 0
        if prompt == "no-verdict":
            data = json.dumps(NO_VERDICT).encode()
        elif prompt == "trunc-500":  # an error whose body is cut short
            status, data, extra = 500, b'{"error": "boom', 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data) + extra))
        self.end_headers()
        self.wfile.write(data)
        if extra:
            self.close_connection = True


class KubeAPI(BaseHTTPRequestHandler):
    """Stub Kubernetes API: the first pod list names pod-a, later ones pod-b (a rollout)."""
    lists = 0

    def log_message(self, *args):
        pass

    def do_GET(self):
        if "/log?" in self.path:
            pod = self.path.split("/pods/")[1].split("/")[0]
            body = f"INFO: hello from {pod}\n".encode()
        else:
            KubeAPI.lists += 1
            pod = "pod-a" if KubeAPI.lists == 1 else "pod-b"
            body = json.dumps({"items": [{"metadata": {"name": pod, "creationTimestamp": "2026-01-01T00:00:00Z"},
                                          "status": {"phase": "Running"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class MonitorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sa = tempfile.mkdtemp()
        for name, value in (("namespace", "test-ns"), ("token", "test-token")):
            with open(os.path.join(cls.sa, name), "w") as f:
                f.write(value)
        cls.upstream = serve(Upstream)
        cls.kube = serve(KubeAPI)
        os.environ.update(SA_DIR=cls.sa, GUARDRAILS_URL=f"http://127.0.0.1:{cls.upstream.server_port}",
                          K8S_API=f"http://127.0.0.1:{cls.kube.server_port}")
        spec = importlib.util.spec_from_file_location("monitor", MONITOR)
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)
        cls.srv = serve(cls.m.Handler)
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"
        threading.Thread(target=cls.m.follow_logs, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.upstream.shutdown()
        cls.kube.shutdown()

    def frames(self, until, timeout=20):
        """Read GET /events over HTTP; return the parsed frames once until(frames) holds."""
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_port, timeout=timeout)
        conn.request("GET", "/events")
        resp = conn.getresponse()
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream")
        frames, kind, deadline = [], None, time.time() + timeout
        try:
            while time.time() < deadline:
                line = resp.fp.readline().decode().rstrip("\n")
                if line.startswith("event: "):
                    kind = line[len("event: "):]
                elif line.startswith("data: "):
                    frames.append((kind, json.loads(line[len("data: "):])))
                    if until(frames):
                        return frames
        finally:
            conn.close()
        self.fail(f"stream never satisfied the condition; got {frames[-5:]}")

    def post(self, body, headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(self.url + "/v1/chat/completions", data,
                                     {"Content-Type": "application/json", **(headers or {})})
        try:
            r = urllib.request.urlopen(req, timeout=10)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_malformed_bodies_get_400(self):
        for body in ([], {"guardrails": None}, {"guardrails": []},
                     {"guardrails": {"options": None}}, {"guardrails": {"options": {"log": 5}}}):
            with self.subTest(body=body):
                status, out = self.post(body)
                self.assertEqual(status, 400)
                self.assertIn("error", out)
        status, _ = self.post(b"not json")
        self.assertEqual(status, 400)

    def test_proxy_forwards_records_and_streams(self):
        q = queue.Queue()
        self.m.subscribers.append(q)
        try:
            status, out = self.post(
                {"messages": [{"role": "user", "content": "rob a house"}], "guardrails": {"config_id": "cs"}},
                {"X-Eval-Run": "t1", "X-Eval-Label": "unsafe", "X-Eval-Total": "1"})
            self.assertEqual(status, 200)
            self.assertEqual(out, BLOCKED)
            sent = Upstream.bodies[-1]["guardrails"]["options"]
            self.assertTrue(sent["output_vars"] and sent["log"]["activated_rails"])  # verdict details requested
            kinds = {}
            while not q.empty():
                head, data = q.get().split("\n")[:2]
                kinds[head.split(": ")[1]] = json.loads(data[len("data: "):])
            self.assertEqual(kinds["run"]["tp"], 1)
            self.assertEqual(kinds["decision"]["verdict"], "blocked")
            self.assertEqual(kinds["decision"]["categories"], ["Criminal Planning/Confessions"])
            self.assertEqual(kinds["decision"]["outcome"], "TP")
        finally:
            self.m.subscribers.remove(q)

    def test_run_snapshots_publish_in_order(self):
        q = queue.Queue()
        self.m.subscribers.append(q)
        real = self.m.publish

        def slow_publish(kind, data):  # widens the window in which a later snapshot could overtake
            time.sleep(0.002)
            real(kind, data)

        self.m.publish = slow_publish
        try:
            evt = {"verdict": "blocked", "config": "cs", "ts": 0}
            threads = [threading.Thread(target=self.m.record, args=(dict(evt), "unsafe", "order", 50, "allowed"))
                       for _ in range(50)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            self.m.publish = real
            self.m.subscribers.remove(q)
        seen = []
        while not q.empty():
            head, data = q.get().split("\n")[:2]
            if head == "event: run":
                seen.append(json.loads(data[len("data: "):])["n"])
        self.assertEqual(seen, list(range(1, 51)))


    def test_stream_delivers_run_and_decision_over_http(self):
        def post_soon():
            time.sleep(0.5)
            self.post({"messages": [{"role": "user", "content": "over http"}], "guardrails": {"config_id": "cs"}},
                      {"X-Eval-Run": "http1", "X-Eval-Label": "unsafe"})
        threading.Thread(target=post_soon).start()
        got = self.frames(lambda fs: any(k == "decision" and d["prompt"] == "over http" for k, d in fs))
        self.assertTrue(any(k == "run" and d["run"] == "http1" and d["tp"] == 1 for k, d in got))

    def test_every_run_survives_the_replay_limit(self):
        for r in range(4):  # 4 runs x 100 decisions: past the 300-frame decision replay
            for i in range(100):
                evt = {"verdict": "blocked", "config": "cs", "ts": 1000 + r, "prompt": f"p{i}"}
                self.m.record(evt, "unsafe", f"hist{r}", 100, "allowed")
                self.m.publish("decision", evt)
        got = self.frames(lambda fs: {f"hist{r}" for r in range(4)} <= {d.get("run") for k, d in fs if k == "run"})
        self.assertEqual({d["n"] for k, d in got if k == "run" and d["run"].startswith("hist")}, {100})
        self.assertEqual(len(self.m.events), self.m.events.maxlen)  # the decision replay did evict

    def test_log_lines_follow_the_pod_across_a_rollout(self):
        self.frames(lambda fs: {"INFO: hello from pod-a", "INFO: hello from pod-b"} <=
                    {d.get("line") for k, d in fs if k == "log"})


    def test_errors_counted_per_errors_as(self):
        # The dashboard's own tallies: a missing verdict is a block only when the eval says so.
        cases = [("no-verdict", "unsafe"), ("no-verdict", "safe"), ("x", "unsafe")]  # x: blocked
        for mode, want in (("allowed", (1, 1, 0, 1, 2)), ("blocked", (2, 0, 1, 0, 2))):
            for prompt, label in cases:
                self.post({"messages": [{"role": "user", "content": prompt}]},
                          {"X-Eval-Run": f"ea-{mode}", "X-Eval-Label": label, "X-Eval-Errors-As": mode})
            r = self.m.runs[f"ea-{mode}"]
            self.assertEqual((r["tp"], r["fn"], r["fp"], r["tn"], r["errors"]), want, mode)
            self.assertEqual(r["errors_as"], mode)

    def test_truncated_upstream_error_is_answered_and_recorded(self):
        status, out = self.post({"messages": [{"role": "user", "content": "trunc-500"}]},
                                {"X-Eval-Run": "trunc", "X-Eval-Label": "unsafe"})
        self.assertEqual(status, 500)
        self.assertIn("unreadable body", out["error"])
        r = self.m.runs["trunc"]
        self.assertEqual((r["fn"], r["errors"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
