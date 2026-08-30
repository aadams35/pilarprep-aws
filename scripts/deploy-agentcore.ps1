param(
  [string]$StackName = "pillarprep-agentcore",
  [string]$BackendStackName = "pillarprep-bedrock",
  [string]$JobsStackName = "pillarprep-jobs",
  [string]$Region = "us-east-1",
  [string]$Profile = "pillarprep-deployer",
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^https://[^/\s]+$')]
  [string]$AllowedOrigin,
  [string]$SecondaryAllowedOrigin = "",
  [string]$ResourcePrefix = "pillarprep-demo",
  [string]$ProjectName = "PilarPrep",
  [string]$EnvironmentName = "demo",
  [string]$Owner = "austin-adams",
  [string]$CostCenter = "hackathon",
  [string]$PackagingBucket = "",
  [string]$PermissionsBoundaryArn = "",
  [string]$DemoAllowedClientIds = "apex-mutual,bluemesa-payments,northstar-health,peakcart-retail,custom-demo",
  [string]$DemoLegacyClientId = "bluemesa-payments",
  [string]$BedrockModelId = "us.amazon.nova-pro-v1:0",
  [string]$BedrockAlternateModelId = "us.amazon.nova-micro-v1:0",
  [string]$BedrockFoundationModelId = "amazon.nova-pro-v1:0",
  [string]$BedrockAlternateFoundationModelId = "amazon.nova-micro-v1:0",
  [string]$BedrockPremiumModelId = "global.anthropic.claude-sonnet-4-6",
  [string]$BedrockPremiumFoundationModelId = "anthropic.claude-sonnet-4-6",
  [string]$UnifiedWorkerRoleArn = "",
  [string]$KnowledgeBaseId = "",
  [string]$KnowledgeBaseArn = "",
  [string]$DataKmsKeyArn = "",
  [string]$UvVersion = "0.11.32",
  [string]$Boto3Version = "1.43.54",
  [string]$BedrockGuardrailId = "",
  [string]$BedrockGuardrailVersion = "",
  [ValidateSet("true", "false")]
  [string]$AllowLegacyDemoBrief = "true"
)

$ErrorActionPreference = "Stop"

function Require-Command($Name) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    throw "$Name is required but was not found on PATH."
  }
}

function Invoke-Aws {
  $commandArgs = @($args)
  if ($Profile) {
    $commandArgs += @("--profile", $Profile)
  }
  & aws @commandArgs
  if ($LASTEXITCODE -ne 0) {
    throw "AWS CLI command failed: aws $($commandArgs -join ' ')"
  }
}

function Stack-Output($Outputs, $Key) {
  $match = $Outputs | Where-Object { $_.OutputKey -eq $Key } | Select-Object -First 1
  if (-not $match.OutputValue) {
    throw "Backend stack output $Key was not found. Deploy the current backend template first."
  }
  return $match.OutputValue
}

function Optional-Stack-Output($Outputs, $Key) {
  $match = $Outputs | Where-Object { $_.OutputKey -eq $Key } | Select-Object -First 1
  if ($match.OutputValue) {
    return [string]$match.OutputValue
  }
  return ""
}

Require-Command aws
Require-Command python

$profileArgs = @()
if ($Profile) {
  $profileArgs = @("--profile", $Profile)
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$agentRoot = Join-Path $repoRoot "backend\agentcore"
$uvToolRoot = Join-Path $repoRoot "work\tooling\uv-$UvVersion"
$uvExe = Join-Path $uvToolRoot "Scripts\uv.exe"
$uvPython = Join-Path $uvToolRoot "Scripts\python.exe"
$templatePath = Join-Path $repoRoot "infrastructure\agentcore.yaml"
$workRoot = Join-Path $repoRoot "work\agentcore"
$runtimeRoot = Join-Path $workRoot "runtime-package"
$packagedTemplate = Join-Path $workRoot "pillarprep-agentcore-packaged.yaml"
$runtimeZip = Join-Path $workRoot "pillarprep-agentcore-runtime.zip"
$lambdaLayerPackageRoot = Join-Path $workRoot "lambda-layer"
$lambdaLayerRoot = Join-Path $lambdaLayerPackageRoot "python"
$lambdaLayerZip = Join-Path $workRoot "pillarprep-agentcore-lambda-sdk-layer.zip"

if (-not (Test-Path -LiteralPath $uvExe)) {
  Write-Host "Bootstrapping pinned uv $UvVersion for reproducible ARM64 packaging..."
  New-Item -ItemType Directory -Path (Split-Path $uvToolRoot -Parent) -Force | Out-Null
  & python -m venv $uvToolRoot
  if ($LASTEXITCODE -ne 0) {
    throw "Could not create the local uv tooling environment."
  }
  & $uvPython -m pip install --disable-pip-version-check --no-input "uv==$UvVersion"
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $uvExe)) {
    throw "Could not install pinned uv $UvVersion."
  }
}

