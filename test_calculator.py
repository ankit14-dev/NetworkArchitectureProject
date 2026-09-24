"""Integration tests for the persistent HTTP/1.1 calculator."""

import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
PORT = 19080


def read_response(sock: socket.socket, buffer: bytes = b""):
    while b"\r\n\r\n" not in buffer:
        buffer += sock.recv(4096)
    raw_headers, buffer = buffer.split(b"\r\n\r\n", 1)
    lines = raw_headers.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = dict(line.split(": ", 1) for line in lines[1:])
    length = int(headers["Content-Length"])
    while len(buffer) < length:
        buffer += sock.recv(4096)
    return status, headers, buffer[:length], buffer[length:]


def request(method: str, target: str, extra_headers: str = "") -> bytes:
    return (
        f"{method} {target} HTTP/1.1\r\n"
        f"Host: localhost:{PORT}\r\n"
        f"{extra_headers}\r\n"
    ).encode("ascii")


class CalculatorIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.process = subprocess.Popen(
            [sys.executable, str(BASE_DIR / "calcserve"), str(PORT)],
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        while True:
            try:
                with socket.create_connection(("127.0.0.1", PORT), timeout=0.1):
                    break
            except OSError:
                if cls.process.poll() is not None or time.monotonic() >= deadline:
                    out, err = cls.process.communicate(timeout=1)
                    raise RuntimeError(f"server failed to start: {out!r} {err!r}")
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.process.terminate()
        cls.process.wait(timeout=3)

    def test_required_requests_share_one_connection(self):
        cases = [
            ("GET", "/add?a=2&b=3", 200, b"5"),
            ("GET", "/sub?a=10&b=4", 200, b"6"),
            ("GET", "/mul?a=6&b=7", 200, b"42"),
            ("GET", "/div?a=9&b=3", 200, b"3"),
            ("GET", "/div?a=1&b=0", 400, b"division by zero"),
            ("GET", "/add?a=x&b=3", 400, b"must be numbers"),
            ("GET", "/pow?a=2&b=8", 404, b"404 Not Found"),
            ("POST", "/add", 405, b"405 Method Not Allowed"),
        ]
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as sock:
            buffer = b""
            for method, target, expected_status, expected_body in cases:
                sock.sendall(request(method, target))
                status, headers, body, buffer = read_response(sock, buffer)
                self.assertEqual(status, expected_status)
                self.assertIn(expected_body, body)
                self.assertEqual(int(headers["Content-Length"]), len(body))
                self.assertEqual(headers["Connection"], "keep-alive")

            sock.settimeout(0.1)
            with self.assertRaises(socket.timeout):
                sock.recv(1)

    def test_pipelined_requests_are_answered_in_order(self):
        payload = b"".join(
            request("GET", target)
            for target in ("/add?a=1&b=2", "/mul?a=3&b=4", "/sub?a=9&b=2")
        )
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as sock:
            sock.sendall(payload)
            buffer = b""
            bodies = []
            for _ in range(3):
                status, _, body, buffer = read_response(sock, buffer)
                self.assertEqual(status, 200)
                bodies.append(body)
        self.assertEqual(bodies, [b"3", b"12", b"7"])

    def test_body_framing_does_not_consume_next_request(self):
        first = request("POST", "/add", "Content-Length: 4\r\n") + b"test"
        second = request("GET", "/add?a=20&b=22")
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as sock:
            sock.sendall(first + second)
            status1, _, _, buffer = read_response(sock)
            status2, _, body2, _ = read_response(sock, buffer)
        self.assertEqual(status1, 405)
        self.assertEqual((status2, body2), (200, b"42"))

    def test_missing_host_is_400_and_closes(self):
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as sock:
            sock.sendall(b"GET /add?a=1&b=2 HTTP/1.1\r\n\r\n")
            status, headers, _, _ = read_response(sock)
            self.assertEqual(status, 400)
            self.assertEqual(headers["Connection"], "close")
            self.assertEqual(sock.recv(1), b"")

    def test_connection_close_is_honoured(self):
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as sock:
            sock.sendall(request("GET", "/div?a=7&b=2", "Connection: close\r\n"))
            status, headers, body, _ = read_response(sock)
            self.assertEqual((status, body), (200, b"3.5"))
            self.assertEqual(headers["Connection"], "close")
            self.assertEqual(sock.recv(1), b"")


if __name__ == "__main__":
    unittest.main()
