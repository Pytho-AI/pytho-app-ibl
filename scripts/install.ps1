# Pytho Windows Installer (Docker Engine in WSL2 Ubuntu)
#
# Prerequisites: WSL2 with Ubuntu and Docker must already be installed.
# Run from a regular (non-admin) PowerShell prompt.
#
# Usage:
#   .\install.ps1 -ElasticPassword "YourPassword"
#   .\install.ps1 -ElasticPassword "MyPass" -DownloadUrl "https://api.github.com/repos/Pytho-AI/pytho-app/zipball/main"
#   .\install.ps1 -ElasticPassword "MyPass" -GitHubToken "ghp_xxx"           # private repos
#   .\install.ps1 -ElasticPassword "MyPass" -GeoAgentUrl "" -CoaEngineUrl "" # skip optional services
#   .\install.ps1 -ElasticPassword "MyPass" -GeoAgentLat "37.7749" -GeoAgentLng "-122.4194" # offline geo data
#   .\install.ps1 -ElasticPassword "MyPass" -Recreate "docker-api,nginx"     # force-rebuild services
#   .\install.ps1 -ElasticPassword "MyPass" -DownloadOnly                    # download without install
#   .\install.ps1 -ElasticPassword "MyPass" -NoDownload                     # skip download, use existing code
#   .\install.ps1 -GeoDataOnly -GeoAgentLat "23.6978" -GeoAgentLng "120.9605" -GeoOsmPbfUrl "https://download.geofabrik.de/asia/taiwan-latest.osm.pbf"
#   .\install.ps1 -GeoDataOnly -GeoAgentLat "23.6978" -GeoAgentLng "120.9605" -GeoAgentRadiusKm 500 -GeoOsmPbfUrl "https://download.geofabrik.de/asia/taiwan-latest.osm.pbf","https://download.geofabrik.de/asia/china/fujian-latest.osm.pbf"
#   .\install.ps1 -SetupAutoStartOnly                                         # only install WSL/Docker login script
#   .\install.ps1 -ElasticPassword "MyPass" -ShowAppRail                      # show Home/Atlas/Anvil/Crucible/Tome app rail (default: hidden)

param(
    [string]$ElasticPassword = "",

    [string]$DownloadUrl = "https://api.github.com/repos/Pytho-AI/pytho-app/zipball/mcwl",
    [string]$GitHubToken = "",
    [string]$InstallDir = "$env:USERPROFILE\pytho",
    [string]$DataDir = "$env:USERPROFILE\pytho-data",
    [string]$GeoAgentUrl = "https://api.github.com/repos/Pytho-AI/geo-agent/zipball/main",
    [string]$GeoAgentLat = "",
    [string]$GeoAgentLng = "",
    [int]$GeoAgentRadiusKm = 50,
    [string[]]$GeoOsmPbfUrl = @(),
    [string]$CoaEngineUrl = "https://api.github.com/repos/Pytho-AI/coa-engine/zipball/mcwl",
    [switch]$DownloadOnly,
    [switch]$NoDownload,
    [switch]$GeoDataOnly,
    [switch]$SetupAutoStartOnly,
    [string]$Recreate = "",
    [switch]$ShowAppRail
)

$ErrorActionPreference = "Stop"
$QA_MODEL = "gemma4:26b"
$DOCGEN_SCAFFOLD_MODEL = "gemma3:27b-it-qat"
$DOCGEN_WRITER_MODEL = "gemma3:27b-it-qat"
$AUDIO_MODEL = "medium.en"

Write-Host ""
Write-Host "  Pytho Installer (WSL2 Docker Engine)" -ForegroundColor Cyan
Write-Host "  Install: $InstallDir | Data: $DataDir" -ForegroundColor Gray
Write-Host ""

# ============ WSL Helpers ============

function Find-WslDistro {
    # Find first Ubuntu-based WSL distro (handles Ubuntu, Ubuntu-22.04, Ubuntu-24.04, etc.)
    $distros = (& wsl.exe -l -q 2>&1) | ForEach-Object { $_ -replace '\x00','' } | Where-Object { $_.Trim() -ne '' }
    foreach ($d in $distros) {
        if ($d.Trim() -match '^Ubuntu') { return $d.Trim() }
    }
    return $null
}

$script:WslDistro = $null

