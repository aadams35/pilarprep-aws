param(
  [string]$StackName = "pillarprep-frontend",
  [string]$Region = "us-east-1",
  [string]$BucketName = "",
  [string]$ResourcePrefix = "pillarprep-demo",
  [string]$ProjectName = "PilarPrep",
  [string]$EnvironmentName = "demo",
  [string]$Owner = "austin-adams",
  [string]$CostCenter = "hackathon",
  [string]$WebACLId = "",
  [string]$CloudFrontPriceClass = "",
  [string]$CustomDomainName = "",
  [string]$AcmCertificateArn = "",
  [string]$BackendStackName = "pillarprep-bedrock",
  [string]$JobsStackName = "pillarprep-jobs",
  [string]$JobsApiUrl = "",
  [string]$JobsApiOriginDomainName = "",
  [string]$ApiOriginVerificationSecretArn = "",
  [string]$BackendRegion = "",
  [string]$CognitoIdentityPoolId = "",
  [string]$CognitoUserPoolClientId = "",
  [string]$CognitoLoginDomain = ""
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
    throw "AWS CLI command failed: aws $($args -join ' ')"
  }
}

function Assert-HttpsUrl($Value, $Name, [bool]$Optional = $false) {
  if ($Optional -and -not $Value) {
    return
  }
  $parsed = $null
  if (-not [Uri]::TryCreate($Value, [UriKind]::Absolute, [ref]$parsed) -or $parsed.Scheme -ne "https") {
    throw "$Name must be an absolute HTTPS URL in an AWS frontend build."
  }
}

Require-Command aws
Require-Command npm.cmd

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$templatePath = Join-Path $repoRoot "infrastructure\frontend.yaml"
$distPath = Join-Path $repoRoot "dist\aws-frontend"

$identityJson = Invoke-Aws sts get-caller-identity --output json | ConvertFrom-Json
$accountId = $identityJson.Account
$identityArn = $identityJson.Arn
if (-not $accountId) {
  throw "Could not determine AWS account. Run aws configure sso or aws configure first."
}
if ([string]$identityArn -match ":root$") {
  throw "Refusing to deploy PilarPrep with AWS account root credentials. Configure or assume a least-privilege IAM deployment role, then retry."
}

if (-not $BucketName) {
  $BucketName = "pillarprep-frontend-$accountId-$Region".ToLowerInvariant()
}

Write-Host "Using AWS account $accountId in $Region"
Write-Host "Frontend bucket: $BucketName"

if (-not $BackendRegion) {
  $BackendRegion = $Region
}

Assert-HttpsUrl $JobsApiUrl "JobsApiUrl" $true

if (-not $CognitoIdentityPoolId) {
  try {
    $backendOutputs = Invoke-Aws cloudformation describe-stacks `
      --stack-name $BackendStackName `
      --region $BackendRegion `
      --query "Stacks[0].Outputs" `
      --output json | ConvertFrom-Json

    $identityOutput = $backendOutputs | Where-Object { $_.OutputKey -eq "DemoIdentityPoolId" } | Select-Object -First 1
    $CognitoIdentityPoolId = $identityOutput.OutputValue
  } catch {
    throw "Could not read the Cognito identity pool from stack $BackendStackName."
  }
}

