"""rails-monitor: a pass-through proxy in front of NeMo Guardrails that shows every verdict live.

Requests to /v1/* are forwarded to the guardrails Service with this pod's ServiceAccount token.
Each answer is classified (allowed / blocked / error), kept in memory, and streamed to the
browser page over Server-Sent Events together with the guardrails pod log. Optional request
headers X-Eval-Run / X-Eval-Label / X-Eval-Total (sent by the eval Job) add confusion-matrix
counters per run. Standard library only. Listens on 127.0.0.1: the oauth-proxy sidecar is the
only way in.
"""
import collections
import functools
import json
import os
import queue
import ssl
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SA = os.environ.get("SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount")
NS = os.environ.get("POD_NAMESPACE") or open(f"{SA}/namespace").read().strip()
NAME = os.environ.get("GUARDRAILS_NAME", "nemoguardrails")
UPSTREAM = os.environ.get("GUARDRAILS_URL", f"https://{NAME}.{NS}.svc")
HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get("K8S_API", "https://kubernetes.default.svc")

# Reentrant: record() publishes while holding it, so run snapshots reach browsers in order.
lock = threading.RLock()
events = collections.deque(maxlen=300)  # recent decisions, replayed to each new browser
log_tail = collections.deque(maxlen=60)  # recent log lines, replayed too
runs = {}  # run id -> counters; every run is replayed, however many decisions came since
subscribers = []


def token():
    with open(f"{SA}/token") as f:  # re-read: the kubelet rotates it
        return f.read().strip()


@functools.lru_cache(maxsize=None)
def tls(cafile):
    return ssl.create_default_context(cafile=f"{SA}/{cafile}")


def ctx(url, cafile):
    return tls(cafile) if url.startswith("https") else None


def frame(kind, data):
    return f"event: {kind}\ndata: {json.dumps(data)}\n\n"


def publish(kind, data):
    msg = frame(kind, data)
    with lock:
        if kind != "run":  # runs are replayed from `runs`, so none ages out of the history
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


def record(evt, label, run, total, errors_as):
    if label not in ("safe", "unsafe") or not run:
        return
    # By default an error is not a block (the client fails open); the eval Job can say otherwise.
    blocked = evt["verdict"] == "blocked" or (evt["verdict"] == "error" and errors_as == "blocked")
    key = ("tp" if blocked else "fn") if label == "unsafe" else ("fp" if blocked else "tn")
    with lock:
        r = runs.setdefault(run, {"run": run, "config": evt["config"], "tp": 0, "fp": 0, "fn": 0,
                                  "tn": 0, "errors": 0, "n": 0, "total": total, "started": evt["ts"],
                                  "errors_as": errors_as})
        r[key] += 1
        r["n"] += 1
        r["errors"] += evt["verdict"] == "error"
        evt["outcome"] = key.upper()
        publish("run", dict(r))  # under the lock: a later snapshot never overtakes an earlier one


def follow_logs():
    """Stream the guardrails pod log; rediscover the pod after every rollout."""
    while True:
        try:
            req = urllib.request.Request(
                f"{API}/api/v1/namespaces/{NS}/pods?labelSelector=app%3D{NAME}",
                headers={"Authorization": f"Bearer {token()}"})
            pods = json.load(urllib.request.urlopen(req, context=ctx(API, "ca.crt"), timeout=10))["items"]
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
            with urllib.request.urlopen(req, context=ctx(API, "ca.crt"), timeout=3600) as resp:
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
            backlog = [frame("run", r) for r in sorted(runs.values(), key=lambda r: r["started"])]
            backlog += list(events) + list(log_tail)
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
        if not isinstance(req, dict) or not isinstance(req.setdefault("guardrails", {}), dict):
            return self.send(400, {"error": "body and its 'guardrails' field must be JSON objects"})
        gr = req["guardrails"]
        if self.path.endswith("/chat/completions"):  # ask for the verdict details the page shows
            opts = gr.setdefault("options", {})
            if not isinstance(opts, dict) or not isinstance(opts.setdefault("log", {}), dict):
                return self.send(400, {"error": "'guardrails.options' and its 'log' must be JSON objects"})
            opts.setdefault("output_vars", True)
            opts["log"].setdefault("activated_rails", True)
        msgs = req.get("messages") if isinstance(req.get("messages"), list) else []
        prompt = next((m.get("content", "") for m in reversed(msgs)
                       if isinstance(m, dict) and m.get("role") == "user"), "")
        started = time.time()
        try:
            up = urllib.request.urlopen(urllib.request.Request(
                UPSTREAM + self.path, json.dumps(req).encode(),
                {"Content-Type": "application/json", "Authorization": f"Bearer {token()}"}),
                context=ctx(UPSTREAM, "service-ca.crt"), timeout=60)
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
        errors_as = "blocked" if self.headers.get("X-Eval-Errors-As") == "blocked" else "allowed"
        record(evt, evt["label"], evt["run"], int(total) if total and total.isdigit() else None, errors_as)
        publish("decision", evt)
        self.send(status, payload)


if __name__ == "__main__":
    threading.Thread(target=follow_logs, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
