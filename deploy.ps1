<#
Copies the twin to the Klipper Raspberry Pi and installs it there as a service (runs pi/install.sh on the Pi).

Usage (PowerShell, in this folder):
    .\deploy.ps1                       # host from config.toml, user "pi"
    .\deploy.ps1 -PiUser baturay -WithState -Samba

-WithState  also copies the learned state (twin_state.json, maintenance log, session history), so the Pi continues
            where the PC left off. Use it once, on the first deploy, with the PC twin stopped; after that the Pi is
            the master and the PC twin should not run.
-Samba      shares the twin's folder on the Pi read-only, so the Unity viewer on the PC can read the logs.
Needs the Windows OpenSSH client (ssh, scp) and SSH access to the Pi; sudo on the Pi asks for its password.
#>
param(
    [string]$PiHost = "",
    [string]$PiUser = "pi",
    [switch]$WithState,
    [switch]$Samba
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $PiHost) {
    $m = Select-String -Path config.toml -Pattern '^\s*printer_host\s*=\s*"([^":/]+)' -ErrorAction SilentlyContinue
    if (-not $m) { throw "No -PiHost given and no printer_host in config.toml" }
    $PiHost = $m.Matches[0].Groups[1].Value
}
$target = "$PiUser@$PiHost"
$remote = "ender3-twin"

$files = @("realtime_websocket.py", "bed_model.py", "evaluate.py", "fault_classifier.py", "gcode_plan.py",
           "closed_loop.py", "session_history.py", "requirements.txt", "config.example.toml",
           "pi/install.sh", "pi/ender3-twin.service", "yazici/printer.cfg")
# this printer's numbers (profile + calibration), if present; without them the Pi runs on the code defaults
$files += @("profile.toml", "calibration.json", "profile.example.toml", "calibration.example.json", "pi/set_kref.py") | Where-Object { Test-Path $_ }
if ($WithState) {
    $files += @("twin_state.json", "bakim_izleme.csv", "twin_gecmis.csv") | Where-Object { Test-Path $_ }
}

# Stage the files with their relative layout; shell script and unit file get LF line endings (bash fails on CRLF)
$stage = Join-Path $env:TEMP "ender3-twin-deploy"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
foreach ($f in $files) {
    $dst = Join-Path $stage $f
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    if ($f -match '\.(sh|service)$') {
        $text = [IO.File]::ReadAllText((Resolve-Path $f)) -replace "`r`n", "`n"
        [IO.File]::WriteAllText($dst, $text, (New-Object Text.UTF8Encoding $false))
    } else {
        Copy-Item $f $dst
    }
}

Write-Host "== copying $($files.Count) files to ${target}:~/$remote"
ssh $target "mkdir -p ~/$remote"
if ($LASTEXITCODE -ne 0) { throw "ssh to $target failed" }
scp -r "$stage\*" "${target}:~/$remote/"
if ($LASTEXITCODE -ne 0) { throw "scp to $target failed" }

$opt = if ($Samba) { "--samba" } else { "" }
Write-Host "== installing on the Pi (sudo may ask for the Pi password)"
ssh -t $target "bash ~/$remote/pi/install.sh $opt"
