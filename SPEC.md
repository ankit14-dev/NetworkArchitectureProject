# Protocol Specification: Binary HTTP (B-HTTP/1.0)

**Status**: Standard Specification  
**Scope**: Course Project • Two Tracks, One Protocol  
**Document Length**: Two Pages  

---

## 1. Overview & Architecture

B-HTTP/1.0 is an application-layer binary protocol for transporting hypermedia resources over a persistent TCP connection. It replaces text-based HTTP/1.1 message parsing with deterministic, fixed-header binary framing and lightweight indexed header compression inspired by HTTP/2 and HPACK.

### 1.1 Connection Lifecycle
- A client establishes a single, persistent TCP connection to a server (`host:port`).
- **Single Connection Guarantee**: The client **MUST NOT** open secondary TCP connections for subsequent requests.
- **Connection Keep-Alive**: The server **MUST** keep the TCP connection open after fulfilling a request, continuing to accept further request frames until the peer terminates the connection or an unrecoverable framing error occurs.

---

## 2. Frame Format

All communication consists of discrete binary frames. Every frame begins with an identical, fixed-size **9-byte header** formatted in network byte order (big-endian), followed by a variable-length payload.

```
 +---------------------------------------------------------------+
 |                        Length (24)                            |
 +-------------------------------+-------------------------------+
 |          Type (8)             |            Flags (8)          |
 +-+-----------------------------+-------------------------------+
 |R|                       Stream ID (31)                        |
 +-+-------------------------------------------------------------+
 |                         Payload (*) ...                       |
 +---------------------------------------------------------------+
```

### 2.1 Frame Header Fields & Width Defense
1. **Length (24 bits / 3 bytes, unsigned int)**:
   - Specifies the length of the payload in bytes ($0 \le \text{Length} \le 16,777,215$). The 9-byte header is *not* included in this count.
   - *Design Defense*: 16 bits (64 KB) is too constrained for modern web assets (requiring excessive frame fragmentation). Conversely, 32 bits (4 GB) exposes servers to memory-exhaustion Denial-of-Service attacks by allowing malicious peers to claim enormous frame buffer allocations. 24 bits (~16 MB max frame) balances throughput and resource defense.
2. **Type (8 bits / 1 byte, unsigned int)**:
   - Identifies the format and semantic role of the frame payload.
   - *Design Defense*: Provides an extensible namespace of 256 distinct frame types.
3. **Flags (8 bits / 1 byte, bit field)**:
   - Assigns 8 boolean modifiers specific to the frame type.
4. **Reserved Bit `R` (1 bit)**:
   - Must be set to `0` on transmission and ignored upon receipt.
5. **Stream Identifier (31 bits / unsigned int, big-endian)**:
   - Identifies the logical request-response flow. Client-initiated requests use monotonically increasing odd integers ($1, 3, 5\dots$). Control frames use Stream ID `0`.
   - *Design Defense*: 31 bits supports up to $2^{31}-1 \approx 2.14$ billion distinct streams over a long-lived connection without identifier rollover.

### 2.2 Forward Compatibility & Unknown Frame Rule
> **MANDATORY RULE**: A receiver (client or server) encountering a frame with an unrecognized `Type` **MUST skip it cleanly**.

To skip an unknown frame:
1. Parse the 9-byte frame header and extract the 24-bit `Length`.
2. Read and discard exactly `Length` bytes from the TCP socket.
3. Resume parsing immediately at the subsequent frame header.
The connection **MUST NOT** be closed or treated as errored upon encountering an unknown frame type.

---

## 3. Defined Frame Types & Flags

| Type ID | Name | Description | Defined Flags |
| :---: | :--- | :--- | :--- |
| `0x00` | **DATA** | Conveys arbitrary application payload/body bytes. | `0x01` (`END_STREAM`) |
| `0x01` | **HEADERS** | Conveys compressed request or response metadata. | `0x01` (`END_STREAM`) |
| `0x07` | **GOAWAY** | Informs peer of graceful session termination. | None (`0x00`) |

### Flag Definitions
- **`END_STREAM (0x01)` (Bit 0)**:
  - When set on `HEADERS`: Indicates the message contains no subsequent payload/body (e.g., standard `GET` requests).
  - When set on `DATA`: Indicates this is the final payload chunk for the given stream.

