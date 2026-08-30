<#
.SYNOPSIS
    반입한 번들을 검증한다. 폐쇄망에서 압축을 푼 뒤 실행한다.

.DESCRIPTION
    세 가지를 확인한다.

      1. 무결성  — MANIFEST.txt 의 SHA256 과 실제 파일을 대조
      2. 실행    — Python 이 뜨고 CREX 가 import 되는가
      3. 테스트  — tests/run_all.py 전부 통과 (LLM·네트워크·pip 불필요)

    여기까지 통과하면 파일은 온전하다. 그다음은 crex.json 에 vLLM 주소를 넣고
    doctor 를 돌릴 차례다.

.PARAMETER SkipManifest
    해시 대조를 건너뛴다. 파일이 많으면 몇 분 걸린다.

.EXAMPLE
    .\tools\verify.ps1
    .\tools\verify.ps1 -SkipManifest
#>
[CmdletBinding()]
param([switch]$SkipManifest)

$ErrorActionPreference = "Stop"
$BundleRoot = Split-Path -Parent $PSScriptRoot
$failed = 0

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Ok([string]$Message)   { Write-Host "    OK   $Message" -ForegroundColor Green }
function Write-Bad([string]$Message)  { Write-Host "    실패 $Message" -ForegroundColor Red; $script:failed++ }
function Write-Note([string]$Message) { Write-Host "    $Message" -ForegroundColor DarkGray }

# --------------------------------------------------------------------------
# 1. 무결성
# --------------------------------------------------------------------------

$manifest = Join-Path $BundleRoot "MANIFEST.txt"

if ($SkipManifest) {
    Write-Step "무결성 확인 건너뜀 (-SkipManifest)"
} elseif (-not (Test-Path $manifest)) {
    Write-Step "무결성 확인"
    Write-Bad "MANIFEST.txt 가 없다. 번들이 온전하지 않다."
} else {
    Write-Step "무결성 확인"

    $mismatched = New-Object System.Collections.Generic.List[string]
    $missing = New-Object System.Collections.Generic.List[string]
    $checked = 0

    foreach ($line in Get-Content $manifest -Encoding utf8) {
        if ($line.StartsWith("#") -or $line.Trim() -eq "") { continue }
        $parts = $line -split "  ", 2
        if ($parts.Count -ne 2) { continue }

        $expected = $parts[0]
        $rel = $parts[1]
        $path = Join-Path $BundleRoot $rel

        if (-not (Test-Path $path)) {
            $missing.Add($rel)
            continue
        }
        $actual = (Get-FileHash -Path $path -Algorithm SHA256).Hash
        if ($actual -ne $expected) { $mismatched.Add($rel) }
        $checked++
    }

    if ($missing.Count -eq 0 -and $mismatched.Count -eq 0) {
        Write-Ok "파일 $checked 개 해시 일치"
    } else {
        foreach ($m in $missing)    { Write-Bad "누락: $m" }
        foreach ($m in $mismatched) { Write-Bad "변조/손상: $m" }
    }
}

# --------------------------------------------------------------------------
# 2. Python
# --------------------------------------------------------------------------

Write-Step "Python 확인"

$bundled = Join-Path $BundleRoot "runtime\python.exe"
if (Test-Path $bundled) {
    $python = $bundled
    Write-Note "번들 런타임 사용"
} else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Write-Bad "번들에 runtime\ 이 없고 시스템 python 도 없다"
        $python = $null
    } else {
        $python = $cmd.Source
        Write-Note "시스템 Python 사용: $python"
    }
}

if ($python) {
    $version = & $python -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
    $major, $minor = $version.Split('.')[0..1]
    if ([int]$major -eq 3 -and [int]$minor -ge 11) {
        Write-Ok "Python $version"
    } else {
        Write-Bad "Python $version — 3.11 이상이 필요하다 (룰 택소노미를 읽는 tomllib)"
    }

    # CREX 가 import 되는가. 임베더블에서 ._pth 가 잘못되면 여기서 걸린다.
    Push-Location $BundleRoot
    try {
        $probe = & $python -c "import crex; print(crex.__version__)" 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "crex $probe import 성공"
        } else {
            Write-Bad "crex import 실패: $probe"
            Write-Note "임베더블이면 runtime\python*._pth 에 '..' 가 있는지 확인하라"
        }
    } finally {
        Pop-Location
    }
}

# --------------------------------------------------------------------------
# 3. 테스트
# --------------------------------------------------------------------------

if ($python) {
    Write-Step "테스트 (LLM·네트워크·pip 불필요)"

    # Windows PowerShell 5.1 은 네이티브 명령이 stderr 를 쓰면 ErrorRecord 로
    # 감싸고 $ErrorActionPreference='Stop' 에서 죽는다. 테스트는 경고를 stderr 로
    # 내는 것이 정상이므로(불일치 감지 테스트 등) 이 구간만 낮춰서 본다.
    Push-Location $BundleRoot
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $python "tests\run_all.py" 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
        Pop-Location
    }

    $summary = $output | Select-String "통과|FAIL|ERROR"
    foreach ($line in $summary) { Write-Note $line.ToString().Trim() }

    if ($code -eq 0) {
        Write-Ok "전체 통과"
    } else {
        Write-Bad "테스트 실패 — 파일이 덜 복사되었을 가능성이 크다"
    }
}

# --------------------------------------------------------------------------

Write-Host ""
if ($failed -eq 0) {
    Write-Host "번들 검증 완료" -ForegroundColor Green
    Write-Host ""
    Write-Host "다음:" -ForegroundColor Yellow
    Write-Host "  1. copy crex.example.json crex.json"
    Write-Host "  2. crex.json 에 사내 vLLM 주소와 모델명을 넣는다"
    Write-Host "  3. crex.cmd doctor"
    Write-Host ""
    Write-Host "  사용 설명서: docs\user_manual_html\index.html (브라우저로 열면 된다)"
    Write-Host ""
    Write-Host "  자세한 절차: docs\user_manual\transfer.md"
    exit 0
} else {
    Write-Host "실패 $failed 건" -ForegroundColor Red
    Write-Host "재반입이 필요할 수 있다. docs\user_manual\transfer.md 의 문제 해결 절을 보라."
    exit 1
}
