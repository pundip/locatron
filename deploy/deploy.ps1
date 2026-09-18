<#
.SYNOPSIS
    Deploy Locatron to the container from Windows.

.DESCRIPTION
    Pushes the current branch, then runs deploy/deploy.sh on the container
    over SSH. The script itself always runs on Linux; this is only a trigger.

.EXAMPLE
    .\deploy\deploy.ps1
    .\deploy\deploy.ps1 -Branch dev -SkipTests

.NOTES
    Set the container address once so you do not have to pass it every time:
        [Environment]::SetEnvironmentVariable('LOCATRON_SSH', 'locatron@192.168.1.50', 'User')
    Then restart your terminal.
#>

[CmdletBinding()]
param(
    [string]$SshTarget = $env:LOCATRON_SSH,
    [string]$Branch = "main",
    [switch]$SkipTests,
    [switch]$Force,
    [switch]$NoPush
)

$ErrorActionPreference = "Stop"

if (-not $SshTarget) {
    Write-Error "No SSH target. Pass -SshTarget locatron@<ip> or set `$env:LOCATRON_SSH"
    exit 1
}

if (-not $NoPush) {
    Write-Host "`n==> Pushing $Branch" -ForegroundColor Cyan

    $dirty = git status --porcelain
    if ($dirty) {
        Write-Host "    Uncommitted changes:" -ForegroundColor Yellow
        git status --short
        Write-Error "Commit or stash before deploying."
        exit 1
    }

    git push origin $Branch
    if ($LASTEXITCODE -ne 0) { Write-Error "git push failed"; exit 1 }
}

$deployArgs = @("--branch", $Branch)
if ($SkipTests) { $deployArgs += "--skip-tests" }
if ($Force)     { $deployArgs += "--force" }

Write-Host "`n==> Deploying on $SshTarget" -ForegroundColor Cyan

# -t allocates a TTY so sudo can prompt if the sudoers file is not installed.
ssh -t $SshTarget "/opt/locatron/app/deploy/deploy.sh $($deployArgs -join ' ')"

if ($LASTEXITCODE -ne 0) {
    Write-Error "Deploy failed with exit code $LASTEXITCODE"
    exit $LASTEXITCODE
}

Write-Host "`nDone.`n" -ForegroundColor Green