if (
  -not $JobsApiUrl -or
  -not $ApiOriginVerificationSecretArn -or
  -not $CognitoUserPoolClientId -or
  -not $CognitoLoginDomain
) {
  $jobsOutputs = Invoke-Aws cloudformation describe-stacks `
    --stack-name $JobsStackName `
    --region $BackendRegion `
    --query "Stacks[0].Outputs" `
    --output json | ConvertFrom-Json
  if (-not $JobsApiUrl) {
    $jobsApiOutput = $jobsOutputs | Where-Object { $_.OutputKey -eq "JobsApiUrl" } | Select-Object -First 1
    $JobsApiUrl = $jobsApiOutput.OutputValue
  }
  if (-not $ApiOriginVerificationSecretArn) {
    $originSecretOutput = $jobsOutputs | Where-Object { $_.OutputKey -eq "ApiOriginVerificationSecretArn" } | Select-Object -First 1
    $ApiOriginVerificationSecretArn = $originSecretOutput.OutputValue
  }
  if (-not $CognitoUserPoolClientId) {
    $clientOutput = $jobsOutputs | Where-Object { $_.OutputKey -eq "WorkspaceUserPoolClientId" } | Select-Object -First 1
    $CognitoUserPoolClientId = $clientOutput.OutputValue
  }
  if (-not $CognitoLoginDomain) {
    $domainOutput = $jobsOutputs | Where-Object { $_.OutputKey -eq "WorkspaceLoginDomain" } | Select-Object -First 1
    $CognitoLoginDomain = $domainOutput.OutputValue
  }
}

if (-not $CognitoIdentityPoolId) {
  throw "CognitoIdentityPoolId is required for the hosted IAM-signed Jobs API."
}
Assert-HttpsUrl $JobsApiUrl "JobsApiUrl"
Assert-HttpsUrl $CognitoLoginDomain "CognitoLoginDomain" $true
$CognitoLoginDomainName = ([Uri]$CognitoLoginDomain).Host
if (-not $CognitoLoginDomainName) {
  throw "CognitoLoginDomainName could not be derived from CognitoLoginDomain."
}
if (-not $JobsApiOriginDomainName) {
  $JobsApiOriginDomainName = ([Uri]$JobsApiUrl).Host
}
if (-not $JobsApiOriginDomainName) {
  throw "JobsApiOriginDomainName could not be derived from JobsApiUrl."
}
if (-not $ApiOriginVerificationSecretArn) {
  throw "ApiOriginVerificationSecretArn is required. Deploy the Jobs stack before publishing the frontend."
}

Write-Host "Configuring the CloudFront-protected workspace API origin..."

