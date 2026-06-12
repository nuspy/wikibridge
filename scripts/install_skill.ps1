# Installa la skill wiki-import:
#  - Claude Code (Windows): junction in ~\.claude\skills\wiki-import
#  - Hermes (WSL): delega a install_skill.sh
$src = "C:\Projects\wikibridge\skill"
$dst = Join-Path $env:USERPROFILE ".claude\skills\wiki-import"

if (Test-Path $dst) {
    Write-Host "già presente: $dst"
} else {
    New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
    New-Item -ItemType Junction -Path $dst -Target $src | Out-Null
    Write-Host "creata junction: $dst -> $src"
}

wsl.exe -d Ubuntu -- bash -lc "/mnt/c/Projects/wikibridge/scripts/install_skill.sh"
