<#
.SYNOPSIS
Packages this repository, ships it to an EC2 instance and runs the Docker stack there.

.DESCRIPTION
Reproduces the manual flow validated against a live Ubuntu instance:

  1. bundles the repository with the same exclusions .dockerignore applies to the
     image context (no Windows venv, no local database, no editor caches),
  2. copies the bundle and scripts/provision-ec2.sh to the instance,
  3. unpacks into ~/<RemoteDirectory> and normalizes CRLF in .env,
  4. installs Docker Engine plus the compose plugin (unless -SkipProvision),
  5. builds and starts the three-container stack, then waits for real health,
  6. prints the verified endpoints plus the SSH tunnel and public-link commands.

The code that is deployed is exactly what this working tree contains, so
`git rev-parse --short HEAD` on the instance identifies the running revision.

.EXAMPLE
./scripts/deploy-ec2.ps1 -Instance 203.0.113.10 -KeyPath 'C:\keys\investigator.pem'

.EXAMPLE
./scripts/deploy-ec2.ps1 -Instance 203.0.113.10 -KeyPath 'C:\keys\investigator.pem' -PublicDashboard
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Instance,
    [Parameter(Mandatory)][string]$KeyPath,
    [string]$User = 'ubuntu',
    [string]$RemoteDirectory = 'mlops-investigation-agent',
    [int]$HealthyTimeoutSeconds = 420,
    [switch]$PublicDashboard,
    [switch]$SkipProvision
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$sshTarget = "$User@$Instance"
$keyArgument = '"' + $KeyPath + '"'
$sshArguments = @('-i', $keyArgument, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15')
$scpArguments = @('-i', $keyArgument, '-o', 'BatchMode=yes')

function Invoke-Remote {
    param([Parameter(Mandatory)][string]$Command)
    # Native stderr output must not become a terminating error while
    # $ErrorActionPreference is 'Stop'; exit codes are checked explicitly.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $output = & ssh @sshArguments $sshTarget $Command 2>&1
    $exit = $LASTEXITCODE
    $ErrorActionPreference = $previous
    $stdout = (($output | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] } | ForEach-Object { $_.ToString() }) -join "`n").Trim()
    $stderr = (($output | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { $_.ToString() }) -join "`n").Trim()
    if ($exit -ne 0) { throw "remote command failed (exit $exit): $Command`n$stderr`n$stdout" }
    return $stdout
}

function Invoke-Copy {
    param([Parameter(Mandatory)][string]$Source, [Parameter(Mandatory)][string]$Destination)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $output = & scp @scpArguments $Source $Destination 2>&1
    $exit = $LASTEXITCODE
    $ErrorActionPreference = $previous
    if ($exit -ne 0) {
        throw "scp $Source -> $Destination failed (exit $exit): $((($output | ForEach-Object { $_.ToString() }) -join ' ').Trim())"
    }
}

function Invoke-RemoteScript {
    param([Parameter(Mandatory)][string]$Script)
    $normalized = $Script.Replace("`r`n", "`n")
    $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($normalized))
    return Invoke-Remote -Command "echo $encoded | base64 -d > /tmp/mlops-step.sh && bash /tmp/mlops-step.sh"
}

Write-Host "Repository : $repoRoot"
Write-Host "Instance   : $sshTarget"
Write-Host "Remote dir : ~/$RemoteDirectory"

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) { throw 'ssh is not available on PATH' }
if (-not (Get-Command scp -ErrorAction SilentlyContinue)) { throw 'scp is not available on PATH' }
if (-not (Get-Command tar.exe -ErrorAction SilentlyContinue)) { throw 'tar.exe is not available on PATH' }
if (-not (Test-Path (Join-Path $PSScriptRoot 'provision-ec2.sh'))) { throw 'scripts/provision-ec2.sh is missing' }

