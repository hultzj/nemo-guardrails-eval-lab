"""Self-checks for rails-monitor: request validation, proxy + recording, and run-event ordering.

Runs the real handler on localhost against a stub guardrails server. Standard library only:
    python3 -m unittest discover -s automation/tests
"""
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


class Upstream(BaseHTTPRequestHandler):
    """Stub guardrails server: answers every POST with a blocked verdict and remembers the body."""
    bodies = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        Upstream.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        data = json.dumps(BLOCKED).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


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
        os.environ.update(SA_DIR=cls.sa, GUARDRAILS_URL=f"http://127.0.0.1:{cls.upstream.server_port}")
        spec = importlib.util.spec_from_file_location("monitor", MONITOR)
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)
        cls.srv = serve(cls.m.Handler)
        cls.url = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.upstream.shutdown()

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


if __name__ == "__main__":
    unittest.main()
