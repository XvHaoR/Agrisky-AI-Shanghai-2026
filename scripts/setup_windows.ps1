param(
  [string]$EnvName = "agrisky",
  [string]$NodeVersion = "v22.22.3"
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$NodeRoot = Join-Path $Root ".codex-tools\node"

function Add-LocalNodeToPath {
  if (Test-Path (Join-Path $NodeRoot "node.exe")) {
    $env:PATH = "$NodeRoot;$env:PATH"
  }
}

if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
  throw "conda is required. Install Anaconda/Miniconda or add conda to PATH."
}

$envList = conda env list | Out-String
if ($envList -notmatch "(^|\s)$EnvName(\s|$)") {
  conda create -n $EnvName python=3.12 -y
}

conda run -n $EnvName python -m pip install --upgrade pip
conda run -n $EnvName python -m pip install -r (Join-Path $Root "requirements.txt") -r (Join-Path $Root "requirements-dev.txt")

if (-not (Get-Command node -ErrorAction SilentlyContinue) -and -not (Test-Path (Join-Path $NodeRoot "node.exe"))) {
  $ToolsDir = Join-Path $Root ".codex-tools"
  $ZipPath = Join-Path $ToolsDir "node-$NodeVersion-win-x64.zip"
  $ExtractedDir = Join-Path $ToolsDir "node-$NodeVersion-win-x64"
  New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
  curl.exe -L -o $ZipPath "https://nodejs.org/dist/$NodeVersion/node-$NodeVersion-win-x64.zip"
  Expand-Archive -LiteralPath $ZipPath -DestinationPath $ToolsDir -Force
  if (Test-Path $NodeRoot) {
    Remove-Item -LiteralPath $NodeRoot -Recurse -Force
  }
  Move-Item -LiteralPath $ExtractedDir -Destination $NodeRoot
}

Add-LocalNodeToPath
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
  throw "npm is unavailable after Node setup."
}

if (-not (Test-Path (Join-Path $Root ".env"))) {
  Copy-Item -LiteralPath (Join-Path $Root ".env.example") -Destination (Join-Path $Root ".env")
}

$FrontendDir = Join-Path $Root "frontend-next"
if (-not (Test-Path (Join-Path $FrontendDir ".env.local"))) {
  Copy-Item -LiteralPath (Join-Path $FrontendDir ".env.example") -Destination (Join-Path $FrontendDir ".env.local")
}

Push-Location $FrontendDir
npm install
Pop-Location

Write-Host "Agrisky AI environment is ready."
Write-Host "Run: powershell -ExecutionPolicy Bypass -File scripts\dev_windows.ps1"