function ConvertTo-WslPath([string]$WinPath) {
    $drive = $WinPath.Substring(0,1).ToLower()
    $rest  = $WinPath.Substring(2).Replace('\','/')
    return "/mnt/$drive$rest"
}

function Invoke-Wsl([string]$cmd, [switch]$AsRoot) {
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $tmp = Join-Path ([IO.Path]::GetTempPath()) "wsl_cmd_$(Get-Random).sh"
    [IO.File]::WriteAllText($tmp, ($cmd -replace "`r"), [Text.UTF8Encoding]::new($false))
    try {
        $wslPath = ConvertTo-WslPath $tmp
        if ($AsRoot) { & wsl.exe -d $script:WslDistro -u root -- bash -l $wslPath 2>&1 }
        else         { & wsl.exe -d $script:WslDistro -- bash -l $wslPath 2>&1 }
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        $ErrorActionPreference = $prevEAP
    }
}

function Invoke-DockerCompose {
    param(
        [string]$ProjectWslPath, [string]$ComposeArgs, [string]$DataWslPath,
        [string]$ElasticPwd = "", [string]$ExtraComposeFile = "",
        [string]$GeoDataWslPath = "", [string]$GeoAgentDirWslPath = "",
        [string]$CoaComposeFile = "", [string]$CoaEngineDirWslPath = ""
    )
    $showAppRailValue = if ($script:ShowAppRail) { 'true' } else { 'false' }
    $envVars = "PYTHO_DATA='$DataWslPath' VITE_SHOW_APP_RAIL='$showAppRailValue'"
    if ($ElasticPwd)        { $envVars += " ELASTIC_PASSWORD='$ElasticPwd'" }
    if ($GeoDataWslPath)    { $envVars += " GEO_DATA_DIR='$GeoDataWslPath'" }
    if ($GeoAgentDirWslPath) {
        $envVars += " GEO_AGENT_DIR='$GeoAgentDirWslPath' VITE_GEO_AGENT_API_URL='/geo-api/v1'"
        $envVars += " GEO_LLM_PROVIDER='ollama' GEO_LLM_MODEL='$QA_MODEL' GEO_OLLAMA_BASE_URL='http://host.docker.internal:11434'"
    }
    if ($CoaEngineDirWslPath) {
        $envVars += " COA_ENGINE_DIR='$CoaEngineDirWslPath' VITE_COA_WS_URL='/coa-ws' VITE_COA_API_URL='/coa-api'"
        $envVars += " COA_LLM_BACKEND='ollama' COA_LLM_MODEL='$QA_MODEL' COA_LLM_BASE_URL='http://host.docker.internal:11434'"
    }
    $fileArgs = ""
    if ($ExtraComposeFile -or $CoaComposeFile) {
        $fileArgs = "-f docker-compose-edge.yml"
        if ($ExtraComposeFile) { $fileArgs += " -f '$ExtraComposeFile'" }
        if ($CoaComposeFile)   { $fileArgs += " -f '$CoaComposeFile'" }
        $fileArgs += " "
    }
    $cmd = "cd '$ProjectWslPath'" + ' && ' + "$envVars docker compose --profile cpu $fileArgs$ComposeArgs"
    Invoke-Wsl $cmd
}

function Test-WslDistro {
    $script:WslDistro = Find-WslDistro
    return ($null -ne $script:WslDistro)
}

# ============ Prerequisites ============

function Test-Prerequisites {
    Write-Host "[1] Checking prerequisites..." -ForegroundColor Yellow

    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        Write-Host "  WSL is not installed. Install it with: wsl --install" -ForegroundColor Red; exit 1
    }
    if (-not (Test-WslDistro)) {
        Write-Host "  No Ubuntu distro found in WSL. Install one with: wsl --install -d Ubuntu" -ForegroundColor Red; exit 1
    }
    Write-Host "  Using WSL distro: $script:WslDistro" -ForegroundColor Gray
    $dockerCheck = Invoke-Wsl 'command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1 && echo OK || echo MISSING'
    if ($dockerCheck -notmatch "OK") {
        # Try starting Docker first (may just need a kick)
        Write-Host "  Docker not running, attempting to start..." -ForegroundColor Yellow
        Invoke-Wsl 'service docker start 2>/dev/null || true' -AsRoot | Out-Null
        Start-Sleep -Seconds 3
        $dockerCheck = Invoke-Wsl 'docker info >/dev/null 2>&1 && echo OK || echo MISSING'
        if ($dockerCheck -notmatch "OK") {
            Write-Host "  Docker is not installed or cannot start in WSL." -ForegroundColor Red
            Write-Host "  Install Docker in WSL Ubuntu first (https://docs.docker.com/engine/install/ubuntu/)." -ForegroundColor Yellow
            exit 1
        }
    }
    Write-Host "  WSL + $script:WslDistro + Docker: OK" -ForegroundColor Green
}

