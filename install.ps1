param(
    [string]$Python,
    [switch]$DryRun,
    [switch]$CliOnly,
    [switch]$RulesOnly
)
$ErrorActionPreference = 'Stop'

# Probe actual interpreters; Windows Store aliases are not Python runtimes.
$candidates = @()
if ($Python) { $candidates += ,@($Python) }
else {
    $candidates += ,@('py.exe', '-3')
    $candidates += ,@('python.exe')
    $candidates += ,@('python3.exe')
}
$runtime = $null
foreach ($candidate in $candidates) {
    try {
        $exe = (Get-Command $candidate[0] -ErrorAction Stop).Source
        if ($exe -like '*\Microsoft\WindowsApps\*') { continue }
        $prefix = @($candidate | Select-Object -Skip 1)
        $resolved = & $exe @prefix -c 'import sys; sys.exit(1) if sys.version_info < (3, 9) else print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $resolved -and (Test-Path -LiteralPath $resolved)) {
            $runtime = $resolved
            break
        }
    } catch { if ($Python) { throw } }
}
if (-not $runtime) { throw 'Python >= 3.9 is required. Pass -Python with the full python.exe path.' }
$options = @()
if ($DryRun) { $options += '--dry-run' }
if ($CliOnly) { $options += '--cli-only' }
if ($RulesOnly) { $options += '--rules-only' }
& $runtime -X utf8 (Join-Path $PSScriptRoot 'scripts\install_windows.py') @options
exit $LASTEXITCODE
