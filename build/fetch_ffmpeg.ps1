# Downloads a static Windows ffmpeg and places ffmpeg.exe in vendor/ so the
# PyInstaller build bundles it *inside* ProctorClient.exe. Students then need
# nothing installed — ffmpeg travels inside the single exe.
#
# Idempotent: if vendor\ffmpeg.exe already exists it does nothing. Run manually
# with:  powershell -ExecutionPolicy Bypass -File build\fetch_ffmpeg.ps1
$ErrorActionPreference = 'Stop'

$repoRoot  = Split-Path -Parent $PSScriptRoot
$vendorDir = Join-Path $repoRoot 'vendor'
$vendorExe = Join-Path $vendorDir 'ffmpeg.exe'

if (Test-Path $vendorExe) {
    Write-Host "[ffmpeg] already present at vendor\ffmpeg.exe - skipping download"
    exit 0
}

# Static build that already includes libx264 (an H.264 encoder). "essentials"
# is the small variant; it has everything the client needs.
$url     = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
$zip     = Join-Path $env:TEMP 'ffmpeg-proctor.zip'
$extract = Join-Path $env:TEMP 'ffmpeg-proctor-extract'

Write-Host "[ffmpeg] downloading $url ..."
Invoke-WebRequest -Uri $url -OutFile $zip

if (Test-Path $extract) { Remove-Item -Recurse -Force $extract }
Write-Host "[ffmpeg] extracting ..."
Expand-Archive -Path $zip -DestinationPath $extract -Force

$found = Get-ChildItem -Path $extract -Recurse -Filter 'ffmpeg.exe' |
         Select-Object -First 1
if (-not $found) { throw "ffmpeg.exe not found inside the downloaded archive" }

New-Item -ItemType Directory -Force -Path $vendorDir | Out-Null
Copy-Item $found.FullName $vendorExe -Force
Write-Host "[ffmpeg] placed -> vendor\ffmpeg.exe"

Remove-Item -Force $zip -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force $extract -ErrorAction SilentlyContinue
