"""Self-checks for the judge proxy: the key is added upstream and never comes from or goes to a caller,
the model is pinned, and only chat completions are forwarded.   python3 -m unittest discover -s automation/tests
"""
import importlib.util
import json
import os
import socket
import tempfile
import time
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY = os.path.join(HERE, "..", "argocd", "components", "safety-model", "files", "judge_proxy.py")


class Upstream(BaseHTTPRequestHandler):
    """Stub judge endpoint: remembers each request; a prompt of "fail" gets a 429."""
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Upstream.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        fail = body["messages"][-1]["content"] == "fail"
        data = json.dumps({"error": "rate"} if fail else {"choices": [{"message": {"content": "No"}}]}).encode()
        self.send_response(429 if fail else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(handler):
    s = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s


class JudgeProxy(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = serve(Upstream)
        cls.keyfile = tempfile.NamedTemporaryFile("w", delete=False)
        cls.keyfile.write("sekret-123\n")
        cls.keyfile.close()
        spec = importlib.util.spec_from_file_location("judge_proxy", PROXY)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)
        cls.mod.JUDGE_URL = f"http://127.0.0.1:{cls.up.server_address[1]}/v1"
        cls.mod.JUDGE_MODEL = "pinned/model"
        cls.mod.KEY_FILE = cls.keyfile.name
        cls.mod.MAX_CONNECTIONS = cls.mod.MAX_PER_CLIENT = 4  # every test client is 127.0.0.1
        cls.mod.CLIENT_TIMEOUT = cls.mod.Handler.timeout = 1  # seconds; 30 in production
        cls.proxy = cls.mod.Server(("127.0.0.1", 0), cls.mod.Handler)
        threading.Thread(target=cls.proxy.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.proxy.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown(); cls.proxy.shutdown()
        cls.up.server_close(); cls.proxy.server_close()
        os.unlink(cls.keyfile.name)

    def post(self, path, body, headers=None):
        req = urllib.request.Request(self.url + path, json.dumps(body).encode(),
                                     {"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_forwards_with_the_key_and_the_pinned_model(self):
        Upstream.seen.clear()
        status, text = self.post("/v1/chat/completions",
                                 {"model": "other/model", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
                                 {"Authorization": "Bearer caller-token"})
        self.assertEqual(status, 200)
        self.assertNotIn("sekret", text)
        s = Upstream.seen[-1]
        self.assertEqual(s["path"], "/v1/chat/completions")
        self.assertEqual(s["auth"], "Bearer sekret-123")  # the proxy's key, never the caller's header
        self.assertEqual((s["body"]["model"], s["body"]["stream"]), ("pinned/model", False))

    def test_upstream_errors_pass_through(self):
        status, text = self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "fail"}]})
        self.assertEqual((status, json.loads(text)), (429, {"error": "rate"}))

    def test_only_chat_completions_is_forwarded(self):
        Upstream.seen.clear()
        for path in ("/v1/models", "/v1/completions", "/v1/chat/completions/../models"):
            self.assertEqual(self.post(path, {"messages": []})[0], 404, path)
        self.assertEqual(Upstream.seen, [])

    def test_bad_bodies_are_refused(self):
        self.assertEqual(self.post("/v1/chat/completions", ["not", "an", "object"])[0], 400)

    def test_no_key_means_503_not_an_unauthenticated_call(self):
        Upstream.seen.clear()
        self.mod.KEY_FILE = "/nonexistent/api-key"
        try:
            status, _ = self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]})
        finally:
            self.mod.KEY_FILE = self.keyfile.name
        self.assertEqual(status, 503)
        self.assertEqual(Upstream.seen, [])

    def test_stalled_callers_are_dropped_and_capped(self):
        # MAX_CONNECTIONS callers that send headers and then stall mid-body, holding their threads
        port = self.proxy.server_address[1]
        stalled = []
        for _ in range(4):
            s = socket.create_connection(("127.0.0.1", port))
            s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\n\r\n{")
            stalled.append(s)
        time.sleep(0.3)
        with self.assertRaises(OSError):  # no free slot: closed at once, not queued behind them
            urllib.request.urlopen(self.url + "/healthz", timeout=5).read()
        time.sleep(1.5)  # past the client timeout: the server dropped every stalled caller
        for s in stalled:
            s.settimeout(2)
            self.assertEqual(s.recv(100), b"")
            s.close()
        with urllib.request.urlopen(self.url + "/healthz", timeout=5) as r:
            self.assertEqual(r.status, 200)

    def stall(self, n):
        out = []
        for _ in range(n):
            s = socket.create_connection(("127.0.0.1", self.proxy.server_address[1]))
            s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: 1000\r\n\r\n{")
            out.append(s)
        time.sleep(0.3)
        return out

    def test_a_trickling_caller_is_cut_off_at_the_deadline(self):
        s = self.stall(1)[0]
        start, closed = time.time(), False
        while time.time() - start < 4:  # one byte every 0.3 s: each read succeeds, the request never ends
            try:
                s.sendall(b" ")
            except OSError:
                closed = True
                break
            s.settimeout(0.3)
            try:
                if s.recv(100) == b"":
                    closed = True
                    break
            except socket.timeout:
                pass
        s.close()
        self.assertTrue(closed, "the trickling connection outlived the whole-request deadline")
        self.assertLess(time.time() - start, 2.5)

    def test_one_client_cannot_take_every_slot(self):
        self.mod.MAX_PER_CLIENT = 2
        try:
            held = self.stall(2)
            with self.assertRaises(OSError):  # a third connection from the same address is closed at once
                urllib.request.urlopen(self.url + "/healthz", timeout=5).read()
        finally:
            self.mod.MAX_PER_CLIENT = 4
        for s in held:
            s.close()
        time.sleep(1.5)

    def test_health_reports_key_presence_without_the_key(self):
        with urllib.request.urlopen(self.url + "/healthz", timeout=10) as r:
            text = r.read().decode()
        self.assertEqual(json.loads(text), {"ok": True, "key": True})


if __name__ == "__main__":
    unittest.main()
