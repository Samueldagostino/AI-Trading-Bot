# ============================================================
# NQ Trading Bot — Google Drive Sync Script
# ============================================================
# Copies the essential bot files to Google Drive for cloud backup.
# Run manually or schedule via Task Scheduler.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/sync_to_gdrive.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/sync_to_gdrive.ps1 -FullSync
# ============================================================

param(
    [switch]$FullSync  # Include data/ folder (large, ~215MB)
)

$ErrorActionPreference = "Stop"

# --- Configuration ---
$BotRoot    = "C:\Users\dagos\OneDrive\Desktop\AI-Trading-Bot"
$GDriveRoot = "G:\My Drive\NQ-Trading-Bot"

# --- Folders to sync ---
$CoreFolders = @(
    "nq_bot_vscode\Broker",
    "nq_bot_vscode\config",
    "nq_bot_vscode\dashboard",
    "nq_bot_vscode\data_feeds",
    "nq_bot_vscode\data_pipeline",
    "nq_bot_vscode\database",
    "nq_bot_vscode\execution",
    "nq_bot_vscode\features",
    "nq_bot_vscode\ml",
    "nq_bot_vscode\monitoring",
    "nq_bot_vscode\research",
    "nq_bot_vscode\risk",
    "nq_bot_vscode\scripts",
    "nq_bot_vscode\signals",
    "nq_bot_vscode\tests",
    "nq_bot_vscode\Knowledge",
    "docs",
    ".github",
    "tests"
)

# --- Top-level files to sync ---
$CoreFiles = @(
    "nq_bot_vscode\main.py",
    "nq_bot_vscode\CLAUDE.md",
    "nq_bot_vscode\requirements.txt",
    "README.md",
    ".gitignore"
)

# --- Start ---
$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  NQ Trading Bot -> Google Drive Sync" -ForegroundColor Cyan
Write-Host "  $timestamp" -ForegroundColor Gray
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Source:  $BotRoot"
Write-Host "  Dest:    $GDriveRoot"
Write-Host ""

# Create root destination
if (-not (Test-Path $GDriveRoot)) {
    New-Item -ItemType Directory -Path $GDriveRoot -Force | Out-Null
    Write-Host "  [+] Created $GDriveRoot" -ForegroundColor Green
}

# --- Sync folders ---
$folderCount = 0
foreach ($folder in $CoreFolders) {
    $src = Join-Path $BotRoot $folder
    $dst = Join-Path $GDriveRoot $folder

    if (Test-Path $src) {
        # Create parent directory
        $dstParent = Split-Path $dst -Parent
        if (-not (Test-Path $dstParent)) {
            New-Item -ItemType Directory -Path $dstParent -Force | Out-Null
        }

        # Use robocopy for efficient sync (only changed files)
        $roboArgs = @($src, $dst, "/MIR", "/XD", "__pycache__", ".pytest_cache", "/XF", "*.pyc", "*.log", "/NFL", "/NDL", "/NJH", "/NJS", "/NC", "/NS", "/NP")
        & robocopy @roboArgs | Out-Null

        $folderCount++
        Write-Host "  [OK] $folder" -ForegroundColor Green
    } else {
        Write-Host "  [--] $folder (not found, skipping)" -ForegroundColor Yellow
    }
}

# --- Sync individual files ---
$fileCount = 0
foreach ($file in $CoreFiles) {
    $src = Join-Path $BotRoot $file
    $dst = Join-Path $GDriveRoot $file

    if (Test-Path $src) {
        $dstDir = Split-Path $dst -Parent
        if (-not (Test-Path $dstDir)) {
            New-Item -ItemType Directory -Path $dstDir -Force | Out-Null
        }
        Copy-Item -Path $src -Destination $dst -Force
        $fileCount++
        Write-Host "  [OK] $file" -ForegroundColor Green
    }
}

# --- Sync logs snapshot (latest state only, not full history) ---
$logsDir = Join-Path $GDriveRoot "nq_bot_vscode\logs"
if (-not (Test-Path $logsDir)) {
    New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
}
$logFiles = @(
    "paper_trading_state.json",
    "safety_state.json",
    "modifier_state.json",
    "candle_buffer.json"
)
foreach ($lf in $logFiles) {
    $src = Join-Path $BotRoot "nq_bot_vscode\logs\$lf"
    if (Test-Path $src) {
        Copy-Item -Path $src -Destination (Join-Path $logsDir $lf) -Force
    }
}
Write-Host "  [OK] logs (state snapshot)" -ForegroundColor Green

# --- Optional: Full data sync ---
if ($FullSync) {
    Write-Host ""
    Write-Host "  Full sync: copying data/ folder (~215MB)..." -ForegroundColor Yellow
    $dataSrc = Join-Path $BotRoot "data"
    $dataDst = Join-Path $GDriveRoot "data"
    & robocopy $dataSrc $dataDst /MIR /XD __pycache__ /XF *.log /NFL /NDL /NJH /NJS /NC /NS /NP | Out-Null
    Write-Host "  [OK] data/" -ForegroundColor Green
}

# --- Write sync timestamp ---
$meta = @{
    last_sync = (Get-Date -Format "o")
    source = $BotRoot
    folders_synced = $folderCount
    files_synced = $fileCount
    full_sync = $FullSync.IsPresent
} | ConvertTo-Json
$meta | Out-File -FilePath (Join-Path $GDriveRoot "sync_meta.json") -Encoding utf8

# --- Summary ---
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Sync complete!" -ForegroundColor Green
Write-Host "  Folders: $folderCount  |  Files: $fileCount" -ForegroundColor Gray
Write-Host "  Google Drive will auto-sync to cloud" -ForegroundColor Gray
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
