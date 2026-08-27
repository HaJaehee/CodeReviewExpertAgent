<#
.SYNOPSIS
    CREX 웹 UI 실행 단축 스크립트 (run_viz.ps1 호출)
#>
[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$script = Join-Path $PSScriptRoot "run_viz.ps1"
if ($NoBrowser) {
    & $script -NoBrowser @Arguments
} else {
    & $script @Arguments
}
exit $LASTEXITCODE
