"""
test_bhttp.py - End-to-end integration and protocol verification test suite.
"""

import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path

import bprotocol

BASE_DIR = Path(__file__).resolve().parent
BSERVE = str(BASE_DIR / "bserve")
BCURL = str(BASE_DIR / "bcurl")
WWW_DIR = str(BASE_DIR / "www")
TEST_PORT = 19090


class TestBHTTP(unittest.TestCase):
    server_proc = None

    @classmethod
    def setUpClass(cls):
        # Start bserve in background
        cls.server_proc = subprocess.Popen(
            [sys.executable, BSERVE, WWW_DIR, str(TEST_PORT)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(BASE_DIR)
        )
        # Give server time to bind and listen
        time.sleep(0.5)
        # Verify server is still alive
        if cls.server_proc.poll() is not None:
            stdout, stderr = cls.server_proc.communicate()
            raise RuntimeError(f"Server failed to start:\nStdout: {stdout.decode()}\nStderr: {stderr.decode()}")

    @classmethod
    def tearDownClass(cls):
        if cls.server_proc:
            cls.server_proc.terminate()
            try:
                cls.server_proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                cls.server_proc.kill()

    def test_01_get_index_html(self):
        """Test basic GET /index.html returns 200 OK and matches file content."""
        expected = (Path(WWW_DIR) / "index.html").read_bytes()
        res = subprocess.run(
            [BCURL, f"localhost:{TEST_PORT}/index.html"],
            capture_output=True,
            timeout=5
        )
        self.assertEqual(res.returncode, 0, f"bcurl failed with code {res.returncode}: {res.stderr.decode()}")
        self.assertEqual(res.stdout, expected)

    def test_02_get_hello_txt(self):
        """Test GET /hello.txt returns 200 OK and matches file content."""
        expected = (Path(WWW_DIR) / "hello.txt").read_bytes()
        res = subprocess.run(
            [BCURL, f"localhost:{TEST_PORT}/hello.txt"],
            capture_output=True,
            timeout=5
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, expected)

    def test_03_root_maps_to_index_html(self):
        """Test GET / automatically maps to index.html."""
        expected = (Path(WWW_DIR) / "index.html").read_bytes()
        res = subprocess.run(
            [BCURL, f"localhost:{TEST_PORT}/"],
            capture_output=True,
            timeout=5
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, expected)

    def test_04_404_not_found(self):
        """Test nonexistent file returns 404 and bcurl exits non-zero."""
        res = subprocess.run(
            [BCURL, f"localhost:{TEST_PORT}/does_not_exist.html"],
            capture_output=True,
            timeout=5
        )
        self.assertNotEqual(res.returncode, 0, "bcurl must exit non-zero on 404")
        self.assertIn(b"404 Not Found", res.stdout)

    def test_05_path_traversal_blocked(self):
        """Test path traversal attempts are rejected with 400 Bad Request."""
        res = subprocess.run(
            [BCURL, f"localhost:{TEST_PORT}/../../etc/passwd"],
            capture_output=True,
            timeout=5
        )
        self.assertNotEqual(res.returncode, 0, "bcurl must exit non-zero on 400")
        self.assertIn(b"400 Bad Request", res.stdout)

    def test_06_verbose_hexdump(self):
        """Test -v flag prints hexdumps of every frame to stderr."""
        res = subprocess.run(
            [BCURL, "-v", f"localhost:{TEST_PORT}/hello.txt"],
            capture_output=True,
            timeout=5
        )
        self.assertEqual(res.returncode, 0)
        stderr_text = res.stderr.decode("utf-8")
        self.assertIn(">>> [SENT FRAME]", stderr_text)
        self.assertIn("<<< [RECV FRAME]", stderr_text)
        self.assertIn("Type: HEADERS", stderr_text)
        self.assertIn("Type: DATA", stderr_text)
        self.assertIn("00000000", stderr_text)

    def test_07_server_skips_unknown_frame_cleanly(self):
        """
        Verify server requirement:
        'A receiver meeting a frame type it does not know MUST skip it cleanly.'
        Client sends an unknown frame type (0xAA) with payload, followed immediately
        by a valid GET request frame over the same connection.
        """
        sock = socket.create_connection(("localhost", TEST_PORT), timeout=5)
        try:
            # 1. Send unknown frame: Type 0xAA, Len 11, Flags 0, Stream 1
            unknown_payload = b"UNKNOWN_EXT"
            unknown_frame = bprotocol.pack_frame(0xAA, 0x00, 1, unknown_payload)
            sock.sendall(unknown_frame)

            # 2. Send valid HEADERS frame on the same stream
            headers = [
                (":method", "GET"),
                (":path", "/hello.txt"),
                ("host", f"localhost:{TEST_PORT}"),
            ]
            hdr_bytes = bprotocol.encode_headers(headers)
            req_frame = bprotocol.pack_frame(
                bprotocol.FRAME_HEADERS,
                bprotocol.FLAG_END_STREAM,
                1,
                hdr_bytes
            )
            sock.sendall(req_frame)

            # 3. Read response: should be valid 200 response
            frame1 = bprotocol.read_frame(sock)
            self.assertIsNotNone(frame1)
            ftype1, flags1, stream1, payload1, _ = frame1
            self.assertEqual(ftype1, bprotocol.FRAME_HEADERS)
            decoded_hdrs = dict(bprotocol.decode_headers(payload1))
            self.assertEqual(decoded_hdrs.get(":status"), "200")

            frame2 = bprotocol.read_frame(sock)
            self.assertIsNotNone(frame2)
            ftype2, flags2, stream2, payload2, _ = frame2
            self.assertEqual(ftype2, bprotocol.FRAME_DATA)
            self.assertTrue(flags2 & bprotocol.FLAG_END_STREAM)
            self.assertIn(b"Hello, binary HTTP world!", payload2)
        finally:
            sock.close()

    def test_08_client_skips_unknown_frame_cleanly(self):
        """
        Verify client requirement:
        'A receiver meeting a frame type it does not know MUST skip it cleanly.'
        We spin up a mock server that sends an unknown frame (0xBB) before the real response.
        """
        mock_port = 19091
        mock_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        mock_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        mock_sock.bind(("localhost", mock_port))
        mock_sock.listen(1)

        def mock_server_work():
            conn, _ = mock_sock.accept()
            try:
                # Read client request
                bprotocol.read_frame(conn)
                # Send unknown frame first (Type 0xBB, Length 8)
                conn.sendall(bprotocol.pack_frame(0xBB, 0x00, 1, b"FUTUREV2"))
                # Send real response HEADERS
                hdrs = bprotocol.encode_headers([(":status", "200"), ("content-type", "text/plain")])
                conn.sendall(bprotocol.pack_frame(bprotocol.FRAME_HEADERS, 0x00, 1, hdrs))
                # Send real DATA
                conn.sendall(bprotocol.pack_frame(bprotocol.FRAME_DATA, bprotocol.FLAG_END_STREAM, 1, b"MockSuccess"))
            finally:
                conn.close()

        import threading
        t = threading.Thread(target=mock_server_work, daemon=True)
        t.start()

        # Run bcurl against mock server
        res = subprocess.run(
            [BCURL, "-v", f"localhost:{mock_port}/test"],
            capture_output=True,
            timeout=5
        )
        mock_sock.close()
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, b"MockSuccess")
        self.assertIn("Skipping unknown frame type 0xbb", res.stderr.decode())

    def test_09_keep_alive_multiple_requests(self):
        """
        Verify server requirement: 'and keep the connection open'.
        Send 3 sequential requests over a single TCP connection.
        """
        sock = socket.create_connection(("localhost", TEST_PORT), timeout=5)
        try:
            for i, target_path in enumerate(["/hello.txt", "/index.html", "/hello.txt"], start=1):
                stream_id = 2 * i - 1  # odd stream IDs: 1, 3, 5
                headers = [
                    (":method", "GET"),
                    (":path", target_path),
                    ("host", f"localhost:{TEST_PORT}"),
                ]
                req = bprotocol.pack_frame(
                    bprotocol.FRAME_HEADERS,
                    bprotocol.FLAG_END_STREAM,
                    stream_id,
                    bprotocol.encode_headers(headers)
                )
                sock.sendall(req)

                # Read HEADERS
                f1 = bprotocol.read_frame(sock)
                self.assertIsNotNone(f1)
                self.assertEqual(f1[0], bprotocol.FRAME_HEADERS)
                hdrs = dict(bprotocol.decode_headers(f1[3]))
                self.assertEqual(hdrs.get(":status"), "200")
                self.assertEqual(f1[2], stream_id)

                # Read DATA
                f2 = bprotocol.read_frame(sock)
                self.assertIsNotNone(f2)
                self.assertEqual(f2[0], bprotocol.FRAME_DATA)
                self.assertEqual(f2[2], stream_id)
                self.assertTrue(f2[1] & bprotocol.FLAG_END_STREAM)
        finally:
            sock.close()

    def test_10_malformed_frame_gives_400(self):
        """
        Verify server requirement: '400 if the frame is malformed'.
        Send an invalid header block (corrupted index).
        """
        sock = socket.create_connection(("localhost", TEST_PORT), timeout=5)
        try:
            # Corrupted payload: claim 1 header, but invalid static index (0x80 | 99)
            corrupt_payload = b"\x01\xe3\x00\x03GET"
            req = bprotocol.pack_frame(
                bprotocol.FRAME_HEADERS,
                bprotocol.FLAG_END_STREAM,
                1,
                corrupt_payload
            )
            sock.sendall(req)

            f1 = bprotocol.read_frame(sock)
            self.assertIsNotNone(f1)
            self.assertEqual(f1[0], bprotocol.FRAME_HEADERS)
            hdrs = dict(bprotocol.decode_headers(f1[3]))
            self.assertEqual(hdrs.get(":status"), "400")
        finally:
            sock.close()


if __name__ == "__main__":
    unittest.main()
