# AWS Infrastructure

This folder contains the CloudFormation and SAM templates behind the [architecture diagram](../docs/architecture/pilarprep-aws-architecture.png). Application behavior lives in `backend/`; this folder describes the AWS services, permissions, and connections that run it.

| Template | What it creates | Deployment script |
| --- | --- | --- |
| [deployment-role.yaml](deployment-role.yaml) | The reviewed role used to deploy PilarPrep | Administrator bootstrap |
| [bedrock.yaml](bedrock.yaml) | Shared storage and encryption, guest identity, Bedrock Guardrails, model permissions, and compatibility resources | [deploy-bedrock.ps1](../scripts/deploy-bedrock.ps1) |
| [agentcore.yaml](agentcore.yaml) | AgentCore Runtime, Strands tools, Memory, request signing, and compatibility routing | [deploy-agentcore.ps1](../scripts/deploy-agentcore.ps1) |
| [jobs-pipeline.yaml](jobs-pipeline.yaml) | Jobs API, SQS and DLQ, AI Worker, application state, audio processing, events, and the Knowledge Base | [deploy-jobs-pipeline.ps1](../scripts/deploy-jobs-pipeline.ps1) |
| [frontend.yaml](frontend.yaml) | CloudFront, private frontend S3, response headers, WAF, and the API origin | [deploy-frontend.ps1](../scripts/deploy-frontend.ps1) |

A new environment is deployed in this order: Bedrock resources, AgentCore, the shared jobs pipeline, then the frontend. The Jobs deployment connects AgentCore to the worker and evidence permissions. After CloudFront provides the final hostname, run the origin-configuration step described in [DEPLOYMENT.md](../DEPLOYMENT.md).

Review the CloudFormation change set before every update, especially when a change could replace a bucket, table, key, or other stateful resource.