$identity = Invoke-Aws sts get-caller-identity --output json | ConvertFrom-Json
$accountId = $identity.Account
$identityArn = $identity.Arn
if (-not $accountId) {
  throw "Could not determine AWS account. Refresh your AWS credentials first."
}
if ([string]$identityArn -match ":root$") {
  throw "Refusing to deploy PilarPrep with AWS account root credentials. Configure or assume a least-privilege IAM deployment role, then retry."
}

if (-not $PackagingBucket) {
  $PackagingBucket = "pillarprep-deploy-$accountId-$Region".ToLowerInvariant()
}

$backendOutputs = Invoke-Aws cloudformation describe-stacks `
  --stack-name $BackendStackName `
  --region $Region `
  --query "Stacks[0].Outputs" `
  --output json | ConvertFrom-Json

$artifactBucket = Stack-Output $backendOutputs "ArtifactBucketName"
$projectTable = Stack-Output $backendOutputs "ProjectStateTableName"
$demoRole = Stack-Output $backendOutputs "DemoInvokeRoleName"
$fallbackFunctionArn = Stack-Output $backendOutputs "BriefFunctionArn"
if (-not $DataKmsKeyArn) {
  $DataKmsKeyArn = Optional-Stack-Output $backendOutputs "DataEncryptionKeyArn"
}
if (-not $BedrockGuardrailId) {
  $BedrockGuardrailId = Stack-Output $backendOutputs "BedrockGuardrailId"
}
if (-not $BedrockGuardrailVersion) {
  $BedrockGuardrailVersion = Stack-Output $backendOutputs "BedrockGuardrailVersion"
}

if (
  -not $UnifiedWorkerRoleArn -or
  -not $KnowledgeBaseId -or
  -not $KnowledgeBaseArn
) {
  $jobsOutputJson = & aws cloudformation describe-stacks --stack-name $JobsStackName --region $Region --query "Stacks[0].Outputs" --output json @profileArgs 2>$null
  if ($LASTEXITCODE -eq 0 -and $jobsOutputJson) {
    $jobsOutputs = $jobsOutputJson | ConvertFrom-Json
    if (-not $UnifiedWorkerRoleArn) {
      $UnifiedWorkerRoleArn = Optional-Stack-Output $jobsOutputs "AiWorkerRoleArn"
    }
    if (-not $KnowledgeBaseId) {
      $KnowledgeBaseId = Optional-Stack-Output $jobsOutputs "KnowledgeBaseId"
      if (-not $KnowledgeBaseId) {
        $KnowledgeBaseId = Optional-Stack-Output $jobsOutputs "BlueMesaKnowledgeBaseId"
      }
    }
    if (-not $KnowledgeBaseArn) {
      $KnowledgeBaseArn = Optional-Stack-Output $jobsOutputs "KnowledgeBaseArn"
      if (-not $KnowledgeBaseArn) {
        $KnowledgeBaseArn = Optional-Stack-Output $jobsOutputs "BlueMesaKnowledgeBaseArn"
      }
    }
  }
}

Write-Host "Using AWS account $accountId in $Region"
Write-Host "AgentCore stack: $StackName"
Write-Host "Reusing artifact bucket: $artifactBucket"
Write-Host "Reusing project table: $projectTable"
if ($KnowledgeBaseArn) {
  Write-Host "Authorizing metadata-scoped PilarPrep Knowledge Base: $KnowledgeBaseId"
}

