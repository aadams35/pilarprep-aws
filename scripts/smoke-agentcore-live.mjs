import { Sha256 } from "@aws-crypto/sha256-js";
import { HttpRequest } from "@smithy/protocol-http";
import { SignatureV4 } from "@smithy/signature-v4";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";

const region = process.env.AWS_REGION ?? "us-east-1";
const backendStack = process.env.PILLARPREP_BACKEND_STACK ?? "pillarprep-bedrock";
const agentStack = process.env.PILLARPREP_AGENT_STACK ?? "pillarprep-agentcore";

function awsJson(args) {
  const output = execFileSync("aws", [...args, "--region", region, "--output", "json"], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
  return JSON.parse(output);
}

function stackOutputs(stackName) {
  const outputs = awsJson([
    "cloudformation", "describe-stacks", "--stack-name", stackName,
    "--query", "Stacks[0].Outputs",
  ]);
  return Object.fromEntries(outputs.map((item) => [item.OutputKey, item.OutputValue]));
}

async function assertCors(url, origin, label) {
  const response = await fetch(url, {
    method: "OPTIONS",
    headers: {
      origin,
      "access-control-request-method": "POST",
      "access-control-request-headers": "authorization,content-type,x-amz-date,x-amz-security-token",
    },
  });
  if (response.status !== 204 || response.headers.get("access-control-allow-origin") !== origin) {
    throw new Error(`${label} does not allow the public PilarPrep origin.`);
  }
}

async function postCognitoIdentity(target, payload) {
  const response = await fetch(`https://cognito-identity.${region}.amazonaws.com/`, {
    method: "POST",
    headers: {
      "content-type": "application/x-amz-json-1.1",
      "x-amz-target": `AWSCognitoIdentityService.${target}`,
    },
    body: JSON.stringify(payload),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`Cognito ${target} failed with HTTP ${response.status}: ${text}`);
  return JSON.parse(text);
}

async function cognitoCredentials(identityPoolId) {
  const identity = await postCognitoIdentity("GetId", { IdentityPoolId: identityPoolId });
  const result = await postCognitoIdentity("GetCredentialsForIdentity", { IdentityId: identity.IdentityId });
  const values = result.Credentials;
  if (!values?.AccessKeyId || !values.SecretKey) throw new Error("Cognito did not return usable demo credentials.");
  return { accessKeyId: values.AccessKeyId, secretAccessKey: values.SecretKey, sessionToken: values.SessionToken };
}

async function signedPostJson(url, payload, credentials) {
  const endpoint = new URL(url);
  const body = JSON.stringify(payload);
  const signer = new SignatureV4({ credentials, region, service: "execute-api", sha256: Sha256 });
  const request = new HttpRequest({
    protocol: endpoint.protocol,
    hostname: endpoint.hostname,
    method: "POST",
    path: endpoint.pathname,
    query: Object.fromEntries(endpoint.searchParams.entries()),
    headers: { accept: "application/json", "content-type": "application/json", host: endpoint.host },
    body,
  });
  const signed = await signer.sign(request);
  const headers = { ...signed.headers };
  delete headers.host;
  return fetch(url, { method: "POST", headers, body });
}

const sleep = (milliseconds) =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));

const agentJobStats = [];

async function postAndRead(url, payload, credentials, label) {
  const startedAt = Date.now();
  let response = await signedPostJson(url, payload, credentials);
  let text = await response.text();
  if (!response.ok) throw new Error(`${label} returned HTTP ${response.status}: ${text}`);

  let result = JSON.parse(text);
  if (response.status !== 202) {
    agentJobStats.push({ label, mode: "synchronous", polls: 0, durationMs: Date.now() - startedAt });
    return result;
  }
  if (
    !result.jobId ||
    result.clientId !== payload.clientId ||
    result.projectId !== payload.projectId
  ) {
    throw new Error(`${label} did not return a usable scoped AgentCore job.`);
  }

  const jobId = result.jobId;
  let polls = 0;
  let remainingWaitMs = 600_000;
  while (remainingWaitMs > 0) {
    const waitMs = Math.max(750, Math.min(result.pollAfterMs ?? 1500, 5000));
    await sleep(waitMs);
    remainingWaitMs -= waitMs;
    polls += 1;
    response = await signedPostJson(
      url,
      {
        operation: "getAgentJob",
        jobId,
        clientId: payload.clientId,
        projectId: payload.projectId,
        sessionId: payload.sessionId,
      },
      credentials,
    );
    text = await response.text();
    if (!response.ok) throw new Error(`${label} poll returned HTTP ${response.status}: ${text}`);
    result = JSON.parse(text);
    if (response.status !== 202) {
      agentJobStats.push({ label, mode: "async", polls, durationMs: Date.now() - startedAt });
      return result;
    }
  }

  throw new Error(`${label} did not complete within ten minutes.`);
}