# ============ Docker Auto-Start on Login ============

function Configure-DockerAutoStart {
    Write-Host "[2] Configuring Docker auto-start..." -ForegroundColor Yellow

    # Enable Docker to start when WSL boots (inside the distro)
    Invoke-Wsl @'
if [ -d /run/systemd/system ]; then
    systemctl enable docker 2>/dev/null || true
else
    if ! grep -q "service docker start" /etc/wsl.conf 2>/dev/null; then
        if grep -q '^\[boot\]' /etc/wsl.conf 2>/dev/null; then
            sed -i '/^\[boot\]/a command=service docker start' /etc/wsl.conf
        else
            printf '\n[boot]\ncommand=service docker start\n' >> /etc/wsl.conf
        fi
    fi
fi
'@ -AsRoot | Out-Null

    # Remove legacy scheduled task if present (unreliable, replaced by Startup folder)
    $logonTaskName = "WSL-Docker-Logon"
    $existingLogon = Get-ScheduledTask -TaskName $logonTaskName -ErrorAction SilentlyContinue
    if ($existingLogon) { Unregister-ScheduledTask -TaskName $logonTaskName -Confirm:$false -ErrorAction SilentlyContinue }

    # Startup folder: hidden VBS script boots WSL on login (wsl.conf handles Docker)
    $startupDir = [IO.Path]::Combine($env:APPDATA, 'Microsoft\Windows\Start Menu\Programs\Startup')
    $vbsPath = Join-Path $startupDir "pytho-wsl-docker.vbs"
    $vbsContent = @"
' Pytho: Boot WSL silently on login (wsl.conf starts Docker automatically)
Set ws = CreateObject("WScript.Shell")
ws.Run "wsl.exe -d $script:WslDistro -- sleep infinity", 0, False
"@
    try {
        Set-Content -Path $vbsPath -Value $vbsContent -Encoding ASCII
        Write-Host "  Startup script created: $vbsPath" -ForegroundColor Green
    } catch {
        Write-Host "  Warning: Could not create startup script: $_" -ForegroundColor Yellow
    }

    # Disable WSL idle shutdown
    $wslConfigPath = Join-Path $env:USERPROFILE ".wslconfig"
    if (-not (Test-Path $wslConfigPath) -or -not ((Get-Content $wslConfigPath -Raw -ErrorAction SilentlyContinue) -match 'vmIdleTimeout')) {
        if (Test-Path $wslConfigPath) {
            $content = Get-Content $wslConfigPath -Raw
            if ($content -match '\[wsl2\]') {
                $content = $content -replace '(\[wsl2\])', "`$1`nvmIdleTimeout=-1"
            } else {
                $content += "`n[wsl2]`nvmIdleTimeout=-1`n"
            }
            Set-Content -Path $wslConfigPath -Value $content -Encoding UTF8
        } else {
            Set-Content -Path $wslConfigPath -Value "[wsl2]`nvmIdleTimeout=-1`n" -Encoding UTF8
        }
        Write-Host "  .wslconfig: vmIdleTimeout=-1" -ForegroundColor Green
    }
}

# ============ Download ============

function Download-Application {
    param([string]$Url, [string]$DestDir, [string]$Token = "", [string]$Label = "application")

    Write-Host "  Downloading $Label..." -ForegroundColor Yellow

    if (Test-Path $DestDir) { Remove-Item -Recurse -Force $DestDir }
    New-Item -ItemType Directory -Force -Path $DestDir | Out-Null

    $zipFile = "$env:TEMP\pytho_$(Get-Random).zip"
    try {
        $headers = if ($Token) { @{ "Authorization" = "Bearer $Token" } } else { @{} }
        Invoke-WebRequest -Uri $Url -OutFile $zipFile -Headers $headers -UseBasicParsing -MaximumRedirection 10

        if (-not (Test-Path $zipFile) -or (Get-Item $zipFile).Length -eq 0) { throw "Empty download" }

        # Verify ZIP magic bytes
        $bytes = [System.IO.File]::ReadAllBytes($zipFile)
        if ($bytes.Length -lt 4 -or $bytes[0] -ne 0x50 -or $bytes[1] -ne 0x4B) {
            throw "Not a valid ZIP file"
        }
    } catch {
        Write-Host "  Failed: $_" -ForegroundColor Red
        if ($Url -match "github.com" -and -not $Token) {
            Write-Host "  Private repo? Use -GitHubToken" -ForegroundColor Yellow
        }
        exit 1
    }

    # Extract and flatten GitHub zipball (Org-Repo-Hash/ -> DestDir/)
    $extractTemp = "$DestDir\_extract_tmp"
    New-Item -ItemType Directory -Force -Path $extractTemp | Out-Null
    Expand-Archive -Path $zipFile -DestinationPath $extractTemp -Force
    Remove-Item $zipFile -ErrorAction SilentlyContinue

    $nestedDirs = Get-ChildItem -Path $extractTemp -Directory
    if ($nestedDirs.Count -eq 1) {
        Get-ChildItem -Path $nestedDirs[0].FullName -Force | Move-Item -Destination $DestDir -Force
    } else {
        Get-ChildItem -Path $extractTemp -Force | Move-Item -Destination $DestDir -Force
    }
    Remove-Item -Recurse -Force $extractTemp -ErrorAction SilentlyContinue

    $sizeMB = [math]::Round($bytes.Length / 1MB, 1)
    Write-Host "  $Label OK, $sizeMB MB" -ForegroundColor Green
    return $DestDir
}

