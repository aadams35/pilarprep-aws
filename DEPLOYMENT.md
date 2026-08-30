# Deploy to AWS

This guide deploys your own copy. Publishing this repository does not update the existing PilarPrep site. The scripts change real AWS resources and may incur charges; inspect them and your account identity before running them.

## Prerequisites

- PowerShell 7, Git, Node.js 22.13+, Python, and AWS CLI v2.
- An AWS account and an assumed deployment role. Never use root credentials.
- The supplied role template or an equivalent reviewed deployment policy. The Jobs script currently checks for the `PilarPrepHackathonDeployer` assumed role name.
- Access to the configured Bedrock models and the services used by the templates in the selected region. The examples use `us-east-1`.
- Permission to create IAM resources, private buckets, CloudFront/WAF, the queue, database, model safeguards, AgentCore resources, and meeting evidence services.

The AgentCore script installs a pinned packaging tool and prepares Python 3.12 ARM64 dependencies. Its initial packaging needs network access. No Docker build is required by that script.

## 1. Verify the Source and Identity

```powershell
npm ci
python -m pip install -r requirements-dev.txt
npx playwright install chromium
npm run verify
$env:AWS_PROFILE = "pillarprep-deployer"
aws sts get-caller-identity
```

Refresh your organization's sign-in using its configured method. Do not export credentials into the repository or browser configuration. Account identity is not proof that every required deployment permission is available.

An administrator can review [infrastructure/deployment-role.yaml](infrastructure/deployment-role.yaml) to provision the deployment role. Review trust and permission parameters for your account rather than granting access to a copied principal.

## 2. Choose the Browser Origin

Use an HTTPS origin you control, with no trailing slash. For a custom hostname, arrange the DNS record and an ACM certificate in `us-east-1`. Do not use the hosted PilarPrep demo's domain for your own deployment.

If you will use a new CloudFront default hostname, it is not known yet. Use `https://deployment-pending.invalid` as a temporary, nonfunctional origin during bootstrap, then replace it with the actual distribution URL in step 5. Do not invite users to sign in until origins and callback URLs are corrected.

```powershell
$origin = "https://deployment-pending.invalid"
```

## 3. Deploy Core Resources and AgentCore

```powershell
.\scripts\deploy-bedrock.ps1 -Region us-east-1 -AllowedOrigin $origin
.\scripts\deploy-agentcore.ps1 -Region us-east-1 -Profile $env:AWS_PROFILE -AllowedOrigin $origin
```

The Bedrock stack owns shared data resources, model permissions, Guardrails, the guest identity pool, and compatibility resources. The first AgentCore deployment creates its runtime, tools, memory, signing secret, and SDK layer. It can precede the Jobs stack; the later deployment adds the unified worker and Knowledge Base authorizations.

## 4. Deploy the Shared Jobs Pipeline and Frontend

```powershell
.\scripts\deploy-jobs-pipeline.ps1 -Region us-east-1 -Profile $env:AWS_PROFILE -AllowedOrigin $origin
.\scripts\deploy-frontend.ps1 -Region us-east-1
```

The Jobs script reads the core/AgentCore outputs, packages the API and worker, and deploys the queue, event rules, workspace identity, meeting storage, and retrieval resources. By default it also prepares synthetic evidence/audio and updates AgentCore authorization. `-SkipMeetingAssets` and `-SkipAgentCoreAuthorization` are intended for controlled redeployments, not a fully initialized first demo.

Synthetic speech generation uses Amazon Polly and is billable. The bundled MP3 is available for manual demonstration, but the preparation script also provisions the evidence needed by the AWS workflow.

The frontend script reads stack outputs, builds the static app, uploads it to private S3, and invalidates CloudFront. For a custom hostname, supply `-CustomDomainName` and `-AcmCertificateArn`; configure its DNS record to the distribution separately.

## 5. Finalize Origins

Read the frontend stack outputs and set `$origin` to your actual HTTPS distribution/custom-domain URL. Redeploy the core and Jobs stacks with that origin, skipping regeneration of unchanged meeting assets. The Jobs script refreshes AgentCore authorization and origin settings. Republish the frontend so it contains current outputs.

```powershell
aws cloudformation describe-stacks --stack-name pillarprep-frontend --region us-east-1 --query "Stacks[0].Outputs" --output table
# Set $origin to your distribution's HTTPS URL before continuing.
.\scripts\deploy-bedrock.ps1 -Region us-east-1 -AllowedOrigin $origin
.\scripts\deploy-jobs-pipeline.ps1 -Region us-east-1 -Profile $env:AWS_PROFILE -AllowedOrigin $origin -SkipMeetingAssets
.\scripts\deploy-frontend.ps1 -Region us-east-1
```

If you expose both a custom hostname and the CloudFront hostname, pass the second one as `-SecondaryAllowedOrigin` to backend deployments. Verify bucket CORS and Cognito callback/logout URLs match the origins you actually use.

## 6. Verify the Deployment

- Confirm CloudFormation operations complete successfully and CloudFront invalidation finishes.
- Open your HTTPS site in a fresh browser and generate a synthetic brief.
- Refine one tab, confirm other tabs stay unchanged, then approve and explicitly create the handoff.
- Sign in before uploading the synthetic BlueMesa audio. Confirm scan, transcription, analysis, review, and follow-on flow.
- Verify unsigned/unauthorized requests fail and private objects cannot be fetched anonymously.
- Check logs, queue age, and DLQ contents. A successful upload is not proof of completed meeting analysis.

The optional `smoke:*` commands exercise configured AWS paths and can generate model charges. Review their environment variables before using them. Older Bedrock/AgentCore smoke scripts also exercise compatibility resources, not only the shared Jobs API.

## Deployment Notes

- Templates are in [infrastructure/](infrastructure/). Physical resource names and historical `PILLARPREP_*` environment names are kept for compatibility; renaming files does not justify replacing stateful AWS resources.
- The active application uses one shared job pipeline. Earlier API/worker and table resources remain in the core templates. Plan their retirement as a separate migration.
- Stack deletion can leave retained or nonempty resources. Read [operations](docs/operations.md) before cleanup.
- This repository layout is covered by local packaging and application tests. A new account still requires permissions, quota/model access, regional availability, and a live end-to-end deployment check.