const backend = stackOutputs(backendStack);
const agent = stackOutputs(agentStack);
await assertCors(backend.BriefApiUrl, "https://pilarprep.app", "Brief API CORS");
await assertCors(agent.AgentApiUrl, "https://pilarprep.app", "Agent API CORS");
const credentials = await cognitoCredentials(backend.DemoIdentityPoolId);
const sessionId = `session-smoke-${randomUUID()}`;

const unsignedResponse = await fetch(agent.AgentApiUrl, {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ action: "generate_catchup", clientId: "bluemesa-payments", projectId: "bluemesa-payments" }),
});
if (unsignedResponse.status !== 403) throw new Error(`Unsigned Agent API request returned HTTP ${unsignedResponse.status}, expected 403.`);

const crossClientResponse = await signedPostJson(agent.AgentApiUrl, {
  action: "generate_catchup",
  clientId: "outside-demo",
  projectId: "outside-demo",
  audienceRole: "New member",
  focus: "Attempt cross-client catch-up",
  sessionId: `cross-client-${randomUUID()}`,
  modelPreference: "nova-pro",
  confirmWrite: false,
  idempotencyKey: `cross-client-${randomUUID()}`,
  approvedBrief: { probe: "contract-valid unauthorized-client request" },
  briefRequest: {},
}, credentials);
if (crossClientResponse.status !== 403) throw new Error(`Cross-client Agent API request returned HTTP ${crossClientResponse.status}, expected 403.`);