try {
  $existingDistributionId = Invoke-Aws cloudformation describe-stacks `
    --stack-name $StackName `
    --region $Region `
    --query "Stacks[0].Outputs[?OutputKey=='CloudFrontDistributionId'].OutputValue | [0]" `
    --output text

  if ($existingDistributionId -and $existingDistributionId -ne "None") {
    if (-not $WebACLId) {
      $existingWebAcl = Invoke-Aws cloudfront get-distribution-config `
        --id $existingDistributionId `
        --query "DistributionConfig.WebACLId" `
        --output text
      if ($existingWebAcl -and $existingWebAcl -ne "None") {
        $WebACLId = $existingWebAcl
        Write-Host "Preserving CloudFront Web ACL attachment."
      }
    }

    $existingConfig = Invoke-Aws cloudfront get-distribution-config `
      --id $existingDistributionId `
      --query "DistributionConfig" `
      --output json | ConvertFrom-Json
    if (-not $CustomDomainName -and $existingConfig.Aliases.Items.Count -gt 0) {
      $CustomDomainName = [string]$existingConfig.Aliases.Items[0]
    }
    if (-not $AcmCertificateArn -and $existingConfig.ViewerCertificate.ACMCertificateArn) {
      $AcmCertificateArn = [string]$existingConfig.ViewerCertificate.ACMCertificateArn
    }
  }
} catch {
  Write-Host "No existing CloudFront distribution detected."
}

if ($WebACLId) {
  $wafArguments = @{
    WebAclArn = $WebACLId
    Region = "us-east-1"
    RateLimit = 100
  }
  if ($env:AWS_PROFILE) {
    $wafArguments.Profile = $env:AWS_PROFILE
  }
  & (Join-Path $PSScriptRoot "ensure-demo-waf-rate-limit.ps1") @wafArguments
}


$parameterOverrides = @(
  "ResourcePrefix=$ResourcePrefix",
  "ProjectName=$ProjectName",
  "EnvironmentName=$EnvironmentName",
  "Owner=$Owner",
  "CostCenter=$CostCenter",
  "FrontendBucketName=$BucketName",
  "WebACLId=$WebACLId",
  "CloudFrontPriceClass=$CloudFrontPriceClass",
  "CustomDomainName=$CustomDomainName",
  "AcmCertificateArn=$AcmCertificateArn",
  "JobsApiOriginDomainName=$JobsApiOriginDomainName",
  "CognitoLoginDomainName=$CognitoLoginDomainName",
  "ApiOriginVerificationSecretArn=$ApiOriginVerificationSecretArn"
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
  $templatePath,
  "--stack-name",
  $StackName,
  "--parameter-overrides"
) + $parameterOverrides + @(
  "--tags"
) + $stackTags + @(
  "--region",
  $Region
)

Invoke-Aws @deployArgs

$outputs = Invoke-Aws cloudformation describe-stacks `
  --stack-name $StackName `
  --region $Region `
  --query "Stacks[0].Outputs" `
  --output json | ConvertFrom-Json

$bucketOutput = $outputs | Where-Object { $_.OutputKey -eq "FrontendBucketName" } | Select-Object -First 1
$distributionOutput = $outputs | Where-Object { $_.OutputKey -eq "CloudFrontDistributionId" } | Select-Object -First 1
$urlOutput = $outputs | Where-Object { $_.OutputKey -eq "FrontendUrl" } | Select-Object -First 1
$workspaceApiOutput = $outputs | Where-Object { $_.OutputKey -eq "WorkspaceApiUrl" } | Select-Object -First 1

if (-not $bucketOutput.OutputValue -or -not $distributionOutput.OutputValue -or -not $workspaceApiOutput.OutputValue) {
  throw "CloudFormation outputs did not include the frontend bucket, distribution ID, and workspace API URL."
}

$bucket = $bucketOutput.OutputValue
$distributionId = $distributionOutput.OutputValue
$url = $urlOutput.OutputValue
$workspaceApiUrl = $workspaceApiOutput.OutputValue
Assert-HttpsUrl $workspaceApiUrl "WorkspaceApiUrl"

Write-Host "Building static frontend with separate guest and authenticated API routes..."
Push-Location $repoRoot
try {
  $env:VITE_PILLARPREP_JOBS_API_URL = $JobsApiUrl
  $env:VITE_PILLARPREP_WORKSPACE_API_URL = $workspaceApiUrl
  $env:VITE_PILLARPREP_BACKEND_REGION = $BackendRegion
  $env:VITE_PILLARPREP_COGNITO_IDENTITY_POOL_ID = $CognitoIdentityPoolId
  $env:VITE_PILLARPREP_COGNITO_USER_POOL_CLIENT_ID = $CognitoUserPoolClientId
  $env:VITE_PILLARPREP_COGNITO_LOGIN_DOMAIN = $CognitoLoginDomain
  npm.cmd run build
  if ($LASTEXITCODE -ne 0) {
    throw "Frontend build failed."
  }
} finally {
  Pop-Location
}


Invoke-Aws s3 sync $distPath "s3://$bucket" `
  --delete `
  --exclude "index.html" `
  --cache-control "public,max-age=31536000,immutable" `
  --region $Region

Invoke-Aws s3 cp (Join-Path $distPath "index.html") "s3://$bucket/index.html" `
  --cache-control "no-cache,no-store,must-revalidate" `
  --content-type "text/html" `
  --region $Region

Invoke-Aws cloudfront create-invalidation `
  --distribution-id $distributionId `
  --paths "/*" | Out-Null

Write-Host ""
Write-Host "Frontend deployed: $url"
Write-Host "CloudFront distribution: $distributionId"
Write-Host "S3 bucket: $bucket"
Write-Host "Authenticated workspace API: $workspaceApiUrl"
