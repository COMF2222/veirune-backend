param(
  [Parameter(Mandatory=$true)][string]$Server,
  [string]$Backup,
  [string]$Destination='D:\VeiruneBackups',
  [string]$Website='D:\projects\fastcloud-website\public'
)
$ErrorActionPreference='Stop'
. "$PSScriptRoot\backup-common.ps1"
if ($Server -notmatch '^root@([0-9.]+)$') {throw 'Use root@NEW_PUBLIC_IPV4'}
$newIp=$Server.Split('@')[1]
$checkout=[IO.Path]::GetFullPath("$PSScriptRoot\..")
git -C $checkout diff --exit-code --quiet
if ($LASTEXITCODE -ne 0) {throw 'Commit the backend changes before migration'}
git -C $checkout diff --cached --exit-code --quiet
if ($LASTEXITCODE -ne 0) {throw 'Commit the staged backend changes before migration'}
$untracked=@(git -C $checkout ls-files --others --exclude-standard)
if ($untracked.Count) {throw 'Commit the new backend source files before migration'}
if ((git -C $checkout branch --show-current) -ne 'main') {throw 'Run the migration from the main branch'}
if (-not $Backup) {$Backup=(Get-ChildItem -LiteralPath $Destination -Filter 'veirune-*.vbackup' -File | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1).FullName}
if (-not $Backup) {throw 'No checked backup found'}
$temporary=Join-Path ([IO.Path]::GetTempPath()) ('veirune-backup-'+[guid]::NewGuid().ToString('N'))
Set-VeirunePrivateDirectory $temporary
try {
  $recovered=Join-Path $temporary 'recovered'
  & "$PSScriptRoot\restore-pc-backup.ps1" -File $Backup -Directory $recovered
  if ($LASTEXITCODE -ne 0) {throw 'Backup validation failed'}
  $bundle=Join-Path $temporary 'source.bundle'
  git -C $checkout bundle create $bundle main
  if ($LASTEXITCODE -ne 0) {throw 'Could not package the committed backend source'}
  $remote='/root/veirune-migration-'+[guid]::NewGuid().ToString('N')
  & ssh -o BatchMode=yes -o StrictHostKeyChecking=yes $Server "umask 077; mkdir '$remote'"
  if ($LASTEXITCODE -ne 0) {throw 'Connect to the new VPS using SSH first and verify its host key'}
  Copy-Item -LiteralPath (Join-Path $recovered '.env') -Destination (Join-Path $temporary 'server.env')
  & scp -q -o BatchMode=yes -o StrictHostKeyChecking=yes $bundle (Join-Path $recovered 'approvals.sqlite3') (Join-Path $recovered 'manifest.json') (Join-Path $temporary 'server.env') "$PSScriptRoot\restore-server.py" "${Server}:$remote/"
  if ($LASTEXITCODE -ne 0) {throw 'Could not upload the private recovery bundle'}
  & ssh -o BatchMode=yes -o StrictHostKeyChecking=yes $Server "python3 '$remote/restore-server.py' --staging '$remote' --ip '$newIp'"
  if ($LASTEXITCODE -ne 0) {throw 'New VPS failed verification; the public endpoint was not changed'}
  & python "$PSScriptRoot\service_endpoint.py" --origin "https://$newIp" --website $Website --key (Join-Path $Destination 'service-endpoint-signing.dpapi')
  if ($LASTEXITCODE -ne 0) {throw 'The server was restored, but endpoint announcement preparation failed'}
  Write-Output 'New VPS is verified. Publish the website service.json to switch clients. The old VPS has not been stopped. Reinstall the backup task with -Server to protect the new VPS.'
} finally {Remove-VeiruneTemporaryDirectory $temporary}
