$dest = "C:\Users\lenovo\Downloads\AION-Deliverables\MIRA-Windows"
$cudartUrl = "https://github.com/ggml-org/llama.cpp/releases/download/b8672/cudart-llama-bin-win-cuda-12.4-x64.zip"
$zip = "$env:TEMP\cudart_12_4.zip"

Write-Host "Downloading CUDA runtime DLLs (373 MB)..." -ForegroundColor Cyan
$wc = New-Object System.Net.WebClient
$wc.DownloadFile($cudartUrl, $zip)
Write-Host "Done."

$out = "$env:TEMP\cudart_extract"
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory($zip, $out)

$dlls = Get-ChildItem $out -Recurse -Filter "*.dll"
Write-Host "Copying $($dlls.Count) CUDA runtime DLLs..."
foreach ($d in $dlls) { Copy-Item $d.FullName $dest -Force; Write-Host "  $($d.Name)" }

Remove-Item $zip -Force
Remove-Item $out -Recurse -Force
Write-Host "CUDA runtime installed. Restart start_llama_server.bat" -ForegroundColor Green
