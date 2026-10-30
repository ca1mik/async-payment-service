"""Local demo endpoint: python tools/webhook_receiver.py --fail-first 2."""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--fail-first", type=int, default=0)
    args = parser.parse_args()
    attempts = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/webhook":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024 * 1024:
                self.send_error(413)
                return
            try:
                body = json.loads(self.rfile.read(length))
                event_id = body["event_id"]
            except (ValueError, KeyError, TypeError):
                self.send_error(400)
                return
            with lock:
                attempts[event_id] = attempts.get(event_id, 0) + 1
                attempt = attempts[event_id]
            status = 503 if attempt <= args.fail_first else 204
            print(
                f"event={event_id} attempt={attempt} payment_status={body.get('status')} "
                f"http_status={status}",
                flush=True,
            )
            self.send_response(status)
            self.end_headers()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"Demo receiver listening on port {args.port}; Ctrl+C to stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
