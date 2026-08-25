# docker-openscap.ps1 (NO WSL LIST PARSING — probes Ubuntu directly)
# - Checks Docker Desktop from Windows
# - Probes whether Ubuntu exists by running a command in it
# - If missing, installs Ubuntu and stops with instructions
# - If present but not initialized, tells you to open Ubuntu once
# - Checks Docker Desktop WSL Integration (docker works inside Ubuntu)
# - Runs OpenSCAP + SSG and copies HTML/XML to Windows Desktop

$ErrorActionPreference = "Stop"

function Require-Cmd($cmd) {
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
    throw "Required command not found: $cmd"
  }
}

Require-Cmd wsl
Require-Cmd docker

function Invoke-Ubuntu([string]$cmd, [switch]$AsRoot) {
  # Write to temp file with Unix (LF) line endings, then execute inside WSL.
  # Avoids both argument-quoting issues (parentheses, pipes) and Windows CRLF breaking sh.
  $ErrorActionPreference = 'Continue'   # local scope — stderr from wsl won't terminate
  $tmp = Join-Path ([IO.Path]::GetTempPath()) "wsl_cmd_$(Get-Random).sh"
  [IO.File]::WriteAllText($tmp, ($cmd -replace "`r"), [Text.UTF8Encoding]::new($false))
  try {
    $drive   = $tmp.Substring(0,1).ToLower()
    $rest    = $tmp.Substring(2).Replace('\','/')
    $wslPath = "/mnt/$drive$rest"
    if ($AsRoot) {
      & wsl.exe -d Ubuntu -u root -- bash -l $wslPath 2>&1
    } else {
      & wsl.exe -d Ubuntu -- bash -l $wslPath 2>&1
    }
  } finally {
    Remove-Item $tmp -Force -ErrorAction SilentlyContinue
  }
}

function Ubuntu-Exists {
  $out = & wsl.exe -d Ubuntu -- echo OK 2>&1
  # If distro doesn't exist, WSL prints an error that includes "does not exist" / "was not found"
  if ($out -match 'does not exist|was not found|no installed distributions|WSL_E_DISTRO_NOT_FOUND') { return $false }
  return $true
}

Write-Host "==> Checking Docker Desktop is reachable from Windows..."
docker info | Out-Null
Write-Host "OK"

Write-Host "==> Checking whether Ubuntu WSL distro exists (probe)..."
if (-not (Ubuntu-Exists)) {
  Write-Host "⚠️  Ubuntu not found. Installing Ubuntu..."
  wsl.exe --install -d Ubuntu

  Write-Host ""
  Write-Host "🚨 ACTION REQUIRED"
  Write-Host "1) Open **Ubuntu** from the Start Menu"
  Write-Host "2) Create a Linux username and password"
  Write-Host "3) Close Ubuntu"
  Write-Host "Then re-run this script."
  exit 1
}
Write-Host "OK (Ubuntu exists)"

Write-Host "==> Verifying Ubuntu is initialized (first-run completed)..."
$who = Invoke-Ubuntu "id -un 2>/dev/null || true"
if (-not $who -or $who.Trim().Length -eq 0) {
  Write-Host "⚠️  Ubuntu exists but isn't initialized yet."
  Write-Host ""
  Write-Host "🚨 ACTION REQUIRED"
  Write-Host "1) Open **Ubuntu** from the Start Menu"
  Write-Host "2) Complete first-run (create username/password)"
  Write-Host "3) Close Ubuntu"
  Write-Host "Then re-run this script."
  exit 1
}
Write-Host "OK (Ubuntu user: $($who.Trim()))"

Write-Host "==> Verifying Docker works inside Ubuntu (Docker Desktop WSL Integration)..."
$dockerCheck = Invoke-Ubuntu "command -v docker >/dev/null 2>&1 && docker version >/dev/null 2>&1 && echo DOCKER_OK || true"
if ($dockerCheck -notmatch "DOCKER_OK") {
  Write-Host "❌ Docker does not work inside Ubuntu yet."
  Write-Host ""
  Write-Host "Fix:"
  Write-Host "Docker Desktop → Settings → Resources → WSL Integration → enable **Ubuntu** → Apply & Restart"
  Write-Host ""
  Write-Host "Then re-run this script."
  exit 1
}
Write-Host "OK"

# Output folder on Windows Desktop
$Desktop = [Environment]::GetFolderPath("Desktop")
$Stamp   = Get-Date -Format "yyyyMMdd-HHmmss"
$OutDir  = Join-Path $Desktop "docker-openscap-$Stamp"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
Write-Host "==> Outputs will be written to: $OutDir"

$payload = @'
set -euo pipefail

echo "==> Enabling universe repo and installing OpenSCAP + SCAP Security Guide..."
# Clean up any conflicting .list file from previous runs
rm -f /etc/apt/sources.list.d/universe.list
# Enable universe: deb822 format (24.04+) vs traditional format
if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then
  if ! grep -q 'universe' /etc/apt/sources.list.d/ubuntu.sources; then
    sed -i 's/^Components: \(.*\)/Components: \1 universe/' /etc/apt/sources.list.d/ubuntu.sources
  fi
else
  . /etc/os-release
  CODENAME="${UBUNTU_CODENAME:-$VERSION_CODENAME}"
  printf 'deb http://archive.ubuntu.com/ubuntu %s universe\n' "$CODENAME" > /etc/apt/sources.list.d/universe.list
  printf 'deb http://archive.ubuntu.com/ubuntu %s-updates universe\n' "$CODENAME" >> /etc/apt/sources.list.d/universe.list
  printf 'deb http://archive.ubuntu.com/ubuntu %s-security universe\n' "$CODENAME" >> /etc/apt/sources.list.d/universe.list
fi
apt-get update -y
apt-get install -y openscap-scanner
# scap-security-guide is not packaged for noble (24.04); fall back to GitHub
if ! apt-get install -y scap-security-guide 2>/dev/null; then
  echo "scap-security-guide not in apt, downloading from ComplianceAsCode..."
  apt-get install -y curl unzip >/dev/null 2>&1
  SSG_VER=$(curl -sL https://api.github.com/repos/ComplianceAsCode/content/releases/latest \
    | grep -Po '"tag_name":\s*"\K[^"]+') || true
  SSG_VER="${SSG_VER#v}"
  if [ -z "$SSG_VER" ]; then SSG_VER="0.1.75"; fi
  echo "Downloading SSG v${SSG_VER}..."
  curl -sL "https://github.com/ComplianceAsCode/content/releases/download/v${SSG_VER}/scap-security-guide-${SSG_VER}.zip" \
    -o /tmp/ssg.zip
  mkdir -p /usr/share/xml/scap/ssg/content
  unzip -o /tmp/ssg.zip -d /tmp/ssg >/dev/null
  find /tmp/ssg -name '*.xml' -exec cp {} /usr/share/xml/scap/ssg/content/ \;
  rm -rf /tmp/ssg /tmp/ssg.zip
  echo "SSG content installed to /usr/share/xml/scap/ssg/content/"
fi

echo "==> Locating SCAP datastream content..."
DS=""
# Try Docker-specific content first
if ls /usr/share/xml/scap/ssg/content/*docker*ds*.xml >/dev/null 2>&1; then
  DS="$(ls /usr/share/xml/scap/ssg/content/*docker*ds*.xml | head -n 1)"
fi
# Fall back to Ubuntu host content (the OS running Docker)
if [ -z "$DS" ]; then
  echo "No Docker-specific SCAP content found, falling back to Ubuntu host scan..."
  . /etc/os-release
  UBUNTU_VER=$(echo "$VERSION_ID" | tr -d '.')
  if ls /usr/share/xml/scap/ssg/content/ssg-ubuntu${UBUNTU_VER}-ds*.xml >/dev/null 2>&1; then
    DS="$(ls /usr/share/xml/scap/ssg/content/ssg-ubuntu${UBUNTU_VER}-ds*.xml | head -n 1)"
  elif ls /usr/share/xml/scap/ssg/content/ssg-ubuntu*-ds*.xml >/dev/null 2>&1; then
    DS="$(ls /usr/share/xml/scap/ssg/content/ssg-ubuntu*-ds*.xml | sort -V | tail -n 1)"
  fi
fi

if [ -z "$DS" ]; then
  echo "ERROR: No SCAP datastream found under /usr/share/xml/scap/ssg/content/"
  echo "Available files:"
  ls /usr/share/xml/scap/ssg/content/ 2>/dev/null || echo "(directory empty)"
  exit 2
fi
echo "Found: $DS"

echo "==> Selecting profile (prefer STIG, else CIS, else first)..."
INFO="$(oscap info "$DS")"
PROFILES="$(printf "%s\n" "$INFO" | sed -n 's/^\s*Id:\s*\(.*profile[^ ]*\).*$/\1/p' | sort -u || true)"
if [ -z "$PROFILES" ]; then
  PROFILES="$(printf "%s\n" "$INFO" | grep -Eo 'xccdf_[^ ]*profile_[^ ]+' | sort -u || true)"
fi

PROFILE_ID="$(printf "%s\n" "$PROFILES" | grep -i 'profile_stig' | head -n 1 || true)"
if [ -z "$PROFILE_ID" ]; then
  PROFILE_ID="$(printf "%s\n" "$PROFILES" | grep -i 'profile_cis' | head -n 1 || true)"
fi
if [ -z "$PROFILE_ID" ]; then
  PROFILE_ID="$(printf "%s\n" "$PROFILES" | head -n 1 || true)"
fi

if [ -z "$PROFILE_ID" ]; then
  echo "ERROR: No profile found"
  exit 3
fi
echo "Using profile: $PROFILE_ID"

WORKDIR="$HOME/scap-reports/docker"
mkdir -p "$WORKDIR"
cd "$WORKDIR"

echo "==> Running scan..."
# oscap returns 0=all pass, 2=some fail/notapplicable (normal), 1=error
oscap xccdf eval \
  --profile "$PROFILE_ID" \
  --results results.xml \
  --report report.html \
  "$DS" || OSCAP_RC=$?
if [ "${OSCAP_RC:-0}" -eq 1 ]; then
  echo "ERROR: oscap encountered an error"
  exit 4
fi

echo "==> Done (oscap exit code: ${OSCAP_RC:-0})."
echo "REPORT_PATH=$WORKDIR/report.html"
echo "RESULTS_PATH=$WORKDIR/results.xml"
echo "PROFILE_ID=$PROFILE_ID"
'@

Write-Host "==> Running OpenSCAP in Ubuntu..."
$out = Invoke-Ubuntu $payload -AsRoot
$out | ForEach-Object { Write-Host $_ }

$reportLine  = $out | Select-String -Pattern "^REPORT_PATH="  | Select-Object -Last 1
$resultsLine = $out | Select-String -Pattern "^RESULTS_PATH=" | Select-Object -Last 1
$profileLine = $out | Select-String -Pattern "^PROFILE_ID="   | Select-Object -Last 1

if (-not $reportLine -or -not $resultsLine) {
  throw "Could not locate REPORT_PATH / RESULTS_PATH in output. Scroll up for errors."
}

$reportWsl  = ($reportLine.ToString()  -split "=",2)[1].Trim()
$resultsWsl = ($resultsLine.ToString() -split "=",2)[1].Trim()
$profileId  = if ($profileLine) { (($profileLine.ToString() -split "=",2)[1].Trim()) } else { "" }

Write-Host "==> Copying report files to Windows..."
$reportWin  = Join-Path $OutDir "docker-openscap-report.html"
$resultsWin = Join-Path $OutDir "docker-openscap-results.xml"
$metaWin    = Join-Path $OutDir "run-metadata.txt"

# Copy from within WSL as root (has access to root-owned files + /mnt/c/)
$outDrive  = $OutDir.Substring(0,1).ToLower()
$outRest   = $OutDir.Substring(2).Replace('\','/')
$outDirWsl = "/mnt/$outDrive$outRest"
Invoke-Ubuntu "cp '$reportWsl' '$outDirWsl/docker-openscap-report.html'" -AsRoot
Invoke-Ubuntu "cp '$resultsWsl' '$outDirWsl/docker-openscap-results.xml'" -AsRoot

@"
Timestamp: $Stamp
WSL Distro: Ubuntu
Profile: $profileId
Report: $reportWin
Results: $resultsWin
"@ | Out-File -FilePath $metaWin -Encoding utf8

Write-Host ""
Write-Host "✅ Completed."
Write-Host "HTML report: $reportWin"
Write-Host "XML results: $resultsWin"
Write-Host "Metadata:    $metaWin"
