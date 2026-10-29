# Chay pl_scrape.py tu PC o Ba Lan (TV cung mang): ket qua o day la chuan.
# Ghi scripts/local-out/pl.m3u + pl-report.json + pl-local-ok.json; -Publish chep pl.m3u, pl-report.json, pl-local-ok.json
# ra goc repo (roi tu commit + push). Action GitHub dung pl-local-ok.json 14 ngay: nguon hong tu Ba Lan bi bo,
# nguon chi hong tu runner (My) ma chay o Ba Lan duoc giu (#VNL-GEO:PL). Chay lai it nhat 2 tuan/lan.
param([switch]$Publish)
$out = Join-Path $PSScriptRoot "local-out"
New-Item -ItemType Directory -Force $out | Out-Null
python -I (Join-Path $PSScriptRoot "pl_scrape.py") --out $out --write-local-ok
if ($LASTEXITCODE -ne 0) { throw "pl_scrape.py loi" }
$root = Split-Path $PSScriptRoot -Parent
if ($Publish) { foreach ($f in 'pl.m3u', 'pl-report.json', 'pl-local-ok.json') { Copy-Item (Join-Path $out $f) (Join-Path $root $f) -Force } ; "Da chep ra $root" }
