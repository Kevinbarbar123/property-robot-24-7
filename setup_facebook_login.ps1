# Facebook Marketplace one-time login setup.
#
# What this does:
#   1. Installs the Python packages this bot needs.
#   2. Installs the Chromium browser that Playwright uses.
#   3. Opens a browser window so you can log into Facebook.
#   4. Saves that login so the bot can browse Marketplace as you.
#
# How to use:
#   In File Explorer, right-click this file and choose "Run with PowerShell".
#   (If Windows blocks it: right-click -> Properties -> check "Unblock" -> OK, then try again.)

Set-Location -Path $PSScriptRoot

Write-Host "Step 1/3: Installing Python packages..." -ForegroundColor Cyan
python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Could not install Python packages. Is Python installed and on PATH?" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host ""
Write-Host "Step 2/3: Installing the Chromium browser (this can take a few minutes)..." -ForegroundColor Cyan
python -m playwright install chromium
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Could not install the Chromium browser." -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

Write-Host ""
Write-Host "Step 3/3: Opening Facebook for you to log in..." -ForegroundColor Cyan
Write-Host "A new browser window will open." -ForegroundColor Yellow
Write-Host "Log into the Facebook account the bot should use for Marketplace," -ForegroundColor Yellow
Write-Host "solve any 2FA/checkpoint prompts, then come back to THIS window and press Enter." -ForegroundColor Yellow
python facebook_login_setup.py

Write-Host ""
Write-Host "All done! data\facebook_session_state.json has been created." -ForegroundColor Green
Read-Host "Press Enter to close this window"
