$ErrorActionPreference = 'Continue'

$outDir  = 'C:\ARIA\out'
$logFile = Join-Path $outDir 'e2e.log'
$results = New-Object System.Collections.ArrayList
$script:launchN = 0

New-Item -ItemType Directory -Force -Path $outDir | Out-Null

function Log($m) {
    $line = "{0} {1}" -f (Get-Date -Format 'HH:mm:ss.fff'), $m
    Add-Content -Path $logFile -Value $line -Encoding UTF8
    Write-Output $line
}
function Record($test, $status, $detail) {
    Log ("[RESULT] {0} | {1} | {2}" -f $test, $status, $detail)
    [void]$results.Add([pscustomobject]@{ Test = $test; Status = $status; Detail = $detail })
}
function Http-Get([string]$url, [string]$token) {
    $headers = @{}
    if ($token) { $headers['X-Local-Agent-Token'] = $token }
    try {
        $resp = Invoke-WebRequest -Uri $url -Method Get -Headers $headers -TimeoutSec 10 -UseBasicParsing
        return $resp.Content
    } catch {
        return $null
    }
}
function Run-WithTimeout([string]$FilePath, [string[]]$Arguments, [int]$TimeoutSec, [string]$Label) {
    Log "  -> run $Label : $FilePath $($Arguments -join ' ')"
    $p = Start-Process -FilePath $FilePath -ArgumentList $Arguments -PassThru -WindowStyle Hidden
    $t0 = Get-Date
    while (-not $p.HasExited -and (Get-Date) -lt $t0.AddSeconds($TimeoutSec)) {
        Start-Sleep -Seconds 1
    }
    if (-not $p.HasExited) {
        Log "  -> TIMEOUT after ${TimeoutSec}s for $Label (pid=$($p.Id)); force-killing"
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
        taskkill /PID $p.Id /T /F 2>$null | Out-Null
        return @{ ok = $false; timedOut = $true; exit = $null; dur = $TimeoutSec }
    }
    $dur = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
    Log "  -> done $Label exit=$($p.ExitCode) in ${dur}s"
    return @{ ok = $true; timedOut = $false; exit = $p.ExitCode; dur = $dur }
}
function Get-WebView2State {
    $paths = @(
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
        'HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
        'HKCU:\Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    )
    foreach ($p in $paths) {
        $v = Get-ItemProperty $p -ErrorAction SilentlyContinue
        if ($v) { return "present (ver $($v.pv))" }
    }
    $exe = Get-ChildItem "$env:ProgramFiles(x86)\Microsoft\EdgeWebView\Application" -Filter 'msedgewebview2.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($exe) { return "present at $($exe.DirectoryName)" }
    return 'missing'
}
function Get-AppExe {
    $names = @('local-agent-ui.exe', 'Local Agent Desktop.exe')
    $reg = @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )
    $key = Get-ItemProperty $reg -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -like '*Local Agent*' } | Select-Object -First 1
    if ($key -and $key.InstallLocation) {
        foreach ($n in $names) {
            $exe = Join-Path $key.InstallLocation $n
            if (Test-Path $exe) { return $exe }
        }
    }
    foreach ($n in $names) {
        $cand = Get-ChildItem $env:LOCALAPPDATA -Recurse -Depth 3 -Filter $n -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($cand) { return $cand.FullName }
    }
    return $null
}
function Get-BackendPid {
    $p = Get-Process -Name 'backend' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($p) { return $p.Id }
    return $null
}
function Get-PortForPid([int]$processId) {
    $candidates = @(Get-Process -Name 'backend' -ErrorAction SilentlyContinue | ForEach-Object { $_.Id }) + @($processId)
    $candidates = @($candidates | Select-Object -Unique)
    $lines = netstat -ano 2>$null | Select-String 'LISTENING'
    foreach ($l in $lines) {
        $parts = ($l.Line -split '\s+') | Where-Object { $_ }
        if ($parts.Count -ge 5 -and $parts[1] -like '127.0.0.1:*' -and $candidates -contains [int]$parts[4]) {
            $hostport = $parts[1] -split ':'
            if ($hostport.Count -ge 2) { return [int]$hostport[1] }
        }
    }
    return $null
}
function Get-ServedMeta([string]$content, [string]$name) {
    if (-not $content) { return $null }
    $n = [regex]::Escape($name)
    $p1 = "name\s*=\s*`"$n`"[^>]*content\s*=\s*`"([^`"]*)`""
    $m = [regex]::Match($content, $p1)
    if ($m.Success) { return $m.Groups[1].Value }
    $p2 = "content\s*=\s*`"([^`"]*)`"[^>]*name\s*=\s*`"$n`""
    $m = [regex]::Match($content, $p2)
    if ($m.Success) { return $m.Groups[1].Value }
    return $null
}
function Wait-PortDown([int]$port, [int]$timeoutSec) {
    $t0 = Get-Date
    while ((Get-Date) -lt $t0.AddSeconds($timeoutSec)) {
        $lines = netstat -ano 2>$null | Select-String 'LISTENING' | Select-String (":$port\s")
        if (-not $lines) { return $true }
        Start-Sleep -Milliseconds 100
    }
    return $false
}
function Wait-Healthy([int]$port, [int]$timeoutSec) {
    $t0 = Get-Date
    $uri = "http://127.0.0.1:$port/status"
    while ((Get-Date) -lt $t0.AddSeconds($timeoutSec)) {
        $body = Http-Get $uri $null
        if ($null -ne $body) {
            try { return ($body | ConvertFrom-Json) } catch { return [pscustomobject]@{ overall = 'online' } }
        }
        Start-Sleep -Milliseconds 200
    }
    Log "  !! Wait-Healthy($uri) timed out (no HTTP 200 within ${timeoutSec}s)"
    return $null
}
function Stop-AppProcess {
    taskkill /IM 'local-agent-ui.exe' /F 2>$null | Out-Null
    taskkill /IM 'Local Agent Desktop.exe' /F 2>$null | Out-Null
    Start-Sleep -Seconds 1
}
function Ensure-NoApp {
    Stop-AppProcess
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Process -Name 'backend' -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
        taskkill /IM 'backend.exe' /T /F 2>$null | Out-Null
        Start-Sleep -Milliseconds 300
    }
}

