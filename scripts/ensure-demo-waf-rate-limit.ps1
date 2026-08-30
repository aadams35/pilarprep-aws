[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$WebAclArn,
  [string]$Region = "us-east-1",
  [string]$Profile = "",
  [ValidateRange(10, 2000000000)]
  [int]$RateLimit = 100
)

$ErrorActionPreference = "Stop"
$ruleName = "PilarPrepPublicDemoRateLimit"

if ($WebAclArn -notmatch "^arn:aws[a-z-]*:wafv2:[^:]+:[0-9]{12}:global/webacl/[^/]+/[^/]+$") {
  throw "WebAclArn must identify a CloudFront-scope AWS WAF web ACL."
}

$parts = $WebAclArn -split "/"
$webAclName = $parts[-2]
$webAclId = $parts[-1]
$getArguments = @(
  "wafv2", "get-web-acl",
  "--scope", "CLOUDFRONT",
  "--name", $webAclName,
  "--id", $webAclId,
  "--region", $Region,
  "--output", "json",
  "--no-cli-pager"
)
if ($Profile) {
  $getArguments += @("--profile", $Profile)
}
$current = (& aws @getArguments) | ConvertFrom-Json -Depth 50
if ($LASTEXITCODE -ne 0 -or -not $current.WebACL -or -not $current.LockToken) {
  throw "The existing CloudFront Web ACL could not be read."
}

$rules = @(
  $current.WebACL.Rules |
    Where-Object { [string]$_.Name -ne $ruleName }
)
$rules += [ordered]@{
  Name = $ruleName
  Priority = 100
  Statement = [ordered]@{
    RateBasedStatement = [ordered]@{
      Limit = $RateLimit
      AggregateKeyType = "IP"
    }
  }
  Action = [ordered]@{
    Block = @{}
  }
  VisibilityConfig = [ordered]@{
    SampledRequestsEnabled = $true
    CloudWatchMetricsEnabled = $true
    MetricName = "PilarPrepPublicDemoRateLimit"
  }
}

$payload = [ordered]@{
  Name = $webAclName
  Scope = "CLOUDFRONT"
  Id = $webAclId
  DefaultAction = $current.WebACL.DefaultAction
  Rules = @($rules)
  VisibilityConfig = $current.WebACL.VisibilityConfig
  LockToken = $current.LockToken
}
$tempPath = Join-Path (
  [System.IO.Path]::GetTempPath()
) ("pillarprep-waf-" + [guid]::NewGuid().ToString("N") + ".json")

try {
  $payload |
    ConvertTo-Json -Depth 50 |
    Set-Content -LiteralPath $tempPath -Encoding utf8NoBOM
  $updateArguments = @(
    "wafv2", "update-web-acl",
    "--cli-input-json", "file://$tempPath",
    "--region", $Region,
    "--output", "json",
    "--no-cli-pager"
  )
  if ($Profile) {
    $updateArguments += @("--profile", $Profile)
  }
  & aws @updateArguments | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "The PilarPrep public-demo WAF rate rule could not be applied."
  }
}
finally {
  if (Test-Path -LiteralPath $tempPath) {
    Remove-Item -LiteralPath $tempPath -Force
  }
}

[pscustomobject]@{
  WebAclArn = $WebAclArn
  RuleName = $ruleName
  RateLimit = $RateLimit
  Action = "BLOCK"
}