# ============ Elasticsearch ============

function Deploy-Elasticsearch {
    param([string]$Password, [string]$ProjectWsl, [string]$DataWsl)

    Write-Host "[5] Elasticsearch..." -ForegroundColor Yellow

    $script:EsApiId = ""
    $script:EsApiKey = ""

    # Ensure the external network exists before any compose up
    Invoke-Wsl 'docker network create pytho-net 2>/dev/null || true' -AsRoot | Out-Null

    # Already running?
    $running = Invoke-Wsl 'docker ps -q -f name=es-secure 2>/dev/null'
    if (-not ($running -and $running.Trim())) {
        Invoke-Wsl ("mkdir -p '$DataWsl/es-data'" + ' && ' + "chown 1000:1000 '$DataWsl/es-data'") -AsRoot

        $out = Invoke-DockerCompose -ProjectWslPath $ProjectWsl -DataWslPath $DataWsl `
            -ElasticPwd $Password -ExtraComposeFile $script:GeoComposeWsl `
            -GeoDataWslPath "$DataWsl/geo-agent" -GeoAgentDirWslPath $script:GeoAgentDirWsl `
            -CoaComposeFile $script:CoaComposeWsl -CoaEngineDirWslPath $script:CoaEngineDirWsl `
            -ComposeArgs "up -d es-secure"
        $out | ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }

        Start-Sleep -Seconds 3
        $verify = Invoke-Wsl 'docker ps -q -f name=es-secure 2>/dev/null'
        if (-not ($verify -and $verify.Trim())) { Write-Host "  Failed to start ES" -ForegroundColor Red; exit 1 }
    } else {
        Write-Host "  Already running" -ForegroundColor Green
    }

    # Wait for ready
    Write-Host "  Waiting for ES..." -ForegroundColor Gray
    for ($i = 1; $i -le 30; $i++) {
        Start-Sleep -Seconds 5
        try {
            $health = Invoke-RestMethod -Uri "http://localhost:9200/_cluster/health" `
                -Headers @{ "Authorization" = "Basic " + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("elastic:$Password")) } `
                -TimeoutSec 5 -ErrorAction Stop
            if ($health.status -eq "green" -or $health.status -eq "yellow") {
                Write-Host "  ES ready ($($health.status))" -ForegroundColor Green
                break
            }
        } catch {
            if ($i -eq 30) { Write-Host "  ES not ready after 150s" -ForegroundColor Red; exit 1 }
        }
    }

    # Create API key
    $apiKeyBody = @{
        name = "doc-chunks-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        role_descriptors = @{
            doc_chunks_rw = @{
                cluster = @("monitor")
                indices = @(@{ names = @("doc_chunks","doctrine_chunks"); privileges = @("read","write","delete","create_index","view_index_metadata") })
            }
        }
    } | ConvertTo-Json -Depth 10 -Compress

    try {
        $resp = Invoke-RestMethod -Uri "http://localhost:9200/_security/api_key" -Method POST `
            -Headers @{ "Authorization" = "Basic " + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("elastic:$Password")) } `
            -ContentType "application/json" -Body $apiKeyBody
        $script:EsApiId = $resp.id
        $script:EsApiKey = $resp.api_key
        Write-Host "  API key created" -ForegroundColor Green
    } catch {
        Write-Host "  Failed to create ES API key: $_" -ForegroundColor Red
        exit 1
    }
}

# ============ Environment + Data ============