Log '=== ARIA_v9 e2e start ==='
Log "Sandbox user: $env:USERNAME | HOME: $env:USERPROFILE"
Log "Date: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"

# ---------------------------------------------------------------------------
# Diagnostics: guest network + WebView2
# ---------------------------------------------------------------------------
Log '--- diag: network ---'
$netOk = $false
try {
    $n = Invoke-WebRequest -Uri 'https://aka.ms/webview2bootstrap' -Method Head -TimeoutSec 15 -UseBasicParsing
    if ($n.StatusCode -eq 200) { $netOk = $true }
    Log "guest net test: HTTP $($n.StatusCode)"
} catch {
    Log "guest net test FAILED: $($_.Exception.Message)"
}
Record 'diag-net' $(if ($netOk) { 'PASS' } else { 'FAIL' }) "guest -> aka.ms/webview2bootstrap ok=$netOk"

Log '--- diag: WebView2 ---'
$wv = Get-WebView2State
Log "WebView2 runtime: $wv"
Record 'diag-webview2' 'INFO' $wv

# ---------------------------------------------------------------------------
# P2: clean install
# ---------------------------------------------------------------------------
Log '--- P2: install ---'
$shareInstaller = 'C:\ARIA\Local Agent Desktop_0.1.0_x64-setup.exe'
$localInstaller = 'C:\Windows\Temp\ARIA-setup.exe'
if (Test-Path $shareInstaller) {
    Copy-Item $shareInstaller $localInstaller -Force
    Log "copied installer to $localInstaller ($([math]::Round((Get-Item $localInstaller).Length/1MB,1)) MB)"
} else {
    Record 'P2-install' 'FAIL' "installer not found on share: $shareInstaller"
}

