# Helpers compartilhados. Não execute direto — carregue com ponto:
#
#     . "$PSScriptRoot\jev_env.ps1"
#
# O interpretador vem da variável de ambiente PYTHON. Se ela não existir,
# procura PYTHON= no .env, depois o PATH, depois instalações comuns do Windows.

function Get-DotEnvValue {
    <#
      Lê um valor do .env. Mesmas regras do carregador em Python: ignora
      comentários e linhas vazias, aceita prefixo "export ", tira aspas.
    #>
    param([string]$Path, [string[]]$Names)
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if ($trimmed -eq "" -or $trimmed.StartsWith("#")) { continue }
        if ($trimmed -notmatch '^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') { continue }
        $name = $Matches[1]
        $value = $Matches[2].Trim()
        if ($value.Length -ge 2 -and $value[0] -eq $value[-1] -and ($value[0] -eq '"' -or $value[0] -eq "'")) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if (($Names -contains $name) -and $value -ne "") { return $value }
    }
    return $null
}

function Resolve-Python {
    <#
      Devolve @{Path; Version; Source} do primeiro interpretador que roda de
      verdade, ou $null. Validar executando descarta o stub da Microsoft Store
      e caminhos mortos no .env.
    #>
    param([string]$EnvFile)

    $candidates = @()
    if ($env:PYTHON) { $candidates += @{ Path = $env:PYTHON; Source = 'variável de ambiente PYTHON' } }
    if ($EnvFile) {
        $fromFile = Get-DotEnvValue -Path $EnvFile -Names @("PYTHON")
        if ($fromFile) { $candidates += @{ Path = $fromFile; Source = "PYTHON no .env" } }
    }
    foreach ($name in @("py", "python", "python3")) {
        $candidates += @{ Path = $name; Source = "PATH ($name)" }
    }
    $globs = @("C:\Python3*\python.exe", "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe")
    foreach ($found in (Get-ChildItem -Path $globs -ErrorAction SilentlyContinue |
                        Sort-Object FullName -Descending)) {
        $candidates += @{ Path = $found.FullName; Source = "instalação encontrada no disco" }
    }

    foreach ($candidate in $candidates) {
        if (-not $candidate.Path) { continue }
        $exe = $null
        if (Test-Path -LiteralPath $candidate.Path -PathType Leaf) {
            $exe = (Resolve-Path -LiteralPath $candidate.Path).Path
        } else {
            $cmd = Get-Command $candidate.Path -CommandType Application -ErrorAction SilentlyContinue |
                   Select-Object -First 1
            if ($cmd) { $exe = $cmd.Source }
        }
        if (-not $exe) { continue }

        $version = $null
        $previous = $ErrorActionPreference
        try {
            $ErrorActionPreference = "Continue"
            $version = & $exe -c "import sys; print(sys.version.split()[0])" 2>$null
        } catch {
            continue
        } finally {
            $ErrorActionPreference = $previous
        }
        if ($LASTEXITCODE -eq 0 -and $version) {
            return [pscustomobject]@{
                Path    = $exe
                Version = ($version | Select-Object -First 1).Trim()
                Source  = $candidate.Source
            }
        }
    }
    return $null
}

function Show-PythonHelp {
    Write-Host "Nenhum interpretador Python utilizável foi encontrado." -ForegroundColor Red
    Write-Host "Aponte um destes jeitos:"
    Write-Host '    $env:PYTHON = "C:\Python312\python.exe"     # só nesta janela'
    Write-Host '    PYTHON=C:\Python312\python.exe              # linha no .env'
}

function Test-ApiKey {
    <# A chave só é exigida pelo framework; o sistema de jogos vive sem ela. #>
    param([string]$EnvFile)
    if ($env:TYPESAFE_API_KEY -or $env:JEV_API_KEY) { return "variável de ambiente" }
    if (Get-DotEnvValue -Path $EnvFile -Names @("TYPESAFE_API_KEY", "JEV_API_KEY")) { return ".env" }
    return $null
}

function Wait-ForHandshake {
    <# Espera /api/handshake responder. Devolve o objeto do serviço ou $null. #>
    param([string]$Url, [int]$TimeoutSeconds = 20)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            return Invoke-RestMethod -Uri "$Url/api/handshake" -TimeoutSec 3
        } catch {
            Start-Sleep -Milliseconds 400
        }
    }
    return $null
}
