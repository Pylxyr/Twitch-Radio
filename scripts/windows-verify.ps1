<#
.SYNOPSIS
  Checks the small ffmpeg (and, optionally, the built installer) on a real Windows machine and
  prints a report you can paste back.

.DESCRIPTION
  What it does, in order. Nothing is installed system-wide and nothing outside this repo checkout,
  %TEMP% and the install you point it at is touched.

    1. Facts about the machine (Windows version, PowerShell, Python, Node).
    2. ffmpeg.exe: size, SHA-256, whether it needs any DLL that is not part of Windows (it is run
       with an empty PATH), its configure line.
    3. scripts\check_ffmpeg.py against it, in a throw-away virtual environment under %TEMP%:
       every component the app uses, the app's own decoder/encoder command lines against every
       container YouTube serves (over local HTTP and local HTTPS), and a real HTTPS download
       through Schannel and the Windows certificate store.
       With -YoutubeUrl it also resolves a real YouTube video with yt-dlp and decodes 12 s of it the way
       the app does, with and without certificate checking.
    4. (-Installer) a silent install into %TEMP%, a launch check, an uninstall, and whether the
       settings folder was kept (the default) - the part of the uninstaller that asks about
       deleting settings is a dialog and is listed under "by hand" below, not automated.

  Run from the repository root in a normal (non-admin) PowerShell:

    powershell -ExecutionPolicy Bypass -File scripts\windows-verify.ps1
    powershell -ExecutionPolicy Bypass -File scripts\windows-verify.ps1 -YoutubeUrl https://www.youtube.com/watch?v=jNQXAC9IVRw
    powershell -ExecutionPolicy Bypass -File scripts\windows-verify.ps1 -Installer "dist\app\Twitch Radio Setup 1.0.2.exe"

  The report is also saved to windows-verify-report.txt in the folder you ran it from.

.PARAMETER Ffmpeg
  Path to ffmpeg.exe. Default: packaging\bin\ffmpeg.exe.

.PARAMETER YoutubeUrl
  A YouTube video to resolve and decode (needs internet; Node.js on PATH helps yt-dlp).

.PARAMETER Installer
  Path to "Twitch Radio Setup x.y.z.exe". Enables step 4.
#>
param(
  [string]$Ffmpeg = "packaging\bin\ffmpeg.exe",
  [string]$YoutubeUrl = "",
  [string]$Installer = ""
)

$ErrorActionPreference = "Continue"
$repo = (Get-Location).Path
$report = Join-Path $repo "windows-verify-report.txt"
"" | Set-Content $report

function Say([string]$text) { Write-Host $text; Add-Content -Path $report -Value $text }
function Section([string]$title) { Say ""; Say "=== $title ==="; }
function Run-Captured([string]$file, [string[]]$arguments) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $file; $psi.Arguments = (($arguments | ForEach-Object { '"' + $_ + '"' }) -join " ")
  $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.UseShellExecute = $false
  $p = [System.Diagnostics.Process]::Start($psi)
  $out = $p.StandardOutput.ReadToEnd() + $p.StandardError.ReadToEnd()
  $p.WaitForExit()
  return @{ Code = $p.ExitCode; Text = $out }
}

Section "1. Machine"
$os = Get-CimInstance Win32_OperatingSystem
Say ("Windows : {0} (build {1}, {2})" -f $os.Caption, $os.BuildNumber, $os.OSArchitecture)
Say ("PowerShell: {0}" -f $PSVersionTable.PSVersion)
$python = Get-Command python -ErrorAction SilentlyContinue
if ($python) { Say ("Python  : " + (& python --version 2>&1)) } else { Say "Python  : NOT FOUND (needed for step 3; install 3.11+ from python.org)" }
$node = Get-Command node -ErrorAction SilentlyContinue
if ($node) { Say ("Node    : " + (& node --version 2>&1)) } else { Say "Node    : not found (only matters for -YoutubeUrl)" }
Say ("Repo    : " + $repo)
try { Say ("Git     : " + (& git rev-parse --short HEAD 2>$null)) } catch { }

Section "2. ffmpeg.exe"
if (-not (Test-Path $Ffmpeg)) { Say "FAIL: $Ffmpeg not found. Download the 'ffmpeg-windows-x64' artifact from CI into packaging\bin, or pass -Ffmpeg."; exit 1 }
$Ffmpeg = (Resolve-Path $Ffmpeg).Path
$item = Get-Item $Ffmpeg
Say ("File    : {0}" -f $Ffmpeg)
Say ("Size    : {0:N1} MB" -f ($item.Length / 1MB))
Say ("SHA-256 : " + (Get-FileHash $Ffmpeg -Algorithm SHA256).Hash)
# An empty PATH (only the Windows folder) proves it needs no DLL shipped separately.
$savedPath = $env:PATH
$env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
$ver = Run-Captured $Ffmpeg @("-hide_banner", "-version")
$env:PATH = $savedPath
Say ("Runs with only the Windows folder on PATH: " + $(if ($ver.Code -eq 0) { "yes" } else { "NO (exit $($ver.Code))" }))
($ver.Text -split "`r?`n" | Select-Object -First 3) | ForEach-Object { Say ("  " + $_) }
$sig = Get-AuthenticodeSignature $Ffmpeg
Say ("Authenticode signature: " + $sig.Status + " (unsigned is expected for the ffmpeg binary itself)")