$wvOk = $wv -like 'present*'
if (-not $wvOk) {
    Log '--- P2: WebView2 preinstall ---'
    $wb = 'C:\ARIA\WebView2RuntimeStandalone.exe'
    if (Test-Path $wb) {
        $localWb = 'C:\Windows\Temp\ARIA-wv2.exe'
        Copy-Item $wb $localWb -Force
        $wr = Run-WithTimeout $localWb @('/silent', '/install') 600 'webview2-preinstall'
        if ($wr.ok -and $wr.exit -eq 0) {
            $wv = Get-WebView2State
            Log "WebView2 after preinstall: $wv"
            $wvOk = $wv -like 'present*'
        } else {
            Record 'P2-webview2-preinstall' 'INFO' "exit=$($wr.exit) timedOut=$($wr.timedOut)"
        }
    } else {
        Log "no WebView2 standalone on share; will rely on installer bootstrapper (may hang if no net)"
    }
}
Record 'P2-webview2-ready' 'INFO' "webview2 ready=$wvOk ($wv)"

if (Test-Path $localInstaller) {
    $ir = Run-WithTimeout $localInstaller @('/S') 600 'app-installer'
    Log "Installer result: ok=$($ir.ok) exit=$($ir.exit) timedOut=$($ir.timedOut) dur=$($ir.dur)"
    $appExe = Get-AppExe
    if (-not $appExe) {
        Log '  !! app exe not found. Dumping LOCALAPPDATA layout:'
        Get-ChildItem $env:LOCALAPPDATA -Depth 2 -ErrorAction SilentlyContinue | ForEach-Object { Log "    DIR: $($_.FullName)" }
        Get-ChildItem $env:LOCALAPPDATA -Recurse -Depth 3 -Filter '*.exe' -ErrorAction SilentlyContinue | Select-Object -First 25 | ForEach-Object { Log "    EXE: $($_.FullName)" }
        Record 'P2-install' 'FAIL' 'app exe not found after install'
    } else {
        Log "App exe: $appExe"
        Record 'P2-install' 'PASS' "exit=$($ir.exit) dur=$($ir.dur)s exe=$appExe"
    }
} else {
    Record 'P2-install' 'FAIL' 'no local installer copy to run'
}

# ---------------------------------------------------------------------------
# Launch helper (returns port + token + firstRun)
# ---------------------------------------------------------------------------
function Launch-App {
    $appExe = Get-AppExe
    if (-not $appExe) { return @{ ok = $false; err = 'app exe not found' } }
    $script:launchN++
    $stdoutLog = Join-Path $outDir "launch$($script:launchN).stdout.log"
    $stderrLog = Join-Path $outDir "launch$($script:launchN).stderr.log"
    Log "  launching app: $appExe (capture $stdoutLog / $stderrLog)"
    Start-Process -FilePath $appExe -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog | Out-Null
    $backendPid = $null
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        $backendPid = Get-BackendPid
        if ($backendPid) { Log "  backend process appeared pid=$backendPid"; break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $backendPid) { return @{ ok = $false; err = 'backend process never appeared' } }
    $port = $null
    $deadline = (Get-Date).AddSeconds(160)
    while ((Get-Date) -lt $deadline) {
        $port = Get-PortForPid $backendPid
        if ($port) { Log "  backend listening on $port"; break }
        Start-Sleep -Milliseconds 200
    }
    if (-not $port) {
        $proc = Get-Process -Id $backendPid -ErrorAction SilentlyContinue
        Log "  !! no port for backend pid=$backendPid alive=$([bool]$proc)"
        Log '  -- backend processes (Id/CPU/StartTime/Path):'
        Get-Process -Name 'backend' -ErrorAction SilentlyContinue |
            Select-Object Id, CPU, StartTime, Path | Format-Table -AutoSize | Out-String -Width 200 |
            ForEach-Object { $_.Trim() -split "`r?`n" | Where-Object { $_ } | ForEach-Object { Log "     $_" } }
        Log '  -- netstat LISTENING:'
        netstat -ano 2>$null | Select-String 'LISTENING' | ForEach-Object { Log "     $($_.Line)" }
        Log '  -- backend.log from _MEIPASS:'
        Get-ChildItem (Join-Path $env:TEMP '_MEI*') -Recurse -Filter 'backend.log' -ErrorAction SilentlyContinue |
            ForEach-Object { Log "     [$($_.FullName)]"; Get-Content $_.FullName -Tail 60 -ErrorAction SilentlyContinue | ForEach-Object { Log "     $_" } }
        Log '  -- app stderr (supervisor + inherited backend):'
        if (Test-Path $stderrLog) { Get-Content $stderrLog -Tail 80 -ErrorAction SilentlyContinue | ForEach-Object { Log "     $_" } } else { Log '     (no stderr file)' }
        Log '  -- app stdout:'
        if (Test-Path $stdoutLog) { Get-Content $stdoutLog -Tail 40 -ErrorAction SilentlyContinue | ForEach-Object { Log "     $_" } } else { Log '     (no stdout file)' }
        $installDir2 = if ($appExe) { Split-Path $appExe } else { Join-Path $env:LOCALAPPDATA 'Local Agent Desktop' }
        Log '  -- install dir listing:'
        Get-ChildItem $installDir2 -Recurse -ErrorAction SilentlyContinue | Select-Object -First 40 | ForEach-Object { Log "     $($_.FullName)" }
        return @{ ok = $false; err = 'no listening port found' }
    }
    $health = Wait-Healthy $port 40
    if (-not $health) { return @{ ok = $false; err = "not healthy on $port" } }
    Start-Sleep -Seconds 2
    $html = Http-Get "http://127.0.0.1:$port/" $null
    $token = Get-ServedMeta $html 'runtime-token'
    $firstRun = Get-ServedMeta $html 'first-run'
    return @{
        ok = $true; pid = $backendPid; port = $port; health = $health
        token = $token; firstRun = $firstRun
    }
}

