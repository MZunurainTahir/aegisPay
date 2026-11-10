# scripts/smoke.ps1
# -----------------
# PowerShell smoke test for Windows:
# Starts gateway (temporary database), executes test refund scenarios, verifies policy enforcement.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/smoke.ps1

$ErrorActionPreference = "Stop"

$port = 18766
$gw = "http://127.0.0.1:$port"
$dbFile = [System.IO.Path]::Combine([System.IO.Path]::GetTempPath(), "aegis_smoke_$([System.Guid]::NewGuid().ToString('N')).db")

Write-Host "── Aegis Gateway Smoke Test (PowerShell) ──────────────────────" -ForegroundColor Cyan
Write-Host "Database: $dbFile" -ForegroundColor Gray

$env:DB_PATH = $dbFile
$env:PAYPAL_CLIENT_ID = "smoke_test_client"
$env:PAYPAL_SECRET = "smoke_test_secret"

# Start gateway background process using python -m uvicorn
$process = Start-Process -FilePath "python" -ArgumentList "-m uvicorn gateway.main:app --host 127.0.0.1 --port $port --log-level error" -PassThru -NoNewWindow

# Wait for gateway to be responsive
$started = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 1
    try {
        $res = Invoke-RestMethod -Uri "$gw/health" -Method Get -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($res.status -eq "ok") {
            $started = $true
            break
        }
    } catch {}
}

if (-not $started) {
    Write-Host "Failed to start gateway within timeout" -ForegroundColor Red
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

$passed = 0
$failed = 0

function Test-Condition($name, $expected, $actualJson, $prop = "status") {
    try {
        $data = $actualJson | ConvertFrom-Json
        $val = $data.$prop
        if ($val -eq $expected -or "$val" -like "*$expected*") {
            Write-Host "  [PASS] $name" -ForegroundColor Green
            $script:passed++
        } else {
            Write-Host "  [FAIL] $name - Expected: $expected, Got: $val" -ForegroundColor Red
            $script:failed++
        }
    } catch {
        Write-Host "  [FAIL] $name - Parsing error: $_" -ForegroundColor Red
        $script:failed++
    }
}

try {
    # 1. Health
    Write-Host "`n1. Health Check..." -ForegroundColor Yellow
    $h = Invoke-RestMethod -Uri "$gw/health" -Method Get
    if ($h.status -eq "ok") {
        Write-Host "  [PASS] Gateway online" -ForegroundColor Green
        $passed++
    } else {
        Write-Host "  [FAIL] Gateway health status" -ForegroundColor Red
        $failed++
    }

    # 2. Small Refund ($20) -> Policy ALLOW (PayPal will fail with mock credentials, which is expected; gateway status is FAILED or EXECUTED, NOT BLOCKED)
    Write-Host "`n2. Evaluating `$20 Refund (Policy ALLOW)..." -ForegroundColor Yellow
    $body20 = @{
        action = "refund"
        params = @{ capture_id = "smoke_cap_20"; amount = 20 }
        amount = 20
        customer = "alice"
    } | ConvertTo-Json
    $r20 = Invoke-WebRequest -Uri "$gw/act" -Method Post -ContentType "application/json" -Body $body20 -UseBasicParsing
    $data20 = $r20.Content | ConvertFrom-Json
    if ($data20.status -ne "BLOCKED") {
        Write-Host "  [PASS] `$20 refund was not blocked by policy (Status: $($data20.status))" -ForegroundColor Green
        $passed++
    } else {
        Write-Host "  [FAIL] `$20 refund was blocked" -ForegroundColor Red
        $failed++
    }

    # 3. Medium Refund ($60) -> PENDING_APPROVAL (> $50 approval threshold)
    Write-Host "`n3. Evaluating `$60 Refund (Threshold Check)..." -ForegroundColor Yellow
    $body60 = @{
        action = "refund"
        params = @{ capture_id = "smoke_cap_60"; amount = 60 }
        amount = 60
        customer = "bob"
    } | ConvertTo-Json
    $r60 = (Invoke-WebRequest -Uri "$gw/act" -Method Post -ContentType "application/json" -Body $body60 -UseBasicParsing).Content
    Test-Condition "`$60 refund requires human approval" "PENDING_APPROVAL" $r60

    # 4. Large Refund ($500) -> BLOCKED (> $100 per-txn cap)
    Write-Host "`n4. Evaluating `$500 Refund (Per-Txn Cap Check)..." -ForegroundColor Yellow
    $body500 = @{
        action = "refund"
        params = @{ capture_id = "smoke_cap_500"; amount = 500 }
        amount = 500
        customer = "carol"
    } | ConvertTo-Json
    $r500 = (Invoke-WebRequest -Uri "$gw/act" -Method Post -ContentType "application/json" -Body $body500 -UseBasicParsing).Content
    Test-Condition "`$500 refund blocked by policy" "BLOCKED" $r500

    # 5. Kill Switch
    Write-Host "`n5. Testing Global Kill Switch..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri "$gw/kill?on=true" -Method Post -UseBasicParsing | Out-Null
    $bodyKill = @{
        action = "refund"
        params = @{ capture_id = "smoke_cap_kill"; amount = 10 }
        amount = 10
        customer = "dave"
    } | ConvertTo-Json
    $rKill = (Invoke-WebRequest -Uri "$gw/act" -Method Post -ContentType "application/json" -Body $bodyKill -UseBasicParsing).Content
    Test-Condition "Kill switch blocks all actions" "BLOCKED" $rKill
    Invoke-WebRequest -Uri "$gw/kill?on=false" -Method Post -UseBasicParsing | Out-Null

    # 6. Cryptographic Audit Chain Verification
    Write-Host "`n6. Cryptographic Audit Log Verification..." -ForegroundColor Yellow
    $v = (Invoke-WebRequest -Uri "$gw/audit/verify" -Method Get -UseBasicParsing).Content
    Test-Condition "Audit hash chain intact" $true $v "ok"

} finally {
    Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    if (Test-Path $dbFile) { Remove-Item $dbFile -Force -ErrorAction SilentlyContinue }
}

Write-Host "`n── Results ──────────────────────────────────────────────────" -ForegroundColor Cyan
Write-Host "  PASSED: $passed  FAILED: $failed"
if ($failed -eq 0) {
    Write-Host "  ALL SMOKE TESTS PASSED!" -ForegroundColor Green
    exit 0
} else {
    Write-Host "  SOME SMOKE TESTS FAILED" -ForegroundColor Red
    exit 1
}
