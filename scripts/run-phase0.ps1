[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SourceCae,

    [Parameter(Mandatory = $true)]
    [string]$RuntimeRoot,

    [int]$RequestedCpus = 18
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$PythonExe = Join-Path $RepositoryRoot ".venv\Scripts\python.exe"
$BridgeConfig = Join-Path $RepositoryRoot "config\local\abaqus_bridge.json"
$BootstrapManifest = Join-Path $RepositoryRoot "config\local\source-manifest.json"
$ReportRoot = Join-Path $RuntimeRoot "reports"

foreach ($Required in @($PythonExe, $BridgeConfig, $BootstrapManifest, $SourceCae)) {
    if (-not (Test-Path -LiteralPath $Required -PathType Leaf)) {
        throw "Required file not found: $Required"
    }
}
if (-not (Test-Path -LiteralPath $ReportRoot -PathType Container)) {
    throw "Report directory not found; run bootstrap first: $ReportRoot"
}

$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Phase0Report = Join-Path $ReportRoot "phase0-$Stamp.json"
$ResourceReport = Join-Path $ReportRoot "resources-$Stamp.json"
$ReadinessReport = Join-Path $ReportRoot "readiness-$Stamp.json"

& $PythonExe (Join-Path $RepositoryRoot "scripts\reproduce_phase0.py") --bridge-config $BridgeConfig --source-cae $SourceCae --output $Phase0Report
if ($LASTEXITCODE -ne 0) {
    throw "Phase 0 reproduction failed"
}

& $PythonExe (Join-Path $RepositoryRoot "scripts\resource_preflight.py") --runtime-root $RuntimeRoot --phase0-report $Phase0Report --requested-cpus $RequestedCpus --output $ResourceReport
if ($LASTEXITCODE -notin @(0, 20)) {
    throw "Resource preflight failed unexpectedly"
}

& $PythonExe (Join-Path $RepositoryRoot "scripts\evaluate_readiness.py") --bootstrap-manifest $BootstrapManifest --phase0-report $Phase0Report --resource-report $ResourceReport --output $ReadinessReport
if ($LASTEXITCODE -notin @(0, 20)) {
    throw "Readiness evaluation failed unexpectedly"
}

Write-Host "Phase 0 report: $Phase0Report"
Write-Host "Resource report: $ResourceReport"
Write-Host "Readiness report: $ReadinessReport"
Write-Host "No Data Check or solver job was submitted."