# ---------------------------------------------------------------------------
# P2: health / skills / tables on clean profile (launch 1)
# ---------------------------------------------------------------------------
Log '--- P2: clean-profile launch ---'
$L1 = Launch-App
if (-not $L1.ok) {
    Record 'P2-launch' 'FAIL' $L1.err
} else {
    $l1status = if ($L1.health.overall) { $L1.health.overall } else { $L1.health.status }
    Log "L1 backend pid=$($L1.pid) port=$($L1.port) status=$l1status"
    $healthJson = $L1.health | ConvertTo-Json -Compress
    Record 'P2-launch' 'PASS' "port=$($L1.port) health=$healthJson"
    Record 'P2-meta-first-run' 'INFO' "launch1 first-run=$($L1.firstRun)"

    $token = $L1.token
    if ($token) {
        $skills = Http-Get "http://127.0.0.1:$($L1.port)/api/skills" $token | ConvertFrom-Json
        $skillCount = @($skills).Count
        Record 'P2-skills' 'INFO' "skills count=$skillCount"
    } else {
        Record 'P2-skills' 'FAIL' 'no runtime token in served HTML'
    }

    $st = Http-Get "http://127.0.0.1:$($L1.port)/api/system/self-test" $token
    try { $stj = $st | ConvertFrom-Json } catch { $stj = $null }
    if ($stj) {
        $modelsCheck = $stj.checks.models
        Log "self-test checks.models = $modelsCheck"
        Record 'P2-tables' 'INFO' $modelsCheck
        $dd = $stj.checks | Where-Object { $_.name -eq 'data_dir' }
        if ($dd) { Log "data_dir = $($dd.detail)"; $script:realDataDir = $dd.detail }
    } else {
        Record 'P2-tables' 'FAIL' "self-test parse error: $(($st -join ' '))"
    }

    $bootstrapHome = Join-Path $env:USERPROFILE '.local-agent-ui\bootstrap.json'
    Record 'P3-bootstrap-after-L1' 'INFO' "bootstrap.json exists=$(Test-Path $bootstrapHome) path=$bootstrapHome"
}