function Setup-Environment {
    param([string]$AppDir, [string]$DataDir, [string]$ApiId, [string]$ApiKey)

    Write-Host "[6] Environment + data..." -ForegroundColor Yellow

    # Write .env
    $appFolder = Get-ChildItem -Path $AppDir -Recurse -Directory -Filter "app" |
        Where-Object { $_.FullName -notmatch '[\\/](geo-agent|coa-engine)[\\/]' } | Select-Object -First 1
    if ($appFolder) {
        $envPath = Join-Path $appFolder.FullName ".env"
        @"
SERVICE_HOST=host.docker.internal
UPLOAD_DIR=uploads
FILE_HOST_API=http://localhost:8000/files/
IMAGE_API_HOST=stability_api
IMAGE_API_PORT=17860
TTS_HOST=kokoro_tts
TTS_PORT=8880
ES_HOST=es-secure
ES_ACCESS_ID=$ApiId
ES_ACCESS_KEY=$ApiKey
Q_AND_A_MODEL=$QA_MODEL
DOCGEN_SCAFFOLD_MODEL=$DOCGEN_SCAFFOLD_MODEL
DOCGEN_WRITER_MODEL=$DOCGEN_WRITER_MODEL
AUDIO_MODEL=$AUDIO_MODEL
"@ | Set-Content -Path $envPath -Encoding UTF8
        Write-Host "  .env written" -ForegroundColor Green
    }

    # Create data directories
    $dirs = @("models","data","uploads","resources","sd-models","sd-models/Stable-diffusion","sd-outputs","geo-agent","coa-engine","coa-engine/runs")
    foreach ($dir in $dirs) {
        $path = Join-Path $DataDir $dir
        if (-not (Test-Path $path)) { New-Item -ItemType Directory -Force -Path $path | Out-Null }
    }

    # Copy default templates if needed
    $resourcesDataDir = Join-Path $DataDir "resources"
    if ((Get-ChildItem -Path $resourcesDataDir -ErrorAction SilentlyContinue | Measure-Object).Count -eq 0) {
        $dockerCompose = Get-ChildItem -Path $AppDir -Recurse -Filter "docker-compose-edge.yml" |
            Where-Object { $_.FullName -notmatch '[\\/](geo-agent|coa-engine)[\\/]' } | Select-Object -First 1
        if ($dockerCompose) {
            $sourceResources = Join-Path $dockerCompose.DirectoryName "resources"
            if (Test-Path $sourceResources) {
                Copy-Item -Path "$sourceResources\*" -Destination $resourcesDataDir -Recurse -Force
                Write-Host "  Default templates copied" -ForegroundColor Green
            }
        }
    }

    Write-Host "  Data directories ready" -ForegroundColor Green
}

# ============ Stable Diffusion Model ============

function Download-SDModel {
    param([string]$DataDir)

    $modelDir = Join-Path $DataDir "sd-models\Stable-diffusion"
    if ((Test-Path $modelDir) -and (Get-ChildItem -Path $modelDir -Filter "*.safetensors" -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        Write-Host "  SD model exists" -ForegroundColor Green; return
    }

    Write-Host "[7] Downloading Stable Diffusion model (~5GB)..." -ForegroundColor Yellow
    $modelUrl = "https://huggingface.co/stabilityai/stable-diffusion-2-1/resolve/main/v2-1_768-ema-pruned.safetensors"
    try {
        Start-BitsTransfer -Source $modelUrl -Destination $modelFile -DisplayName "SD Model"
        Write-Host "  SD model downloaded" -ForegroundColor Green
    } catch {
        try { Invoke-WebRequest -Uri $modelUrl -OutFile $modelFile -UseBasicParsing }
        catch { Write-Host "  Warning: SD model download failed (non-critical)" -ForegroundColor Yellow }
    }
}

# ============ Deploy All Services ============

function Deploy-Application {
    param([string]$AppDir, [string]$DataDir, [string]$ElasticPwd, [string[]]$Recreate = @())

    Write-Host "[8] Deploying services..." -ForegroundColor Yellow

    $dockerCompose = Get-ChildItem -Path $AppDir -Recurse -Filter "docker-compose-edge.yml" |
        Where-Object { $_.FullName -notmatch '[\\/](geo-agent|coa-engine)[\\/]' } | Select-Object -First 1
    if (-not $dockerCompose) { Write-Host "  docker-compose-edge.yml not found" -ForegroundColor Red; exit 1 }

    $projectWsl = ConvertTo-WslPath $dockerCompose.DirectoryName
    $dataWsl = ConvertTo-WslPath $DataDir
    $geoDataWsl = ConvertTo-WslPath (Join-Path $DataDir "geo-agent")

    Invoke-Wsl 'service docker status >/dev/null 2>&1 || service docker start' -AsRoot | Out-Null

    $composeArgs = @{
        ProjectWslPath    = $projectWsl
        DataWslPath       = $dataWsl
        ElasticPwd        = $ElasticPwd
        ExtraComposeFile  = $script:GeoComposeWsl
        GeoDataWslPath    = $geoDataWsl
        GeoAgentDirWslPath = $script:GeoAgentDirWsl
        CoaComposeFile    = $script:CoaComposeWsl
        CoaEngineDirWslPath = $script:CoaEngineDirWsl
    }

    if ($Recreate.Count -gt 0) {
        $svcList = $Recreate -join ' '
        Write-Host "  Force-recreating: $svcList" -ForegroundColor Yellow
        $out = Invoke-DockerCompose @composeArgs -ComposeArgs "up -d --build --force-recreate $svcList"
        $out | ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }
    }

    # Force rebuild nginx without cache to pick up config changes
    Write-Host "  Building nginx (no cache)..." -ForegroundColor Gray
    $out = Invoke-DockerCompose @composeArgs -ComposeArgs "build --no-cache nginx"
    $out | ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }

    # Start all services (existing unchanged containers are left untouched automatically)
    Write-Host "  Starting services..." -ForegroundColor Yellow
    $out = Invoke-DockerCompose @composeArgs -ComposeArgs "up -d --build"
    $out | ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }

    Write-Host "  Deployed" -ForegroundColor Green
}

