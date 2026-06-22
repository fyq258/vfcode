# vfcode

一个很小的验证码同步工具。

手机端筛选短信验证码后通过 HTTP POST 发到服务器；电脑端运行客户端常驻，发现新验证码后通知并复制到剪贴板。

## 服务端

安装依赖：

```powershell
python -m pip install -r requirements-server.txt
```

启动：

```powershell
python server.py --host 0.0.0.0 --port 8765
```

手机端 POST 地址：

```text
http://服务器IP:8765/vfcode
```

电脑端读取最新验证码：

```text
http://服务器IP:8765/latest
```

电脑端 WebSocket 即时连接：

```text
ws://服务器IP:8765/ws
```

如果服务端暴露在公网，建议加 token：

```powershell
python server.py --host 0.0.0.0 --port 8765 --token your-secret
```

手机端 URL 可以写成：

```text
http://服务器IP:8765/vfcode?token=your-secret
```

电脑客户端也传同一个 token。

## 电脑客户端

控制台常驻模式不需要托盘依赖：

```powershell
python client.py --server http://服务器IP:8765 --no-tray
```

客户端默认使用 WebSocket 即时连接，服务端收到手机 POST 后会立刻推送给电脑端；连接断开时客户端会自动重连。

托盘模式需要安装可选依赖：

```powershell
python -m pip install -r requirements-client.txt
python client.py --server http://服务器IP:8765
```

启用 token：

```powershell
python client.py --server http://服务器IP:8765 --token your-secret
```

默认通知不显示验证码，只提示已经复制。需要在通知文字里显示验证码：

```powershell
python client.py --server http://服务器IP:8765 --show-code
```

双击 exe 时，客户端会读取同目录的 `client_config.json`：

```json
{
  "server": "http://服务器IP:8765",
  "token": "",
  "interval": 1.5,
  "show_code": false
}
```

## 打包客户端 exe

建议使用项目虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
```

然后运行：

```powershell
.\build_client.ps1
```

生成文件：

```text
dist\vfcode-client.exe
dist\client_config.json
```

## 已确认的手机发送格式

当前手机端发来的请求格式是：

```http
Content-Type: application/x-www-form-urlencoded
```

表单字段：

- `from`：短信发送方号码
- `content`：短信正文，验证码在这里
- `timestamp`：时间戳

示例：

```text
from=10694118402477660&content=【小米】小米账号27*****868登录验证码399853，请勿将验证码透露给他人&timestamp=1782117375094
```

这条短信提取出的验证码是 `399853`。

## 调试脚本

如果后面手机端格式又变化，可以运行调试接收器看完整请求：

```powershell
python server_test.py --host 0.0.0.0 --port 8765
```

## 本机测试

启动服务端后，另开终端：

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/vfcode -ContentType "application/x-www-form-urlencoded" -Body "from=10694118402477660&content=验证码399853&timestamp=1782117375094"
Invoke-RestMethod http://127.0.0.1:8765/latest
```
