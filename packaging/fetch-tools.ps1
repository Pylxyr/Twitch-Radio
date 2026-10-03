# Downloads the external program the bot needs and puts it in packaging/bin,
# from where the installer bundles it:
#   ffmpeg  - audio decoding / Opus encoding
# There is no JavaScript runtime to fetch: the packaged app uses Electron's own
# (see twitch_radio/config.py), which keeps the installer about 29 MB smaller.
# Versions, URLs and SHA-256 checksums are pinned in packaging/tools.lock.json;
# a download whose checksum differs is deleted and the build fails.
# Re-running skips anything already present (use -Force to fetch again).
# Run via build-windows.bat, or directly in CI.
param([switch]$Force)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$bin = Join-Path $PSScriptRoot "bin"
$cache = Join-Path $PSScriptRoot ".cache"
New-Item -ItemType Directory -Force -Path $bin, $cache | Out-Null
$lock = Get-Content -Raw -Path (Join-Path $PSScriptRoot "tools.lock.json") | ConvertFrom-Json

function Get-Verified($tool) {
    $name = [IO.Path]::GetFileName(([Uri]$tool.url).AbsolutePath)
    $zip = Join-Path $cache $name
    if (-not (Test-Path $zip)) {
        Write-Host "Downloading $($tool.url)"
        Invoke-WebRequest -Uri $tool.url -OutFile $zip -UseBasicParsing
    }
    $actual = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash.ToLowerInvariant()
    if ($actual -ne $tool.sha256.ToLowerInvariant()) {
        Remove-Item -Force $zip
        throw "SHA-256 mismatch for $name. Expected $($tool.sha256), got $actual. The file was deleted."
    }
    Write-Host "SHA-256 OK: $name"
    return $zip
}

if ($Force) {
    Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $bin "ffmpeg.exe")
}
# Older builds put deno.exe here; anything in bin\ is bundled, so drop the leftover.
Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $bin "deno.exe")

if (-not (Test-Path (Join-Path $bin "ffmpeg.exe"))) {
    $zip = Get-Verified $lock.ffmpeg
    $tmp = Join-Path $cache "ffmpeg"
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $exe = Get-ChildItem -Path $tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1
    if (-not $exe) { throw "ffmpeg.exe not found inside the downloaded archive." }
    Copy-Item $exe.FullName (Join-Path $bin "ffmpeg.exe")
    $license = Get-ChildItem -Path $tmp -Recurse -Filter LICENSE* | Select-Object -First 1
    if ($license) { Copy-Item $license.FullName (Join-Path $bin "FFMPEG-LICENSE.txt") }
}

$ffmpegLine = (& (Join-Path $bin "ffmpeg.exe") -version | Select-Object -First 1)
Write-Host $ffmpegLine
# A tool already in bin\ (reused from PATH by build-windows.bat) may differ from the pin; say so.
if ($ffmpegLine -notlike "*$($lock.ffmpeg.version)*") { Write-Warning "ffmpeg in packaging\bin is not the pinned $($lock.ffmpeg.version)." }
Write-Host "Tools ready in $bin"
