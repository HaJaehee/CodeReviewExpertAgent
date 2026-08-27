<#
.SYNOPSIS
    CREX 웹 UI (viz) 실행 스크립트

.DESCRIPTION
    python -m crex.viz 모듈을 실행하고 브라우저를 엽니다.

.EXAMPLE
    .\run_viz.ps1
    .\run_viz.ps1 --workspace D:\work\myrepo
    .\run_viz.ps1 --port 18765 -NoBrowser
#>

[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# 1. Python 인터프리터 탐색 (.venv -> venv -> runtime -> PATH)
$pythonExe = $null
$candidates = @(
    "$PSScriptRoot\.venv\Scripts\python.exe",
    "$PSScriptRoot\venv\Scripts\python.exe",
    "$PSScriptRoot\runtime\python.exe"
)

foreach ($candidate in $candidates) {
    if (Test-Path $candidate) {
        $pythonExe = $candidate
        break
    }
}

if (-not $pythonExe) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) {
        $pythonExe = $cmd.Source
    }
}

if (-not $pythonExe) {
    Write-Host "[오류] Python을 찾을 수 없습니다. Python 3.10+ 이 설치되어 있고 PATH에 등록되어 있는지 확인하세요." -ForegroundColor Red
    exit 1
}

# 2. PYTHONPATH에 프로젝트 루트 추가
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$PSScriptRoot;$env:PYTHONPATH"
} else {
    $env:PYTHONPATH = $PSScriptRoot
}

# 3. 접속 주소 및 포트 확인
$hostAddress = "127.0.0.1"
$port = 18765

if ($Arguments) {
    for ($i = 0; $i -lt $Arguments.Count; $i++) {
        if ($Arguments[$i] -eq "--port" -and ($i + 1) -lt $Arguments.Count) {
            $port = [int]$Arguments[$i + 1]
        }
        elseif ($Arguments[$i] -eq "--host" -and ($i + 1) -lt $Arguments.Count) {
            $hostAddress = $Arguments[$i + 1]
        }
    }
}

$url = "http://$hostAddress`:$port"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  CREX 웹 UI (viz) 서버 시작" -ForegroundColor Cyan
Write-Host "  주소: $url" -ForegroundColor Green
Write-Host "  Python: $pythonExe" -ForegroundColor Gray
Write-Host "==========================================" -ForegroundColor Cyan

# 4. 브라우저 자동 오픈 (NoBrowser 스위치가 없을 때)
if (-not $NoBrowser) {
    Start-Job -ScriptBlock {
        param($targetUrl)
        Start-Sleep -Milliseconds 900
        Start-Process $targetUrl
    } -ArgumentList $url | Out-Null
}

# 5. 서버 실행 (Ctrl+C 로 종료할 때까지 대기)
& $pythonExe -m crex.viz @Arguments
exit $LASTEXITCODE