& aws s3api head-bucket --bucket $PackagingBucket @profileArgs 2>$null
if ($LASTEXITCODE -ne 0) {
  if ($Region -eq "us-east-1") {
    Invoke-Aws s3api create-bucket --bucket $PackagingBucket --region $Region | Out-Null
  } else {
    Invoke-Aws s3api create-bucket --bucket $PackagingBucket --region $Region `
      --create-bucket-configuration "LocationConstraint=$Region" | Out-Null
  }
  Invoke-Aws s3api put-public-access-block --bucket $PackagingBucket `
    --public-access-block-configuration `
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" | Out-Null
}

$resolvedWorkRoot = [System.IO.Path]::GetFullPath($workRoot)
$resolvedRepoRoot = [System.IO.Path]::GetFullPath($repoRoot)
if (-not $resolvedWorkRoot.StartsWith($resolvedRepoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
  throw "Refusing to clean a work directory outside the repository."
}
if (Test-Path -LiteralPath $workRoot) {
  Remove-Item -LiteralPath $workRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $runtimeRoot | Out-Null
New-Item -ItemType Directory -Path (Join-Path $runtimeRoot "runtime") | Out-Null
New-Item -ItemType Directory -Path $lambdaLayerRoot | Out-Null

Copy-Item -LiteralPath (Join-Path $agentRoot "runtime\main.py") -Destination (Join-Path $runtimeRoot "main.py")
Copy-Item -LiteralPath (Join-Path $agentRoot "runtime\__init__.py") -Destination (Join-Path $runtimeRoot "runtime\__init__.py")
Copy-Item -LiteralPath (Join-Path $agentRoot "runtime\service.py") -Destination (Join-Path $runtimeRoot "runtime\service.py")
Copy-Item -LiteralPath (Join-Path $agentRoot "runtime\gateway.py") -Destination (Join-Path $runtimeRoot "runtime\gateway.py")
Copy-Item -LiteralPath (Join-Path $agentRoot "runtime\memory.py") -Destination (Join-Path $runtimeRoot "runtime\memory.py")
Copy-Item -LiteralPath (Join-Path $agentRoot "runtime\meeting.py") -Destination (Join-Path $runtimeRoot "runtime\meeting.py")
Copy-Item -LiteralPath (Join-Path $agentRoot "runtime\evidence.py") -Destination (Join-Path $runtimeRoot "runtime\evidence.py")
New-Item -ItemType Directory -Path (Join-Path $runtimeRoot "common") | Out-Null
Copy-Item -LiteralPath (Join-Path $agentRoot "common\__init__.py") -Destination (Join-Path $runtimeRoot "common\__init__.py")
Copy-Item -LiteralPath (Join-Path $agentRoot "common\contracts.py") -Destination (Join-Path $runtimeRoot "common\contracts.py")
Copy-Item -LiteralPath (Join-Path $agentRoot "common\identifiers.py") -Destination (Join-Path $runtimeRoot "common\identifiers.py")
Copy-Item -LiteralPath (Join-Path $agentRoot "common\security.py") -Destination (Join-Path $runtimeRoot "common\security.py")
New-Item -ItemType Directory -Path (Join-Path $runtimeRoot "shared") | Out-Null
Copy-Item -LiteralPath (Join-Path $repoRoot "backend\shared\__init__.py") -Destination (Join-Path $runtimeRoot "shared\__init__.py")
Copy-Item -LiteralPath (Join-Path $repoRoot "backend\shared\content_safety.py") -Destination (Join-Path $runtimeRoot "shared\content_safety.py")

& $uvExe pip install `
  --python-platform aarch64-manylinux2014 `
  --python-version 3.12 `
  --target $runtimeRoot `
  --only-binary=:all: `
  -r (Join-Path $agentRoot "runtime\requirements.txt")
if ($LASTEXITCODE -ne 0) {
  throw "AgentCore ARM64 dependency packaging failed."
}

$lambdaSdkArgs = @(
  "pip", "install",
  "--python-platform", "aarch64-manylinux2014",
  "--python-version", "3.12",
  "--target", $lambdaLayerRoot,
  "--only-binary=:all:",
  "boto3==$Boto3Version"
)
& $uvExe @lambdaSdkArgs
if ($LASTEXITCODE -ne 0) {
  throw "Pinned Boto3 Lambda layer packaging failed."
}

Compress-Archive -Path (Join-Path $runtimeRoot "*") -DestinationPath $runtimeZip -CompressionLevel Optimal
Compress-Archive -Path (Join-Path $lambdaLayerPackageRoot "*") -DestinationPath $lambdaLayerZip -CompressionLevel Optimal
$artifactStamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$runtimeKey = "agentcore/runtime/$artifactStamp-runtime.zip"
$lambdaLayerKey = "agentcore/layers/$artifactStamp-boto3-$Boto3Version.zip"
Invoke-Aws s3 cp $runtimeZip "s3://$PackagingBucket/$runtimeKey" --region $Region | Out-Null
Invoke-Aws s3 cp $lambdaLayerZip "s3://$PackagingBucket/$lambdaLayerKey" --region $Region | Out-Null

Invoke-Aws cloudformation package `
  --template-file $templatePath `
  --s3-bucket $PackagingBucket `
  --s3-prefix agentcore/lambda `
  --output-template-file $packagedTemplate `
  --region $Region | Out-Null

$parameterOverrides = @(
  "ResourcePrefix=$ResourcePrefix",
  "ProjectName=$ProjectName",
  "EnvironmentName=$EnvironmentName",
  "Owner=$Owner",
  "CostCenter=$CostCenter",
  "AllowedOrigin=$AllowedOrigin",
  "SecondaryAllowedOrigin=$SecondaryAllowedOrigin",
  "ArtifactBucketName=$artifactBucket",
  "ProjectStateTableName=$projectTable",
  "DemoInvokeRoleName=$demoRole",
  "FallbackFunctionArn=$fallbackFunctionArn",
  "RuntimeCodeBucket=$PackagingBucket",
  "RuntimeCodeKey=$runtimeKey",
  "LambdaDependencyBucket=$PackagingBucket",
  "LambdaDependencyKey=$lambdaLayerKey",
  "BedrockModelId=$BedrockModelId",
  "BedrockAlternateModelId=$BedrockAlternateModelId",
  "BedrockPremiumModelId=$BedrockPremiumModelId",
  "BedrockFoundationModelId=$BedrockFoundationModelId",
  "BedrockAlternateFoundationModelId=$BedrockAlternateFoundationModelId",
  "BedrockPremiumFoundationModelId=$BedrockPremiumFoundationModelId",
  "BedrockGuardrailId=$BedrockGuardrailId",
  "BedrockGuardrailVersion=$BedrockGuardrailVersion",
  "DemoAllowedClientIds=$DemoAllowedClientIds",
  "DemoLegacyClientId=$DemoLegacyClientId",
  "AllowLegacyDemoBrief=$AllowLegacyDemoBrief",
  "UnifiedWorkerRoleArn=$UnifiedWorkerRoleArn",
  "KnowledgeBaseId=$KnowledgeBaseId",
  "KnowledgeBaseArn=$KnowledgeBaseArn",
  "DataKmsKeyArn=$DataKmsKeyArn",
  "PermissionsBoundaryArn=$PermissionsBoundaryArn"
)

Invoke-Aws cloudformation deploy `
  --template-file $packagedTemplate `
  --stack-name $StackName `
  --parameter-overrides $parameterOverrides `
  --capabilities CAPABILITY_NAMED_IAM `
  --no-fail-on-empty-changeset `
  --tags `
  "Name=$StackName" `
  "Project=$ProjectName" `
  "Application=sa-briefing-generator" `
  "Environment=$EnvironmentName" `
  "Owner=$Owner" `
  "CostCenter=$CostCenter" `
  "ManagedBy=cloudformation" `
  "Repository=aadams35/pilarprep-aws" `
  "DataClassification=demo" `
  --region $Region

$outputs = Invoke-Aws cloudformation describe-stacks `
  --stack-name $StackName `
  --region $Region `
  --query "Stacks[0].Outputs" `
  --output table

Write-Host ""
Write-Host $outputs
Write-Host "AgentCore deployment completed. Publish the frontend afterward to inject AgentApiUrl."
