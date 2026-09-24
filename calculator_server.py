"""A small HTTP/1.1 calculator implemented directly on TCP sockets.

The module deliberately does not use an HTTP framework.  It contains just
enough parsing to demonstrate the important HTTP/1.1 boundary rule: after the
headers, a request body is exactly ``Content-Length`` bytes long, and any bytes
after that belong to the next request on the persistent connection.
"""

from __future__ import annotations

import argparse
import math
import signal
import socket
import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit


MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = 1024 * 1024

REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
}


class BadRequest(Exception):
    """Raised when a request cannot be parsed or validated."""


@dataclass(frozen=True)
class Request:
    method: str
    target: str
    version: str
    headers: Dict[str, str]
    body: bytes


def _read_request(sock: socket.socket, buffer: bytes) -> Tuple[Optional[Request], bytes]:
    """Read one request, retaining already-received bytes for the next one."""
    while b"\r\n\r\n" not in buffer:
        if len(buffer) > MAX_HEADER_BYTES:
            raise BadRequest("request headers are too large")
        chunk = sock.recv(4096)
        if not chunk:
            if buffer:
                raise BadRequest("connection closed in the middle of the headers")
            return None, b""
        buffer += chunk

    header_block, buffer = buffer.split(b"\r\n\r\n", 1)
    if len(header_block) > MAX_HEADER_BYTES:
        raise BadRequest("request headers are too large")

    try:
        lines = header_block.decode("iso-8859-1").split("\r\n")
    except UnicodeDecodeError as exc:
        raise BadRequest("headers are not valid ISO-8859-1") from exc

    parts = lines[0].split(" ")
    if len(parts) != 3 or not all(parts):
        raise BadRequest("malformed request line")
    method, target, version = parts
    if version != "HTTP/1.1":
        raise BadRequest("only HTTP/1.1 is supported")

    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if line[:1] in (" ", "\t") or ":" not in line:
            raise BadRequest("malformed header line")
        name, value = line.split(":", 1)
        name = name.strip().lower()
        value = value.strip()
        if not name or name in headers:
            raise BadRequest("empty or duplicate header name")
        headers[name] = value

    if not headers.get("host"):
        raise BadRequest("HTTP/1.1 requires a Host header")
    if "transfer-encoding" in headers:
        raise BadRequest("Transfer-Encoding is not supported")

    raw_length = headers.get("content-length", "0")
    try:
        content_length = int(raw_length, 10)
    except ValueError as exc:
        raise BadRequest("invalid Content-Length") from exc
    if content_length < 0 or content_length > MAX_BODY_BYTES:
        raise BadRequest("invalid Content-Length")

    while len(buffer) < content_length:
        chunk = sock.recv(min(4096, content_length - len(buffer)))
        if not chunk:
            raise BadRequest("connection closed in the middle of the body")
        buffer += chunk

    body = buffer[:content_length]
    remaining = buffer[content_length:]
    return Request(method, target, version, headers, body), remaining


def _number(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise BadRequest("a and b must be numbers") from exc
    if not math.isfinite(value):
        raise BadRequest("a and b must be finite numbers")
    return value


def _format_number(value: float) -> str:
    if value == 0:
        return "0"
    if value.is_integer():
        return str(int(value))
    return format(value, ".15g")


def calculate(target: str) -> Tuple[int, bytes]:
    """Return an HTTP status and response body for a calculator target."""
    parsed = urlsplit(target)
    operation = parsed.path.lstrip("/")
    if operation not in {"add", "sub", "mul", "div"}:
        return 404, b"404 Not Found\n"

    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=False)
    if set(query) != {"a", "b"} or len(query["a"]) != 1 or len(query["b"]) != 1:
        return 400, b"400 Bad Request: expected exactly one a and one b parameter\n"

    try:
        a = _number(query["a"][0])
        b = _number(query["b"][0])
    except BadRequest as exc:
        return 400, f"400 Bad Request: {exc}\n".encode("utf-8")

    if operation == "add":
        result = a + b
    elif operation == "sub":
        result = a - b
    elif operation == "mul":
        result = a * b
    else:
        if b == 0:
            return 400, b"400 Bad Request: division by zero\n"
        result = a / b

    if not math.isfinite(result):
        return 400, b"400 Bad Request: result is not finite\n"
    return 200, _format_number(result).encode("ascii")


def _response(status: int, body: bytes, close: bool = False) -> bytes:
    headers = [
        f"HTTP/1.1 {status} {REASONS[status]}",
        "Content-Type: text/plain; charset=utf-8",
        f"Content-Length: {len(body)}",
        f"Connection: {'close' if close else 'keep-alive'}",
        "Server: line-calculator/1.0",
        "",
        "",
    ]
    return "\r\n".join(headers).encode("ascii") + body


def handle_client(sock: socket.socket) -> None:
    """Serve sequential or pipelined requests until the client closes."""
    buffer = b""
    sock.settimeout(60.0)
    with sock:
        while True:
            try:
                request, buffer = _read_request(sock, buffer)
            except (BadRequest, socket.timeout) as exc:
                if isinstance(exc, BadRequest):
                    body = f"400 Bad Request: {exc}\n".encode("utf-8")
                    try:
                        sock.sendall(_response(400, body, close=True))
                    except OSError:
                        pass
                return
            except (ConnectionError, OSError):
                return

            if request is None:
                return

            close = request.headers.get("connection", "").lower() == "close"
            if request.method != "GET":
                status = 405
                body = b"405 Method Not Allowed\n"
            else:
                status, body = calculate(request.target)

            try:
                sock.sendall(_response(status, body, close=close))
            except (ConnectionError, OSError):
                return
            if close:
                return


def serve(host: str, port: int) -> None:
    """Listen forever, handling each TCP connection in a daemon thread."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen()
        print(f"Calculator listening on http://{host or '0.0.0.0'}:{port}", flush=True)
        while True:
            client, _ = listener.accept()
            threading.Thread(target=handle_client, args=(client,), daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="persistent HTTP/1.1 calculator")
    parser.add_argument("port", nargs="?", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: raise_exit())
    try:
        serve(args.host, args.port)
    except KeyboardInterrupt:
        pass


def raise_exit() -> None:
    raise SystemExit(0)


if __name__ == "__main__":
    main()
