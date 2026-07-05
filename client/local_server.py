"""Local HTTP server to receive student credentials from Safe Exam Browser.

Listens on localhost (127.0.0.1) and handles OPTIONS and POST requests to /login.
Updates the proctor client session once the student logs in.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer


class LoginServerHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: any) -> None:
        # Suppress default request logging to avoid cluttering proctor console.
        pass

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight requests."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self) -> None:
        """Serve the login page at / so SEB can point directly here."""
        if self.path == "/" or self.path == "/index.html":
            html_path = self.server.login_html_path
            if html_path and os.path.exists(html_path):
                with open(html_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Login page not found. Place index.html next to the executable.")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        """Handle student login submissions."""
        if self.path == "/login":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                post_data = self.rfile.read(content_length)
                data = json.loads(post_data.decode("utf-8"))
            except Exception as e:
                self._send_json(400, {"error": f"Invalid JSON payload: {e}"})
                return

            username = data.get("username")
            exam_id = data.get("exam_id")

            if not username:
                self._send_json(400, {"error": "Missing 'username' field"})
                return

            # Keep all other payload fields in metadata (excluding username/exam_id for config resolution)
            custom_meta = {k: v for k, v in data.items() if k not in ("username", "exam_id")}

            # Store received login details on the server object
            self.server.login_data = {
                "username": username.strip(),
                "exam_id": exam_id.strip() if exam_id else None,
                "metadata": custom_meta,
            }
            # Notify the main thread waiting for login
            self.server.login_event.set()

            print(f"\n[local-server] Received login: student_id={username}")
            self._send_json(200, {"status": "success"})
        else:
            self.send_response(404)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

    def _send_json(self, status_code: int, data: dict) -> None:
        try:
            body = json.dumps(data).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass


class LocalLoginServer(HTTPServer):
    """Custom HTTPServer carrying login state and event synchronization."""
    def __init__(self, server_address: tuple[str, int], RequestHandlerClass: type):
        super().__init__(server_address, RequestHandlerClass)
        self.login_event = threading.Event()
        self.login_data: dict | None = None
        self.login_html_path: str | None = None


def _find_index_html() -> str | None:
    """Locate index.html next to the executable (frozen) or in cwd (source)."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.getcwd()
    candidate = os.path.join(base, "index.html")
    return candidate if os.path.isfile(candidate) else None


def start_local_server(port: int) -> LocalLoginServer:
    """Start the local login receiver server on a background thread.
    
    Returns the LocalLoginServer instance.
    """
    server_address = ("127.0.0.1", port)
    
    # Enable address reuse to avoid port binding errors on restarts
    class StoppableHTTPServer(LocalLoginServer):
        allow_reuse_address = True
        
    try:
        server = StoppableHTTPServer(server_address, LoginServerHandler)
    except socket.error as exc:
        raise RuntimeError(
            f"Could not bind to local port {port}. Please verify no other "
            f"instance is running on this port: {exc}"
        )

    # Resolve the login page so GET / can serve it
    html_path = _find_index_html()
    server.login_html_path = html_path
    if html_path:
        print(f"[local-server] Serving login page from {html_path}")
    else:
        print("[local-server] Warning: index.html not found; GET / will return 404")

    thread = threading.Thread(
        target=server.serve_forever,
        name="local-login-server",
        daemon=True
    )
    thread.start()
    print(f"[local-server] Login page: http://127.0.0.1:{port}/")
    print(f"[local-server] POST endpoint: http://127.0.0.1:{port}/login")
    return server
