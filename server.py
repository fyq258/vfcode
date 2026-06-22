from __future__ import annotations

import argparse
import json
import re
import socket
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.parse import parse_qs

from aiohttp import WSMsgType, web


MAX_BODY_SIZE = 64 * 1024
MAX_WS_CLIENTS = 8
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_POSTS = 30
KEYWORD_CODE_RE = re.compile(
    r"(?i)(?:验证码|校验码|动态码|\bcode\b|\botp\b|\bpin\b|\bcaptcha\b)"
    r"[^A-Za-z0-9]{0,12}([A-Za-z0-9][A-Za-z0-9\s-]{2,16}[A-Za-z0-9])"
)
NUMBER_CODE_RE = re.compile(r"(?<!\d)(\d[\d\s-]{2,12}\d)(?!\d)")


class State:
    def __init__(self, token: str | None = None) -> None:
        self.token = token
        self.next_id = 1
        self.latest: dict | None = None
        self.clients: set[web.WebSocketResponse] = set()
        self.post_times: dict[str, deque[float]] = defaultdict(deque)

    def set_latest(self, message: dict) -> dict:
        message["id"] = self.next_id
        self.next_id += 1
        self.latest = dict(message)
        return dict(message)


def authorized(request: web.Request) -> bool:
    token = request.app["state"].token
    if not token:
        return True
    return token in {request.query.get("token"), request.headers.get("X-VFCode-Token")}


@web.middleware
async def security_headers(request: web.Request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Server"] = "vfcode"
    return response


async def handle_health(_request: web.Request) -> web.Response:
    return json_response({"ok": True})


async def handle_latest(request: web.Request) -> web.Response:
    if not authorized(request):
        return json_response({"ok": False, "error": "unauthorized"}, status=401)
    return json_response({"ok": True, "message": public_message(request.app["state"].latest)})


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    if not authorized(request):
        raise web.HTTPUnauthorized(text="unauthorized")

    state: State = request.app["state"]
    if len(state.clients) >= MAX_WS_CLIENTS:
        raise web.HTTPTooManyRequests(text="too many clients")

    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    state.clients.add(ws)
    if state.latest:
        await ws.send_json({"type": "latest", "message": public_message(state.latest)}, dumps=json_dumps)

    try:
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                if message.data == "ping":
                    await ws.send_str("pong")
            elif message.type == WSMsgType.ERROR:
                break
    finally:
        state.clients.discard(ws)

    return ws


async def handle_post(request: web.Request) -> web.Response:
    if not authorized(request):
        return json_response({"ok": False, "error": "unauthorized"}, status=401)

    if request.path != "/vfcode":
        return json_response({"ok": False, "error": "not found"}, status=404)

    state: State = request.app["state"]
    if is_rate_limited(state, request.remote or "unknown"):
        return json_response({"ok": False, "error": "rate limited"}, status=429)

    body = await request.read()
    if len(body) > MAX_BODY_SIZE:
        return json_response({"ok": False, "error": "body too large"}, status=413)

    body_text = decode_body(body)
    payload = parse_payload(request.headers.get("Content-Type", ""), body_text)

    content = pick_value(payload, "content", "text", "message", "msg", "body") or body_text
    sender = pick_value(payload, "from", "sender", "phone", "address")
    phone_timestamp = pick_value(payload, "timestamp", "time", "date")
    code = extract_code(content)

    message = state.set_latest(
        {
            "received_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "client_ip": request.remote,
            "from": sender,
            "content": content,
            "timestamp": phone_timestamp,
            "code": code,
        }
    )

    print(
        f"[{message['received_at']}] #{message['id']} from={sender or '-'} "
        f"code={code or '-'} clients={len(state.clients)} content_len={len(content)}",
        flush=True,
    )
    await broadcast(state, message)
    return json_response({"ok": True, "message": public_message(message)})


async def broadcast(state: State, message: dict) -> None:
    if not state.clients:
        return

    payload = {"type": "code", "message": public_message(message)}
    dead: list[web.WebSocketResponse] = []
    for ws in list(state.clients):
        try:
            await ws.send_json(payload, dumps=json_dumps)
        except Exception:
            dead.append(ws)

    for ws in dead:
        state.clients.discard(ws)


def decode_body(body: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def parse_payload(content_type: str, body_text: str) -> dict:
    media_type = content_type.split(";", 1)[0].strip().lower()
    stripped = body_text.strip()

    if media_type == "application/x-www-form-urlencoded" or "=" in stripped:
        parsed = parse_qs(stripped, keep_blank_values=True)
        return {key: values[0] if len(values) == 1 else values for key, values in parsed.items()}

    if media_type == "application/json" or stripped.startswith(("{", "[")):
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {"content": str(value)}

    return {"content": body_text}


def pick_value(data: dict, *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            value = value[0] if value else None
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def extract_code(text: str) -> str | None:
    for match in KEYWORD_CODE_RE.finditer(text):
        code = normalize_code(match.group(1))
        if code:
            return code

    for match in NUMBER_CODE_RE.finditer(text):
        code = normalize_code(match.group(1))
        if code:
            return code

    return None


def normalize_code(value: str) -> str | None:
    code = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    if 4 <= len(code) <= 10 and any(char.isdigit() for char in code):
        return code
    return None


def public_message(message: dict | None) -> dict | None:
    if not message:
        return None
    return {
        "id": message.get("id"),
        "received_at": message.get("received_at"),
        "from": message.get("from"),
        "timestamp": message.get("timestamp"),
        "code": message.get("code"),
    }


def is_rate_limited(state: State, key: str) -> bool:
    now = time.monotonic()
    times = state.post_times[key]
    while times and now - times[0] > RATE_LIMIT_WINDOW_SECONDS:
        times.popleft()
    if len(times) >= RATE_LIMIT_MAX_POSTS:
        return True
    times.append(now)
    return False


def json_response(payload: dict, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, dumps=json_dumps)


def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def guess_lan_ip() -> str | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def create_app(token: str | None = None) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY_SIZE, middlewares=[security_headers])
    app["state"] = State(token=token)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/latest", handle_latest)
    app.router.add_get("/ws", handle_ws)
    app.router.add_post("/vfcode", handle_post)
    return app


def main() -> int:
    parser = argparse.ArgumentParser(description="Run vfcode server.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host.")
    parser.add_argument("--port", type=int, default=8765, help="Bind port.")
    parser.add_argument("--token", default=None, help="Optional token for phone and desktop client.")
    args = parser.parse_args()

    lan_ip = guess_lan_ip()
    print(f"vfcode server listening on http://{args.host}:{args.port}", flush=True)
    if lan_ip:
        print(f"Phone POST URL: http://{lan_ip}:{args.port}/vfcode", flush=True)
        print(f"Client HTTP URL: http://{lan_ip}:{args.port}", flush=True)
        print(f"Client WS URL:   ws://{lan_ip}:{args.port}/ws", flush=True)
    if args.token:
        print("Token enabled: use ?token=... or X-VFCode-Token.", flush=True)
    print("Press Ctrl+C to stop.", flush=True)

    web.run_app(create_app(args.token), host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
