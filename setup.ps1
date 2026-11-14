# ═══════════════════════════════════════════════════════════════════════════
# Gemini Canvas Proxy — Setup Script (Windows PowerShell)
# ═══════════════════════════════════════════════════════════════════════════
# Run in PowerShell: .\setup.ps1
#
# This script:
#   1. Verifies Python is installed
#   2. Generates (or reuses) the bearer token for the HTTP API
#   3. Asks for the Chrome extension ID (after you load the extension)
#   4. Creates a .bat wrapper and installs the native messaging host
#      manifest in the registry for Chrome, Chromium, and Edge
#
# Works on: Windows 10/11, PowerShell 5.1+
# ═══════════════════════════════════════════════════════════════════════════

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$NativeHostName = "com.gemini.proxy"
$HostScript = Join-Path $ScriptDir "native_host\gemini_proxy.py"
$HostScriptWin = $HostScript -replace "/", "\"
$TokenFile = Join-Path $ScriptDir "native_host\.proxy_token"

# Sanity check: make sure we're in the right directory
if (-not (Test-Path $HostScript)) {
    Write-Host "[ERROR] gemini_proxy.py not found at $HostScript" -ForegroundColor Red
    Write-Host "  Make sure you're running this from the gemini-canvas-proxy directory." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host "       Gemini Canvas Proxy - Setup (Windows)       " -ForegroundColor Cyan
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host ""

# ── Step 1: Verify Python ────────────────────────────────────────────────────

$pythonExe = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonExe) {
    $pythonExe = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $pythonExe) {
    Write-Host "[ERROR] Python not found. Install Python 3.8+ from https://python.org" -ForegroundColor Red
    exit 1
}
# Resolve the interpreter that was actually found. Re-querying `python` here
# would return the Microsoft Store shim, or nothing at all on a machine that
# only has `python3`.
$PythonPath = $pythonExe.Source
Write-Host "[OK] Python found: $PythonPath" -ForegroundColor Green

if (-not (Test-Path $TokenFile) -or [string]::IsNullOrWhiteSpace((Get-Content $TokenFile -Raw))) {
    $ProxyToken = [guid]::NewGuid().ToString()
    Set-Content -Path $TokenFile -Value $ProxyToken -NoNewline -Encoding ASCII
} else {
    $ProxyToken = (Get-Content $TokenFile -Raw).Trim()
}
Write-Host "[OK] Bearer token written to $TokenFile" -ForegroundColor Green

# ── Step 2: Get extension ID ────────────────────────────────────────────────

Write-Host ""
Write-Host "=== Load the Chrome Extension ===" -ForegroundColor Yellow
Write-Host "1. Open chrome://extensions/"
Write-Host "2. Enable 'Developer mode' (top-right toggle)"
Write-Host "3. Click 'Load unpacked' and select: $ScriptDir\extension\"
Write-Host "4. Copy the Extension ID (32-char string below the extension name)"
Write-Host ""
$ExtensionId = Read-Host "Paste Extension ID"

if ($ExtensionId -notmatch '^[a-p]{32}$') {
    Write-Host "[ERROR] Invalid extension ID." -ForegroundColor Red
    Write-Host "Chrome extension IDs must contain exactly 32 characters from a-p."
    exit 1
}

Write-Host ""
Write-Host "[OK] Extension ID: $ExtensionId" -ForegroundColor Green

# ── Step 3: Create a wrapper batch file ──────────────────────────────────────
# On Windows, the native messaging host path must point to an .exe or .bat file

$BatchPath = Join-Path $ScriptDir "native_host\gemini_proxy.bat"
$BatchContent = "@echo off`r`n`"$PythonPath`" `"$HostScriptWin`" %*"
Set-Content -Path $BatchPath -Value $BatchContent -Encoding ASCII
Write-Host "[OK] Created wrapper: $BatchPath" -ForegroundColor Green

# ── Step 4: Generate and install the manifest ───────────────────────────────

$Manifest = @{
    name = $NativeHostName
    description = "Gemini Canvas Proxy - free unlimited LLM API via Canvas MessageChannel bridge"
    path = $BatchPath
    type = "stdio"
    allowed_origins = @("chrome-extension://$ExtensionId/")
} | ConvertTo-Json -Depth 3

# Windows registry locations for native messaging hosts
$RegistryPaths = @(
    "HKCU:\SOFTWARE\Google\Chrome\NativeMessagingHosts\$NativeHostName",
    "HKCU:\SOFTWARE\Chromium\NativeMessagingHosts\$NativeHostName",
    "HKCU:\SOFTWARE\Microsoft\Edge\NativeMessagingHosts\$NativeHostName"
)

# Also install as a file (some setups use file-based discovery)
$FileLocations = @(
    "$env:LOCALAPPDATA\Google\Chrome\User Data\NativeMessagingHosts",
    "$env:LOCALAPPDATA\Chromium\User Data\NativeMessagingHosts"
)

$ManifestFile = Join-Path $ScriptDir "native_host\$NativeHostName.json"
Set-Content -Path $ManifestFile -Value $Manifest -Encoding UTF8

# Install in registry (Chrome on Windows uses registry)
foreach ($RegPath in $RegistryPaths) {
    try {
        $keyParent = Split-Path $RegPath -Parent
        if (-not (Test-Path $keyParent)) {
            New-Item -Path $keyParent -Force | Out-Null
        }
        New-Item -Path $RegPath -Force | Out-Null
        Set-ItemProperty -Path $RegPath -Name "(Default)" -Value $ManifestFile
        Write-Host "[OK] Registry: $RegPath" -ForegroundColor Green
    } catch {
        Write-Host "[SKIP] Registry: $RegPath (error: $_)" -ForegroundColor DarkGray
    }
}

# Install as file in user data directories
foreach ($FileLoc in $FileLocations) {
    try {
        New-Item -ItemType Directory -Path $FileLoc -Force | Out-Null
        Copy-Item $ManifestFile -Destination (Join-Path $FileLoc "$NativeHostName.json") -Force
        Write-Host "[OK] File: $FileLoc" -ForegroundColor Green
    } catch {
        Write-Host "[SKIP] File: $FileLoc" -ForegroundColor DarkGray
    }
}

# ── Done ─────────────────────────────────────────────────────────────────────

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host "              Setup Complete!" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:"
Write-Host ""
Write-Host "  1. Go to gemini.google.com"
Write-Host "  2. Click the '+' icon (left of the prompt bar)"
Write-Host "  3. Select 'Canvas' from the menu"
Write-Host "  4. Type: Create an HTML web app"
Write-Host "  5. Switch to the Code tab (top of the Canvas panel)"
Write-Host "  6. Select all generated code -> delete it"
Write-Host "  7. Open canvas-proxy.html from this project"
Write-Host "  8. Copy ALL contents -> paste into Canvas code editor"
Write-Host "  9. Click Preview - you should see 'Gemini Canvas Proxy'"
Write-Host "     with a green 'Proxy Active' status"
Write-Host ""
Write-Host "  10. Test the proxy:"
Write-Host "     curl http://127.0.0.1:8765/v1/chat/completions -H `"Authorization: Bearer $ProxyToken`" -H `"Content-Type: application/json`" -d `"{\`"model\`":\`"gemini-3-flash-preview\`",\`"messages\`":[{\`"role\`":\`"user\`",\`"content\`":\`"Hello!\`"}]}`""
Write-Host ""
Write-Host "  Bearer token: $ProxyToken"
Write-Host "  Keep this token private; clients must send it as their API key."
Write-Host ""
Write-Host "  11. See README.md for integration with Hermes, OpenClaw, etc."
Write-Host ""