Section "3. scripts\check_ffmpeg.py (components, app command lines, local HTTP + HTTPS, real HTTPS)"
if (-not $python) { Say "SKIPPED: no Python." }
else {
  $venv = Join-Path $env:TEMP "twitch-radio-verify-venv"
  if (-not (Test-Path "$venv\Scripts\python.exe")) {
    Say "Creating a throw-away virtual environment in $venv (one-off, a minute or two)..."
    & python -m venv $venv 2>&1 | Out-Null
    & "$venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --require-hashes -r requirements-build.lock 2>&1 | Select-Object -Last 3 | ForEach-Object { Say "  pip: $_" }
    & "$venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q trustme==1.2.1 2>&1 | Select-Object -Last 3 | ForEach-Object { Say "  pip: $_" }
  }
  $httpsUrl = "https://raw.githubusercontent.com/Pylxyr/Twitch-Radio/main/tests/fixtures/audio/tone.webm"
  $sha = (& git rev-parse HEAD 2>$null)
  if ($sha) { $httpsUrl = "https://raw.githubusercontent.com/Pylxyr/Twitch-Radio/$sha/tests/fixtures/audio/tone.webm" }
  $pyArgs = @("scripts\check_ffmpeg.py", $Ffmpeg, "--require-tls", "--https-url", $httpsUrl)
  if ($YoutubeUrl) { $pyArgs += @("--youtube-url", $YoutubeUrl) }
  Say ("Command : python " + ($pyArgs -join " "))
  Say "(The real-HTTPS line needs the commit to be pushed to GitHub; if it is not, that one line fails with a 404 and nothing else is affected.)"
  $result = Run-Captured "$venv\Scripts\python.exe" $pyArgs
  ($result.Text -split "`r?`n") | ForEach-Object { Say $_ }
  Say ("check_ffmpeg.py exit code: " + $result.Code)
}

if ($Installer) {
  Section "4. Installer: silent install, launch, uninstall, settings kept"
  if (-not (Test-Path $Installer)) { Say "FAIL: installer not found: $Installer" }
  else {
    $Installer = (Resolve-Path $Installer).Path
    Say ("Installer: {0} ({1:N1} MB)" -f $Installer, ((Get-Item $Installer).Length / 1MB))
    Say ("Signature: " + (Get-AuthenticodeSignature $Installer).Status)
    $target = Join-Path $env:TEMP "twitch-radio-verify-install"
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    $t0 = Get-Date
    $p = Start-Process -FilePath $Installer -ArgumentList "/S", "/D=$target" -Wait -PassThru
    Say ("Silent install exit code {0} in {1:N0} s" -f $p.ExitCode, ((Get-Date) - $t0).TotalSeconds)
    $installedFfmpeg = Get-ChildItem $target -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($installedFfmpeg) {
      Say ("Installed ffmpeg.exe: {0} ({1:N1} MB), identical to the one checked above: {2}" -f $installedFfmpeg.FullName, ($installedFfmpeg.Length / 1MB), ((Get-FileHash $installedFfmpeg.FullName).Hash -eq (Get-FileHash $Ffmpeg).Hash))
    } else { Say "FAIL: no ffmpeg.exe in the installed folder" }
    $installedSize = (Get-ChildItem $target -Recurse -File | Measure-Object Length -Sum).Sum
    Say ("Installed size on disk: {0:N0} MB" -f ($installedSize / 1MB))
    Say ("Notices shipped: " + [bool](Get-ChildItem $target -Recurse -Filter "THIRD_PARTY_NOTICES.txt" -ErrorAction SilentlyContinue))
    # A settings folder with a fake token, to prove the uninstaller leaves it alone by default.
    $data = Join-Path $env:APPDATA "TwitchRadio"
    $hadData = Test-Path $data
    New-Item -ItemType Directory -Force -Path "$data\data" | Out-Null
    "{}" | Set-Content "$data\data\verify-marker.txt"
    $exe = Get-ChildItem $target -Filter "*.exe" | Where-Object { $_.Name -notmatch "Uninstall" } | Select-Object -First 1
    if ($exe) {
      $app = Start-Process -FilePath $exe.FullName -PassThru
      Start-Sleep -Seconds 8
      Say ("App still running 8 s after launch: " + (-not $app.HasExited))
      Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($target) } | Stop-Process -Force -ErrorAction SilentlyContinue
      Start-Sleep -Seconds 2
    }
    $un = Get-ChildItem $target -Filter "Uninstall*.exe" | Select-Object -First 1
    if ($un) {
      $p = Start-Process -FilePath $un.FullName -ArgumentList "/S", "_?=$target" -Wait -PassThru
      Say ("Silent uninstall exit code {0}" -f $p.ExitCode)
    } else { Say "FAIL: no uninstaller found" }
    Say ("Settings folder kept by a silent uninstall (expected: True): " + (Test-Path "$data\data\verify-marker.txt"))
    Remove-Item "$data\data\verify-marker.txt" -Force -ErrorAction SilentlyContinue
    if (-not $hadData) { Remove-Item $data -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path $target) { Remove-Item $target -Recurse -Force -ErrorAction SilentlyContinue }
  }
}

Section "By hand (cannot be automated - please tell me what you see)"
Say "a) Run the installer normally. Does the wizard show the licence/notices page before the folder page, and does Next work?"
Say "b) Uninstall from Settings > Apps. Does the dialog ask whether to also delete settings and Twitch tokens, with No as the default button? Try Yes once on a test install: is %APPDATA%\TwitchRadio gone afterwards?"
Say "c) Start the app, sign in, request a song with !sr from your channel, listen through OBS for a full song. Any stutter, silence or error in the app's log?"
Say "d) !sq (and !songqueue), !skip as the requester, !skip as a different viewer (should be refused), !radio, !radio off as you (broadcaster) and as a mod (mod should be refused)."
Say "e) Task Manager while a song plays: CPU and memory of ffmpeg.exe, TwitchRadioCore.exe and the app (rough numbers are fine)."

Section "Done"
Say ("Report saved to: " + $report)
Say "Paste the whole report here."
