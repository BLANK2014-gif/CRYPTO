# =============================================================================
# Roostoo Bot -- Real-Time Live Watcher & Auto-Sync to AWS EC2
# =============================================================================
# Keeps local code and AWS EC2 continuously synchronized.
# Whenever you save a file in VS Code or any editor, it automatically
# uploads the file to EC2 and restarts the bot service within seconds.
# =============================================================================

$KeyPath = "C:\Users\daksh\Downloads\crypto2.pem"
$HostIP  = "13.211.41.40"
$User    = "ubuntu"
$RemoteDir = "/home/ubuntu/roostoo-bot"
$LocalDir  = $PSScriptRoot

Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host "     ROOSTOO BOT -- REAL-TIME AUTO-SYNC WATCHER TO AWS (Sydney)" -ForegroundColor Cyan
Write-Host "============================================================================" -ForegroundColor Cyan
Write-Host "Monitoring: $LocalDir" -ForegroundColor Green
Write-Host "Target:     $User@${HostIP}:$RemoteDir" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop watching." -ForegroundColor Yellow
Write-Host "----------------------------------------------------------------------------" -ForegroundColor Gray

# FileSystemWatcher
$Watcher = New-Object System.IO.FileSystemWatcher
$Watcher.Path = $LocalDir
$Watcher.IncludeSubdirectories = $true
$Watcher.EnableRaisingEvents = $true
$Watcher.Filter = "*.*"

$LastSync = [DateTime]::MinValue
$DebounceMs = 1500

$SyncAction = {
    param($EventPath, $ChangeType)

    # Filter out noise (git, pycache, logs, tmp)
    if ($EventPath -match "(\.git|__pycache__|\.pytest_cache|logs\\|\.pid|\.tmp)") {
        return
    }

    $Now = [DateTime]::Now
    if (($Now - $script:LastSync).TotalMilliseconds -lt $script:DebounceMs) {
        return
    }
    $script:LastSync = $Now

    $RelativePath = $EventPath.Substring($script:LocalDir.Length).TrimStart("\/")
    $RemoteFile = "$script:RemoteDir/" + ($RelativePath -replace "\\", "/")
    $RemoteParent = [System.IO.Path]::GetDirectoryName($RemoteFile) -replace "\\", "/"

    Write-Host "`n[$([DateTime]::Now.ToString('HH:mm:ss'))] File changed: $RelativePath ($ChangeType)" -ForegroundColor Yellow
    Write-Host "  -> Uploading to EC2..." -ForegroundColor DarkGray

    # Upload file
    & scp -i "$script:KeyPath" -o StrictHostKeyChecking=no "$EventPath" "$script:User@${script:HostIP}:$RemoteFile" 2>$null

    if ($LASTEXITCODE -eq 0) {
        Write-Host "  -> Upload OK. Reloading roostoo-bot on EC2..." -ForegroundColor Green
        & ssh -i "$script:KeyPath" -o StrictHostKeyChecking=no "$script:User@${script:HostIP}" "sudo systemctl restart roostoo-bot" 2>$null
        Write-Host "  -> [OK] Bot reloaded on AWS!" -ForegroundColor Cyan
    } else {
        Write-Host "  -> [WARN] Sync failed. Will retry on next save." -ForegroundColor Red
    }
}

Register-ObjectEvent $Watcher "Changed" -Action { & $SyncAction $Event.SourceEventArgs.FullPath $Event.SourceEventArgs.ChangeType } | Out-Null
Register-ObjectEvent $Watcher "Created" -Action { & $SyncAction $Event.SourceEventArgs.FullPath $Event.SourceEventArgs.ChangeType } | Out-Null

while ($true) {
    Start-Sleep -Seconds 1
}
