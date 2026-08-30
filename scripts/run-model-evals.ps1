param(
  [switch]$Setup,
  [switch]$Live,
  [switch]$List,
  [string[]]$Models = @("nova-pro"),
  [string]$Judge = "nova-pro",
  [string[]]$Case = @(),
  [string[]]$Tag = @(),
  [string[]]$Candidate = @(),
  [int]$Limit = 3,
  [int]$Repeats = 1,
  [int]$MaxCalls = 24,
  [int]$MaxTokens = 4800,
  [int]$TimeoutSeconds = 180,
  [string]$Profile = "pillarprep-deployer",
  [string]$Region = "us-east-1"
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$environment = Join-Path $root "work\model-eval-venv"
$python = Join-Path $environment "Scripts\python.exe"
Push-Location -LiteralPath $root
try {
  if ($Setup) {
    if (-not (Test-Path -LiteralPath $python)) {
      & python -m venv $environment
      if ($LASTEXITCODE -ne 0) { throw "Python 3.10+ is required to create the evaluation environment." }
    }
    & $python -m pip install --disable-pip-version-check -r evals/requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Evaluation dependency installation failed." }
  }
  if (-not (Test-Path -LiteralPath $python)) {
    throw "Run .\scripts\run-model-evals.ps1 -Setup first. Setup does not invoke models."
  }
  $arguments = @("-m", "evals.model_eval", "--models") + $Models + @(
    "--judge", $Judge, "--limit", "$Limit", "--repeats", "$Repeats",
    "--max-calls", "$MaxCalls", "--max-tokens", "$MaxTokens",
    "--timeout", "$TimeoutSeconds", "--profile", $Profile, "--region", $Region
  )
  if ($Live) { $arguments += "--live" }
  if ($List) { $arguments += "--list" }
  foreach ($value in $Case) { $arguments += @("--case", $value) }
  foreach ($value in $Tag) { $arguments += @("--tag", $value) }
  foreach ($value in $Candidate) { $arguments += @("--candidate", $value) }
  & $python @arguments
  $result = $LASTEXITCODE
} finally {
  Pop-Location
}
exit $result
