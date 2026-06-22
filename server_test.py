from __future__ import annotations

import argparse
import json
import re
import socket
import sys
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pprint import pformat
from urllib.parse import parse_qs, urlparse


MAX_BODY_SIZE = 2 * 1024 * 1024
KEYWORD_CODE_RE = re.compile(
    r"(?i)(?:验证码|校验码|动态码|\bcode\b|\botp\b|\bpin\b|\bcaptcha\b)"
    r"[^A-Za-z0-9]{0,12}([A-Za-z0-9][A-Za-z0-9\s-]{2,16}[A-Za-z0-9])"
)
NUMBER_CODE_RE = re.compile(r"(?<!\d)(\d[\d\s-]{2,12}\d)(?!\d)")


class DebugHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if urlparse(self.path).path == "/health":
            self._send_text("ok\n")
            return

        self._send_text(
            "vfcode debug receiver is running.\n"
            "Send HTTP POST requests to any path on this server.\n"
        )

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", "0") or "0")
        if content_length > MAX_BODY_SIZE:
            self._send_json({"ok": False, "error": "body too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return

        body = self.rfile.read(content_length)
        body_text = decode_body(body)
        content_type = self.headers.get("Content-Type", "")
        parsed = parse_body_guess(content_type, body_text)
        candidates = guess_codes(parsed, body_text)
        best_code = candidates[0] if candidates else None

        print_request(
            client_ip=self.client_address[0] if self.client_address else "-",
            path=self.path,
            headers={key: value for key, value in self.headers.items()},
            body=body,
            body_text=body_text,
            parsed=parsed,
            best_code=best_code,
            candidates=candidates,
        )

        self._send_json(
            {
                "ok": True,
                "received_bytes": len(body),
                "path": self.path,
                "best_code": best_code,
                "possible_codes": candidates,
            }
        )

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_text(self, text: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def decode_body(body: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def parse_body_guess(content_type: str, body_text: str):
    media_type = content_type.split(";", 1)[0].strip().lower()
    stripped = body_text.strip()
    if not stripped:
        return None

    if media_type == "application/json" or stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError as exc:
            return {"json_error": str(exc)}

    if media_type == "application/x-www-form-urlencoded" or "=" in stripped:
        parsed = parse_qs(stripped, keep_blank_values=True)
        if parsed:
            return {key: values[0] if len(values) == 1 else values for key, values in parsed.items()}

    return None


def guess_codes(parsed, body_text: str) -> list[str]:
    candidates: list[str] = []
    texts = collect_texts(parsed) or [body_text]

    for text in texts:
        for match in KEYWORD_CODE_RE.finditer(text):
            add_code(candidates, match.group(1))

    for text in texts:
        for match in NUMBER_CODE_RE.finditer(text):
            add_code(candidates, match.group(1))

    return candidates


def collect_texts(value) -> list[str]:
    if isinstance(value, dict):
        preferred_keys = ("content", "text", "message", "msg", "body")
        preferred = [str(value[key]) for key in preferred_keys if key in value and value[key] is not None]
        others = [
            str(child)
            for key, child in value.items()
            if key not in preferred_keys and key.lower() not in {"timestamp", "time", "date"}
            for child in ([child] if not isinstance(child, list) else child)
            if child is not None
        ]
        return preferred + others
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if value is None:
        return []
    return [str(value)]


def add_code(candidates: list[str], value: str) -> None:
    code = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    if 4 <= len(code) <= 10 and any(char.isdigit() for char in code) and code not in candidates:
        candidates.append(code)


def print_request(
    *,
    client_ip: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    body_text: str,
    parsed,
    best_code: str | None,
    candidates: list[str],
) -> None:
    print("\n" + "=" * 80, flush=True)
    print(f"Time: {datetime.now().astimezone().isoformat(timespec='seconds')}", flush=True)
    print(f"Client: {client_ip}", flush=True)
    print(f"Path: {path}", flush=True)
    print("-" * 80, flush=True)
    print("Headers:", flush=True)
    print(pformat(headers, width=120), flush=True)
    print("-" * 80, flush=True)
    print(f"Raw body bytes: {len(body)}", flush=True)
    print("Body text:", flush=True)
    print(body_text if body_text else "<empty>", flush=True)
    print("-" * 80, flush=True)
    print("Parsed guess:", flush=True)
    print(pformat(parsed, width=120) if parsed is not None else "<not json/form>", flush=True)
    print("-" * 80, flush=True)
    print("Best code:", best_code or "<none>", flush=True)
    print("Possible codes:", ", ".join(candidates) if candidates else "<none>", flush=True)
    print("=" * 80 + "\n", flush=True)


def guess_lan_ip() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Print incoming verification-code HTTP POST requests.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host.")
    parser.add_argument("--port", type=int, default=8765, help="Bind port.")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), DebugHandler)
    lan_ip = guess_lan_ip()

    print(f"vfcode debug receiver listening on http://{args.host}:{args.port}", flush=True)
    print(f"Local test: http://127.0.0.1:{args.port}/test", flush=True)
    if lan_ip:
        print(f"Phone URL:  http://{lan_ip}:{args.port}/vfcode", flush=True)
    print("Press Ctrl+C to stop.", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