# ============ Geo-Agent Offline Data ============

function Prepare-GeoOfflineData {
    param([string]$GeoAgentDir, [string]$DataDir, [string]$Lat, [string]$Lng, [int]$RadiusKm, [string[]]$OsmPbfUrls = @())

    Write-Host "[Geo] Downloading offline data..." -ForegroundColor Magenta

    $offlineDir = Join-Path $DataDir "geo-agent\offline"
    if (-not (Test-Path $offlineDir)) { New-Item -ItemType Directory -Force -Path $offlineDir | Out-Null }

    # Compute bounding box
    $latVal = [double]$Lat; $lngVal = [double]$Lng
    $dLat = $RadiusKm / 111.0
    $dLng = $RadiusKm / (111.0 * [Math]::Cos([Math]::PI * $latVal / 180.0))
    $bbox = "$([Math]::Round($lngVal - $dLng, 4)),$([Math]::Round($latVal - $dLat, 4)),$([Math]::Round($lngVal + $dLng, 4)),$([Math]::Round($latVal + $dLat, 4))"

    Write-Host "  Center: $Lat, $Lng | Radius: ${RadiusKm}km" -ForegroundColor Gray

    # Download OSM PBF(s) and merge if needed
    $stagingDir = Join-Path $offlineDir "staging"
    if (-not (Test-Path $stagingDir)) { New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null }
    $pbfFile = Join-Path $stagingDir "region.osm.pbf"

    if ($OsmPbfUrls.Count -gt 0 -and -not (Test-Path $pbfFile)) {
        $downloadedFiles = @()
        $index = 0
        foreach ($url in $OsmPbfUrls) {
            $index++
            $fileName = [System.IO.Path]::GetFileName(([uri]$url).AbsolutePath)
            $destFile = Join-Path $stagingDir "${index}_${fileName}"

            if (Test-Path $destFile) {
                $sizeMB = [math]::Round((Get-Item $destFile).Length / 1MB, 1)
                Write-Host "  [$index/$($OsmPbfUrls.Count)] Already exists: $fileName ($sizeMB MB)" -ForegroundColor Gray
            } else {
                Write-Host "  [$index/$($OsmPbfUrls.Count)] Downloading $fileName..." -ForegroundColor Yellow
                try {
                    Invoke-WebRequest -Uri $url -OutFile $destFile -UseBasicParsing
                    $sizeMB = [math]::Round((Get-Item $destFile).Length / 1MB, 1)
                    Write-Host "  [$index/$($OsmPbfUrls.Count)] Downloaded ($sizeMB MB)" -ForegroundColor Green
                } catch {
                    Write-Host "  Failed to download: $_" -ForegroundColor Red
                    Write-Host "  URL: $url" -ForegroundColor Yellow
                    return
                }
            }
            $downloadedFiles += $destFile
        }

        if ($downloadedFiles.Count -eq 1) {
            Copy-Item $downloadedFiles[0] $pbfFile -Force
            Write-Host "  PBF file ready" -ForegroundColor Green
        } else {
            Write-Host "  Merging $($downloadedFiles.Count) PBF files with osmium..." -ForegroundColor Yellow
            $stagingWsl = ConvertTo-WslPath $stagingDir
            $inputNames = $downloadedFiles | ForEach-Object { "/data/$([System.IO.Path]::GetFileName($_))" }
            $inputList = $inputNames -join ' '
            $mergeCmd = "docker run --rm -v '${stagingWsl}:/data' stefda/osmium-tool osmium merge $inputList -o /data/region.osm.pbf --overwrite"
            $out = Invoke-Wsl $mergeCmd
            $out | ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }

            if (-not (Test-Path $pbfFile)) {
                Write-Host "  Merge failed -- region.osm.pbf not created" -ForegroundColor Red
                return
            }
            $sizeMB = [math]::Round((Get-Item $pbfFile).Length / 1MB, 1)
            Write-Host "  Merged PBF ready ($sizeMB MB)" -ForegroundColor Green
        }
    } elseif (-not (Test-Path $pbfFile) -and $OsmPbfUrls.Count -eq 0) {
        Write-Host "  No OSM PBF file at $pbfFile" -ForegroundColor Red
        Write-Host "  Provide -GeoOsmPbfUrl with one or more Geofabrik URLs, e.g.:" -ForegroundColor Yellow
        Write-Host "    -GeoOsmPbfUrl 'https://download.geofabrik.de/asia/taiwan-latest.osm.pbf'" -ForegroundColor Yellow
        Write-Host "    -GeoOsmPbfUrl 'url1','url2','url3'  (multiple regions)" -ForegroundColor Yellow
        return
    }

    $prepareScript = Get-ChildItem -Path $GeoAgentDir -Recurse -Filter "prepare_offline.sh" | Select-Object -First 1
    if (-not $prepareScript) { Write-Host "  prepare_offline.sh not found" -ForegroundColor Red; return }

    $scriptWsl = ConvertTo-WslPath $prepareScript.FullName
    $offlineWsl = ConvertTo-WslPath $offlineDir

    $out = Invoke-Wsl ("chmod +x '$scriptWsl'" + ' && ' + "'$scriptWsl' --bbox '$bbox' --data-dir '$offlineWsl'")
    $out | ForEach-Object { Write-Host "    $_" -ForegroundColor Gray }
    Write-Host "  Offline data ready" -ForegroundColor Green
}

