# Снимает p50/p95 по этапам ответа с запущенного приложения.
# Токен и порт берутся у живого бэкенда (bootstrap.json десктопной сборкой не обновляется).
# Использование:  .\backend\scripts\timings.ps1 [-WindowSec 1200]
param([int]$WindowSec = 1200)

$ids = (Get-Process backend -ErrorAction SilentlyContinue).Id
if (-not $ids) { Write-Error "Процесс backend не найден: запусти приложение."; exit 1 }
$port = (Get-NetTCPConnection -State Listen -OwningProcess $ids -ErrorAction SilentlyContinue | Select-Object -First 1).LocalPort
if (-not $port) { Write-Error "Не нашёл порт, который слушает backend."; exit 1 }
$base = "http://127.0.0.1:$port"
$html = (Invoke-WebRequest -UseBasicParsing "$base/").Content
$tok = [regex]::Match($html, 'name="runtime-token" content="([^"]+)"').Groups[1].Value
if (-not $tok) { Write-Error "В главной странице нет runtime-token."; exit 1 }

$r = Invoke-RestMethod -Headers @{ "X-Local-Agent-Token" = $tok } "$base/system/timings?window_sec=$WindowSec"
$r.stages.PSObject.Properties |
    ForEach-Object { [pscustomobject]@{ stage = $_.Name; count = $_.Value.count; errors = $_.Value.errors; p50_ms = $_.Value.p50_ms; p95_ms = $_.Value.p95_ms; max_ms = $_.Value.max_ms } } |
    Sort-Object p95_ms -Descending | Format-Table -AutoSize
