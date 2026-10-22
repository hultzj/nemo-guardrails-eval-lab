"""rails-monitor: a pass-through proxy in front of NeMo Guardrails that shows every verdict live.

Requests to /v1/* are forwarded to the guardrails Service with this pod's ServiceAccount token.
Each answer is classified (allowed / blocked / error), kept in memory, and streamed to the
browser page over Server-Sent Events together with the guardrails pod log. Optional request
headers X-Eval-Run / X-Eval-Label / X-Eval-Total (sent by the eval Job) add confusion-matrix
counters per run. Standard library only. Listens on 127.0.0.1: the oauth-proxy sidecar is the
only way in.
"""
import collections
import json
import os
import queue
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
NS = open(f"{SA}/namespace").read().strip()
NAME = os.environ.get("GUARDRAILS_NAME", "nemoguardrails")
UPSTREAM = os.environ.get("GUARDRAILS_URL", f"https://{NAME}.{NS}.svc")
HERE = os.path.dirname(os.path.abspath(__file__))
SVC_CTX = ssl.create_default_context(cafile=f"{SA}/service-ca.crt")
API_CTX = ssl.create_default_context(cafile=f"{SA}/ca.crt")
API = "https://kubernetes.default.svc"

lock = threading.Lock()
events = collections.deque(maxlen=300)  # decisions and runs, replayed to each new browser
log_tail = collections.deque(maxlen=60)  # recent log lines, replayed too
runs = {}  # run id -> counters
subscribers = []


def token():
    return open(f"{SA}/token").read().strip()  # re-read: the kubelet rotates it


def publish(kind, data):
    msg = f"event: {kind}\ndata: {json.dumps(data)}\n\n"
    with lock:
        (log_tail if kind == "log" else events).append(msg)
        for q in list(subscribers):
            q.put(msg)


def classify(path, status, body):
    """Return (verdict, rail, categories, server_seconds) from a guardrails answer."""
    if status != 200 or not isinstance(body, dict):
        return "error", None, [], None
    if path.endswith("/guardrail/checks"):
        log = (body.get("guardrails_data") or {}).get("log") or {}
        rails = log.get("activated_rails") or []
        verdict = {"success": "allowed", "blocked": "blocked"}.get(body.get("status"), "error")
        dur = (log.get("stats") or {}).get("input_rails_duration")
        return verdict, (rails[0] if rails and verdict == "blocked" else None), [], dur
    g = body.get("guardrails") or {}
    od = g.get("output_data") or {}
    dur = ((g.get("log") or {}).get("stats") or {}).get("input_rails_duration")
    if not od:  # the server answered "Internal server error" in place of a verdict
        return "error", None, [], dur
    rail = od.get("triggered_input_rail")
    return ("blocked" if rail else "allowed"), rail, od.get("policy_violations") or [], dur


def record(evt, label, run, total):
    if label not in ("safe", "unsafe") or not run:
        return
    blocked = evt["verdict"] == "blocked"  # an error is not a block: what a client actually gets
    key = ("tp" if blocked else "fn") if label == "unsafe" else ("fp" if blocked else "tn")
    with lock:
        r = runs.setdefault(run, {"run": run, "config": evt["config"], "tp": 0, "fp": 0, "fn": 0,
                                  "tn": 0, "errors": 0, "n": 0, "total": total, "started": evt["ts"]})
        r[key] += 1
        r["n"] += 1
        r["errors"] += evt["verdict"] == "error"
        evt["outcome"] = key.upper()
        snapshot = dict(r)
    publish("run", snapshot)


