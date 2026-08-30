# Infrastructure

These templates configure the AWS services shown in the [architecture diagram](../docs/architecture/pilarprep-aws-architecture.png). Application handlers live under `backend/`; managed services are represented here, not by empty source folders.

| Template | Responsibility | Deployment script |
| --- | --- | --- |
| [deployment-role.yaml](deployment-role.yaml) | Reviewed deployment-role trust and permissions | Administrator bootstrap |
| [bedrock.yaml](bedrock.yaml) | Shared data/key resources, guest identity, Guardrails, model permissions, and retained compatibility API | [deploy-bedrock.ps1](../scripts/deploy-bedrock.ps1) |
| [agentcore.yaml](agentcore.yaml) | Runtime, Gateway, tool Lambda, Memory, signing, SDK layer, and compatibility routing | [deploy-agentcore.ps1](../scripts/deploy-agentcore.ps1) |
| [jobs-pipeline.yaml](jobs-pipeline.yaml) | Jobs API, AI Worker, queue/DLQ, active state, identity, audio security, events, and Knowledge Base | [deploy-jobs-pipeline.ps1](../scripts/deploy-jobs-pipeline.ps1) |
| [frontend.yaml](frontend.yaml) | CloudFront, private frontend S3, response headers, WAF, and workspace API origin | [deploy-frontend.ps1](../scripts/deploy-frontend.ps1) |

Initial order is core Bedrock resources, AgentCore bootstrap, shared Jobs pipeline, then frontend. The Jobs script updates AgentCore with the newly created worker and retrieval permissions. Finalize origins after CloudFront assigns a new hostname.

See [DEPLOYMENT.md](../DEPLOYMENT.md) for the full sequence. Review change sets and stateful-resource replacement before applying infrastructure changes.
