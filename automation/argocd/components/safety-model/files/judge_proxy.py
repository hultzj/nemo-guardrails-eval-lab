"""Module 5's judge proxy: the only holder of the judge endpoint's API key.

Tenants' guardrails servers call this proxy without a key; it forwards POST /v1/chat/completions
to JUDGE_URL with the key from KEY_FILE and the model pinned to JUDGE_MODEL. Nothing a caller
sends can redirect it or read the key: participants control their rail configs (and so the URL
their server calls), which is why the key must never be in a tenant namespace. Standard library only.
"""
import json
import os
import socket
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

JUDGE_URL = os.environ.get("JUDGE_URL", "").rstrip("/")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "")
KEY_FILE = os.environ.get("KEY_FILE", "/secret/api-key")
MAX_BODY = 1 << 20
CLIENT_TIMEOUT = 30  # seconds in total a caller may take to send its request, however it trickles
MAX_CONNECTIONS = 32  # beyond this, new connections are closed at once rather than queued
MAX_PER_CLIENT = 8  # connections one source address may hold, so one pod cannot take every slot


def key():
    try:
        with open(KEY_FILE) as f:  # re-read per request: a rotated Secret takes effect without a restart
            return f.read().strip()
    except OSError:
        return ""


class Handler(BaseHTTPRequestHandler):
    timeout = CLIENT_TIMEOUT  # per read; the deadline below bounds the whole request

    def setup(self):
        super().setup()
        # A socket timeout restarts with every byte: a deadline timer bounds headers and body together.
        self.deadline = threading.Timer(CLIENT_TIMEOUT, self.expire)
        self.deadline.daemon = True
        self.deadline.start()

    def expire(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)  # unblocks a pending read with EOF
        except OSError:
            pass

    def finish(self):
        self.deadline.cancel()
        super().finish()

    def log_message(self, fmt, *args):  # no request lines with client details; errors only
        pass

    def reply(self, status, data, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def fail(self, status, message):
        self.reply(status, json.dumps({"error": {"message": message}}).encode())

    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, json.dumps({"ok": True, "key": bool(key())}).encode())
        self.fail(404, "not found")

    def do_POST(self):
        if self.path.split("?")[0] not in ("/v1/chat/completions", "/chat/completions"):
            return self.fail(404, "only /v1/chat/completions is forwarded")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self.fail(400, "bad Content-Length")
        if not 0 < n <= MAX_BODY:
            return self.fail(413 if n > MAX_BODY else 400, "body missing or too large")
        try:
            data = self.rfile.read(n)
            if len(data) < n:
                raise TimeoutError  # cut short by the deadline (or the caller hung up)
            body = json.loads(data)
            if not isinstance(body, dict):
                raise ValueError
        except (TimeoutError, OSError):
            self.close_connection = True
            return  # the caller stalled mid-body: drop it without an answer
        except ValueError:
            return self.fail(400, "body is not a JSON object")
        finally:
            self.deadline.cancel()  # the request is in (or gone): the upstream call has its own timeout
        k = key()
        if not k:
            return self.fail(503, "no judge API key: the facilitator runs make judge-secret")
        body["model"], body["stream"] = JUDGE_MODEL, False
        req = urllib.request.Request(JUDGE_URL + "/chat/completions", json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "Authorization": f"Bearer {k}"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return self.reply(r.status, r.read())
        except urllib.error.HTTPError as e:
            return self.reply(e.code, e.read())
        except Exception as e:  # refused, reset, timed out: a gateway error, without upstream details
            print(f"upstream error: {type(e).__name__}", flush=True)
            return self.fail(502, f"judge endpoint unreachable ({type(e).__name__})")


class Server(ThreadingHTTPServer):
    """A thread per connection: at most MAX_CONNECTIONS, and MAX_PER_CLIENT from one address."""
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock, self.live, self.per_client = threading.Lock(), 0, {}

    def take(self, host):
        with self.lock:
            if self.live >= MAX_CONNECTIONS or self.per_client.get(host, 0) >= MAX_PER_CLIENT:
                return False
            self.live += 1
            self.per_client[host] = self.per_client.get(host, 0) + 1
            return True

    def give(self, host):
        with self.lock:
            self.live -= 1
            self.per_client[host] -= 1
            if not self.per_client[host]:
                del self.per_client[host]

    def process_request(self, request, client_address):
        if not self.take(client_address[0]):
            return self.shutdown_request(request)
        try:
            super().process_request(request, client_address)
        except Exception:
            self.give(client_address[0])
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.give(client_address[0])


if __name__ == "__main__":
    assert JUDGE_URL.startswith("https://") and JUDGE_MODEL, "JUDGE_URL (https) and JUDGE_MODEL are required"
    Server(("0.0.0.0", 8080), Handler).serve_forever()