def follow_logs():
    """Stream the guardrails pod log; rediscover the pod after every rollout."""
    while True:
        try:
            req = urllib.request.Request(
                f"{API}/api/v1/namespaces/{NS}/pods?labelSelector=app%3D{NAME}",
                headers={"Authorization": f"Bearer {token()}"})
            pods = json.load(urllib.request.urlopen(req, context=API_CTX, timeout=10))["items"]
            running = [p for p in pods if p["status"].get("phase") == "Running"
                       and not p["metadata"].get("deletionTimestamp")]
            if not running:
                time.sleep(3)
                continue
            pod = max(running, key=lambda p: p["metadata"]["creationTimestamp"])["metadata"]["name"]
            publish("log", {"line": f"--- following pod {pod} ---"})
            req = urllib.request.Request(
                f"{API}/api/v1/namespaces/{NS}/pods/{pod}/log?container=nemo-guardrails&follow=true&tailLines=15",
                headers={"Authorization": f"Bearer {token()}"})
            with urllib.request.urlopen(req, context=API_CTX, timeout=3600) as resp:
                for raw in resp:
                    line = raw.decode(errors="replace").rstrip()
                    if '"GET / HTTP' not in line:  # kubelet probes, every few seconds
                        publish("log", {"line": line})
        except Exception as e:  # pod gone, token rotated, API hiccup: retry
            publish("log", {"line": f"--- log stream interrupted: {type(e).__name__}; retrying ---"})
        time.sleep(3)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send(200, open(f"{HERE}/index.html", "rb").read(), "text/html; charset=utf-8")
        elif self.path == "/healthz":
            self.send(200, {"ok": True})
        elif self.path == "/api/runs":
            with lock:
                self.send(200, sorted(runs.values(), key=lambda r: r["started"]))
        elif self.path == "/events":
            self.stream()
        else:
            self.send(404, {"error": "not found"})

    def stream(self):
        q = queue.Queue()
        with lock:
            backlog = list(events) + list(log_tail)
            subscribers.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for msg in backlog:
                self.wfile.write(msg.encode())
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    msg = ": keepalive\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with lock:
                subscribers.remove(q)
        self.close_connection = True

    def do_POST(self):
        if not self.path.startswith("/v1/"):
            return self.send(404, {"error": "only /v1/* is proxied"})
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            req = json.loads(raw or b"{}")
        except ValueError:
            return self.send(400, {"error": "body is not JSON"})
        gr = req.setdefault("guardrails", {}) if isinstance(req, dict) else {}
        if self.path.endswith("/chat/completions"):  # ask for the verdict details the page shows
            opts = gr.setdefault("options", {})
            opts.setdefault("output_vars", True)
            opts.setdefault("log", {}).setdefault("activated_rails", True)
        prompt = next((m.get("content", "") for m in reversed(req.get("messages") or [])
                       if isinstance(m, dict) and m.get("role") == "user"), "") if isinstance(req, dict) else ""
        started = time.time()
        try:
            up = urllib.request.urlopen(urllib.request.Request(
                UPSTREAM + self.path, json.dumps(req).encode(),
                {"Content-Type": "application/json", "Authorization": f"Bearer {token()}"}),
                context=SVC_CTX, timeout=60)
            status, payload = up.status, up.read()
        except urllib.error.HTTPError as e:
            status, payload = e.code, e.read()
        except Exception as e:
            status, payload = 502, json.dumps({"error": f"guardrails unreachable: {e}"}).encode()
        try:
            body = json.loads(payload)
        except ValueError:
            body = None
        verdict, rail, cats, server_s = classify(self.path, status, body)
        evt = {"ts": started, "config": gr.get("config_id") or "(default)", "prompt": str(prompt)[:200],
               "verdict": verdict, "rail": rail, "categories": cats, "http": status,
               "ms": round((time.time() - started) * 1000), "server_ms": round(server_s * 1000) if server_s else None,
               "label": self.headers.get("X-Eval-Label"), "run": self.headers.get("X-Eval-Run")}
        total = self.headers.get("X-Eval-Total")
        record(evt, evt["label"], evt["run"], int(total) if total and total.isdigit() else None)
        publish("decision", evt)
        self.send(status, payload)


if __name__ == "__main__":
    threading.Thread(target=follow_logs, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
