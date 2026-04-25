$ErrorActionPreference = "Stop"
$dest = "C:\Users\lenovo\Downloads\AION-Deliverables\MIRA-Windows"

Write-Host ""
Write-Host "  MIRA - llama.cpp Binary Downloader" -ForegroundColor Cyan
Write-Host ""

# GPU detection
$gpuType = "cpu"
$gpuName = "Unknown"
try {
    $gpus = @(Get-WmiObject Win32_VideoController | Select-Object -ExpandProperty Name)
    foreach ($g in $gpus) {
        if ($g -match "NVIDIA") { $gpuType = "cuda"; $gpuName = $g; break }
        elseif ($g -match "AMD|Radeon|Intel") { $gpuType = "vulkan"; $gpuName = $g }
    }
} catch {}
Write-Host "  GPU    : $gpuName"
Write-Host "  Backend: $gpuType"
Write-Host ""

# Get latest release
Write-Host "  Fetching latest release from GitHub..." -ForegroundColor DarkGray
$rel = Invoke-RestMethod "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest" `
    -Headers @{"User-Agent" = "MIRA-Setup"}
$tag = $rel.tag_name
Write-Host "  Latest : $tag" -ForegroundColor Green

# Show all win zips for debugging
Write-Host ""
Write-Host "  Available Windows assets:" -ForegroundColor DarkGray
$rel.assets | Where-Object { $_.name -match "win.*zip" } | ForEach-Object {
    Write-Host "    $($_.name)  ($([math]::Round($_.size/1MB,1)) MB)"
}
Write-Host ""

# Asset selection — MUST start with "llama-" to avoid cudart/other helper zips
$asset = $null

if ($gpuType -eq "cuda") {
    # Pick the main CUDA binary zip: starts with "llama-", contains "win", "cuda", "x64", ends ".zip"
    $asset = $rel.assets | Where-Object {
        $_.name -match "^llama-.*-bin-win-cuda.*x64.*\.zip$"
    } | Select-Object -First 1
}

if (-not $asset -and $gpuType -eq "vulkan") {
    $asset = $rel.assets | Where-Object {
        $_.name -match "^llama-.*-bin-win-vulkan.*x64.*\.zip$"
    } | Select-Object -First 1
}

# Fallback: any main llama win x64 zip
if (-not $asset) {
    $asset = $rel.assets | Where-Object {
        $_.name -match "^llama-.*-bin-win.*x64.*\.zip$"
    } | Select-Object -First 1
}

if (-not $asset) {
    Write-Host "  ERROR: Could not find a suitable llama-server binary zip." -ForegroundColor Red
    Write-Host "  Please download manually: https://github.com/ggml-org/llama.cpp/releases/tag/$tag"
    exit 1
}

$sizeMB = [math]::Round($asset.size / 1MB, 1)
Write-Host "  Selected: $($asset.name)  ($sizeMB MB)" -ForegroundColor Green
Write-Host ""
Write-Host "  Downloading... (may take a few minutes)" -ForegroundColor DarkGray

# Download
$zip = "$env:TEMP\llama_$tag.zip"
$wc = New-Object System.Net.WebClient
$wc.Headers.Add("User-Agent", "MIRA-Setup")
$wc.DownloadFile($asset.browser_download_url, $zip)
Write-Host "  Download complete." -ForegroundColor Green

# Extract
$out = "$env:TEMP\llama_extract_$tag"
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
Write-Host "  Extracting..." -ForegroundColor DarkGray
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $out)

# List contents for debugging
$exes = Get-ChildItem $out -Recurse -Filter "*.exe" | Select-Object Name, FullName
Write-Host "  Executables found in zip:" -ForegroundColor DarkGray
$exes | ForEach-Object { Write-Host "    $($_.Name)" }

# Find llama-server.exe
$exe = $exes | Where-Object { $_.Name -eq "llama-server.exe" } | Select-Object -First 1
if (-not $exe) {
    Write-Host "  ERROR: llama-server.exe not found in zip." -ForegroundColor Red
    exit 1
}

# Copy exe + all DLLs from same folder
Copy-Item $exe.FullName $dest -Force
Write-Host "  Copied : llama-server.exe" -ForegroundColor Green

$binDir = Split-Path $exe.FullName
$dlls = Get-ChildItem $binDir -Filter "*.dll"
$count = 0
foreach ($d in $dlls) {
    Copy-Item $d.FullName $dest -Force
    $count++
}
Write-Host "  Copied : $count DLLs" -ForegroundColor DarkGray

# Cleanup
Remove-Item $zip -Force -ErrorAction SilentlyContinue
Remove-Item $out -Recurse -Force -ErrorAction SilentlyContinue

# Verify
Write-Host ""
try {
    $v = & "$dest\llama-server.exe" --version 2>&1
    Write-Host "  Version: $v" -ForegroundColor Green
} catch {
    Write-Host "  (version check skipped)" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "  SUCCESS: llama-server.exe installed!" -ForegroundColor Green
Write-Host "  Run start_llama_server.bat to load Gemma 4 and start MIRA." -ForegroundColor White
Write-Host ""
