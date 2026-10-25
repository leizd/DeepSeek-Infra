param([Parameter(Mandatory)][string]$Binary, [Parameter(Mandatory)][string]$Output)
$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_OS -ne 'Windows') {
    throw 'Native Windows custody execution is cloud-only'
}
$binaryPath = [IO.Path]::GetFullPath($Binary).Replace([IO.Path]::DirectorySeparatorChar, '/')
$outputPath = [IO.Path]::GetFullPath($Output).Replace([IO.Path]::DirectorySeparatorChar, '/')
if (-not (Test-Path -LiteralPath $binaryPath -PathType Leaf)) { throw 'Native fixture missing' }
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null
$source = (& git rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $source -ne $env:GITHUB_SHA) { throw 'Unexpected Windows source revision' }
$sourceTree = (& git rev-parse 'HEAD^{tree}').Trim()
if ($LASTEXITCODE -ne 0 -or (& git status --porcelain --untracked-files=all)) { throw 'Dirty Windows source snapshot' }
$temporary = Join-Path $env:RUNNER_TEMP ('native-mcp-windows-' + [guid]::NewGuid().ToString('N'))
$temporary = [IO.Path]::GetFullPath($temporary).Replace([IO.Path]::DirectorySeparatorChar, '/')
$runnerTemp = [IO.Path]::GetFullPath($env:RUNNER_TEMP).Replace([IO.Path]::DirectorySeparatorChar, '/').TrimEnd('/')
if (-not $temporary.StartsWith($runnerTemp + '/', [StringComparison]::OrdinalIgnoreCase)) { throw 'Windows temp boundary mismatch' }
New-Item -ItemType Directory -Path $temporary | Out-Null
$rows = [Collections.Generic.List[object]]::new()
$report = [ordered]@{
    sourceCommit = $source; sourceTree = $sourceTree; runId = $env:GITHUB_RUN_ID; event = $env:GITHUB_EVENT_NAME
    status = 'FAIL'; readiness = 'NOT_READY'; fixtureOnly = $true; fullSandboxQualified = $false
    realRedisLeaseQualified = $false; allWindowsAppFeaturesQualified = $false; fullMigrationQualified = $false
    localNativeCompilation = $false; actualUserWindows11Qualified = $false
    runnerOS = $env:RUNNER_OS; runnerImage = $env:ImageOS
    binarySha256 = (Get-FileHash -LiteralPath $binaryPath -Algorithm SHA256).Hash.ToLowerInvariant()
    cases = $rows
}

function Start-OwnedDriver([string]$trace, [string]$kind) {
    $workspace = (Join-Path $trace 'workspace').Replace([IO.Path]::DirectorySeparatorChar, '/')
    New-Item -ItemType Directory -Force -Path $workspace | Out-Null
    [IO.File]::WriteAllText((Join-Path $workspace 'input.txt'), "native Windows fixture`n")
    $outcome = (Join-Path $trace 'outcome.json').Replace([IO.Path]::DirectorySeparatorChar, '/')
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $binaryPath
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.WorkingDirectory = $workspace
    foreach ($argument in @('--driver', $workspace, $kind, $outcome)) { $info.ArgumentList.Add($argument) }
    $info.Environment['MCP_TASK_PROGRAM'] = $binaryPath
    $info.Environment['MCP_WINDOWS_TRACE_ROOT'] = $trace
    $process = [Diagnostics.Process]::Start($info)
    $null = $process.SafeHandle
    return $process
}

