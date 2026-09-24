# Network Architecture Assignments

This repository contains both socket-programming assignments. Each program is
implemented in Python using the standard library only.

## Assignment 1: Calculator That Stays on the Line

`calcserve` is a small HTTP/1.1 server built directly on a TCP socket. It
supports `GET /add`, `/sub`, `/mul`, and `/div` with `a` and `b` query
parameters. It returns the required 400, 404, and 405 responses and keeps the
same connection open across sequential or pipelined requests.

```bash
./calcserve                 # listens on 127.0.0.1:8080
./calcserve 9001            # choose another port
curl 'http://localhost:8080/add?a=2&b=3'
python3 test_calculator.py
```

The parser reads through `\r\n\r\n`, then consumes exactly the number of body
bytes declared by `Content-Length`. Bytes beyond that boundary remain buffered
for the next request. This is what makes persistent HTTP/1.1 connections safe.

## Assignment 2: HTTP, in Binary (B-HTTP/1.0)

A lightweight binary application-layer protocol and reference implementation inspired by HTTP/2 framing and HPACK header compression.

---

## Deliverables Summary

1. **Protocol Specification (`SPEC.md`)**:
   - A complete, self-contained 2-page specification designed for independent interoperability.
   - Comprehensive justification and defense of the 9-byte header (`24 / 8 / 8 / 31`).
   - HPACK static table (10 headers) + length-prefixed literal headers.
   - Forward compatibility unknown frame skipping rule.
2. **Programs**:
   - **`bserve`**: Binary HTTP server (`./bserve <root_dir> <port>`).
   - **`bcurl`**: Binary HTTP client with verbose framing hexdump (`./bcurl [-v] <host:port/path>`).
   - **`bprotocol.py`**: Shared binary codec and protocol framing utilities.
3. **Annotated Hexdump (`hexdump_annotated.txt`)**:
   - Complete byte-by-byte trace and field interpretation of a full request/response session.
4. **Automated Test Suite (`test_bhttp.py`)**:
   - 10 comprehensive integration and compliance tests.

---

## Quick Start

### 1. Start the Server
```bash
./bserve ./www 9000
```

### 2. Fetch a File with Client
```bash
./bcurl localhost:9000/index.html
```

### 3. Verbose Mode with Frame Hexdumps
```bash
./bcurl -v localhost:9000/index.html
```

### 4. Run the Test Suite
```bash
python3 test_bhttp.py
```

---

## Key Features & Compliance

- **Fixed-Size Frame Header (9 bytes)**:
  - 24-bit Length (~16MB max payload)
  - 8-bit Frame Type (256 frame types)
  - 8-bit Flags (`END_STREAM = 0x01`)
  - 1-bit Reserved + 31-bit Stream Identifier
- **Header Compression**:
  - Top 10 common headers indexed in 1 byte (`0x80 | Index`).
  - Custom / arbitrary headers length-prefixed (`0x00 | NameLen`).
- **Resilient Unknown Frame Handling**:
  - Receivers cleanly skip unknown frame types by reading their length, preserving connection state for future protocol versions.
- **Connection Keep-Alive**:
  - The server keeps the TCP socket open across multiple requests.
  - The client operates over a single TCP connection.
- **Exit Codes**:
  - `bcurl` exits with `0` on successful responses (`2xx`/`3xx`).
  - `bcurl` exits with `1` on error responses (`4xx`/`5xx`).
