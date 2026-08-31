# Architecture to Code

Use the same labels as the [architecture diagram](pilarprep-aws-architecture.png). Managed AWS services live in infrastructure templates; they do not need placeholder source folders pretending to implement the service.

| Diagram component | Application code | Infrastructure |
| --- | --- | --- |
| PilarPrep React app | [App](../../frontend/src/App.tsx), [browser clients](../../frontend/src/lib/) | [Frontend](../../infrastructure/frontend.yaml) |
| Route 53 | DNS lookup only; no application handler | Live `pilarprep.app` public hosted zone with A/AAAA aliases to CloudFront; DNS is configured separately from the [frontend template](../../infrastructure/frontend.yaml), as described in [deployment](../../DEPLOYMENT.md) |
| CloudFront and frontend S3 | [Browser entry](../../frontend/src/main.tsx), [build](../../vite.config.ts) | [Frontend](../../infrastructure/frontend.yaml) |
| AWS Certificate Manager | HTTPS certificate; no application handler | Existing ACM certificate supplied through `AcmCertificateArn` in the [frontend template](../../infrastructure/frontend.yaml) |
| AWS WAF | Edge protection; no separate application handler | [CloudFront Web ACL association and optional managed ACL](../../infrastructure/frontend.yaml) |
| Cognito | [Workspace authentication](../../frontend/src/lib/cognito-auth.ts), [guest signing](../../frontend/src/lib/aws-sigv4.ts), [server scope](../../backend/pipeline/state.py) | [Jobs pipeline](../../infrastructure/jobs-pipeline.yaml), [core Bedrock stack](../../infrastructure/bedrock.yaml) |
| API Gateway | [Jobs API routing](../../backend/jobs_api/handler.py) | [Jobs pipeline](../../infrastructure/jobs-pipeline.yaml) |
| Jobs API Lambda | [jobs_api/handler.py](../../backend/jobs_api/handler.py) | `JobsApiFunction` in [jobs-pipeline.yaml](../../infrastructure/jobs-pipeline.yaml) |
| Amazon SQS and DLQ | [Message handling](../../backend/ai_worker/handler.py), [job state](../../backend/pipeline/state.py) | [Queue, redrive policy, and event mapping](../../infrastructure/jobs-pipeline.yaml) |
| AI Worker Lambda | [ai_worker/handler.py](../../backend/ai_worker/handler.py) | `AiWorkerFunction` in [jobs-pipeline.yaml](../../infrastructure/jobs-pipeline.yaml) |
| Amazon Bedrock | [brief_generator.py](../../backend/bedrock/brief_generator.py) | [Model and Guardrail resources](../../infrastructure/bedrock.yaml), [worker permissions](../../infrastructure/jobs-pipeline.yaml) |
| Guardrails | [Shared safety](../../backend/shared/content_safety.py), [brief generation](../../backend/bedrock/brief_generator.py) | [Guardrail](../../infrastructure/bedrock.yaml) |
| Bedrock content-policy checks | [Context-preserving content validation](../../backend/shared/content_safety.py) | [Worker enablement and Guardrail permissions](../../infrastructure/jobs-pipeline.yaml), [runtime enablement and Guardrail permissions](../../infrastructure/agentcore.yaml) |
| AgentCore + Strands | [Runtime entry](../../backend/agentcore/runtime/main.py), [handoff/catch-up](../../backend/agentcore/runtime/service.py), [meeting analysis](../../backend/agentcore/runtime/meeting.py) | [AgentCore](../../infrastructure/agentcore.yaml) |
| AgentCore internals (not expanded in the diagram) | [Tool handler](../../backend/agentcore/tools/handler.py), [Gateway client](../../backend/agentcore/runtime/gateway.py), [memory](../../backend/agentcore/runtime/memory.py) | [Gateway, tool Lambda, Memory](../../infrastructure/agentcore.yaml) |
| Bedrock Knowledge Base / S3 Vectors | [Scoped retrieval](../../backend/agentcore/runtime/evidence.py), [evidence lifecycle](../../backend/pipeline/evidence.py) | [Knowledge Base and vector store](../../infrastructure/jobs-pipeline.yaml) |
| Validate + save | [Worker validation and completion](../../backend/ai_worker/handler.py), [review promotion](../../backend/pipeline/handoff_promotion.py), [artifact tools](../../backend/agentcore/tools/handler.py) | Runs inside existing compute; not a separate Lambda |
| DynamoDB | [State and idempotency](../../backend/pipeline/state.py) | [Application table](../../infrastructure/jobs-pipeline.yaml); compatibility resources also remain in [core stack](../../infrastructure/bedrock.yaml) |
| Private artifact S3 | [State/artifact access](../../backend/pipeline/state.py), [DOCX generation](../../backend/agentcore/tools/docx.py) | [Artifact bucket](../../infrastructure/bedrock.yaml) |
| Meeting audio private S3 | [Upload and meeting state](../../backend/pipeline/meeting.py), [upload UI](../../frontend/src/components/meeting-intelligence.tsx) | [Meeting evidence bucket](../../infrastructure/jobs-pipeline.yaml) |
| GuardDuty | [Scan-result verification](../../backend/pipeline/meeting.py), [worker continuation](../../backend/ai_worker/handler.py) | [Malware protection](../../infrastructure/jobs-pipeline.yaml) |
| EventBridge: scan result | [Scan event handler](../../backend/ai_worker/handler.py) | [GuardDuty event rule and queue policy](../../infrastructure/jobs-pipeline.yaml) |
| Amazon Transcribe | [Transcription start/result handling](../../backend/pipeline/meeting.py) | [Worker permissions](../../infrastructure/jobs-pipeline.yaml) |
| EventBridge: transcript ready | [Transcript continuation](../../backend/ai_worker/handler.py) | [Transcribe event rule](../../infrastructure/jobs-pipeline.yaml) |
| CloudWatch, X-Ray, and SNS (not expanded in the diagram) | [Job metrics](../../backend/pipeline/state.py), [worker diagnostics](../../backend/ai_worker/handler.py) | [Logs, Lambda tracing, dashboard, alarms, and notification topic](../../infrastructure/jobs-pipeline.yaml) |
| AWS KMS (not expanded in the diagram) | Encryption through AWS service configuration | [Application encryption key](../../infrastructure/bedrock.yaml), [data and queue key settings](../../infrastructure/jobs-pipeline.yaml) |
| Secrets Manager and IAM (not expanded in the diagram) | [Application scope](../../backend/pipeline/state.py), [agent scope validation](../../backend/agentcore/common/security.py) | [API-origin secret and service roles](../../infrastructure/jobs-pipeline.yaml), [scope secret and agent roles](../../infrastructure/agentcore.yaml) |

## Follow One Request

Start with `requestPipelineJob` in the React app. Follow `handler` in the Jobs API, then `handler` in the AI Worker. From there choose the Bedrock generator or AgentCore runtime. Shared job state is in `pipeline/state.py`; audio is in `pipeline/meeting.py`.

The queue and databases are AWS-managed services configured through templates, not additional Python applications. This is why their names appear under `infrastructure/` and in this map rather than as empty source folders.

## Compatibility

`backend/agentcore/compatibility/handler.py` supports the retained earlier agent API. Earlier Bedrock Lambda handlers remain in `backend/bedrock/brief_generator.py` because the core template still packages them. Neither is the browser's active asynchronous entry point. Retiring those resources is separate from reorganizing the repository.

The diagram SVG embeds its AWS icons and can be opened independently: [editable SVG](pilarprep-aws-architecture.svg).
