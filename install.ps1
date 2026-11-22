# One-line installer for UTA ERB 335 Printers Skill + CLI (Windows)
# Usage: irm https://raw.githubusercontent.com/RyanHailey21/erb335-printers/main/install.ps1 | iex

$ErrorActionPreference = "Stop"
$RepoRaw = "https://raw.githubusercontent.com/RyanHailey21/erb335-printers/main/skills/erb335-printers"

$SkillRoots = @(
    "$env:USERPROFILE\.agents\skills\erb335-printers",
    "$env:USERPROFILE\.claude\skills\erb335-printers",
    "$env:USERPROFILE\.codex\skills\erb335-printers",
    "$env:USERPROFILE\.gemini\config\skills\erb335-printers"
)

$TmpDir = Join-Path $env:TEMP ("erb335-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path "$TmpDir\scripts" -Force | Out-Null

try {
    Write-Host "Downloading erb335-printers skill & CLI..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri "$RepoRaw/SKILL.md" -OutFile "$TmpDir\SKILL.md" -UseBasicParsing
    Invoke-WebRequest -Uri "$RepoRaw/scripts/printer_cli.py" -OutFile "$TmpDir\scripts\printer_cli.py" -UseBasicParsing

    # Copy as real directories (Antigravity skips Windows directory junctions)
    foreach ($dest in $SkillRoots) {
        $parent = Split-Path $dest -Parent
        if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        if (Test-Path $dest) {
            $item = Get-Item $dest -Force
            if ($item.LinkType) { cmd /c rmdir "$dest" } else { Remove-Item $dest -Recurse -Force }
        }
        Copy-Item -Path $TmpDir -Destination $dest -Recurse -Force
        Write-Host "  Installed skill -> $dest"
    }

    # Install global CLI shim in ~/.local/bin
    $BinDir = "$env:USERPROFILE\.local\bin"
    if (-not (Test-Path $BinDir)) { New-Item -ItemType Directory -Path $BinDir -Force | Out-Null }
    $ShimPath = Join-Path $BinDir "erb335-printers.cmd"
    "@echo off`r`npython `"%USERPROFILE%\.agents\skills\erb335-printers\scripts\printer_cli.py`" %*`r`n" | Set-Content -Path $ShimPath -Encoding ASCII
    Write-Host "  Installed CLI   -> $ShimPath"

    # Ensure ~/.local/bin is in User PATH
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($UserPath -notlike "*$BinDir*") {
        $NewPath = if ($UserPath) { "$UserPath;$BinDir" } else { $BinDir }
        [Environment]::SetEnvironmentVariable("Path", $NewPath, "User")
        $env:PATH = "$env:PATH;$BinDir"
        Write-Host "  Added $BinDir to User PATH" -ForegroundColor Yellow
    }

    Write-Host "`nDone! Run 'erb335-printers status' to verify (restart Antigravity/Claude/Codex to refresh skills)." -ForegroundColor Green
} finally {
    Remove-Item $TmpDir -Recurse -Force -ErrorAction SilentlyContinue
}
