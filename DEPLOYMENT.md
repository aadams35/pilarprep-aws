# Deploy PilarPrep to AWS

This guide creates a new PilarPrep environment from the repository. The scripts deploy real AWS resources and can incur charges, so use a reviewed deployment role and confirm the account before every run.

## Before You Start

You need:

- PowerShell 7, Git, Node.js 22.13+, Python 3.12, and AWS CLI v2.
- An AWS account with access to the required Bedrock models in your chosen region.
- An assumed deployment role with permission to create the services in [infrastructure](infrastructure/). Do not deploy as the root user.
- An HTTPS origin for browser and Cognito configuration. The examples use `us-east-1`.

The AgentCore deployment downloads pinned Python packaging dependencies the first time it runs. Docker is not required.

## 1. Check the Repository and AWS Identity

Install the local tools and run the offline verification suite:

```powershell
npm ci
python -m pip install -r requirements-dev.txt
npx playwright install chromium
npm run verify
```

Then select your AWS profile and confirm the account:

```powershell
$env:AWS_PROFILE = "pillarprep-deployer"
aws sts get-caller-identity --profile $env:AWS_PROFILE
```

Refresh the profile with the sign-in method already configured by your organization. Never place exported credentials in this repository or in frontend environment variables.

An administrator can use [infrastructure/deployment-role.yaml](infrastructure/deployment-role.yaml) as a starting point for the deployment role. Review its trust policy and account-specific parameters before applying it.

## 2. Choose the Allowed Browser Origin

Use an HTTPS URL with no trailing slash:

```powershell
$origin = "https://deployment-pending.invalid"
```

The placeholder is useful during the first deployment because the CloudFront hostname does not exist yet. It is deliberately nonfunctional. Replace it with the real CloudFront or custom-domain URL before inviting anyone to sign in.

For a custom hostname, create or supply:

- A public DNS record.
- An ACM certificate in `us-east-1`.
- The matching Cognito callback and logout URLs.

## 3. Deploy the AWS Stacks

The first deployment follows this order:

```powershell
.\scripts\deploy-bedrock.ps1 -Region us-east-1 -AllowedOrigin $origin
.\scripts\deploy-agentcore.ps1 -Region us-east-1 -Profile $env:AWS_PROFILE -AllowedOrigin $origin
.\scripts\deploy-jobs-pipeline.ps1 -Region us-east-1 -Profile $env:AWS_PROFILE -AllowedOrigin $origin
.\scripts\deploy-frontend.ps1 -Region us-east-1
```

Why this order:

1. The Bedrock stack creates shared storage, encryption, Guardrails, model permissions, and guest identity resources.
2. The AgentCore stack creates the runtime, governed tools, memory, signing secret, and runtime layer.
3. The Jobs stack connects the API, queue, worker, application state, audio workflow, and Knowledge Base.
4. The frontend stack builds the React app, uploads it to private S3, and creates or updates CloudFront.

The Jobs deployment prepares the fictional evidence and meeting assets used by the demo. Amazon Polly and live model calls can incur charges. Use `-SkipMeetingAssets` only for a controlled redeployment after the initial setup.

## 4. Replace the Temporary Origin

Read the frontend outputs:

```powershell
aws cloudformation describe-stacks `
  --stack-name pillarprep-frontend `
  --region us-east-1 `
  --query "Stacks[0].Outputs" `
  --output table
```

Set `$origin` to the actual HTTPS URL, then refresh the backend origin settings and rebuild the frontend:

```powershell
$origin = "https://YOUR-CLOUDFRONT-OR-CUSTOM-DOMAIN"
.\scripts\deploy-bedrock.ps1 -Region us-east-1 -AllowedOrigin $origin
.\scripts\deploy-jobs-pipeline.ps1 -Region us-east-1 -Profile $env:AWS_PROFILE -AllowedOrigin $origin -SkipMeetingAssets
.\scripts\deploy-frontend.ps1 -Region us-east-1
```

If both the custom hostname and CloudFront hostname will be used, pass the second URL as `-SecondaryAllowedOrigin` where supported. Check S3 CORS and Cognito callback/logout settings against the exact origins.

## 5. Walk the Deployed Flow

Use a fresh browser and synthetic data:

1. Open the HTTPS site and generate a brief.
2. Refine one tab and confirm the others do not change.
3. Approve the current packet and create the pre-call handoff.
4. Open **Catch-up** and generate a role-aware summary from the latest approved packet.
5. Sign in, upload the synthetic BlueMesa recording, and follow it through scanning, transcription, review, and follow-up.
6. Download a DOCX and confirm private S3 objects are not anonymously accessible.
7. Review CloudWatch logs, queue age, and the DLQ before calling the deployment healthy.

The optional `smoke:*` commands use configured AWS resources and can invoke paid models. Read each script before running it. The normal `npm run verify` suite is offline.

## Existing Environments and Storage

New S3 names follow:

`pilarprep-<environment>-<purpose>-<account>-<region>`

Purpose labels include `web-assets`, `artifacts`, `meeting-evidence`, `deployments`, and `evidence-vectors`. CloudFront uses a display name such as `pilarprep-<environment>-web`.

An S3 bucket cannot be renamed in place. Changing a name parameter on an environment with data can replace the bucket with an empty one. Treat any storage rename as a planned migration with inventory, copy verification, rollback, and a quiet cutover window. The helper in `scripts/migrate_resource_names.py` exists for the original PilarPrep migration; read it before using it against another environment.

Historical stack names and `PILLARPREP_*` environment variables remain for compatibility. Cosmetic cleanup is not a reason to replace stateful AWS resources.

## Updating an Existing Deployment

CloudFormation parameters are sticky, but deployment scripts can still update more than the one setting you care about. Review the full command and resulting change set before applying it.

The active frontend uses the shared Jobs API. Older compatibility handlers remain in the core templates so existing environments can be upgraded safely. Retire them only through a separately tested infrastructure migration.

## Cleanup

Deleting a stack does not guarantee that every object disappears. Versioned buckets, retained resources, KMS keys, and deployment artifacts may remain and continue to cost money.

Before teardown:

1. Inspect each stack's deletion and retention policies.
2. Export only data you are authorized to keep.
3. Confirm bucket names and object ownership before any cleanup.
4. Delete retained resources separately only after the stacks and dependencies are understood.

See [operations](docs/operations.md) for troubleshooting, monitoring, and safe cleanup.