# ------------------------------------------------------------------ 1. bundle
$bundle = Join-Path ([IO.Path]::GetTempPath()) 'mlops-ec2-bundle.tgz'
Remove-Item -Force $bundle -ErrorAction SilentlyContinue
& tar.exe -czf $bundle -C $repoRoot `
    --exclude='Agent2' `
    --exclude='.mypy_cache' `
    --exclude='.pytest_cache' `
    --exclude='.kilo' `
    --exclude='data/investigations.sqlite3' `
    --exclude='data/simulation_state.json' `
    '.'
if ($LASTEXITCODE -ne 0) { throw 'packaging the repository failed' }
Write-Host ("Bundle     : {0} KB" -f [math]::Round((Get-Item $bundle).Length / 1KB, 1))

# ------------------------------------------------------------------ 2. copy
Invoke-Copy -Source $bundle -Destination "${sshTarget}:/tmp/mlops-ec2-bundle.tgz"
Invoke-Copy -Source (Join-Path $PSScriptRoot 'provision-ec2.sh') -Destination "${sshTarget}:/tmp/provision-ec2.sh"

# ------------------------------------------------------------------ 3. unpack
$unpack = @'
set -euo pipefail
rm -rf ~/__REMOTE_DIR__
mkdir -p ~/__REMOTE_DIR__
tar -xzf /tmp/mlops-ec2-bundle.tgz -C ~/__REMOTE_DIR__ --warning=no-unknown-keyword
if [ -f ~/__REMOTE_DIR__/.env ]; then tr -d '\r' < ~/__REMOTE_DIR__/.env > /tmp/env.clean && mv /tmp/env.clean ~/__REMOTE_DIR__/.env; fi
find ~/__REMOTE_DIR__/scripts -name '*.sh' -exec sed -i 's/\r$//' {} + 2>/dev/null || true
echo "unpacked=$(ls -1 ~/__REMOTE_DIR__ | wc -l) entries"
echo "commit=$(cd ~/__REMOTE_DIR__ && git rev-parse --short HEAD 2>/dev/null || echo unknown)"
'@
Write-Host '--- unpacking ---'
Write-Host (Invoke-RemoteScript -Script $unpack.Replace('__REMOTE_DIR__', $RemoteDirectory))

# ------------------------------------------------------------------ 4. provision
if ($SkipProvision) {
    Write-Host '--- provisioning skipped (-SkipProvision) ---'
}
else {
    Write-Host '--- provisioning Docker (this can take a minute) ---'
    Write-Host (Invoke-Remote -Command 'bash /tmp/provision-ec2.sh 2>&1 | grep -v SCHILY')
}

# ------------------------------------------------------------------ 5. build and start
$deploy = @'
set -euo pipefail
cd ~/__REMOTE_DIR__
sudo docker compose up -d --build
'@
Write-Host '--- building and starting the stack ---'
Write-Host (Invoke-RemoteScript -Script $deploy.Replace('__REMOTE_DIR__', $RemoteDirectory))

if ($PublicDashboard) {
    $publish = @'
set -euo pipefail
printf 'services:\n  dashboard:\n    ports: !override\n      - "0.0.0.0:7860:7860"\n' > /tmp/mlops-public-port.yaml
cd ~/__REMOTE_DIR__
sudo docker compose -f compose.yaml -f /tmp/mlops-public-port.yaml up -d
'@
    Write-Host '--- publishing the dashboard beyond loopback ---'
    Write-Host (Invoke-RemoteScript -Script $publish.Replace('__REMOTE_DIR__', $RemoteDirectory))
}

# ------------------------------------------------------------------ 6. wait for health
$wait = @'
set -uo pipefail
cd ~/__REMOTE_DIR__
ready=no
for attempt in $(seq 1 __TRIES__); do
  if curl -fsS -o /dev/null http://127.0.0.1:8000/health && curl -fsS -o /dev/null http://127.0.0.1:7860/; then
    ready=yes
    break
  fi
  sleep 5
done
echo "ready=$ready after $attempt attempt(s)"
echo "--- status ---"
sudo docker compose ps
if [ "$ready" != "yes" ]; then
  echo "--- last dashboard logs ---"
  sudo docker compose logs --tail 30 dashboard
  exit 1
fi
'@
$tries = [int][math]::Ceiling($HealthyTimeoutSeconds / 5)
Write-Host '--- waiting for the stack to answer ---'
Write-Host (Invoke-RemoteScript -Script $wait.Replace('__REMOTE_DIR__', $RemoteDirectory).Replace('__TRIES__', "$tries"))

# ------------------------------------------------------------------ 7. verify and report
$verify = @'
set -uo pipefail
cd ~/__REMOTE_DIR__
echo "commit=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "dashboard_sha256=$(sha256sum src/mlops_investigator/dashboard.py | cut -d ' ' -f 1)"
for path in / /config; do
  printf '%s -> %s\n' "http://127.0.0.1:7860$path" "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:7860$path")"
done
printf '%s -> %s %s\n' "http://127.0.0.1:8000/health" "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health)" "$(curl -s http://127.0.0.1:8000/health)"
printf '%s -> %s\n' "http://127.0.0.1:8000/metrics" "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/metrics)"
'@
Write-Host '--- verification ---'
Write-Host (Invoke-RemoteScript -Script $verify.Replace('__REMOTE_DIR__', $RemoteDirectory))

$stray = Join-Path ([IO.Path]::GetTempPath()) 'mlops-ec2-bundle.tgz'
if (Test-Path $stray) { Remove-Item -Force $stray }

Write-Host ''
Write-Host 'The stack is running on the instance. Reach it with either option:'
Write-Host ("  tunnel : ssh -i `"$KeyPath`" -N -L 17860:127.0.0.1:7860 $sshTarget")
Write-Host '           then open http://127.0.0.1:17860 (pick a free local port; 7860 may be taken locally)'
Write-Host '           login = GRADIO_AUTH_USERNAME / GRADIO_AUTH_PASSWORD from your .env'
if ($PublicDashboard) {
    try {
        $publicIp = (Invoke-RestMethod -Uri 'https://checkip.amazonaws.com' -TimeoutSec 10).Trim()
        Write-Host ''
        Write-Host '  public : allow inbound TCP 7860 from your IP in the instance security group, then open'
        Write-Host "           http://${Instance}:7860  (bound on 0.0.0.0, plain HTTP, auth enforced)"
        Write-Host "           aws ec2 authorize-security-group-ingress --group-id <sg-id> --protocol tcp --port 7860 --cidr $publicIp/32"
    }
    catch {
        Write-Host '  public : allow inbound TCP 7860 from your IP in the instance security group'
    }
}
else {
    Write-Host '  public : re-run with -PublicDashboard to also bind 0.0.0.0:7860, then open the security group'
}
Write-Host ''
Write-Host 'Daily operations on the instance:'
Write-Host ("  ssh -i `"$KeyPath`" $sshTarget")
Write-Host "  cd ~/$RemoteDirectory && sudo docker compose ps | sudo docker compose up -d | sudo docker compose down"


