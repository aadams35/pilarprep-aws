param(
  [string]$StackName = "pillarprep-jobs",
  [string]$BackendStackName = "pillarprep-bedrock",
  [string]$AgentStackName = "pillarprep-agentcore",
  [string]$Region = "us-east-1",
  [string]$Profile = "pillarprep-deployer",
  [Parameter(Mandatory = $true)]
  [string]$AllowedOrigin,
  [string]$SecondaryAllowedOrigin = "",
  [string]$ResourcePrefix = "pillarprep-demo",
  [string]$ProjectName = "PilarPrep",
  [string]$EnvironmentName = "demo",
  [string]$Owner = "austin-adams",
  [string]$CostCenter = "hackathon",
  [string]$PackagingBucket = "",
  [string]$MeetingEvidenceBucketName = "",
  [string]$EvidenceVectorBucketName = "",
  [string]$PermissionsBoundaryArn = "",
  [string]$DemoAllowedClientIds = "apex-mutual,bluemesa-payments,northstar-health,peakcart-retail,custom-demo",
  [string]$BedrockModelId = "us.amazon.nova-pro-v1:0",
  [string]$BedrockAlternateModelId = "us.amazon.nova-micro-v1:0",
  [string]$BedrockFoundationModelId = "amazon.nova-pro-v1:0",
  [string]$BedrockAlternateFoundationModelId = "amazon.nova-micro-v1:0",
  [string]$BedrockPremiumModelId = "global.anthropic.claude-sonnet-4-6",
  [string]$BedrockPremiumFoundationModelId = "anthropic.claude-sonnet-4-6",
  [ValidateSet("true", "false")]
  [string]$LiveAiEnabled = "true",
  [ValidateRange(1, 100)]
  [int]$GuestHourlyAiLimit = 12,
  [ValidateRange(1, 500)]
  [int]$GuestDailyAiLimit = 30,
  [ValidateRange(1, 2000)]
  [int]$AuthUserDailyAiLimit = 100,
  [ValidateRange(1, 10000)]
  [int]$AuthTenantDailyAiLimit = 500,
  [ValidateRange(1, 100)]
  [int]$ClaudeDailyAiLimit = 5,
  [ValidateRange(2, 50)]
  [int]$WorkerMaximumConcurrency = 2,
  [string]$DataKmsKeyArn = "",
  [string]$KnowledgeBaseGeneration = "v2",
  [string]$OperationsAlertEmail = "",
  [ValidateRange(1, 2)]
  [int]$MaxReplayCount = 1,
  [switch]$SkipMeetingAssets,
  [switch]$SkipAgentCoreAuthorization
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "resource-names.ps1")

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
    throw "Stack output $Key was not found."
  }
  return $match.OutputValue
}

function Assert-HttpsUrl($Value, $Name, [bool]$Optional = $false) {
  if ($Optional -and -not $Value) {
    return
  }
  $parsed = $null
  if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$parsed) -or $parsed.Scheme -ne "https") {
    throw "$Name must be an absolute HTTPS URL for deployed PilarPrep resources."
  }
}

Require-Command aws

Assert-HttpsUrl $AllowedOrigin "AllowedOrigin"
Assert-HttpsUrl $SecondaryAllowedOrigin "SecondaryAllowedOrigin" $true

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$templatePath = Join-Path $repoRoot "infrastructure\jobs-pipeline.yaml"
$workRoot = Join-Path $repoRoot "work\jobs-pipeline"
$packagedPath = Join-Path $workRoot "pillarprep-jobs-packaged.yaml"
New-Item -ItemType Directory -Path $workRoot -Force | Out-Null

$identity = Invoke-Aws sts get-caller-identity --output json | ConvertFrom-Json
if (-not $identity.Account) {
  throw "Could not determine the AWS account. Refresh the deployment role first."
}
if ([string]$identity.Arn -match ":root$") {
  throw "Refusing to deploy PilarPrep with AWS account root credentials."
}
if ([string]$identity.Arn -notmatch "assumed-role/PilarPrepHackathonDeployer/") {
  throw "Use the PilarPrepHackathonDeployer role for this deployment."
}

$accountId = $identity.Account
if (-not $PackagingBucket) {
  $PackagingBucket = Get-PilarPrepStorageName -Purpose "deployments" -AccountId $accountId -Region $Region -EnvironmentName $EnvironmentName
}
if (-not $PSBoundParameters.ContainsKey("MeetingEvidenceBucketName")) {
  $defaultMeetingName = Get-PilarPrepStorageName -Purpose "meeting-evidence" -AccountId $accountId -Region $Region -EnvironmentName $EnvironmentName
  $MeetingEvidenceBucketName = Resolve-PilarPrepBucketParameter -StackName $StackName -ParameterName "MeetingEvidenceBucketName" -DefaultName $defaultMeetingName -Region $Region -Profile $Profile
}
if (-not $PSBoundParameters.ContainsKey("EvidenceVectorBucketName")) {
  $defaultVectorName = Get-PilarPrepStorageName -Purpose "evidence-vectors" -AccountId $accountId -Region $Region -EnvironmentName $EnvironmentName
  $EvidenceVectorBucketName = Resolve-PilarPrepBucketParameter -StackName $StackName -ParameterName "EvidenceVectorBucketName" -DefaultName $defaultVectorName -Region $Region -Profile $Profile
}
if (-not $PSBoundParameters.ContainsKey("KnowledgeBaseGeneration")) {
  $KnowledgeBaseGeneration = Resolve-PilarPrepKnowledgeBaseGeneration -StackName $StackName -Region $Region -Profile $Profile -DefaultGeneration $KnowledgeBaseGeneration
}

