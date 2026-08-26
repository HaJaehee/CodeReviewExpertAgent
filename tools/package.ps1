<#
.SYNOPSIS
    폐쇄망 반입용 번들을 만든다. 인터넷 되는 장비에서 실행한다.

.DESCRIPTION
    Python 런타임까지 통째로 담은 zip 하나를 dist/ 에 만든다.
    PyInstaller 를 쓰지 않는다 — 소스는 소스 그대로 들어가므로 반입 심사에서
    사람이 읽고 확인할 수 있다. 그게 폐쇄망에서 통과시키기 쉬운 형태다.

    번들 구조:
        crex-<날짜>/
          runtime/      Python 임베더블 (설치 불필요, 레지스트리 안 건드림)
          pylibs/       fastmcp, GitPython 등을 미리 풀어둔 것
          wheels/       원본 wheel (직접 pip 하고 싶을 때만)
          crex/ docs/ wiki/ rules/ eval/ tests/ ...
          tools/msbuild-compiledb/   MSBuild -> compile_commands.json 로거 (소스)
          crex.cmd       실행 진입점
          MANIFEST.txt  전 파일 SHA256
          설치.md        폐쇄망에서 할 일

.PARAMETER PythonVersion
    담을 Python 버전. 3.11 이상이어야 한다 (tomllib).

.PARAMETER OutDir
    번들을 놓을 디렉터리. 기본 dist

.PARAMETER SkipRuntime
    Python 런타임을 빼고 소스만 담는다. 대상 장비에 이미 3.11+ 가 있을 때.

.PARAMETER SkipDeps
    fastmcp/GitPython 을 빼고 담는다. MCP 서버를 안 쓸 때.
    코어(review/scan/doctor)는 이것들 없이 동작한다.

.EXAMPLE
    .\tools\package.ps1
    .\tools\package.ps1 -PythonVersion 3.12.10 -SkipDeps
#>
[CmdletBinding()]
param(
    [string]$PythonVersion = "3.12.10",
    [string]$OutDir = "dist",
    [switch]$SkipRuntime,
    [switch]$SkipDeps
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # 5.1 의 진행 표시줄은 다운로드를 몇 배 느리게 만든다

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Stamp = Get-Date -Format "yyyyMMdd"
$BundleName = "crex-$Stamp"
$DistDir = Join-Path $RepoRoot $OutDir
$Staging = Join-Path $DistDir $BundleName

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Write-Note([string]$Message) {
    Write-Host "    $Message" -ForegroundColor DarkGray
}

function Invoke-Native {
    <#
        네이티브 실행 파일을 돌리고 종료 코드로 성공을 판정한다.

        Windows PowerShell 5.1 은 네이티브 명령이 stderr 에 한 줄이라도 쓰면
        ErrorRecord 로 감싸고, $ErrorActionPreference='Stop' 이면 거기서 죽는다.
        pip 는 정상 동작 중에도 stderr 를 쓰므로 그대로 두면 성공한 명령에
        걸려 넘어진다. 그래서 이 구간만 Continue 로 낮추고 $LASTEXITCODE 로 본다.
    #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$What
    )

    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $FilePath @Arguments 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }

    if ($code -ne 0) {
        $tail = ($output | Select-Object -Last 15) -join "`n"
        throw "$What 실패 (종료 코드 $code):`n$tail"
    }
    return $output
}

# --------------------------------------------------------------------------
# 0. 준비
# --------------------------------------------------------------------------

Write-Step "준비"

# 버전은 crex/__init__.py 한 곳에서만 관리한다. 여기에 또 적으면 번들 매니페스트가
# 소스와 다른 버전을 주장하게 된다 — 반입 심사에서 대조하는 값이라 특히 나쁘다.
$initPath = Join-Path $RepoRoot "crex\__init__.py"
$CrexVersion = "unknown"
if (Test-Path $initPath) {
    $match = Select-String -Path $initPath -Pattern '^__version__\s*=\s*"([^"]+)"'
    if ($match) { $CrexVersion = $match.Matches[0].Groups[1].Value }
}
Write-Note "crex $CrexVersion"

$minor = [int]($PythonVersion.Split('.')[1])
if ($minor -lt 11) {
    throw "Python $PythonVersion 은 쓸 수 없다. tomllib 이 3.11 부터 표준이라 3.11 이상이 필요하다."
}

