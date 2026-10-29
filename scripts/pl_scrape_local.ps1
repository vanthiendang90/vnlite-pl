# Chay pl_scrape.py tu PC o Ba Lan de doi chieu voi ket qua runner GitHub (My).
# -Strict: bo nguon hong thay vi gan #VNL-GEO:PL. Ket qua: scripts/local-out/.
param([switch]$Strict)
$out = Join-Path $PSScriptRoot "local-out"
New-Item -ItemType Directory -Force $out | Out-Null
$a = @("-I", (Join-Path $PSScriptRoot "pl_scrape.py"), "--out", $out)
if ($Strict) { $a += "--strict-geo" }
python @a
Write-Host "So sanh: $(Split-Path $PSScriptRoot -Parent)pl.m3u <-> $outpl.m3u"
