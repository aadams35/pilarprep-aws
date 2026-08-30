param(
  [string]$StackName = "pillarprep-bedrock",
  [string]$Region = "us-east-1",
  [Parameter(Mandatory = $true)]
  [ValidatePattern('^https://[^/\s]+$')]
  [string]$AllowedOrigin,
  [string]$SecondaryAllowedOrigin = "",
  [string]$BedrockModelId = "us.amazon.nova-pro-v1:0",
  [string]$BedrockAlternateModelId = "us.amazon.nova-micro-v1:0",
  [string]$BedrockFoundationModelId = "amazon.nova-pro-v1:0",
  [string]$BedrockAlternateFoundationModelId = "amazon.nova-micro-v1:0",
  [string]$BedrockPremiumModelId = "global.anthropic.claude-sonnet-4-6",
  [string]$BedrockPremiumFoundationModelId = "anthropic.claude-sonnet-4-6",
  [string]$PillarPrepApiKey = "",
  [string]$PermissionsBoundaryArn = "",
  [string]$DailyBudgetLimitUsd = "1",
  [string]$BudgetNotificationEmail = "",
  [ValidateSet("true", "false")]
  [string]$UseCustomerManagedKmsKey = "true",
  [string]$ResourcePrefix = "pillarprep-demo",
  [string]$ProjectName = "PilarPrep",
  [string]$EnvironmentName = "demo",
  [string]$Owner = "austin-adams",
  [string]$CostCenter = "hackathon"
)

$ErrorActionPreference = "Stop"

function Require-Command($Name) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    throw "$Name is required but was not found on PATH."
  }
}

function Invoke-Aws {
  & aws @args
  if ($LASTEXITCODE -ne 0) {
    throw "AWS CLI command failed. Re-run with AWS CLI debug output only if needed."
  }
}

function New-TagSetJson($Name) {
  @{
    TagSet = @(
      @{ Key = "Name"; Value = $Name },
      @{ Key = "Project"; Value = $ProjectName },
      @{ Key = "Application"; Value = "sa-briefing-generator" },
      @{ Key = "Environment"; Value = $EnvironmentName },
      @{ Key = "Owner"; Value = $Owner },
      @{ Key = "CostCenter"; Value = $CostCenter },
      @{ Key = "ManagedBy"; Value = "cloudformation" },
      @{ Key = "Repository"; Value = "aadams35/pilarprep-aws" },
      @{ Key = "DataClassification"; Value = "demo" }
    )
  } | ConvertTo-Json -Depth 5 -Compress
}

Require-Command aws

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$templatePath = Join-Path $repoRoot "infrastructure\bedrock.yaml"
$packagedPath = Join-Path $repoRoot "work\pillarprep-packaged.yaml"
$workDir = Split-Path $packagedPath -Parent
New-Item -ItemType Directory -Path $workDir -Force | Out-Null

$identityJson = Invoke-Aws sts get-caller-identity --output json | ConvertFrom-Json
$accountId = $identityJson.Account
$identityArn = $identityJson.Arn
if (-not $accountId) {
  throw "Could not determine AWS account. Run aws configure sso or aws configure first."
}
if ([string]$identityArn -match ":root$") {
  throw "Refusing to deploy PilarPrep with AWS account root credentials. Configure or assume a least-privilege IAM deployment role, then retry."
}

$bucketName = "pillarprep-deploy-$accountId-$Region".ToLowerInvariant()
Write-Host "Using AWS account $accountId in $Region"
Write-Host "Packaging bucket: $bucketName"

$bucketExists = $false
try {
  Invoke-Aws s3api head-bucket --bucket $bucketName --region $Region | Out-Null
  $bucketExists = $true
} catch {
  $bucketExists = $false
}

if (-not $bucketExists) {
  Write-Host "Creating packaging bucket..."
  Invoke-Aws s3 mb "s3://$bucketName" --region $Region | Out-Null
  Invoke-Aws s3api wait bucket-exists --bucket $bucketName --region $Region

  $publicAccessBlock = '{"BlockPublicAcls":true,"IgnorePublicAcls":true,"BlockPublicPolicy":true,"RestrictPublicBuckets":true}'
  Invoke-Aws s3api put-public-access-block `
    --bucket $bucketName `
    --public-access-block-configuration $publicAccessBlock `
    --region $Region | Out-Null
}

$packagingBucketTagsPath = Join-Path $workDir "packaging-bucket-tags.json"
[System.IO.File]::WriteAllText($packagingBucketTagsPath, (New-TagSetJson "$ResourcePrefix-cfn-package"), [System.Text.UTF8Encoding]::new($false))
Invoke-Aws s3api put-bucket-tagging `
  --bucket $bucketName `
  --tagging "file://$packagingBucketTagsPath" `
  --region $Region | Out-Null

Invoke-Aws cloudformation package `
  --template-file $templatePath `
  --s3-bucket $bucketName `
  --output-template-file $packagedPath `
  --region $Region | Out-Null

$parameterOverrides = @(
  "ResourcePrefix=$ResourcePrefix",
  "ProjectName=$ProjectName",
  "EnvironmentName=$EnvironmentName",
  "Owner=$Owner",
  "CostCenter=$CostCenter",
  "BedrockModelId=$BedrockModelId",
  "BedrockAlternateModelId=$BedrockAlternateModelId",
  "BedrockPremiumModelId=$BedrockPremiumModelId",
  "BedrockFoundationModelId=$BedrockFoundationModelId",
  "BedrockAlternateFoundationModelId=$BedrockAlternateFoundationModelId",
  "BedrockPremiumFoundationModelId=$BedrockPremiumFoundationModelId",
  "AllowedOrigin=$AllowedOrigin",
  "SecondaryAllowedOrigin=$SecondaryAllowedOrigin",
  "PillarPrepApiKey=$PillarPrepApiKey",
  "PermissionsBoundaryArn=$PermissionsBoundaryArn",
  "DailyBudgetLimitUsd=$DailyBudgetLimitUsd",
  "BudgetNotificationEmail=$BudgetNotificationEmail",
  "UseCustomerManagedKmsKey=$UseCustomerManagedKmsKey"
)

$stackTags = @(
  "Name=$StackName",
  "Project=$ProjectName",
  "Application=sa-briefing-generator",
  "Environment=$EnvironmentName",
  "Owner=$Owner",
  "CostCenter=$CostCenter",
  "ManagedBy=cloudformation",
  "Repository=aadams35/pilarprep-aws",
  "DataClassification=demo"
)

$deployArgs = @(
  "cloudformation",
  "deploy",
  "--template-file",
  $packagedPath,
  "--stack-name",
  $StackName,
  "--capabilities",
  "CAPABILITY_IAM",
  "CAPABILITY_NAMED_IAM",
  "--parameter-overrides"
) + $parameterOverrides + @(
  "--tags"
) + $stackTags + @(
  "--region",
  $Region
)

Invoke-Aws @deployArgs

Write-Host ""
Write-Host "Stack outputs:"
Invoke-Aws cloudformation describe-stacks `
  --stack-name $StackName `
  --region $Region `
  --query "Stacks[0].Outputs" `
  --output table
