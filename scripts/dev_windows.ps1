param(
  [string]$EnvName = "agrisky",
  [int]$ApiPort = 8000,
  [int]$WebPort = 3000
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$NodeRoot = Join-Path $Root ".codex-tools\node"
$NodePathPrefix = if (Test-Path (Join-Path $NodeRoot "node.exe")) { "$NodeRoot;" } else { "" }

if (-not (Test-Path (Join-Path $Root ".env"))) {
  Copy-Item -LiteralPath (Join-Path $Root ".env.example") -Destination (Join-Path $Root ".env")
}

$FrontendDir = Join-Path $Root "frontend-next"
if (-not (Test-Path (Join-Path $FrontendDir ".env.local"))) {
  Copy-Item -LiteralPath (Join-Path $FrontendDir ".env.example") -Destination (Join-Path $FrontendDir ".env.local")
}

$ApiCommand = "Set-Location '$Root\api_gateway'; conda run -n $EnvName python -m uvicorn main:app --reload --host 127.0.0.1 --port $ApiPort"
$WebCommand = "`$env:PATH='$NodePathPrefix' + `$env:PATH; Set-Location '$FrontendDir'; npm run dev -- --hostname 127.0.0.1 --port $WebPort"

Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $ApiCommand)
Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $WebCommand)

Write-Host "API: http://127.0.0.1:$ApiPort"
Write-Host "Web: http://127.0.0.1:$WebPort"
Write-Host "Claims console: http://127.0.0.1:$WebPort/claims"
