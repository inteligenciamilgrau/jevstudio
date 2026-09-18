<#
    Sobe os dois sistemas e confirma que eles se enxergam.

        .\start.ps1                      # duas janelas novas
        .\start.ps1 -Same                # tudo nesta janela, Ctrl+C derruba os dois
        .\start.ps1 -FrameworkPort 9000 -GamePort 9100

    O interpretador vem de $env:PYTHON, ou de PYTHON= no .env, ou do PATH.
#>
[CmdletBinding()]
param(
    [int]$FrameworkPort = 8000,
    [int]$GamePort = 8100,
    [switch]$Same,          # roda como jobs nesta janela em vez de abrir outras
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
. "$PSScriptRoot\jev_env.ps1"

$envFile = Join-Path $PSScriptRoot ".env"

# ---- interpretador -------------------------------------------------------
$python = Resolve-Python -EnvFile $envFile
if (-not $python) { Show-PythonHelp; exit 1 }
Write-Host "Python $($python.Version)" -ForegroundColor Green -NoNewline
Write-Host "  $($python.Path)  [$($python.Source)]" -ForegroundColor DarkGray

# ---- chave (só o framework precisa) --------------------------------------
$keySource = Test-ApiKey -EnvFile $envFile
if ($keySource) {
    Write-Host "Chave TypeSafe encontrada ($keySource)." -ForegroundColor Green
} else {
    Write-Host "Sem TYPESAFE_API_KEY — o framework sobe, mas toda decisão vai falhar." -ForegroundColor Yellow
    Write-Host '    adicione ao .env:  TYPESAFE_API_KEY=sua_chave' -ForegroundColor DarkGray
}

# ---- portas livres? ------------------------------------------------------
foreach ($item in @(@{N = "framework"; P = $FrameworkPort }, @{N = "jogos"; P = $GamePort })) {
    $busy = Get-NetTCPConnection -LocalPort $item.P -State Listen -ErrorAction SilentlyContinue
    if ($busy) {
        Write-Host "A porta $($item.P) ($($item.N)) já está ocupada pelo PID $($busy[0].OwningProcess)." -ForegroundColor Red
        Write-Host "    feche o processo, ou use -FrameworkPort / -GamePort" -ForegroundColor DarkGray
        exit 1
    }
}

$frameworkUrl = "http://127.0.0.1:$FrameworkPort"
$gameUrl = "http://127.0.0.1:$GamePort"

# O framework é quem fala com a TypeSafe; o jogo só precisa saber onde ele está.
$frameworkEnv = @{ FRAMEWORK_PORT = "$FrameworkPort" }
$gameEnv = @{ GAME_PORT = "$GamePort"; FRAMEWORK_URL = $frameworkUrl }

function Start-System {
    param([string]$Title, [string]$Folder, [string]$Script, [hashtable]$Vars)
    $assignments = ($Vars.GetEnumerator() | ForEach-Object { "`$env:$($_.Key)='$($_.Value)'" }) -join '; '
    $command = "$assignments; `$Host.UI.RawUI.WindowTitle='$Title'; & '$($python.Path)' -u '$Script'"
    Start-Process powershell -WorkingDirectory $Folder `
        -ArgumentList @("-NoExit", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $command) | Out-Null
}

function Start-SystemJob {
    param([string]$Name, [string]$Folder, [string]$Script, [hashtable]$Vars)
    Start-Job -Name $Name -ScriptBlock {
        param($exe, $folder, $script, $vars)
        foreach ($pair in $vars.GetEnumerator()) {
            Set-Item -Path "env:$($pair.Key)" -Value $pair.Value
        }
        Set-Location -LiteralPath $folder
        & $exe -u $script 2>&1
    } -ArgumentList $python.Path, $Folder, $Script, $Vars | Out-Null
}

Write-Host ""
if ($Same) {
    Start-SystemJob -Name "framework" -Folder "$PSScriptRoot\framework" -Script "framework_server.py" -Vars $frameworkEnv
    Start-SystemJob -Name "jogos"     -Folder "$PSScriptRoot\game"      -Script "game_server.py"      -Vars $gameEnv
} else {
    Start-System -Title "Jev framework :$FrameworkPort" -Folder "$PSScriptRoot\framework" `
                 -Script "framework_server.py" -Vars $frameworkEnv
    Start-Sleep -Milliseconds 700   # o jogo dá handshake ao subir; deixa o framework nascer antes
    Start-System -Title "Jev jogos :$GamePort" -Folder "$PSScriptRoot\game" `
                 -Script "game_server.py" -Vars $gameEnv
}

# ---- confirma que os dois estão de pé e se enxergando --------------------
Write-Host "Subindo…" -ForegroundColor DarkGray
$framework = Wait-ForHandshake -Url $frameworkUrl
$game = Wait-ForHandshake -Url $gameUrl

Write-Host ""
if ($framework) {
    Write-Host "  [ok] " -ForegroundColor Green -NoNewline
    Write-Host "framework  $frameworkUrl  ($($framework.service) v$($framework.version), " -NoNewline
    Write-Host "$($framework.funnels.Count) funis, Jev=$($framework.jev.configured))"
} else {
    Write-Host "  [--] framework não respondeu em $frameworkUrl" -ForegroundColor Red
}
if ($game) {
    Write-Host "  [ok] " -ForegroundColor Green -NoNewline
    Write-Host "jogos      $gameUrl  ($($game.service) v$($game.version), " -NoNewline
    Write-Host "$(($game.games | ForEach-Object { $_.id }) -join ', '))"
} else {
    Write-Host "  [--] sistema de jogos não respondeu em $gameUrl" -ForegroundColor Red
}

if ($framework -and $game) {
    Write-Host ""
    Write-Host "  abra o console do framework: $frameworkUrl/arena.html" -ForegroundColor Cyan
    Write-Host "  abra o jogo:                 $gameUrl/game.html" -ForegroundColor Cyan
    if (-not $NoBrowser) {
        Start-Process "$frameworkUrl/arena.html"
        Start-Process "$gameUrl/game.html"
    }
}

if ($Same) {
    Write-Host ""
    Write-Host "Rodando nesta janela. Ctrl+C derruba os dois." -ForegroundColor DarkGray
    try {
        while ($true) {
            Receive-Job -Name framework, jogos -ErrorAction SilentlyContinue | ForEach-Object { $_ }
            Start-Sleep -Milliseconds 500
        }
    } finally {
        Stop-Job -Name framework, jogos -ErrorAction SilentlyContinue
        Remove-Job -Name framework, jogos -Force -ErrorAction SilentlyContinue
        Write-Host "Os dois sistemas foram parados." -ForegroundColor DarkGray
    }
}
