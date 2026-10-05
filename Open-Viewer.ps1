param(
    [int]$Seconds = 1800,
    [string]$Database = (Join-Path $PSScriptRoot 'data\mailbox.sqlite3')
)
$ErrorActionPreference = 'Stop'
$Host.UI.RawUI.WindowTitle = 'Agent Bridge communications (read only)'
& python -u (Join-Path $PSScriptRoot 'viewer.py') --db $Database --seconds $Seconds --interval 2 --cursor-file (Join-Path $PSScriptRoot 'data\viewer-cursor.json')
if ($LASTEXITCODE -ne 0) { Write-Host 'Viewer stopped with an error. Mailbox contents were not modified.' }
