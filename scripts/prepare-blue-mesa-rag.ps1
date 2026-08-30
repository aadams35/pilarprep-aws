[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [string]$EvidenceBucket,
  [Parameter(Mandatory = $true)]
  [string]$KnowledgeBaseId,
  [Parameter(Mandatory = $true)]
  [string]$DataSourceId,
  [string]$Profile = "pillarprep-deployer",
  [string]$Region = "us-east-1",
  [switch]$SkipAudio
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $repoRoot "data\blue-mesa-meeting-script.json"
$corpusPath = Join-Path $repoRoot "data\blue-mesa-evidence"
$workPath = Join-Path $repoRoot "work\blue-mesa-meeting-audio"
$outputPath = Join-Path $workPath "blue-mesa-discovery.mp3"
$audioKey = "audio/public-demo/blue-mesa-payments/blue-mesa-discovery.mp3"
$evidencePrefix = "evidence/public-demo/blue-mesa-payments/"

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
  throw "AWS CLI v2 is required."
}
if (-not (Test-Path -LiteralPath $scriptPath)) {
  throw "Synthetic meeting script was not found at $scriptPath"
}
if (-not (Test-Path -LiteralPath $corpusPath)) {
  throw "Blue Mesa evidence corpus was not found at $corpusPath"
}
New-Item -ItemType Directory -Force -Path $workPath | Out-Null

if (-not $SkipAudio) {
  $meeting = Get-Content -Raw -LiteralPath $scriptPath | ConvertFrom-Json
  $voiceArguments = @(
    "polly", "describe-voices",
    "--profile", $Profile,
    "--region", $Region,
    "--output", "json"
  )
  $voices = (& aws @voiceArguments) | ConvertFrom-Json
  if ($LASTEXITCODE -ne 0 -or -not $voices.Voices) {
    throw "Amazon Polly voices could not be discovered."
  }
  $voiceEngines = @{}
  foreach ($voice in $voices.Voices) {
    $voiceEngines[[string]$voice.Id] = @($voice.SupportedEngines)
  }
  $segmentFiles = [System.Collections.Generic.List[string]]::new()
  $index = 0
  foreach ($segment in $meeting.segments) {
    $index += 1
    $voiceId = [string]$segment.voiceId
    $supportedEngines = @($voiceEngines[$voiceId])
    if (-not $supportedEngines.Count) {
      throw "Amazon Polly voice $voiceId is unavailable in $Region."
    }
    $engine = if ($supportedEngines -contains "neural") {
      "neural"
    } elseif ($supportedEngines -contains "standard") {
      "standard"
    } else {
      [string]$supportedEngines[0]
    }
    $segmentPath = Join-Path $workPath ("segment-{0:D2}.mp3" -f $index)
    $escaped = [System.Security.SecurityElement]::Escape([string]$segment.text)
    $ssml = "<speak><prosody rate='95%'>$escaped</prosody><break time='650ms'/></speak>"
    $arguments = @(
      "polly", "synthesize-speech",
      "--profile", $Profile,
      "--region", $Region,
      "--output-format", "mp3",
      "--engine", $engine,
      "--voice-id", $voiceId,
      "--text-type", "ssml",
      "--text", $ssml,
      $segmentPath
    )
    & aws @arguments | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $segmentPath)) {
      throw "Amazon Polly failed while creating segment $index."
    }
    $segmentFiles.Add($segmentPath)
  }

  $output = [System.IO.File]::Open(
    $outputPath,
    [System.IO.FileMode]::Create,
    [System.IO.FileAccess]::Write
  )
  try {
    foreach ($segmentPath in $segmentFiles) {
      $bytes = [System.IO.File]::ReadAllBytes($segmentPath)
      $output.Write($bytes, 0, $bytes.Length)
    }
  }
  finally {
    $output.Dispose()
  }
  if ((Get-Item -LiteralPath $outputPath).Length -lt 100000) {
    throw "Generated MP3 is unexpectedly small."
  }

  $uploadArguments = @(
    "s3", "cp", $outputPath, "s3://$EvidenceBucket/$audioKey",
    "--profile", $Profile,
    "--region", $Region,
    "--sse", "AES256",
    "--content-type", "audio/mpeg",
    "--metadata", "scenario-id=blue-mesa-payments,synthetic=true"
  )
  & aws @uploadArguments
  if ($LASTEXITCODE -ne 0) {
    throw "Synthetic MP3 upload failed."
  }
}

$syncArguments = @(
  "s3", "sync", $corpusPath, "s3://$EvidenceBucket/$evidencePrefix",
  "--profile", $Profile,
  "--region", $Region,
  "--sse", "AES256",
  "--delete"
)
& aws @syncArguments
if ($LASTEXITCODE -ne 0) {
  throw "Blue Mesa evidence synchronization failed."
}

$ingestionArguments = @(
  "bedrock-agent", "start-ingestion-job",
  "--profile", $Profile,
  "--region", $Region,
  "--knowledge-base-id", $KnowledgeBaseId,
  "--data-source-id", $DataSourceId,
  "--description", "PilarPrep approved Blue Mesa synthetic evidence",
  "--output", "json"
)
$ingestion = (& aws @ingestionArguments) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) {
  throw "Knowledge Base ingestion could not be started."
}

$ingestionJobId = $ingestion.ingestionJob.ingestionJobId
$ingestionStatus = "STARTING"
for ($attempt = 0; $attempt -lt 36; $attempt += 1) {
  Start-Sleep -Seconds 10
  $statusArguments = @(
    "bedrock-agent", "get-ingestion-job",
    "--profile", $Profile,
    "--region", $Region,
    "--knowledge-base-id", $KnowledgeBaseId,
    "--data-source-id", $DataSourceId,
    "--ingestion-job-id", $ingestionJobId,
    "--output", "json"
  )
  $status = (& aws @statusArguments) | ConvertFrom-Json
  if ($LASTEXITCODE -ne 0) {
    throw "Knowledge Base ingestion status could not be read."
  }
  $ingestionStatus = [string]$status.ingestionJob.status
  if ($ingestionStatus -eq "COMPLETE") {
    break
  }
  if ($ingestionStatus -eq "FAILED") {
    throw "Knowledge Base ingestion failed."
  }
}
if ($ingestionStatus -ne "COMPLETE") {
  throw "Knowledge Base ingestion did not complete within six minutes."
}

[pscustomobject]@{
  EvidenceBucket = $EvidenceBucket
  AudioKey = $audioKey
  KnowledgeBaseId = $KnowledgeBaseId
  DataSourceId = $DataSourceId
  IngestionJobId = $ingestionJobId
  IngestionStatus = $ingestionStatus
  AudioGenerated = -not $SkipAudio
} | Format-List
