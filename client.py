from __future__ import annotations

import argparse
import base64
import json
import platform
import ctypes
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


CREATE_NO_WINDOW = 0x08000000
APP_NAME = "vfcode"


@dataclass
class Config:
    server: str
    token: str | None
    interval: float
    show_code: bool


class Client:
    def __init__(
        self,
        config: Config,
        *,
        on_status: Callable[[str], None] | None = None,
        on_code: Callable[[dict], None] | None = None,
    ) -> None:
        self.config = config
        self.on_status = on_status or (lambda _status: None)
        self.on_code = on_code or (lambda _message: None)
        self.stop_event = threading.Event()
        self.last_id = 0
        self.last_code: str | None = None

    def stop(self) -> None:
        self.stop_event.set()

    def run_forever(self) -> None:
        self.on_status("running")
        if run_websocket_loop(self):
            return

        self.on_status("websocket unavailable, polling")
        while not self.stop_event.is_set():
            try:
                self.poll_once()
                self.on_status("connected")
            except Exception as exc:
                self.on_status(f"error: {exc}")
            self.stop_event.wait(self.config.interval)

    def poll_once(self) -> dict | None:
        message = fetch_latest(self.config.server, self.config.token)
        if not message:
            return None
        return self.handle_message(message)

    def handle_message(self, message: dict) -> dict:
        if not message:
            return message

        message_id = int(message.get("id") or 0)
        code = message.get("code")
        if message_id <= self.last_id or not code:
            return message

        self.last_id = message_id
        self.last_code = str(code)
        copy_to_clipboard(self.last_code)
        notify_code(message, show_code=self.config.show_code)
        self.on_code(message)
        return message


def run_websocket_loop(client: Client) -> bool:
    try:
        import websocket
    except Exception as exc:
        client.on_status(f"websocket dependency missing: {exc}")
        return False

    while not client.stop_event.is_set():
        ws_url = build_ws_url(client.config.server)
        headers = []
        if client.config.token:
            headers.append(f"X-VFCode-Token: {client.config.token}")
        client.on_status("connecting websocket")
        try:
            ws = websocket.create_connection(ws_url, timeout=15, header=headers)
            ws.settimeout(1)
            client.on_status("connected websocket")
            try:
                while not client.stop_event.is_set():
                    try:
                        raw = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue
                    if not raw:
                        break
                    handle_ws_payload(client, raw)
            finally:
                ws.close()
        except Exception as exc:
            client.on_status(f"websocket error: {exc}")
            client.stop_event.wait(max(1.0, client.config.interval))

    return True


def build_ws_url(server: str) -> str:
    parsed = urllib.parse.urlparse(server)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = append_url_path(parsed.path, "ws")
    return urllib.parse.urlunparse((scheme, parsed.netloc, path, "", parsed.query, ""))


def handle_ws_payload(client: Client, raw: str) -> None:
    if raw == "pong":
        return
    payload = json.loads(raw)
    message = payload.get("message") if isinstance(payload, dict) else None
    if isinstance(message, dict):
        client.handle_message(message)


def fetch_latest(server: str, token: str | None = None) -> dict | None:
    url = api_url(server, "latest")
    headers = {"User-Agent": "vfcode-client"}
    if token:
        headers["X-VFCode-Token"] = token

    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error") or "server returned error")
    return payload.get("message")


def latest_browser_url(server: str, token: str | None = None) -> str:
    url = api_url(server, "latest")
    if not token:
        return url

    parsed = urllib.parse.urlparse(url)
    params = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    params.append(("token", token))
    return urllib.parse.urlunparse(
        parsed._replace(query=urllib.parse.urlencode(params))
    )


def api_url(server: str, path_part: str) -> str:
    parsed = urllib.parse.urlparse(server)
    path = append_url_path(parsed.path, path_part)
    return urllib.parse.urlunparse(parsed._replace(path=path))


def append_url_path(base_path: str, path_part: str) -> str:
    return (base_path.rstrip("/") + "/" + path_part.lstrip("/")) or "/" + path_part.lstrip("/")


def copy_to_clipboard(text: str) -> None:
    try:
        import pyperclip

        pyperclip.copy(text)
        return
    except Exception:
        pass

    if platform.system() == "Windows":
        subprocess.run(["clip.exe"], input=text, text=True, check=True, creationflags=CREATE_NO_WINDOW)
        return

    raise RuntimeError("No clipboard backend available. Install pyperclip.")


def notify_code(message: dict, *, show_code: bool) -> None:
    code = str(message.get("code") or "")
    sender = message.get("from") or "vfcode"
    title = "收到验证码"
    body = f"{sender}: {code} 已复制到剪贴板" if show_code else f"{sender}: 验证码已复制到剪贴板"

    if try_winotify(title, body):
        return
    if try_windows_balloon(title, body):
        return

    print(f"{title}: {body}", flush=True)


def try_winotify(title: str, body: str) -> bool:
    if platform.system() != "Windows":
        return False
    try:
        from winotify import Notification

        Notification(app_id=APP_NAME, title=title, msg=body).show()
        return True
    except Exception:
        return False


def try_windows_balloon(title: str, body: str) -> bool:
    if platform.system() != "Windows":
        return False

    script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Information