# ============ Main ============
try {
    # --- Auto-start only mode ---
    if ($SetupAutoStartOnly) {
        Test-WslDistro | Out-Null
        if (-not $script:WslDistro) { Write-Host "  No Ubuntu distro found in WSL." -ForegroundColor Red; exit 1 }
        Configure-DockerAutoStart
        Write-Host "  Auto-start configured! Restart your PC or log out/in to test." -ForegroundColor Green
        exit 0
    }

    # --- Geo Data Only mode ---
    if ($GeoDataOnly) {
        if (-not $GeoAgentLat -or -not $GeoAgentLng) {
            Write-Host "  -GeoDataOnly requires -GeoAgentLat and -GeoAgentLng" -ForegroundColor Red; exit 1
        }
        Test-WslDistro | Out-Null
        Prepare-GeoOfflineData -GeoAgentDir "$InstallDir\geo-agent" -DataDir $DataDir -Lat $GeoAgentLat -Lng $GeoAgentLng -RadiusKm $GeoAgentRadiusKm -OsmPbfUrls $GeoOsmPbfUrl
        Write-Host "  Geo data fetch complete!" -ForegroundColor Green
        exit 0
    }

    # --- Full install requires ElasticPassword ---
    if (-not $ElasticPassword) {
        Write-Host "  -ElasticPassword is required for full installation." -ForegroundColor Red
        Write-Host "  Use -GeoDataOnly to fetch only geo data without a full install." -ForegroundColor Yellow
        exit 1
    }

    # [1] Prerequisites
    Test-Prerequisites

    # [2] Docker auto-start on login
    Configure-DockerAutoStart

    # [3] Download code
    if ($NoDownload) {
        Write-Host "[3] Skipping download (using existing code)..." -ForegroundColor Yellow
        $appDir = "$InstallDir\pytho-app"
        if (-not (Test-Path $appDir)) { Write-Host "  $appDir not found. Run without -NoDownload first." -ForegroundColor Red; exit 1 }
        if ($GeoAgentUrl)  { $geoAgentDir = "$InstallDir\geo-agent" }
        if ($CoaEngineUrl) { $coaEngineDir = "$InstallDir\coa-engine" }
    } else {
        Write-Host "[3] Downloading code..." -ForegroundColor Yellow
        $appDir = Download-Application -Url $DownloadUrl -DestDir "$InstallDir\pytho-app" -Token $GitHubToken -Label "pytho-app"

        if ($GeoAgentUrl) {
            $geoAgentDir = Download-Application -Url $GeoAgentUrl -DestDir "$InstallDir\geo-agent" -Token $GitHubToken -Label "geo-agent"
        }
        if ($CoaEngineUrl) {
            $coaEngineDir = Download-Application -Url $CoaEngineUrl -DestDir "$InstallDir\coa-engine" -Token $GitHubToken -Label "coa-engine"
        }

        if ($DownloadOnly) {
            Write-Host "`n  Download complete: $InstallDir" -ForegroundColor Green
            exit 0
        }
    }

    # [4] Locate compose overlays for geo-agent / coa-engine
    $dockerCompose = Get-ChildItem -Path $appDir -Recurse -Filter "docker-compose-edge.yml" |
        Where-Object { $_.FullName -notmatch '[\\/](geo-agent|coa-engine)[\\/]' } | Select-Object -First 1
    if (-not $dockerCompose) { Write-Host "  docker-compose-edge.yml not found in $appDir" -ForegroundColor Red; exit 1 }
    $projectWsl = ConvertTo-WslPath $dockerCompose.DirectoryName
    $dataWsl = ConvertTo-WslPath $DataDir

    $script:GeoComposeWsl = ""; $script:GeoAgentDirWsl = ""
    $script:CoaComposeWsl = ""; $script:CoaEngineDirWsl = ""

    if ($GeoAgentUrl) {
        $geoCompose = Get-ChildItem -Path $geoAgentDir -Recurse -Filter "docker-compose.pytho.yml" | Select-Object -First 1
        if ($geoCompose) {
            $script:GeoComposeWsl = ConvertTo-WslPath $geoCompose.FullName
            $script:GeoAgentDirWsl = ConvertTo-WslPath $geoCompose.DirectoryName
        }
    }
    if ($CoaEngineUrl) {
        $coaCompose = Get-ChildItem -Path $coaEngineDir -Recurse -Filter "docker-compose.pytho.yml" | Select-Object -First 1
        if ($coaCompose) {
            $script:CoaComposeWsl = ConvertTo-WslPath $coaCompose.FullName
            $script:CoaEngineDirWsl = ConvertTo-WslPath $coaCompose.DirectoryName
        }
    }

    # [5] Elasticsearch
    Deploy-Elasticsearch -Password $ElasticPassword -ProjectWsl $projectWsl -DataWsl $dataWsl

    # [6] Environment + data directories
    Setup-Environment -AppDir $appDir -DataDir $DataDir -ApiId $script:EsApiId -ApiKey $script:EsApiKey

    # [7] SD model
    Download-SDModel -DataDir $DataDir

    # [8] Deploy everything
    $recreateList = if ($Recreate) { $Recreate -split ',' | ForEach-Object { $_.Trim() } } else { @() }
    Deploy-Application -AppDir $appDir -DataDir $DataDir -ElasticPwd $ElasticPassword -Recreate $recreateList

    # Geo-agent offline data (optional)
    if ($GeoAgentUrl -and $GeoAgentLat -and $GeoAgentLng) {
        Prepare-GeoOfflineData -GeoAgentDir $geoAgentDir -DataDir $DataDir -Lat $GeoAgentLat -Lng $GeoAgentLng -RadiusKm $GeoAgentRadiusKm -OsmPbfUrls $GeoOsmPbfUrl
    }

    # Done
    Write-Host ""
    Write-Host "  Installation complete!" -ForegroundColor Green
    Write-Host ""
    Write-Host "  App:  http://localhost:8080" -ForegroundColor Cyan
    Write-Host "  API:  http://localhost:8000" -ForegroundColor Cyan
    Write-Host "  LLM:  Ollama $QA_MODEL -- ensure 'ollama serve' is running" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Code: $InstallDir" -ForegroundColor Gray
    Write-Host "  Data: $DataDir" -ForegroundColor Gray
    Write-Host "  Logs: wsl.exe -d $script:WslDistro -- docker compose logs -f" -ForegroundColor Gray
    Write-Host ""
}
catch {
    Write-Host "`n  Installation failed: $_" -ForegroundColor Red
    exit 1
}
