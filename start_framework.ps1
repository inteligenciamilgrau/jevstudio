<#
    SISTEMA 1 de 2 — o framework (motor de funis + conector Jev).
    É quem tem a chave da API. Não conhece nenhum jogo.

    Rode e pronto:   .\start_framework.ps1
#>

# ----------------------------------------------------------------------
#  Porta deste sistema. É só mudar aqui.
# ----------------------------------------------------------------------
$Port = 8000
$GameUrl = "http://127.0.0.1:8100"   # jogo que o driver pilota por padrão
# ----------------------------------------------------------------------

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
. "$PSScriptRoot\jev_env.ps1"

if ($env:FRAMEWORK_PORT) { $Port = [int]$env:FRAMEWORK_PORT }
if ($env:GAME_URL) { $GameUrl = $env:GAME_URL }
$envFile = Join-Path $PSScriptRoot ".env"
$url = "http://127.0.0.1:$Port"

Write-Host ""
Write-Host "  FRAMEWORK  (sistema 1 de 2)" -ForegroundColor Cyan
Write-Host "  ---------------------------" -ForegroundColor DarkGray

# --- chave: este é o único dos dois que precisa dela ---
$keySource = Test-ApiKey -EnvFile $envFile
if ($keySource) {
    Write-Host "  chave TypeSafe  " -NoNewline
    Write-Host "ok ($keySource)" -ForegroundColor Green
} else {
    Write-Host "  chave TypeSafe  " -NoNewline
    Write-Host "NÃO ENCONTRADA" -ForegroundColor Red
    Write-Host "                  o servidor sobe, mas toda decisão vai falhar." -ForegroundColor DarkGray
    Write-Host "                  adicione ao .env:  TYPESAFE_API_KEY=sua_chave" -ForegroundColor DarkGray
}

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
Write-Host "  pilota          $GameUrl" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  console:  $url/arena.html" -ForegroundColor Cyan
Write-Host ""

# abre o navegador assim que o servidor responder, sem travar a inicialização
# defina $env:JEV_NO_BROWSER=1 para nao abrir o navegador sozinho
if (-not $env:JEV_NO_BROWSER) {
Start-Job -Name "abrir-framework" -ScriptBlock {
    param($u)
    for ($i = 0; $i -lt 40; $i++) {
        try { Invoke-RestMethod -Uri "$u/api/handshake" -TimeoutSec 2 | Out-Null
              Start-Process "$u/arena.html"; return } catch { Start-Sleep -Milliseconds 400 }
    }
} -ArgumentList $url | Out-Null
}

try {
    $env:FRAMEWORK_PORT = "$Port"
    $env:GAME_URL = $GameUrl
    & $python.Path -u (Join-Path $PSScriptRoot "framework\framework_server.py")
} finally {
    Remove-Job -Name "abrir-framework" -Force -ErrorAction SilentlyContinue
}
exit $LASTEXITCODE