$notify.BalloonTipTitle = {ps_quote(title)}
$notify.BalloonTipText = {ps_quote(body)}
$notify.Visible = $true
$notify.ShowBalloonTip(3000)
Start-Sleep -Milliseconds 3500
$notify.Dispose()
"""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-EncodedCommand", encoded],
            creationflags=CREATE_NO_WINDOW,
        )
        return True
    except Exception:
        return False


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def show_error(title: str, message: str) -> None:
    if platform.system() == "Windows":
        try:
            ctypes.windll.user32.MessageBoxW(None, message, title, 0x00000010)
            return
        except Exception:
            pass
    print(f"{title}: {message}", file=sys.stderr)


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", app_dir()))
    return base / relative


def load_config(path: Path | None) -> dict[str, Any]:
    candidates: list[Path] = []
    if path:
        candidates.append(path)
    candidates.extend(
        [
            app_dir() / "client_config.json",
            Path.cwd() / "client_config.json",
        ]
    )

    for candidate in candidates:
        if not candidate.exists():
            continue
        with candidate.open("r", encoding="utf-8") as file:
            value = json.load(file)
        if not isinstance(value, dict):
            raise ValueError(f"Config must be a JSON object: {candidate}")
        return value
    return {}


def resolve_config(args: argparse.Namespace) -> Config:
    file_config = load_config(args.config)
    server = args.server or file_config.get("server")
    if not server:
        raise ValueError("Missing server. Set --server or create client_config.json next to the exe.")

    token = args.token if args.token is not None else file_config.get("token")
    interval = args.interval if args.interval is not None else file_config.get("interval", 1.5)
    show_code = args.show_code or bool(file_config.get("show_code", False))

    return Config(
        server=str(server),
        token=str(token) if token else None,
        interval=float(interval),
        show_code=show_code,
    )


def run_console(config: Config) -> int:
    client = Client(
        config,
        on_status=lambda status: print(f"[status] {status}", flush=True),
        on_code=lambda message: print(
            f"[code] #{message.get('id')} {message.get('code')} copied",
            flush=True,
        ),
    )
    try:
        client.run_forever()
    except KeyboardInterrupt:
        client.stop()
    return 0


def run_tray(config: Config) -> int:
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception as exc:
        print(f"Tray unavailable: {exc}", file=sys.stderr)
        print("Falling back to console mode.", file=sys.stderr)
        return run_console(config)

    status = {"text": "starting", "last_code": "", "server": config.server}
    client = Client(
        config,
        on_status=lambda value: status.update(text=value),
        on_code=lambda message: status.update(last_code=str(message.get("code") or "")),
    )

    def make_icon() -> Image.Image:
        image = Image.new("RGBA", (64, 64), (28, 31, 36, 255))
        draw = ImageDraw.Draw(image)
        draw.ellipse((6, 6, 58, 58), fill=(22, 119, 255, 255), outline=(255, 255, 255, 255), width=2)
        draw.rounded_rectangle((18, 28, 46, 47), radius=6, fill=(255, 255, 255, 255))
        draw.rounded_rectangle((23, 18, 41, 33), radius=6, fill=(255, 255, 255, 255))
        draw.rectangle((27, 23, 37, 30), fill=(22, 119, 255, 255))
        draw.rectangle((27, 36, 37, 39), fill=(22, 119, 255, 255))
        return image

    def copy_last(_icon, _item) -> None:
        if status["last_code"]:
            copy_to_clipboard(status["last_code"])
            notify_code({"code": status["last_code"], "from": "vfcode"}, show_code=config.show_code)

    def open_latest(_icon, _item) -> None:
        webbrowser.open(latest_browser_url(config.server, config.token))

    def quit_app(icon, _item) -> None:
        client.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(lambda _item: f"服务器: {status['server']}", None, enabled=False),
        pystray.MenuItem(lambda _item: f"状态: {status['text']}", None, enabled=False),
        pystray.MenuItem(lambda _item: f"最近验证码: {status['last_code'] or '-'}", None, enabled=False),
        pystray.MenuItem("复制最近验证码", copy_last),
        pystray.MenuItem("打开 latest", open_latest),
        pystray.MenuItem("退出", quit_app),
    )
    icon = pystray.Icon("vfcode", make_icon(), "vfcode", menu)

    def setup(_icon) -> None:
        _icon.visible = True
        threading.Thread(target=client.run_forever, daemon=True).start()
        threading.Thread(target=refresh_menu, args=(_icon,), daemon=True).start()

    def refresh_menu(icon) -> None:
        while icon.visible and not client.stop_event.is_set():
            time.sleep(1)
            try:
                icon.update_menu()
            except Exception:
                return

    icon.run(setup=setup)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run vfcode desktop client.")
    parser.add_argument("--server", default=None, help="Server base URL, for example http://1.2.3.4:8765")
    parser.add_argument("--token", default=None, help="Optional server token.")
    parser.add_argument("--interval", type=float, default=None, help="Polling interval seconds.")
    parser.add_argument("--config", type=Path, default=None, help="Path to client_config.json.")
    parser.add_argument("--show-code", action="store_true", help="Show code in notification text.")
    parser.add_argument("--no-tray", action="store_true", help="Run in console mode.")
    parser.add_argument("--once", action="store_true", help="Fetch once, copy if new, then exit.")
    args = parser.parse_args()

    try:
        config = resolve_config(args)
    except Exception as exc:
        show_error("vfcode 配置错误", str(exc))
        return 2

    if args.once:
        client = Client(
            config,
            on_code=lambda message: print(f"copied #{message.get('id')} {message.get('code')}", flush=True),
        )
        message = client.poll_once()
        if not message:
            print("no code yet", flush=True)
        return 0

    if args.no_tray:
        return run_console(config)
    return run_tray(config)


if __name__ == "__main__":
    raise SystemExit(main())