---

## 4. Header Compression (HPACK Mechanisms)

Headers are serialized into the payload of a `HEADERS` frame using two mechanisms: **Indexed Names** and **Length-Prefixed Literals**.

### 4.1 Header Block Binary Layout
```
[NumHeaders: 1 byte (uint8)]
Followed by NumHeaders sequential header entries:
  [Name Identifier: 1 byte]
    ├─ If Bit 7 == 1 (0x80 | Index): Static Table Index (1 <= Index <= 10)
    └─ If Bit 7 == 0 (0x00 | NameLen): Literal name follows for NameLen ASCII bytes
  [Value Length: 2 bytes (uint16, big-endian)]
  [Value Bytes: Value Length bytes (UTF-8 string)]
```

### 4.2 Static Header Name Table
| Index | Header Name | Semantics |
| :---: | :--- | :--- |
| `1` | `:method` | Pseudo-header: Request method (e.g. `GET`). Must be uppercase. |
| `2` | `:path` | Pseudo-header: Target resource path (e.g. `/index.html`). |
| `3` | `:status` | Pseudo-header: 3-digit response status code (e.g. `200`, `404`, `400`). |
| `4` | `content-type` | MIME type of payload (e.g. `text/html; charset=utf-8`). |
| `5` | `content-length` | Decimal byte count of payload body. |
| `6` | `user-agent` | Client software identifier. |
| `7` | `host` | Target host and port (`hostname:port`). |
| `8` | `accept` | Acceptable media types for response. |
| `9` | `server` | Server software identifier. |
| `10` | `date` | HTTP-formatted date timestamp (RFC 7231). |

---

## 5. Protocol Exchange & Requirements

### 5.1 Request Exchange (Track 2: Client Requirements)
1. **Target Parsing**: Parse `<host:port/path>`. If path is omitted, default to `/`.
2. **Connection**: Establish single TCP connection to `host:port`.
3. **Frame Construction**: Transmit a single `HEADERS` frame on Stream `1` with flag `END_STREAM (0x01)`.
   - Headers **MUST** include `:method: GET`, `:path: <path>`, and `host: <host:port>`.
4. **Verbose Hexdump (`-v`)**:
   - When `-v` is provided, print canonical hexdump (`hexdump -C` format) to `stderr` for every transmitted and received frame (including unknown extension frames).
5. **Output & Exit Codes**:
   - Write received body bytes from `DATA` frames directly to `stdout`.
   - If response `:status` is `2xx` or `3xx`, exit `0`.
   - If response `:status` is `4xx` or `5xx`, exit non-zero (`1`).
6. **No Secondary Connections**: Never open a second socket connection.

### 5.2 Server Processing (Track 1: Server Requirements)
1. **Invocation**: Listen on TCP port `<port>`, serving directory `<root_dir>`.
2. **Request Validation**:
   - If a frame header is corrupted or exceeds buffer bounds: reply `400 Bad Request`.
   - If headers lack `:path` or contain unknown pseudo-headers: reply `400 Bad Request`.
3. **Path Resolution & Security**:
   - Map `:path` relative to `<root_dir>`. A path of `/` maps to `<root_dir>/index.html`.
   - Canonicalize and resolve all paths. If a path escapes `<root_dir>` (e.g., via `../`), reply `400 Bad Request`.
   - If the mapped file does not exist or is not a regular file: reply `404 Not Found`.
4. **Response Transmission**:
   - Reply with a `HEADERS` frame (`:status`, `server`, `date`, `content-type`, `content-length`) on the same `Stream ID`.
   - Reply with one or more `DATA` frames carrying file bytes, with `END_STREAM (0x01)` set on the final frame.
5. **Keep-Alive**: Keep the TCP socket open and loop to process subsequent frames.

---

## 6. Error Reference & Status Codes

- **`200 OK`**: File located and transmitted successfully.
- **`400 Bad Request`**: Malformed frame header/payload, unsupported HTTP method, missing pseudo-headers, or illegal path traversal syntax.
- **`404 Not Found`**: Target file does not exist within the document root.
- **`500 Internal Server Error`**: Unexpected filesystem I/O exception.
