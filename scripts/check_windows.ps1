param(
  [string]$EnvName = "agrisky",
  [switch]$SkipFrontendBuild
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$NodeRoot = Join-Path $Root ".codex-tools\node"
if (Test-Path (Join-Path $NodeRoot "node.exe")) {
  $env:PATH = "$NodeRoot;$env:PATH"
}

Push-Location $Root
conda run -n $EnvName python -m pytest
Pop-Location

$FrontendDir = Join-Path $Root "frontend-next"
Push-Location $FrontendDir
npm run typecheck
npm run lint
Pop-Location

if (-not $SkipFrontendBuild) {
  $TempRoot = (Resolve-Path $env:TEMP).Path
  $BuildDir = Join-Path $TempRoot ("agrisky-frontend-build-" + [guid]::NewGuid().ToString("N"))
  robocopy $FrontendDir $BuildDir /E /XD node_modules .next /NFL /NDL /NJH /NJS /NP | Out-Null
  if ($LASTEXITCODE -gt 7) {
    exit $LASTEXITCODE
  }
  try {
    Push-Location $BuildDir
    npm ci
    npm run build
  } finally {
    Pop-Location
    if ((Test-Path -LiteralPath $BuildDir) -and $BuildDir.StartsWith($TempRoot)) {
      Remove-Item -LiteralPath $BuildDir -Recurse -Force -ErrorAction SilentlyContinue
    }
  }
}

Write-Host "All checks passed."
