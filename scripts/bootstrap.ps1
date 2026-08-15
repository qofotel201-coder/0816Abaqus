[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,

    [Parameter(Mandatory = $true)]
    [string]$RuntimeRoot,

    [Parameter(Mandatory = $true)]
    [string]$SourceCae,

    [Parameter(Mandatory = $true)]
    [string]$AbaqusCommand,

    [Parameter(Mandatory = $true)]
    [string]$Subroutine,

    [string]$SourceArchive
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$VenvRoot = Join-Path $RepositoryRoot ".venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python executable not found: $PythonExe"
}
if (Test-Path -LiteralPath $VenvRoot) {
    throw "Refusing to overwrite existing virtual environment: $VenvRoot"
}

& $PythonExe -m venv $VenvRoot
if ($LASTEXITCODE -ne 0) {
    throw "Virtual environment creation failed"
}
& $VenvPython -m pip install --disable-pip-version-check -r (Join-Path $RepositoryRoot "requirements-dev.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed"
}

$BootstrapArguments = @(
    (Join-Path $RepositoryRoot "scripts\bootstrap.py"),
    "--runtime-root", $RuntimeRoot,
    "--source-cae", $SourceCae,
    "--abaqus-command", $AbaqusCommand,
    "--subroutine", $Subroutine
)
if ($SourceArchive) {
    $BootstrapArguments += @("--source-archive", $SourceArchive)
}
& $VenvPython @BootstrapArguments
if ($LASTEXITCODE -ne 0) {
    throw "Portable configuration bootstrap failed"
}

$env:PYTHONDONTWRITEBYTECODE = "1"
Push-Location $RepositoryRoot
try {
    & $VenvPython -B -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        throw "Unit or contract tests failed"
    }
    & $VenvPython -B scripts\verify_public_tree.py
    if ($LASTEXITCODE -ne 0) {
        throw "Public-tree safety check failed"
    }
}
finally {
    Pop-Location
}

Write-Host "Bootstrap complete. No Abaqus solver was started."