if (-not $PSBoundParameters.ContainsKey("WorkerMaximumConcurrency")) {
  $existingCapacity = Resolve-PilarPrepBucketParameter -StackName $StackName -ParameterName "WorkerMaximumConcurrency" -DefaultName "2" -Region $Region -Profile $Profile
  if ($existingCapacity) { $WorkerMaximumConcurrency = [int]$existingCapacity }
}
$lambdaLimits = Invoke-Aws lambda get-account-settings --region $Region --query "AccountLimit" --output json | ConvertFrom-Json
# Budget a tool invocation per busy worker and four executions for the API and other functions.
$requiredCapacity = (2 * $WorkerMaximumConcurrency) + 4
if ($null -eq $lambdaLimits.UnreservedConcurrentExecutions -or [int]$lambdaLimits.UnreservedConcurrentExecutions -lt $requiredCapacity) {
  throw "WorkerMaximumConcurrency=$WorkerMaximumConcurrency needs at least $requiredCapacity unreserved Lambda executions under the PilarPrep capacity plan. Request a quota increase or reduce the worker limit."
}
Write-Host "Worker limit: $WorkerMaximumConcurrency; account unreserved capacity: $($lambdaLimits.UnreservedConcurrentExecutions)"

$backendOutputs = Invoke-Aws cloudformation describe-stacks `
  --stack-name $BackendStackName `
  --region $Region `
  --query "Stacks[0].Outputs" `
  --output json | ConvertFrom-Json
$agentOutputs = Invoke-Aws cloudformation describe-stacks `
  --stack-name $AgentStackName `
  --region $Region `
  --query "Stacks[0].Outputs" `
  --output json | ConvertFrom-Json

$artifactBucket = Stack-Output $backendOutputs "ArtifactBucketName"
$projectTable = Stack-Output $backendOutputs "ProjectStateTableName"
$demoRole = Stack-Output $backendOutputs "DemoInvokeRoleName"
$guardrailId = Stack-Output $backendOutputs "BedrockGuardrailId"
$guardrailVersion = Stack-Output $backendOutputs "BedrockGuardrailVersion"
$agentRuntimeArn = Stack-Output $agentOutputs "AgentRuntimeArn"
$dataKeyOutput = $backendOutputs |
  Where-Object { $_.OutputKey -eq "DataEncryptionKeyArn" } |
  Select-Object -First 1
if (-not $DataKmsKeyArn -and $dataKeyOutput.OutputValue) {
  $DataKmsKeyArn = $dataKeyOutput.OutputValue
}

