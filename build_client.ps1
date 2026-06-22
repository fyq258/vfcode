$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Missing project virtual environment. Run: python -m venv .venv"
}

Get-Process -Name "vfcode-client" -ErrorAction SilentlyContinue | Stop-Process -Force

$iconDir = Join-Path $root "assets"
$iconPath = Join-Path $iconDir "vfcode.ico"
$trayIconPath = Join-Path $iconDir "vfcode-tray.png"
New-Item -ItemType Directory -Force -Path $iconDir | Out-Null

@'
from pathlib import Path
from PIL import Image, ImageDraw

icon_out = Path("assets/vfcode.ico")
tray_out = Path("assets/vfcode-tray.png")
icon_out.parent.mkdir(parents=True, exist_ok=True)

sizes = [16, 24, 32, 48, 64, 128, 256]
images = []
for size in sizes:
    scale = size / 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def box(coords):
        return tuple(round(value * scale) for value in coords)

    radius = max(2, round(9 * scale))
    draw.ellipse(box((6, 6, 58, 58)), fill=(22, 119, 255, 255), outline=(255, 255, 255, 255), width=max(1, round(2 * scale)))
    draw.rounded_rectangle(box((18, 28, 46, 47)), radius=radius, fill=(255, 255, 255, 255))
    draw.rounded_rectangle(box((23, 18, 41, 33)), radius=radius, fill=(255, 255, 255, 255))
    draw.rectangle(box((27, 23, 37, 30)), fill=(22, 119, 255, 255))
    draw.rectangle(box((27, 36, 37, 39)), fill=(22, 119, 255, 255))
    images.append(image)

images[-1].save(icon_out, sizes=[(size, size) for size in sizes], append_images=images[:-1])
images[4].save(tray_out)
print(icon_out)
print(tray_out)
'@ | & $python -

$distDir = Join-Path $root "dist"
$exePath = Join-Path $distDir "vfcode-client.exe"
if (Test-Path -LiteralPath $exePath) {
    Remove-Item -LiteralPath $exePath -Force
}

& $python -m PyInstaller --noconfirm --clean vfcode-client.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$configPath = Join-Path $distDir "client_config.json"
if (-not (Test-Path -LiteralPath $configPath)) {
    Copy-Item -LiteralPath (Join-Path $root "client_config.example.json") -Destination $configPath
}

Write-Host "Built: $(Join-Path $distDir 'vfcode-client.exe')"
Write-Host "Config: $configPath"
