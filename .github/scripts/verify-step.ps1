# verify-step.ps1 — single DoD check, run from a dedicated workflow step.
# Usage:
#   powershell -File verify-step.ps1 -Check 1 -BaseUrl http://127.0.0.1:<port> [-Token xxx]
# Each check exits 1 on mismatch so the Actions job fails visibly per step.
#
# Checks (see TZ table):
#   1  GET /                                -> 200, HTML contains <meta name="runtime-token"
#   2  GET /spa/fallback/route              -> 200, index.html (runtime-token marker)
#   3  GET /assets/<name from index.html>   -> 200
#   4  GET /api/status (no token)           -> 200 (public health)
#   5  GET /api/model/info no token / token -> 401 / 200
#   6  GET /api/nonexistent                 -> 404, JSON body
#   7  OPTIONS / Origin: tauri.localhost    -> 400, NO Access-Control-Allow-Origin header
#   8  POST /system/shutdown (token)        -> 200, backend process exits afterwards

param(
    [Parameter(Mandatory = $true)][int]$Check,
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [string]$Token = ""
)

$ErrorActionPreference = "Stop"

function Fail($msg) {
    Write-Error "CHECK $Check FAILED: $msg"
    exit 1
}

function Req($method, $url, $headers = @{}, $allowHttpErr = $false) {
    $params = @{
        Method = $method
        Uri = $url
        Headers = $headers
        UseBasicParsing = $true
        TimeoutSec = 10
    }
    if ($method -in @("POST", "OPTIONS")) { $params.Body = [byte[]]@() }
    try {
        $resp = Invoke-WebRequest @params
        return @{ Status = [int]$resp.StatusCode; Body = $resp.Content; Headers = $resp.Headers }
    } catch {
        if ($allowHttpErr -and $_.Exception.Response) {
            $resp = $_.Exception.Response
            $body = ""
            # PS 5.1 leaves the error stream already consumed — prefer ErrorDetails,
            # which carries the response body; fall back to reading the stream.
            if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
                $body = $_.ErrorDetails.Message
            } else {
                try {
                    $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
                    $body = $reader.ReadToEnd()
                } catch {}
            }
            $hs = @{}
            foreach ($k in $resp.Headers.Keys) { $hs[$k] = $resp.Headers[$k] }
            return @{ Status = [int]$resp.StatusCode; Body = $body; Headers = $hs }
        }
        throw
    }
}

switch ($Check) {
    1 {
        $r = Req "GET" "$BaseUrl/"
        if ($r.Status -ne 200) { Fail "status $($r.Status), expected 200" }
        if ($r.Body -notmatch '<meta name="runtime-token"') { Fail "no runtime-token meta in HTML" }
        # persist token for later steps via GITHUB_ENV when available
        if ($r.Body -match 'content="([^"]+)"') {
            $env:ARIA_RUNTIME_TOKEN = $Matches[1]
            $envFile = $env:GITHUB_ENV
            if ($envFile) {
                Add-Content -Path $envFile -Value "ARIA_RUNTIME_TOKEN=$($Matches[1])"
            }
        }
        Write-Output "CHECK 1 OK: GET / -> 200, runtime-token meta present"
    }
    2 {
        $r = Req "GET" "$BaseUrl/spa/fallback/route"
        if ($r.Status -ne 200) { Fail "status $($r.Status), expected 200" }
        if ($r.Body -notmatch 'runtime-token') { Fail "SPA fallback did not return index.html (no runtime-token marker)" }
        Write-Output "CHECK 2 OK: SPA fallback -> 200 index.html"
    }
    3 {
        # find the real hashed asset name from the served index.html
        $idx = Req "GET" "$BaseUrl/"
        if ($idx.Body -notmatch 'src="/assets/([^"]+\.js)"') { Fail "no /assets/*.js reference in index.html" }
        $asset = $Matches[1]
        $r = Req "GET" "$BaseUrl/assets/$asset"
        if ($r.Status -ne 200) { Fail "asset /assets/$asset -> $($r.Status), expected 200" }
        if ($r.Body.Length -lt 1000) { Fail "asset body suspiciously small ($($r.Body.Length) bytes)" }
        Write-Output "CHECK 3 OK: /assets/$asset -> 200 ($($r.Body.Length) bytes)"
    }
    4 {
        $r = Req "GET" "$BaseUrl/api/status"
        if ($r.Status -ne 200) { Fail "status $($r.Status), expected 200 (public health)" }
        Write-Output "CHECK 4 OK: /api/status (no token) -> 200 public health"
    }
    5 {
        $rNo = Req "GET" "$BaseUrl/api/model/info" @{} $true
        if ($rNo.Status -ne 401) { Fail "no-token -> $($rNo.Status), expected 401" }
        if (-not $Token) { Fail "no token supplied for check 5" }
        $rYes = Req "GET" "$BaseUrl/api/model/info" @{ "X-Local-Agent-Token" = $Token }
        if ($rYes.Status -ne 200) { Fail "with-token -> $($rYes.Status), expected 200" }
        Write-Output "CHECK 5 OK: /api/model/info -> 401 (no token) / 200 (token)"
    }
    6 {
        $r = Req "GET" "$BaseUrl/api/nonexistent" @{} $true
        if ($r.Status -ne 404) { Fail "status $($r.Status), expected 404" }
        if ($r.Body -notmatch '"detail"') { Fail "body is not JSON: $($r.Body.Substring(0, [Math]::Min(80, $r.Body.Length)))" }
        Write-Output "CHECK 6 OK: /api/nonexistent -> 404 JSON"
    }
    7 {
        $r = Req "OPTIONS" "$BaseUrl/" @{ "Origin" = "http://tauri.localhost"; "Access-Control-Request-Method" = "GET" } $true
        if ($r.Status -ne 400) { Fail "status $($r.Status), expected 400 (CORS disabled)" }
        $acao = $r.Headers["Access-Control-Allow-Origin"]
        if ($acao) { Fail "Access-Control-Allow-Origin present: $acao (must be absent)" }
        Write-Output "CHECK 7 OK: OPTIONS -> 400, no Access-Control-Allow-Origin"
    }
    8 {
        if (-not $Token) { Fail "no token supplied for check 8" }
        $r = Req "POST" "$BaseUrl/system/shutdown" @{ "X-Local-Agent-Token" = $Token }
        if ($r.Status -ne 200) { Fail "status $($r.Status), expected 200" }
        Write-Output "CHECK 8 OK: POST /system/shutdown -> 200 (backend should now exit)"
    }
    default { Fail "unknown -Check $Check" }
}
exit 0