# ---------------------------------------------------------------------------
# P3: first-run meta across relaunch (localStorage must not win)
# ---------------------------------------------------------------------------
Log '--- P3: relaunch (launch 2) ---'
if ($L1.ok) {
    taskkill /IM 'local-agent-ui.exe' 2>$null | Out-Null
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Process -Name 'backend' -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 200
    }
    if (Get-Process -Name 'backend' -ErrorAction SilentlyContinue) {
        Log '  backend survived graceful close; force killing'
        taskkill /IM 'backend.exe' /T /F 2>$null | Out-Null
        Start-Sleep -Seconds 2
    }
    Start-Sleep -Seconds 2
    $L2 = Launch-App
    if (-not $L2.ok) {
        Record 'P3-relaunch' 'FAIL' $L2.err
    } else {
        Record 'P3-meta-first-run' 'INFO' "launch2 first-run=$($L2.firstRun) (TZ expects false after onboarding)"
    }
    taskkill /IM 'local-agent-ui.exe' 2>$null | Out-Null
    Start-Sleep -Seconds 2
} else {
    Log 'Skipping P3 (no launch 1)'
}

# ---------------------------------------------------------------------------
# P1: supervisor respawn + backoff (crash storm)
# ---------------------------------------------------------------------------
Log '--- P1: crash storm ---'
$L3 = Launch-App
if (-not $L3.ok) {
    Record 'P1-respawn' 'FAIL' $L3.err
} else {
    $port = $L3.port
    Log "L3 pid=$($L3.pid) port=$port"
    $intervals = @()
    $tokens = @()
    for ($i = 1; $i -le 4; $i++) {
        $bp = Get-BackendPid
        if (-not $bp) { Record 'P1-crash' 'FAIL' "no backend pid at crash $i"; break }
        taskkill /PID $bp /T /F 2>$null | Out-Null
        $down = Wait-PortDown $port 8
        if (-not $down) { Record 'P1-crash' 'FAIL' "port $port did not free at crash $i"; break }
        $t0 = Get-Date
        $h = Wait-Healthy $port 140
        $dt = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)
        if ($h) {
    $html = Http-Get "http://127.0.0.1:$port/" $null
            $tk = Get-ServedMeta $html 'runtime-token'
            $tokens += $tk
            Log "crash#$i respawn OK in ${dt}s (port still $port)"
            $intervals += $dt
        } else {
            Record 'P1-crash' "FAIL" "crash#$i not healthy after 40s"
            break
        }
    }
    Record 'P1-respawn' 'INFO' "respawn intervals (s): $($intervals -join ', ') [4 crashes]"
    Record 'P1-same-port' 'INFO' "port stable: $port"
    $tokenStable = if ($tokens.Count -gt 1) { ($tokens | Select-Object -Unique).Count -eq 1 } else { $null }
    Record 'P1-same-token' 'INFO' "runtime-token stable across respawns=$tokenStable (tokens=$($tokens -join ','))"
}

# ---------------------------------------------------------------------------
# P6: shutdown/respawn race — graceful close must not orphan backend
# ---------------------------------------------------------------------------
Log '--- P6: graceful close orphan check ---'
$L4 = Launch-App
if (-not $L4.ok) {
    Record 'P6-graceful-close' 'FAIL' $L4.err
} else {
    taskkill /IM 'local-agent-ui.exe' 2>$null | Out-Null
    $deadline = (Get-Date).AddSeconds(15)
    $orphan = $true
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Name 'backend' -ErrorAction SilentlyContinue)) { $orphan = $false; break }
        Start-Sleep -Milliseconds 200
    }
    Record 'P6-graceful-close' $(if ($orphan) { 'FAIL' } else { 'PASS' }) "orphan backend after graceful close=$orphan"
    if ($orphan) { taskkill /IM 'backend.exe' /T /F 2>$null | Out-Null }
}