if (Test-Path $Staging) {
    Write-Note "기존 $Staging 제거"
    Remove-Item $Staging -Recurse -Force
}
New-Item -ItemType Directory -Path $Staging -Force | Out-Null
Write-Note "작업 위치: $Staging"

# --------------------------------------------------------------------------
# 1. 소스 복사
# --------------------------------------------------------------------------

Write-Step "소스 복사"

$Sources = @("crex", "rules", "eval", "tests", "docs", "wiki", "tools")
$Files = @(
    "README.md", "CLAUDE.md", "AGENTS.md", "crex.example.toml",
    "requirements.txt", "requirements-optional.txt", ".gitignore"
)

foreach ($dir in $Sources) {
    $src = Join-Path $RepoRoot $dir
    if (-not (Test-Path $src)) {
        Write-Warning "$dir 이 없다. 건너뛴다."
        continue
    }
    # __pycache__ 와 리뷰 산출물은 제외한다. 반입 심사 대상만 늘린다.
    robocopy $src (Join-Path $Staging $dir) /E /NFL /NDL /NJH /NJS /NP `
        /XD __pycache__ .git reports .opencodereview /XF *.pyc *.pyo | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "$dir 복사 실패 (robocopy $LASTEXITCODE)" }
    Write-Note "$dir"
}

# MSBuild 로거는 `crex compiledb` 의 재료다. 조용히 빠지면 폐쇄망에서
# C++ 프로젝트의 compile_commands.json 을 만들 방법이 없어진다 — 그런데 그
# 사실은 몇 주 뒤 누가 clang-tidy 결과를 의심할 때에야 드러난다.
$LoggerFiles = @(
    "tools\msbuild-compiledb\CompileCommandsJson.cs",
    "tools\msbuild-compiledb\CompileCommandsJson.crex.csproj",
    "tools\msbuild-compiledb\LICENSE"
)
foreach ($needed in $LoggerFiles) {
    if (-not (Test-Path (Join-Path $Staging $needed))) {
        throw "$needed 가 번들에 없다. 이게 빠지면 crex compiledb 의 MSBuild 경로가 죽는다."
    }
}
Write-Note "msbuild-compiledb 로거 소스 확인"

foreach ($file in $Files) {
    $src = Join-Path $RepoRoot $file
    if (Test-Path $src) {
        Copy-Item $src -Destination $Staging
        Write-Note $file
    }
}

# 골든셋 디렉터리 뼈대는 남긴다 (내용은 팀이 채운다)
New-Item -ItemType Directory -Path (Join-Path $Staging "eval\golden\diffs") -Force | Out-Null

# --------------------------------------------------------------------------
# 2. Python 런타임
# --------------------------------------------------------------------------

if ($SkipRuntime) {
    Write-Step "Python 런타임 건너뜀 (-SkipRuntime)"
    Write-Note "대상 장비에 Python $PythonVersion 이상이 있어야 한다"
} else {
    Write-Step "Python $PythonVersion 임베더블 내려받기"

    $EmbedZip = Join-Path $DistDir "python-$PythonVersion-embed-amd64.zip"
    $EmbedUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"

    if (Test-Path $EmbedZip) {
        Write-Note "이미 받아둔 것을 쓴다: $EmbedZip"
    } else {
        Write-Note $EmbedUrl
        try {
            Invoke-WebRequest -Uri $EmbedUrl -OutFile $EmbedZip -UseBasicParsing
        } catch {
            throw "임베더블 패키지를 받지 못했다. 버전이 맞는지 확인하라 ($EmbedUrl): $_"
        }
    }

    $RuntimeDir = Join-Path $Staging "runtime"
    Expand-Archive -Path $EmbedZip -DestinationPath $RuntimeDir -Force
    Write-Note "runtime/ 에 풀었다"

    # ._pth 를 고쳐 번들 루트와 pylibs 를 sys.path 에 넣는다.
    #
    # 임베더블 패키지는 sys.path 를 ._pth 파일로 못 박는다. 기본값에는 번들
    # 루트가 없어서 `python -m crex` 가 crex 패키지를 못 찾는다. 경로는 python.exe 가
    # 있는 디렉터리 기준 상대경로다.
    $PthFile = Get-ChildItem -Path $RuntimeDir -Filter "python*._pth" | Select-Object -First 1
    if (-not $PthFile) { throw "runtime 에 ._pth 파일이 없다. 임베더블 패키지가 맞는지 확인하라." }

    $zipName = (Get-ChildItem -Path $RuntimeDir -Filter "python*.zip" | Select-Object -First 1).Name
    @(
        $zipName
        "."
        ".."                # 번들 루트 — crex 패키지가 여기 있다
        "..\pylibs"         # 미리 설치해둔 서드파티
        # pywin32 는 자기 .pth 로 아래 둘을 sys.path 에 넣는데, --target 설치본의
        # .pth 는 실행되지 않는다. 직접 적어준다. 없는 경로는 그냥 무시된다.
        "..\pylibs\win32"
        "..\pylibs\win32\lib"
        "import site"
    ) | Set-Content -Path $PthFile.FullName -Encoding ascii
    Write-Note "$($PthFile.Name) 수정 — 번들 루트와 pylibs 를 sys.path 에 추가"
}

# --------------------------------------------------------------------------
# 3. 서드파티 (MCP 서버용)
# --------------------------------------------------------------------------

if ($SkipDeps) {
    Write-Step "서드파티 건너뜀 (-SkipDeps)"
    Write-Note "코어는 표준 라이브러리만 쓰므로 review/scan/doctor 와 테스트는 그대로 동작한다"
    Write-Note "python -m crex.mcp (Zed 연동) 는 쓸 수 없다"
} else {
    Write-Step "서드파티 내려받기 (fastmcp, GitPython, TreeSitter, Uvicorn)"

    $ReqFile = Join-Path $RepoRoot "requirements.txt"
    $WheelDir = Join-Path $Staging "wheels"
    $LibDir = Join-Path $Staging "pylibs"
    $pyTag = $PythonVersion.Split('.')[0] + $PythonVersion.Split('.')[1]

    # wheel 원본도 함께 담는다. 대상 장비의 다른 Python 에 직접 설치하고 싶을 때 쓴다.
    Write-Note "wheels/ 로 내려받는 중 (몇 분 걸린다)"
    Invoke-Native -FilePath "python" -What "pip download" -Arguments @(
        "-m", "pip", "download", "-r", $ReqFile, "-d", $WheelDir,
        "--platform", "win_amd64", "--python-version", $pyTag,
        "--only-binary=:all:", "--quiet"
    ) | Out-Null

    # 폐쇄망에서 pip 를 아예 안 돌려도 되도록 미리 풀어둔다.
    # 보안 검토에서 '설치 스크립트 실행 없음' 은 설명하기 쉬운 조건이다.
    Write-Note "pylibs/ 로 미리 설치하는 중"
    Invoke-Native -FilePath "python" -What "pip install --target" -Arguments @(
        "-m", "pip", "install", "-r", $ReqFile, "--target", $LibDir,
        "--platform", "win_amd64", "--python-version", $pyTag,
        "--only-binary=:all:", "--no-compile", "--upgrade", "--quiet"
    ) | Out-Null

    $wheelCount = (Get-ChildItem $WheelDir -Filter *.whl).Count
    Write-Note "wheel $wheelCount 개, pylibs/ 준비 완료"

    # ---- pywin32 뒷정리 ----
    #
    # MCP SDK 는 Windows 에서 stdio 를 다루려고 pywintypes 를 쓴다. pywin32 는
    # `pip install --target` 으로 넣으면 그대로는 import 되지 않는다. 이유가 둘이다.
    #
    #   1. pywin32.pth 가 win32, win32\lib 을 sys.path 에 넣어주는데, --target
    #      디렉터리의 .pth 는 site 처리 대상이 아니라 실행되지 않는다.
    #   2. pywintypes312.dll 이 pywin32_system32\ 에 있어 DLL 검색 경로 밖이다.
    #
    # 여기서 미리 해결해 둔다. 대상 장비에서 후처리 스크립트를 돌릴 필요가 없다.
    $sys32 = Join-Path $LibDir "pywin32_system32"
    if (Test-Path $sys32) {
        if ($SkipRuntime) {
            Write-Note "pywin32 감지 — runtime 미포함이라 DLL 배치는 대상 장비 몫이다"
        } else {
            # python.exe 가 있는 디렉터리는 항상 DLL 검색 대상이다.
            Copy-Item (Join-Path $sys32 "*.dll") -Destination (Join-Path $Staging "runtime") -Force
            Write-Note "pywin32 DLL 을 runtime\ 으로 복사"
        }
    }
}

# --------------------------------------------------------------------------
# 4. 실행 진입점
# --------------------------------------------------------------------------

Write-Step "실행 진입점 생성"

if (-not $SkipRuntime) {
    # cmd 래퍼 — 번들 안의 python 을 쓰므로 PATH 를 건드릴 필요가 없다
    @'
@echo off
rem crex 실행 진입점. 번들 안의 Python 을 쓴다.
rem   crex review --staged
rem   crex doctor
"%~dp0runtime\python.exe" -m crex %*
'@ | Set-Content -Path (Join-Path $Staging "crex.cmd") -Encoding ascii

    @'
@echo off
rem 반입 무결성 확인. 여기서 실패하면 파일이 덜 복사된 것이다.
"%~dp0runtime\python.exe" "%~dp0tests\run_all.py"
'@ | Set-Content -Path (Join-Path $Staging "테스트.cmd") -Encoding ascii

    @'
@echo off
rem Zed MCP 서버. Zed settings.json 의 command 로 이 파일을 지정해도 된다.
"%~dp0runtime\python.exe" -m crex.mcp %*
'@ | Set-Content -Path (Join-Path $Staging "crex-mcp.cmd") -Encoding ascii

    Write-Note "crex.cmd / crex-mcp.cmd / 테스트.cmd"
}

# --------------------------------------------------------------------------
# 5. 매니페스트
# --------------------------------------------------------------------------

Write-Step "매니페스트 생성"

$manifestPath = Join-Path $Staging "MANIFEST.txt"
$entries = Get-ChildItem -Path $Staging -Recurse -File |
    Where-Object { $_.Name -ne "MANIFEST.txt" } |
    Sort-Object FullName

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("# CREX 반입 번들 매니페스트")
$lines.Add("# crex: $CrexVersion")
$lines.Add("# 생성: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')")
$lines.Add("# Python: $(if ($SkipRuntime) { '미포함' } else { $PythonVersion })")
$lines.Add("# 서드파티: $(if ($SkipDeps) { '미포함' } else { 'fastmcp, GitPython' })")
$lines.Add("#")
$lines.Add("# SHA256  상대경로")

foreach ($entry in $entries) {
    $rel = $entry.FullName.Substring($Staging.Length + 1)
    $hash = (Get-FileHash -Path $entry.FullName -Algorithm SHA256).Hash
    $lines.Add("$hash  $rel")
}
$lines | Set-Content -Path $manifestPath -Encoding utf8
Write-Note "파일 $($entries.Count) 개의 SHA256 기록"

# --------------------------------------------------------------------------
# 6. 압축
# --------------------------------------------------------------------------

Write-Step "압축"

$ZipPath = Join-Path $DistDir "$BundleName.zip"
if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
Compress-Archive -Path $Staging -DestinationPath $ZipPath -CompressionLevel Optimal

$zipHash = (Get-FileHash -Path $ZipPath -Algorithm SHA256).Hash
$sizeMB = [math]::Round((Get-Item $ZipPath).Length / 1MB, 1)

# zip 해시를 따로 남긴다. 반입 신청서에 적고, 반대편에서 대조한다.
"$zipHash  $BundleName.zip" | Set-Content -Path "$ZipPath.sha256" -Encoding ascii

Write-Host ""
Write-Host "완료" -ForegroundColor Green
Write-Host "  버전   : crex $CrexVersion"
Write-Host "  번들   : $ZipPath ($sizeMB MB)"
Write-Host "  SHA256 : $zipHash"
Write-Host "  해시   : $ZipPath.sha256"
Write-Host ""
Write-Host "다음:" -ForegroundColor Yellow
Write-Host "  1. zip 과 .sha256 을 함께 반입 신청한다"
Write-Host "  2. 폐쇄망에서 압축을 풀고 tools\verify.ps1 을 실행한다"
Write-Host "  3. 자세한 절차는 번들 안의 docs\transfer.md 를 본다"