function Observe-Fixture([string]$file) {
    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    do {
        if (-not (Test-Path -LiteralPath $file)) { Start-Sleep -Milliseconds 10; continue }
        try {
            $value = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
            $process = Get-Process -Id ([int]$value.pid) -ErrorAction Stop
            $null = $process.SafeHandle
            $exe = $process.Path.Replace([IO.Path]::DirectorySeparatorChar, '/')
            if (-not [string]::Equals($exe, $binaryPath, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unowned Windows fixture image' }
            return @{ process = $process; pid = $process.Id; startTicks = $process.StartTime.ToFileTimeUtc().ToString(); executable = $exe }
        } catch {
            if ($_.Exception.Message -eq 'Unowned Windows fixture image') { throw }
            Start-Sleep -Milliseconds 10
        }
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Native Windows fixture identity was not observed alive'
}

function Alive($identity) {
    if ($null -eq $identity) { return $false }
    return -not $identity.process.HasExited
}

function Stop-Owned($identity) {
    if ($null -eq $identity) { return }
    $process = $identity.process
    try {
        if (-not $process.HasExited) {
            if ($process.StartTime.ToFileTimeUtc().ToString() -ne $identity.startTicks) { throw 'Windows fixture creation identity changed' }
            $path = $process.Path.Replace([IO.Path]::DirectorySeparatorChar, '/')
            if (-not [string]::Equals($path, $binaryPath, [StringComparison]::OrdinalIgnoreCase)) { throw 'Windows fixture executable changed' }
            $process.Kill()
            if (-not $process.WaitForExit(3000)) { throw 'Owned Windows fixture did not terminate' }
        }
    } finally { $process.Dispose() }
}

try {
    $controlTrace = Join-Path $temporary 'short control'
    $control = Start-OwnedDriver $controlTrace 'short-control'
    try {
        if (-not $control.WaitForExit(10000) -or $control.ExitCode -ne 0) { throw 'Actual Windows short control failed' }
        $outcome = Get-Content -LiteralPath (Join-Path $controlTrace 'outcome.json') -Raw | ConvertFrom-Json
        if ($outcome.exitCode -ne 0 -or $null -ne $outcome.error -or $outcome.stdout -ne "native Windows short control completed`n") { throw 'Original Windows outcome was not retained' }
        $report.shortControl = $outcome
    } finally { if (-not $control.HasExited) { $control.Kill(); $null = $control.WaitForExit(3000) }; $control.Dispose() }

    foreach ($kind in @('timeout-regular', 'timeout-detached', 'parent-exit', 'driver-crash', 'cancel')) {
        $trace = (Join-Path $temporary ('中文 case ' + $kind)).Replace([IO.Path]::DirectorySeparatorChar, '/')
        $driver = $null; $root = $null; $descendant = $null
        $row = [ordered]@{ kind = $kind; observedAt = [DateTime]::UtcNow.ToString('o') }
        $rows.Add($row)
        try {
            $driver = Start-OwnedDriver $trace $kind
            $root = Observe-Fixture (Join-Path $trace 'root.json')
            $descendant = Observe-Fixture (Join-Path $trace 'descendant.json')
            $row.rootIdentity = @{ pid = $root.pid; startTicks = $root.startTicks; executable = $root.executable }
            $row.descendantIdentity = @{ pid = $descendant.pid; startTicks = $descendant.startTicks; executable = $descendant.executable }
            $row.driverPid = $driver.Id
            if (-not (Alive $root) -or -not (Alive $descendant)) { throw 'Windows live-process precondition failed' }
            $watch = [Diagnostics.Stopwatch]::StartNew()
            if ($kind -eq 'parent-exit') {
                [IO.File]::WriteAllText((Join-Path $trace 'root-release'), 'observer confirmed both live native processes')
                $row.normalRootExitReleased = $true
            } elseif ($kind -eq 'driver-crash') {
                $driver.Kill()
                if (-not $driver.WaitForExit(3000)) { throw 'Windows driver crash was not observed' }
                $row.actualDriverTermination = $true
            } elseif ($kind -eq 'cancel') {
                [IO.File]::WriteAllText((Join-Path $trace 'cancel-request'), 'observer requested native cancellation')
                $row.actualCancelRequested = $true
            }
            while ($watch.ElapsedMilliseconds -lt 3000) { Start-Sleep -Milliseconds 20 }
            $row.sampleElapsedMs = $watch.ElapsedMilliseconds
            $row.rootAliveAtDeadline = Alive $root
            $row.descendantAliveAtDeadline = Alive $descendant
            $row.driverExitedAtDeadline = $driver.HasExited
            $outcomePath = Join-Path $trace 'outcome.json'
            $row.outcomePresentAtDeadline = Test-Path -LiteralPath $outcomePath
            if ($row.outcomePresentAtDeadline) { $row.outcomeAtDeadline = Get-Content -LiteralPath $outcomePath -Raw | ConvertFrom-Json }
            while ($watch.ElapsedMilliseconds -lt 6500) { Start-Sleep -Milliseconds 20 }
            $effect = Join-Path $trace 'late-effect.txt'
            $row.lateEffectPresent = Test-Path -LiteralPath $effect
            if ($row.lateEffectPresent) { $row.lateEffectContents = [IO.File]::ReadAllText($effect) }
            $row.rootAliveBeforeManualCleanup = Alive $root
            $row.descendantAliveBeforeManualCleanup = Alive $descendant
            $row.noLateEffectAndDescendantsStopped = -not $row.rootAliveAtDeadline -and -not $row.descendantAliveAtDeadline -and -not $row.lateEffectPresent -and $row.driverExitedAtDeadline
        } finally {
            # Pinned Process/SafeHandle objects keep cleanup on the observed
            # synthetic processes; no global process-name/tree kill is used.
            Stop-Owned $root
            Stop-Owned $descendant
            if ($null -ne $driver) {
                if (-not $driver.HasExited) { $driver.Kill(); $null = $driver.WaitForExit(3000) }
                $driver.Dispose()
            }
        }
    }
    if (@($rows | Where-Object { -not $_.noLateEffectAndDescendantsStopped }).Count -ne 0) {
        throw 'native Windows descendants remained active or produced a late effect after authority ended'
    }
    $report.status = 'PASS'
    $report.nativeWindowsProcessCustodyQualified = $true
} catch {
    $report.error = $_.Exception.Message
    throw
} finally {
    $report.checkedAt = [DateTime]::UtcNow.ToString('o')
    $report | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $outputPath 'process-custody.json') -Encoding utf8NoBOM
}