const latestState = awsJson([
  "dynamodb", "get-item",
  "--table-name", backend.ProjectStateTableName,
  "--key", JSON.stringify({
    projectId: { S: "TENANT#demo|CLIENT#bluemesa-payments|PROJECT#bluemesa-payments" },
    sortKey: { S: "BRIEF#LATEST" },
  }),
  "--consistent-read",
]);
const approvedVersion = Number(latestState.Item?.approvedPacketVersion?.N ?? 0);
const approvedKey = latestState.Item?.approvedArtifactKey?.S ?? "";
const projectPrefix = "tenants/demo/clients/bluemesa-payments/projects/bluemesa-payments";
const allowedKeys = new Set([
  `${projectPrefix}/brief/latest.json`,
  `${projectPrefix}/brief/approved/v${String(approvedVersion).padStart(6, "0")}/packet.json`,
]);
if (!approvedVersion || !allowedKeys.has(approvedKey)) {
  throw new Error("AgentCore smoke could not resolve a scoped approved Blue Mesa packet.");
}
const storedPacket = JSON.parse(execFileSync(
  "aws",
  [
    "s3", "cp",
    `s3://${backend.ArtifactBucketName}/${approvedKey}`,
    "-", "--region", region, "--no-progress",
  ],
  { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
));
const brief = storedPacket.response ?? storedPacket;
if (!brief || brief.metadata?.approvalStatus !== "approved") {
  throw new Error("AgentCore smoke requires the server's latest approved Blue Mesa packet.");
}
const approvedBrief = Object.fromEntries(
  [
    "businessCase",
    "technical",
    "executive",
    "stakeholders",
    "gameplan",
    "objections",
    "projectAnswer",
    "projectArtifacts",
    "citations",
    "evidence",
  ].map((key) => [key, brief[key]])
);
const briefRequest = {
  mode: "project",
  meetingNotes: "Use the latest approved customer context and project evidence.",
  approvedBrief,
};

const handoff = await postAndRead(agent.AgentApiUrl, {
  action: "create_handoff",
  clientId: "bluemesa-payments",
  projectId: "bluemesa-payments",
  sessionId,
  audienceRole: "PM",
  focus: "Create the first two-week plan.",
  meetingNotes: briefRequest.meetingNotes,
  modelPreference: "nova-pro",
  confirmWrite: true,
  idempotencyKey: `handoff-${randomUUID()}`,
  approvedBrief,
  briefRequest: { ...briefRequest, mode: "project", approvedBrief },
}, credentials, "AgentCore handoff");

const catchup = await postAndRead(agent.AgentApiUrl, {
  action: "generate_catchup",
  clientId: "bluemesa-payments",
  projectId: "bluemesa-payments",
  sessionId,
  audienceRole: "New member",
  focus: "Where should I start?",
  meetingNotes: briefRequest.meetingNotes,
  modelPreference: "nova-pro",
  confirmWrite: false,
  idempotencyKey: `catchup-${randomUUID()}`,
  approvedBrief,
  briefRequest: { ...briefRequest, mode: "project", role: "New member", prompt: "Where should I start?", approvedBrief },
}, credentials, "AgentCore catch-up");

for (const [label, result] of [["handoff", handoff], ["catchup", catchup]]) {
  if (result.provider !== "agentcore" || result.metadata?.fallbackUsed) throw new Error(`${label} did not complete through AgentCore.`);
  if (!result.metadata?.memoryUsed || !result.metadata?.gatewayUsed) throw new Error(`${label} did not report AgentCore Memory and Gateway usage.`);
  if (!result.projectAnswer || !Array.isArray(result.citations) || !result.citations.length) throw new Error(`${label} was missing grounded project output.`);
  if (result.businessCase?.scenario !== brief.businessCase?.scenario) {
    throw new Error(label + " did not use the latest refined Business Scenario from S3.");
  }

  const nextSteps = result.projectArtifacts?.nextSteps;
  if (
    !Array.isArray(nextSteps?.immediateActions) ||
    nextSteps.immediateActions.length < 3 ||
    !nextSteps.immediateActions.every((item) =>
      ["action", "owner", "timing", "dependency", "decisionGate"].every((field) => item?.[field]?.trim())
    ) ||
    nextSteps.openQuestions?.length < 2 ||
    nextSteps.nextMeeting?.attendees?.length < 2 ||
    !nextSteps.customerSummary?.trim() ||
    !nextSteps.internalNotes?.trim()
  ) {
    throw new Error(label + " did not include complete, actionable handoff next steps.");
  }
}

if (!Array.isArray(handoff.evidence) || !handoff.evidence.some((item) => item.section === "projectAnswer")) {
  throw new Error("Handoff did not preserve paragraph-level evidence through AgentCore.");
}
const handoffCitations = new Set(handoff.citations);
if (handoff.evidence.some((item) => item.sources.some((source) => !handoffCitations.has(source)))) {
  throw new Error("Handoff evidence referenced a source outside the approved citation set.");
}

if (handoff.metadata?.artifactRetention !== "latest-only" || !handoff.metadata?.docxDownloadUrl) throw new Error("Handoff did not return a latest-only DOCX artifact.");
if (handoff.metadata?.agentSessionId !== catchup.metadata?.agentSessionId) throw new Error("The second request did not reuse the same AgentCore session.");
if (catchup.metadata?.projectVersion !== handoff.metadata?.projectVersion) throw new Error("Catch-up unexpectedly changed project state.");
if (catchup.metadata?.toolCalls?.some((name) => name === "save_project_update" || name === "create_handoff_packet")) {
  throw new Error("Catch-up invoked a write-capable AgentCore tool.");
}

console.table({
  unsignedAgentApi: "403",
  crossClientAccess: "403",
  briefProvider: brief.provider,
  briefModel: brief.metadata?.modelId,
  refinementInstructions: brief.metadata?.refinementInstructionCount,
  contradictionCorrection: brief.metadata?.contradictionValidationPassed,
  latestRefinedPacket: "used by handoff + catch-up",
  handoffProvider: handoff.provider,
  handoffModel: handoff.metadata?.modelId,
  projectVersion: handoff.metadata?.projectVersion,
  handoffTools: handoff.metadata?.toolCalls?.join(", "),
  handoffEvidence: handoff.evidence.length,
  handoffNextSteps: handoff.projectArtifacts.nextSteps.immediateActions.length,
  handoffDocx: "issued",
  catchupProvider: catchup.provider,
  catchupTools: catchup.metadata?.toolCalls?.join(", "),
  sameSession: true,
  publicDomainCors: "allowed",
  catchupReadOnly: true,
  handoffJob: `${agentJobStats[0]?.durationMs ?? 0} ms / ${agentJobStats[0]?.polls ?? 0} polls`,
  catchupJob: `${agentJobStats[1]?.durationMs ?? 0} ms / ${agentJobStats[1]?.polls ?? 0} polls`,
});
