# Local watcher runner (Task Scheduler entry point).
# Watches the stores whose bot protection blocks GitHub's datacenter IPs
# (config "cloud": false) from this PC's residential connection, and shares
# state with the cloud watcher through git.
# Usage: run_local.ps1 [sweep]
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Set-Location $PSScriptRoot
Start-Transcript -Path "$PSScriptRoot\local.log" -Append | Out-Null
try {
    # load webhook secrets from the gitignored local.env
    if (Test-Path "$PSScriptRoot\local.env") {
        Get-Content "$PSScriptRoot\local.env" | ForEach-Object {
            if ($_ -match '^\s*([^#=\s][^=]*)=(.*)$') {
                [Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), "Process")
            }
        }
    }
    $env:WATCHER_PROFILE = "local"

    # commit any leftover state from an interrupted cycle, then sync
    git add state.json 2>$null
    git -c user.name="restock-watcher-local" -c user.email="local@users.noreply.github.com" commit -m "update local watcher state [skip ci]" 2>$null | Out-Null
    git pull --rebase -X theirs origin main 2>$null | Out-Null

    if ($args.Count -gt 0 -and $args[0] -eq "sweep") {
        python watcher.py --sweep
    } else {
        python watcher.py
    }

    git add state.json 2>$null
    git -c user.name="restock-watcher-local" -c user.email="local@users.noreply.github.com" commit -m "update local watcher state [skip ci]" 2>$null | Out-Null
    git pull --rebase -X theirs origin main 2>$null | Out-Null
    git push origin main 2>$null | Out-Null
} finally {
    Stop-Transcript | Out-Null
    # keep the log from growing forever
    $log = "$PSScriptRoot\local.log"
    if ((Test-Path $log) -and (Get-Item $log).Length -gt 2MB) {
        Get-Content $log -Tail 2000 | Set-Content "$log.tmp" -Encoding utf8
        Move-Item "$log.tmp" $log -Force
    }
}
