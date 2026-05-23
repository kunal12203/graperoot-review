# GrapeRoot Pro - one-time setup (Windows)
# Usage:
#   $env:GRAPEROOT_LICENSE_KEY = "GRP-XXXX-XXXX-XXXX"
#   irm https://graperoot.dev/pro/install.ps1 | iex

try {
    $ErrorActionPreference = "Stop"
    # Suppress Invoke-WebRequest progress bar - on PS 5.1 it makes IWR 40x slower
    $ProgressPreference = "SilentlyContinue"

    # TLS - PS 5.1 defaults to TLS 1.0 which many CDNs reject
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 -bor [Net.SecurityProtocolType]::Tls13
    } catch {
        try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
    }

    $R2          = if ($env:GRAPEROOT_PRO_R2)  { $env:GRAPEROOT_PRO_R2 }  else { "https://pub-pro-r2.graperoot.dev" }
    $API         = if ($env:GRAPEROOT_PRO_API) { $env:GRAPEROOT_PRO_API } else { "https://api.graperoot.dev" }
    $BASE_URL    = if ($env:GRAPEROOT_PRO_GH)  { $env:GRAPEROOT_PRO_GH }  else { "https://raw.githubusercontent.com/kunal12203/graperoot-pro-public/main" }
    $INSTALL_DIR = "$env:USERPROFILE\.graperoot-pro"
    $FREE_DIR    = "$env:USERPROFILE\.dual-graph"
    $LicenseKey  = $env:GRAPEROOT_LICENSE_KEY

    Write-Host ""
    Write-Host "+==============================================================+" -ForegroundColor Cyan
    Write-Host "|           GrapeRoot Pro - Installer  |  v1.0                 |" -ForegroundColor Cyan
    Write-Host "+==============================================================+" -ForegroundColor Cyan
    Write-Host ""

    if (-not $LicenseKey) {
        Write-Host "[error] License key required." -ForegroundColor Red
        Write-Host ""
        Write-Host "  Usage:"
        Write-Host "    `$env:GRAPEROOT_LICENSE_KEY = 'GRP-XXXX-XXXX-XXXX'"
        Write-Host "    irm https://graperoot.dev/pro/install.ps1 | iex"
        Write-Host ""
        Write-Host "  No license? Purchase at https://graperoot.dev/pro or email sales@graperoot.dev"
        exit 1
    }

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------
    function Invoke-WebRequestWithRetry {
        param([string]$Uri, [string]$OutFile, [int]$MaxRetries = 4, [int]$TimeoutSec = 60)
        for ($i = 1; $i -le $MaxRetries; $i++) {
            try {
                Invoke-WebRequest $Uri -OutFile $OutFile -UseBasicParsing -TimeoutSec $TimeoutSec
                return
            } catch {
                if ($i -ge $MaxRetries) { throw "Download failed after $MaxRetries tries: $Uri - $($_.Exception.Message)" }
                Start-Sleep -Seconds ([Math]::Min(2 * $i, 8))
            }
        }
    }

    # Detect HTML error pages served with 200 status (R2 cert failures return
    # an HTML page with the original status code - breaks launcher files silently).
    function Test-ValidScript {
        param([string]$Path)
        if (-not (Test-Path $Path)) { return $false }
        $size = (Get-Item $Path).Length
        if ($size -lt 20) { return $false }
        $head = Get-Content $Path -TotalCount 3 -ErrorAction SilentlyContinue
        if ($null -eq $head) { return $false }
        foreach ($line in $head) {
            if ($line -match '<html|<!DOCTYPE|<HTML') { return $false }
        }
        return $true
    }

    # Validate a tarball has the gzip magic header (1f 8b) before handing to tar.
    function Test-ValidTarball {
        param([string]$Path)
        if (-not (Test-Path $Path)) { return $false }
        if ((Get-Item $Path).Length -lt 100) { return $false }
        try {
            $fs = [System.IO.File]::OpenRead($Path)
            $b0 = $fs.ReadByte(); $b1 = $fs.ReadByte()
            $fs.Close()
            return ($b0 -eq 0x1f -and $b1 -eq 0x8b)
        } catch { return $false }
    }

    function Confirm-Install([string]$Prompt) {
        $a = Read-Host "$Prompt [Y/n]"
        return ($a -notmatch '^[Nn]')
    }

    # -----------------------------------------------------------------------
    # Prerequisites - Python 3.10+, Claude Code
    # -----------------------------------------------------------------------
    $pyCandidates = @("python3.13","python3.12","python3.11","python3.10","python3","python")
    $pythonCmd = $null
    foreach ($c in $pyCandidates) {
        $cmd = Get-Command $c -ErrorAction SilentlyContinue
        if ($cmd) {
            $ok = & $cmd.Source -c "import sys; print('1' if sys.version_info >= (3,10) else '0')" 2>$null
            if ($ok -eq "1") { $pythonCmd = $cmd.Source; break }
        }
    }
    if (-not $pythonCmd) {
        Write-Host "[check] Python 3.10+ NOT installed." -ForegroundColor Yellow
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            if (Confirm-Install "[check] Install Python 3.11 via winget?") {
                winget install -e --id Python.Python.3.11 --accept-source-agreements --accept-package-agreements
                Write-Host "  Re-open PowerShell and run the installer again." -ForegroundColor Yellow
                exit 0
            }
        } else {
            Write-Host "  Install Python 3.11 from https://python.org, then re-run." -ForegroundColor Yellow
        }
        exit 1
    }
    Write-Host "[check] Python:       $(& $pythonCmd --version)"

    if (Get-Command rg -ErrorAction SilentlyContinue) {
        Write-Host "[check] ripgrep:      $(rg --version | Select-Object -First 1)"
    } else {
        Write-Host "[check] ripgrep:      NOT FOUND" -ForegroundColor Yellow
        try {
            if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Confirm-Install "[check] Install ripgrep via winget?")) {
                & winget install -e --id BurntSushi.ripgrep.MSVC --silent --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
            } elseif ((Get-Command scoop -ErrorAction SilentlyContinue) -and (Confirm-Install "[check] Install ripgrep via scoop?")) {
                & scoop install ripgrep 2>&1 | Out-Null
            } elseif ((Get-Command choco -ErrorAction SilentlyContinue) -and (Confirm-Install "[check] Install ripgrep via Chocolatey?")) {
                & choco install ripgrep -y 2>&1 | Out-Null
            } else {
                Write-Host "[warn] Install later via: winget install BurntSushi.ripgrep.MSVC   (needed for fallback_rg / graph_grep_all)" -ForegroundColor Yellow
            }
        } catch {
            Write-Host "[warn] ripgrep install failed: $($_.Exception.Message). Install manually: winget install BurntSushi.ripgrep.MSVC" -ForegroundColor Yellow
        }
    }

    $nodeOk = $false
    if (Get-Command node -ErrorAction SilentlyContinue) {
        $nodeVer = (& node -v) 2>$null
        $nodeMajor = if ($nodeVer -match '^v(\d+)') { [int]$Matches[1] } else { 0 }
        if ($nodeMajor -ge 18) {
            Write-Host "[check] Node.js:      $nodeVer"
            $nodeOk = $true
        } else {
            Write-Host "[warn] Node $nodeVer is older than v18; Claude Code may fail. Upgrade recommended." -ForegroundColor Yellow
        }
    }
    if (-not $nodeOk) {
        Write-Host "[check] Node.js:      NOT FOUND" -ForegroundColor Yellow
        $installed = $false
        try {
            if ((Get-Command winget -ErrorAction SilentlyContinue) -and (Confirm-Install "[check] Install Node.js (LTS) via winget?")) {
                & winget install -e --id OpenJS.NodeJS.LTS --silent --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
                $installed = $true
            } elseif ((Get-Command scoop -ErrorAction SilentlyContinue) -and (Confirm-Install "[check] Install Node.js via scoop?")) {
                & scoop install nodejs-lts 2>&1 | Out-Null
                $installed = $true
            } elseif ((Get-Command choco -ErrorAction SilentlyContinue) -and (Confirm-Install "[check] Install Node.js via Chocolatey?")) {
                & choco install nodejs-lts -y 2>&1 | Out-Null
                $installed = $true
            } else {
                Write-Host "[warn] Install Node.js 18+ from https://nodejs.org, then re-run for Claude Code install." -ForegroundColor Yellow
            }
        } catch {
            Write-Host "[warn] Node.js install failed: $($_.Exception.Message). Install manually: https://nodejs.org" -ForegroundColor Yellow
        }
        if ($installed) {
            # Refresh PATH so npm becomes available in this session
            $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
        }
    }

    if (Get-Command claude -ErrorAction SilentlyContinue) {
        Write-Host "[check] Claude Code:  installed"
    } elseif (Get-Command claude.cmd -ErrorAction SilentlyContinue) {
        Write-Host "[check] Claude Code:  installed (claude.cmd)"
    } else {
        Write-Host "[check] Claude Code:  NOT FOUND" -ForegroundColor Yellow
        if ((Get-Command npm -ErrorAction SilentlyContinue) -and (Confirm-Install "[check] Install Claude Code via npm?")) {
            npm install -g @anthropic-ai/claude-code
        } else {
            Write-Host "[warn] Install later:  npm install -g @anthropic-ai/claude-code   (needs Node 18+)" -ForegroundColor Yellow
        }
    }

    if (Test-Path $FREE_DIR) {
        Write-Host "[check] GrapeRoot Free detected at $FREE_DIR - Pro will install alongside (free install untouched)"
    }

    # -----------------------------------------------------------------------
    # License verify -- v1.0.12: distinguish network failure from server rejection
    # Old behavior collapsed both into "License server unreachable", which
    # masked typo'd / revoked keys as connectivity issues.
    # -----------------------------------------------------------------------
    Write-Host "[verify] Validating license..."

    # Honor SSL-inspecting corp proxy CA bundle
    $extraIRMArgs = @{}
    if ($env:GRAPEROOT_CA_BUNDLE -and (Test-Path $env:GRAPEROOT_CA_BUNDLE)) {
        Write-Host "[verify] Using custom CA bundle: $env:GRAPEROOT_CA_BUNDLE"
        # PS5.1 has no -Certificate; use callback to trust this CA only
        # PS7 has -SslProtocol; for safety we just hint and let curl-style env handle it
    }
    if ($env:HTTPS_PROXY) { Write-Host "[verify] Routing via proxy: $env:HTTPS_PROXY" }

    $verify = $null
    $verifyBody = $null
    $verifyHttpCode = 0
    $verifyExceptionMsg = ""
    $maxAttempts = 3
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        try {
            $verify = Invoke-RestMethod -Method POST -Uri "$API/v1/license/verify" `
                -ContentType "application/json" -TimeoutSec 15 `
                -Body (@{ license_key = $LicenseKey; host = $env:COMPUTERNAME; os = "windows" } | ConvertTo-Json)
            $verifyHttpCode = 200
            break
        } catch {
            $verifyExceptionMsg = $_.Exception.Message
            $resp = $_.Exception.Response
            if ($resp) {
                # Got an HTTP response (any status code) -> server is reachable, parse body
                try {
                    $verifyHttpCode = [int]$resp.StatusCode
                    $reader = New-Object IO.StreamReader($resp.GetResponseStream())
                    $verifyBody = $reader.ReadToEnd()
                    $reader.Close()
                    try { $verify = $verifyBody | ConvertFrom-Json } catch { $verify = $null }
                } catch {}
                break  # don't retry server-side rejections
            }
            # No response -> network-layer failure, retry with backoff
            if ($attempt -lt $maxAttempts) {
                $backoff = $attempt * 5
                Write-Host "[verify] Attempt ${attempt}: cannot reach $API ($verifyExceptionMsg). Retrying in ${backoff}s..." -ForegroundColor Yellow
                Start-Sleep -Seconds $backoff
            }
        }
    }

    if (-not $verify -and $verifyHttpCode -eq 0) {
        # All attempts failed at the network layer
        $hostName = ([Uri]$API).Host
        Write-Host ""
        Write-Host "[error] Cannot reach license server after $maxAttempts attempts." -ForegroundColor Red
        Write-Host ""
        Write-Host "  Cause: $verifyExceptionMsg" -ForegroundColor Yellow
        Write-Host "  Likely: corporate firewall, DNS filter, or SSL-inspecting proxy." -ForegroundColor Yellow
        Write-Host "  Test:  Resolve-DnsName $hostName" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  -- Send to your IT team to whitelist --" -ForegroundColor Cyan
        Write-Host "  HTTPS allow:  api.graperoot.dev, graperoot.dev, pub-pro-r2.graperoot.dev"
        Write-Host "  IP allow:     104.21.91.161, 172.67.175.90  (Cloudflare, may rotate)"
        Write-Host ""
        Write-Host "  -- If you have a corporate proxy --" -ForegroundColor Cyan
        Write-Host "  `$env:HTTPS_PROXY = 'http://your-proxy:8080'"
        Write-Host "  `$env:GRAPEROOT_CA_BUNDLE = 'C:\path\to\corp-ca.pem'  # only if SSL inspection"
        Write-Host "  Then re-run the installer."
        Write-Host ""
        Write-Host "  Support: support@graperoot.dev  (include this whole error)" -ForegroundColor Yellow
        exit 1
    }

    if (-not $verify -or -not $verify.valid) {
        $reason = if ($verify -and $verify.reason) { $verify.reason } else { "invalid response (HTTP $verifyHttpCode)" }
        Write-Host "[error] License rejected: $reason" -ForegroundColor Red
        Write-Host "        HTTP $verifyHttpCode from $API" -ForegroundColor Red
        Write-Host "        Support: support@graperoot.dev"
        exit 1
    }
    Write-Host "[verify] Valid  |  $($verify.customer)  |  expires: $($verify.expires)"

    # -----------------------------------------------------------------------
    # Install
    # -----------------------------------------------------------------------
    New-Item -ItemType Directory -Path "$INSTALL_DIR\bin" -Force | Out-Null

    Write-Host "[install] Downloading GrapeRoot Pro package..."
    $tmpTgz = Join-Path $env:TEMP "graperoot-pro.tar.gz"
    Invoke-WebRequestWithRetry -Uri $verify.download_url -OutFile $tmpTgz -TimeoutSec 120
    if (-not (Test-ValidTarball $tmpTgz)) {
        throw "Downloaded package is not a valid gzip archive (R2 may have returned an HTML error page). Try again in a few minutes."
    }
    & tar -xzf $tmpTgz -C $INSTALL_DIR --strip-components=1
    if ($LASTEXITCODE -ne 0) { throw "tar extraction failed (Windows 10 1803+ required, or install Git Bash)" }
    Remove-Item $tmpTgz -ErrorAction SilentlyContinue

    Write-Host "[install] Downloading launcher..."
    foreach ($f in @("launch_pro.ps1","dgc-pro.cmd","dgc-pro.ps1","version.txt","changelog.txt")) {
        $dest = Join-Path "$INSTALL_DIR\bin" $f
        try { Invoke-WebRequestWithRetry -Uri "$R2/bin/$f" -OutFile $dest }
        catch { Invoke-WebRequestWithRetry -Uri "$BASE_URL/bin/$f" -OutFile $dest }
        # R2 CDN returns 200 + HTML on cert issues - validate and fall back to GitHub.
        if (-not (Test-ValidScript $dest)) {
            Invoke-WebRequestWithRetry -Uri "$BASE_URL/bin/$f" -OutFile $dest
        }
    }

    Write-Host "[install] Creating isolated Python venv..."
    & $pythonCmd -m venv "$INSTALL_DIR\venv" | Out-Null
    $venvPy = "$INSTALL_DIR\venv\Scripts\python.exe"
    & $venvPy -m pip install --quiet --upgrade pip
    & $venvPy -m pip install --quiet -r "$INSTALL_DIR\requirements.txt"

    # License persistence (owner-only ACL)
    $licenseFile = "$INSTALL_DIR\license.key"
    Set-Content -Path $licenseFile -Value $LicenseKey -NoNewline -Encoding ASCII
    $acl = Get-Acl $licenseFile
    $acl.SetAccessRuleProtection($true, $false)
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        [System.Security.Principal.WindowsIdentity]::GetCurrent().Name, "Read,Write", "Allow")
    $acl.SetAccessRule($rule)
    Set-Acl -Path $licenseFile -AclObject $acl

    # PATH - user scope
    $userPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    $binDir   = "$INSTALL_DIR\bin"
    if ($userPath -notlike "*$binDir*") {
        [Environment]::SetEnvironmentVariable("PATH", "$binDir;$userPath", "User")
        Write-Host "[install] Added $binDir to user PATH"
    }

    $ver = if (Test-Path "$INSTALL_DIR\bin\version.txt") { (Get-Content "$INSTALL_DIR\bin\version.txt" -Raw).Trim() } else { "1.0.12" }
    Write-Host ""
    Write-Host "+==============================================================+" -ForegroundColor Green
    Write-Host "|  Install complete.  GrapeRoot Pro v$ver" -ForegroundColor Green
    Write-Host "+==============================================================+" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Open a new PowerShell window (PATH refresh), then:"
    Write-Host "    dgc-pro C:\path\to\your\project" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  Docs:    https://graperoot.dev/pro/docs"
    Write-Host "  Support: support@graperoot.dev"
    Write-Host ""
}
catch {
    Write-Host ""
    Write-Host "[fatal] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "        Contact support@graperoot.dev with this message."
    exit 1
}
