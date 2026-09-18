<#
    SISTEMA 2 de 2 — os jogos (Vitamina, Corrida, Suporte).
    Não tem chave de API e não chama ninguém. Fica aberto servindo frames;
    quem quiser jogar pede um frame e manda uma ação.

    Rode e pronto:   .\start_game.ps1
    A ordem não importa: o framework pluga quando subir.
#>

# ----------------------------------------------------------------------
#  Porta deste sistema e onde mora o framework. É só mudar aqui.
# ----------------------------------------------------------------------
$Port = 8100
# ----------------------------------------------------------------------

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
. "$PSScriptRoot\jev_env.ps1"

if ($env:GAME_PORT) { $Port = [int]$env:GAME_PORT }
$envFile = Join-Path $PSScriptRoot ".env"
$url = "http://127.0.0.1:$Port"

Write-Host ""
Write-Host "  JOGOS  (sistema 2 de 2)" -ForegroundColor Cyan
Write-Host "  -----------------------" -ForegroundColor DarkGray

# --- interpretador ---
$python = Resolve-Python -EnvFile $envFile
if (-not $python) { Show-PythonHelp; exit 1 }
Write-Host "  python          $($python.Version)  " -NoNewline
Write-Host "$($python.Path)" -ForegroundColor DarkGray

# --- porta ---
$busy = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
    $owner = (Get-Process -Id $busy[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
    Write-Host "  porta $Port      " -NoNewline
    Write-Host "OCUPADA pelo PID $($busy[0].OwningProcess) ($owner)" -ForegroundColor Red
    Write-Host "                  feche esse processo, ou mude `$Port no topo deste arquivo." -ForegroundColor DarkGray
    exit 1
}
Write-Host "  porta           $Port"
Write-Host "  papel           servidor passivo — espera clientes"

Write-Host ""
Write-Host "  jogo:     $url/game.html" -ForegroundColor Cyan
Write-Host ""

# defina $env:JEV_NO_BROWSER=1 para nao abrir o navegador sozinho
if (-not $env:JEV_NO_BROWSER) {
Start-Job -Name "abrir-jogo" -ScriptBlock {
    param($u)
    for ($i = 0; $i -lt 40; $i++) {
        try { Invoke-RestMethod -Uri "$u/api/handshake" -TimeoutSec 2 | Out-Null
              Start-Process "$u/game.html"; return } catch { Start-Sleep -Milliseconds 400 }
    }
} -ArgumentList $url | Out-Null
}

try {
    $env:GAME_PORT = "$Port"
    & $python.Path -u (Join-Path $PSScriptRoot "game\game_server.py")
} finally {
    Remove-Job -Name "abrir-jogo" -Force -ErrorAction SilentlyContinue
}
exit $LASTEXITCODE