$lambdaLayerArn = Stack-Output $agentOutputs "AgentLambdaSdkLayerArn"
$scopeSecretOutput = $agentOutputs | Where-Object { $_.OutputKey -eq "ScopeSigningSecretArn" } | Select-Object -First 1
if ($scopeSecretOutput.OutputValue) {
  $scopeSecretArn = $scopeSecretOutput.OutputValue
} else {
  $scopeSecretArn = Invoke-Aws cloudformation describe-stack-resource `
    --stack-name $AgentStackName `
    --logical-resource-id ScopeSigningSecret `
    --region $Region `
    --query "StackResourceDetail.PhysicalResourceId" `
    --output text
}

Write-Host "Using $($identity.Arn)"
Write-Host "Deploying unified pipeline stack $StackName in $Region"
Write-Host "Reusing one table: $projectTable"
Write-Host "Reusing one artifact bucket: $artifactBucket"

Invoke-Aws cloudformation package `
  --template-file $templatePath `
  --s3-bucket $PackagingBucket `
  --s3-prefix jobs-pipeline `
  --output-template-file $packagedPath `
  --region $Region | Out-Null

$templateObjectKey = "jobs-pipeline/templates/$StackName-packaged.yaml"
Invoke-Aws s3 cp `
  $packagedPath `
  "s3://$PackagingBucket/$templateObjectKey" `
  --region $Region | Out-Null

$templateUrl = "https://${PackagingBucket}.s3.${Region}.amazonaws.com/$templateObjectKey"
Invoke-Aws cloudformation validate-template `
  --template-url $templateUrl `
  --region $Region | Out-Null

$parameterOverrides = @(
  "MeetingEvidenceBucketName=$MeetingEvidenceBucketName",
  "EvidenceVectorBucketName=$EvidenceVectorBucketName",
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
  "AgentRuntimeArn=$agentRuntimeArn",
  "ScopeSecretArn=$scopeSecretArn",
  "LambdaSdkLayerArn=$lambdaLayerArn",
  "BedrockModelId=$BedrockModelId",
  "BedrockAlternateModelId=$BedrockAlternateModelId",
  "BedrockPremiumModelId=$BedrockPremiumModelId",
  "BedrockFoundationModelId=$BedrockFoundationModelId",
  "BedrockAlternateFoundationModelId=$BedrockAlternateFoundationModelId",
  "BedrockPremiumFoundationModelId=$BedrockPremiumFoundationModelId",
  "BedrockGuardrailId=$guardrailId",
  "BedrockGuardrailVersion=$guardrailVersion",
  "DemoAllowedClientIds=$DemoAllowedClientIds",
  "LiveAiEnabled=$LiveAiEnabled",
  "GuestHourlyAiLimit=$GuestHourlyAiLimit",
  "GuestDailyAiLimit=$GuestDailyAiLimit",
  "AuthUserDailyAiLimit=$AuthUserDailyAiLimit",
  "AuthTenantDailyAiLimit=$AuthTenantDailyAiLimit",
  "ClaudeDailyAiLimit=$ClaudeDailyAiLimit",
  "WorkerMaximumConcurrency=$WorkerMaximumConcurrency",
  "DataKmsKeyArn=$DataKmsKeyArn",
  "KnowledgeBaseGeneration=$KnowledgeBaseGeneration",
  "OperationsAlertEmail=$OperationsAlertEmail",
  "MaxReplayCount=$MaxReplayCount",
  "PermissionsBoundaryArn=$PermissionsBoundaryArn"
)

Invoke-Aws cloudformation deploy `
  --template-file $packagedPath `
  --s3-bucket $PackagingBucket `
  --s3-prefix jobs-pipeline/templates `
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

$pipelineOutputs = Invoke-Aws cloudformation describe-stacks `
  --stack-name $StackName `
  --region $Region `
  --query "Stacks[0].Outputs" `
  --output json | ConvertFrom-Json
$workerRoleArn = Stack-Output $pipelineOutputs "AiWorkerRoleArn"
$evidenceBucket = Stack-Output $pipelineOutputs "MeetingEvidenceBucketName"
$knowledgeBaseId = Stack-Output $pipelineOutputs "KnowledgeBaseId"
$knowledgeBaseArn = Stack-Output $pipelineOutputs "KnowledgeBaseArn"
$dataSourceId = Stack-Output $pipelineOutputs "KnowledgeBaseDataSourceId"

$meetingCors = Invoke-Aws s3api get-bucket-cors `
  --bucket $evidenceBucket `
  --region $Region `
  --output json | ConvertFrom-Json
$meetingCorsOrigins = @(
  $meetingCors.CORSRules |
    ForEach-Object { $_.AllowedOrigins } |
    Where-Object { $_ }
)
if (-not ($meetingCorsOrigins -contains $AllowedOrigin)) {
  throw "Meeting audio bucket CORS is missing the primary application origin: $AllowedOrigin"
}
if (
  $SecondaryAllowedOrigin -and
  -not ($meetingCorsOrigins -contains $SecondaryAllowedOrigin)
) {
  throw "Meeting audio bucket CORS is missing the secondary application origin: $SecondaryAllowedOrigin"
}
Write-Host "Verified private meeting audio upload CORS for $($meetingCorsOrigins -join ', ')"

if (-not $SkipMeetingAssets) {
  $prepareArguments = @{
    EvidenceBucket = $evidenceBucket
    KnowledgeBaseId = $knowledgeBaseId
    DataSourceId = $dataSourceId
    Profile = $Profile
    Region = $Region
  }
  & (Join-Path $PSScriptRoot "prepare-blue-mesa-rag.ps1") @prepareArguments
}

if (-not $SkipAgentCoreAuthorization) {
  $previousProfile = $env:AWS_PROFILE
  try {
    $env:AWS_PROFILE = $Profile
    & (Join-Path $PSScriptRoot "deploy-agentcore.ps1") `
      -StackName $AgentStackName `
      -BackendStackName $BackendStackName `
      -JobsStackName $StackName `
      -Region $Region `
      -Profile $Profile `
      -PackagingBucket $PackagingBucket `
      -AllowedOrigin $AllowedOrigin `
      -SecondaryAllowedOrigin $SecondaryAllowedOrigin `
      -UnifiedWorkerRoleArn $workerRoleArn -KnowledgeBaseId $knowledgeBaseId -KnowledgeBaseArn $knowledgeBaseArn
    if ($LASTEXITCODE -ne 0) {
      throw "AgentCore authorization update failed. The legacy path remains available."
    }
  } finally {
    $env:AWS_PROFILE = $previousProfile
  }
}

Write-Host ""
Write-Host "Unified pipeline outputs:"
$pipelineOutputs | Format-Table OutputKey, OutputValue -AutoSize
