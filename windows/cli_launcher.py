"""Generate text-pipeline and native-byte Windows CLI entry points."""


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def launcher_scripts(python, target):
    # Both scripts use the same explicit argv quoting. Their output contracts
    # differ: a direct .ps1 emits PowerShell objects; the .cmd process emits bytes.
    start = "\ufeff# everos-bridge managed launcher\n" + r'''
$ErrorActionPreference = 'Stop'
function Quote-WindowsArgument([string]$value) {
    $value = [regex]::Replace($value, '(\\*)"', '$1$1\"')
    $value = [regex]::Replace($value, '(\\+)$', '$1$1')
    return '"' + $value + '"'
}
$start = New-Object System.Diagnostics.ProcessStartInfo
$start.UseShellExecute = $false
# The interactive CLI must share the caller's console to receive Ctrl+C.
$start.CreateNoWindow = $false
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$start.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$start.StandardErrorEncoding = [System.Text.Encoding]::UTF8
''' + "$start.FileName = {}\n$arguments = @('-X', 'utf8', {}) + $args\n".format(
        ps_quote(python), ps_quote(target)) + r'''
$start.Arguments = ($arguments | ForEach-Object { Quote-WindowsArgument ([string]$_) }) -join ' '
$process = [System.Diagnostics.Process]::Start($start)
try {
'''
    finish = r'''
    $process.WaitForExit()
    $exitCode = $process.ExitCode
} finally {
    $process.Dispose()
}
exit $exitCode
'''
    native = r'''
    $stdout = $process.StandardOutput.BaseStream.CopyToAsync([Console]::OpenStandardOutput())
    $stderr = $process.StandardError.BaseStream.CopyToAsync([Console]::OpenStandardError())
    [void]$stdout.GetAwaiter().GetResult()
    [void]$stderr.GetAwaiter().GetResult()
'''
    pipeline = r'''
    # Read both pipes concurrently, but emit objects only on this runspace.
    # Polling also lets PowerShell stop its pipeline on Ctrl+C.
    $stdout = $process.StandardOutput.ReadLineAsync()
    $stderr = $process.StandardError.ReadLineAsync()
    while ($null -ne $stdout -or $null -ne $stderr) {
        $pending = @()
        if ($null -ne $stdout) { $pending += $stdout }
        if ($null -ne $stderr) { $pending += $stderr }
        [void][System.Threading.Tasks.Task]::WaitAny([System.Threading.Tasks.Task[]]$pending, 100)
        if ($null -ne $stdout -and $stdout.IsCompleted) {
            $line = $stdout.GetAwaiter().GetResult()
            if ($null -eq $line) { $stdout = $null } else {
                Write-Output $line
                $stdout = $process.StandardOutput.ReadLineAsync()
            }
        }
        if ($null -ne $stderr -and $stderr.IsCompleted) {
            $line = $stderr.GetAwaiter().GetResult()
            if ($null -eq $line) { $stderr = $null } else {
                Write-Error -Message $line -ErrorId NativeCommandError -ErrorAction Continue
                $stderr = $process.StandardError.ReadLineAsync()
            }
        }
    }
'''
    return {
        # No interpolated Unicode paths in batch source; cmd expands %~dp0.
        "everos.cmd": '@rem everos-bridge managed launcher\n@echo off\n@setlocal DisableDelayedExpansion\npowershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0everos-native.ps1" %*\n@exit /b %ERRORLEVEL%\n',
        "everos.ps1": start + pipeline + finish,
        "everos-native.ps1": start + native + finish,
    }
