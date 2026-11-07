param([string]$Destination='D:\VeiruneBackups')
$ErrorActionPreference='Stop'
. "$PSScriptRoot\backup-common.ps1"
$copy=Get-ChildItem -LiteralPath $Destination -Filter 'veirune-*.vbackup' -File | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
if (-not $copy) {throw 'No PC backup found'}
$temporary=Join-Path ([IO.Path]::GetTempPath()) ('veirune-backup-'+[guid]::NewGuid().ToString('N'))
Set-VeirunePrivateDirectory $temporary
try {
  $manifest=Expand-VeiruneBundle (Unprotect-VeiruneBytes ([IO.File]::ReadAllBytes($copy.FullName))) (Join-Path $temporary 'recovered')
  & python "$PSScriptRoot\..\backup.py" restore (Join-Path $temporary 'recovered\approvals.sqlite3') --target (Join-Path $temporary 'trial.sqlite3')
  if ($LASTEXITCODE -ne 0) {throw 'Backup restoration or complete table verification failed'}
  Write-Output ('PASS: latest encrypted PC backup restores every table and private configuration. Snapshot: '+$copy.Name)
} finally {Remove-VeiruneTemporaryDirectory $temporary}