Log '--- P6: crash-in-flight race ---'
$L5 = Launch-App
if (-not $L5.ok) {
    Record 'P6-crash-in-flight' 'FAIL' $L5.err
} else {
    $bp = Get-BackendPid
    if ($bp) { taskkill /PID $bp /T /F 2>$null | Out-Null }
    Start-Sleep -Milliseconds 150
    taskkill /IM 'local-agent-ui.exe' 2>$null | Out-Null
    $deadline = (Get-Date).AddSeconds(15)
    $orphan = $true
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-Process -Name 'backend' -ErrorAction SilentlyContinue)) { $orphan = $false; break }
        Start-Sleep -Milliseconds 200
    }
    Record 'P6-crash-in-flight' $(if ($orphan) { 'FAIL' } else { 'PASS' }) "orphan backend after in-flight close=$orphan"
    if ($orphan) { taskkill /IM 'backend.exe' /T /F 2>$null | Out-Null }
}

# ---------------------------------------------------------------------------
# P5: auto-update (offline) — record version + updater state
# ---------------------------------------------------------------------------
Log '--- P5: updater ---'
$appExe = Get-AppExe
$ver = (Get-Item $appExe -ErrorAction SilentlyContinue).VersionInfo
Record 'P5-version' 'INFO' "installed=$($ver.FileVersion) product=$($ver.ProductVersion) (TZ target 0.1.1)"
Record 'P5-updater' 'BLOCKED' "updater endpoints in tauri.conf.json = placeholder github.com/<OWNER>/<REPO>; offline sandbox cannot fetch real release; plugin present but not wired"

# ---------------------------------------------------------------------------
# P4: uninstall hygiene
# ---------------------------------------------------------------------------
Log '--- P4: uninstall ---'
$appExe = Get-AppExe
$installDir = if ($appExe) { Split-Path $appExe } else { Join-Path $env:LOCALAPPDATA 'Local Agent Desktop' }
$uninstaller = Get-ChildItem $installDir -Filter 'Uninstall*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $uninstaller) {
    $uninstaller = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Local Agent Desktop') -Filter 'Uninstall*.exe' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
}
$uninstallerPath = if ($uninstaller) { $uninstaller.FullName } else { $null }
if ($uninstallerPath) {
    $up = Run-WithTimeout $uninstallerPath @('/S') 120 'app-uninstaller'
    Record 'P4-uninstall' 'INFO' "exit=$($up.exit) timedOut=$($up.timedOut)"
} else {
    Record 'P4-uninstall' 'FAIL' "uninstaller not found under $installDir"
}
Start-Sleep -Seconds 3

$leftoverCandidates = [ordered]@{
    'install-dir'          = $installDir
    'appdata-com-ident'    = (Join-Path $env:APPDATA 'com.local.agent.desktop')
    'appdata-product'      = (Join-Path $env:APPDATA 'Local Agent Desktop')
    'localappdata-product' = (Join-Path $env:LOCALAPPDATA 'Local Agent Desktop')
    'localappdata-ui-aria' = (Join-Path $env:LOCALAPPDATA 'local-agent-ui\ARIA')
    'home-local-agent-ui'  = (Join-Path $env:USERPROFILE '.local-agent-ui')
    'home-bootstrap-json'  = (Join-Path $env:USERPROFILE '.local-agent-ui\bootstrap.json')
}
if ($script:realDataDir) { $leftoverCandidates['backend-data-dir'] = $script:realDataDir }
foreach ($name in $leftoverCandidates.Keys) {
    $path = $leftoverCandidates[$name]
    $exists = Test-Path $path
    Log "leftover $name = $exists :: $path"
    Record 'P4-leftover' 'INFO' "$name exists=$exists"
}
if (Get-Process -Name 'backend' -ErrorAction SilentlyContinue) {
    Record 'P4-no-orphan' 'FAIL' 'backend still running after uninstall'
} else {
    Record 'P4-no-orphan' 'PASS' 'no backend process after uninstall'
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Log '=== E2E COMPLETE ==='
$summary = @{ completed = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'); results = @($results) }
$summary | ConvertTo-Json -Depth 5 | Set-Content -Path (Join-Path $outDir 'DONE.json') -Encoding UTF8
Add-Content -Path (Join-Path $outDir 'DONE') -Value (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
Log 'Wrote DONE + DONE.json'
