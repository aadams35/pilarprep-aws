import { Sha256 } from "@aws-crypto/sha256-js";
import { HttpRequest } from "@smithy/protocol-http";
import { SignatureV4 } from "@smithy/signature-v4";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";

const region = process.env.AWS_REGION ?? "us-east-1";
const backendStack = process.env.PILLARPREP_BACKEND_STACK ?? "pillarprep-bedrock";
const jobsStack = process.env.PILLARPREP_JOBS_STACK ?? "pillarprep-jobs";
const origin = process.env.PILLARPREP_PUBLIC_ORIGIN ?? "https://pilarprep.app";
const resumeApproved =
  process.env.PILLARPREP_SMOKE_RESUME_APPROVED === "true";
const refreshApproval =
  process.env.PILLARPREP_SMOKE_REFRESH_APPROVAL === "true";
const skipHandoff = process.env.PILLARPREP_SMOKE_SKIP_HANDOFF === "true";
const briefOnly = process.env.PILLARPREP_SMOKE_BRIEF_ONLY === "true";
const smokeModelPreference =
  process.env.PILLARPREP_SMOKE_MODEL ?? "nova-pro";
const customScenario = process.env.PILLARPREP_SMOKE_CUSTOM === "true";
const legacyBlueMesaDirection =
  process.env.PILLARPREP_SMOKE_LEGACY_BLUE_MESA === "true";
const audienceRefinements =
  process.env.PILLARPREP_SMOKE_AUDIENCE_REFINEMENTS === "true";
const rotateIdentityBeforeRefinement =
  process.env.PILLARPREP_SMOKE_ROTATE_IDENTITY === "true";
const smokePresetClient =
  process.env.PILLARPREP_SMOKE_PRESET ?? "peakcart-retail";
if (
  ![
    "apex-mutual",
    "bluemesa-payments",
    "northstar-health",
    "peakcart-retail",
  ].includes(smokePresetClient)
) {
  throw new Error("PILLARPREP_SMOKE_PRESET is not a supported preset client.");
}
const directApiUrl = process.env.PILLARPREP_JOBS_API_URL ?? "";
const directIdentityPoolId = process.env.PILLARPREP_IDENTITY_POOL_ID ?? "";
const directArtifactBucket = process.env.PILLARPREP_ARTIFACT_BUCKET ?? "";
if (
  !["nova-pro", "nova-micro", "claude-sonnet-4.6"].includes(smokeModelPreference)
) {
  throw new Error("PILLARPREP_SMOKE_MODEL is not a supported model preference.");
}

function awsJson(args) {
  const output = execFileSync(
    "aws",
    [...args, "--region", region, "--output", "json"],
    { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] }
  );
  return JSON.parse(output);
}

function stackOutputs(stackName) {
  const outputs = awsJson([
    "cloudformation",
    "describe-stacks",
    "--stack-name",
    stackName,
    "--query",
    "Stacks[0].Outputs",
  ]);
  return Object.fromEntries(
    outputs.map((item) => [item.OutputKey, item.OutputValue])
  );
}

async function cognitoRequest(target, payload) {
  const response = await fetch(
    `https://cognito-identity.${region}.amazonaws.com/`,
    {
      method: "POST",
      headers: {
        "content-type": "application/x-amz-json-1.1",
        "x-amz-target": `AWSCognitoIdentityService.${target}`,
      },
      body: JSON.stringify(payload),
    }
  );
  const text = await response.text();
  if (!response.ok) {
    throw new Error(
      `Cognito ${target} returned HTTP ${response.status}: ${text}`
    );
  }
  return JSON.parse(text);
}

async function cognitoCredentials(identityPoolId) {
  const identity = await cognitoRequest("GetId", {
    IdentityPoolId: identityPoolId,
  });
  const response = await cognitoRequest("GetCredentialsForIdentity", {
    IdentityId: identity.IdentityId,
  });
  const values = response.Credentials;
  if (!values?.AccessKeyId || !values.SecretKey) {
    throw new Error("Cognito did not return usable temporary credentials.");
  }
  return {
    accessKeyId: values.AccessKeyId,
    secretAccessKey: values.SecretKey,
    sessionToken: values.SessionToken,
  };
}

async function signedFetch(url, method, credentials, payload) {
  const endpoint = new URL(url);
  const body = payload === undefined ? undefined : JSON.stringify(payload);
  const signer = new SignatureV4({
    credentials,
    region,
    service: "execute-api",
    sha256: Sha256,
  });
  const request = new HttpRequest({
    protocol: endpoint.protocol,
    hostname: endpoint.hostname,
    method,
    path: endpoint.pathname,
    query: Object.fromEntries(endpoint.searchParams.entries()),
    headers: {
      accept: "application/json",
      ...(body === undefined ? {} : { "content-type": "application/json" }),
      host: endpoint.host,
    },
    ...(body === undefined ? {} : { body }),
  });
  const signed = await signer.sign(request);
  const headers = { ...signed.headers };
  delete headers.host;
  return fetch(url, {
    method,
    headers,
    ...(body === undefined ? {} : { body }),
  });
}

async function signedJson(url, method, credentials, payload, label) {
  const response = await signedFetch(url, method, credentials, payload);
  const text = await response.text();
  let parsed;
  try {
    parsed = text ? JSON.parse(text) : {};
  } catch {
    throw new Error(`${label} returned invalid JSON with HTTP ${response.status}.`);
  }
  if (!response.ok) {
    throw new Error(
      `${label} returned HTTP ${response.status}: ${parsed.error ?? text}`
    );
  }
  return { status: response.status, body: parsed };
}

const sleep = (milliseconds) =>
  new Promise((resolve) => setTimeout(resolve, milliseconds));

async function runJob(apiUrl, credentials, envelope, label) {
  const startedAt = Date.now();
  const accepted = await signedJson(
    `${apiUrl}/jobs`,
    "POST",
    credentials,
    envelope,
    label
  );
  if (
    accepted.status !== 202 ||
    !accepted.body.jobId ||
    accepted.body.clientId !== envelope.clientId ||
    accepted.body.projectId !== envelope.projectId
  ) {
    throw new Error(`${label} did not return a scoped HTTP 202 job envelope.`);
  }

  const deadline = Date.now() + 720_000;
  let polls = 0;
  let waitMs = accepted.body.pollAfterMs ?? 1500;
  while (Date.now() < deadline) {
    await sleep(Math.max(750, Math.min(waitMs, 5000)));
    polls += 1;
    const query = new URLSearchParams({
      clientId: envelope.clientId,
      projectId: envelope.projectId,
      sessionId: envelope.sessionId,
    });
    const status = await signedJson(
      `${apiUrl}/jobs/${accepted.body.jobId}?${query}`,
      "GET",
      credentials,
      undefined,
      `${label} poll`
    );
    if (
      ["queued", "running", "validating", "saving"].includes(
        status.body.status
      )
    ) {
      waitMs = status.body.pollAfterMs ?? waitMs;
      continue;
    }
    if (status.body.status === "failed") {
      throw new Error(`${label} failed: ${status.body.error ?? "unknown error"}`);
    }
    if (status.body.status !== "complete" || !status.body.result) {
      throw new Error(`${label} returned an invalid terminal job state.`);
    }
    return {
      result: status.body.result,
      jobId: accepted.body.jobId,
      polls,
      durationMs: Date.now() - startedAt,
    };
  }
  throw new Error(`${label} did not complete within twelve minutes.`);
}

function snapshot(brief) {
  return Object.fromEntries(
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
    ].map((key) => [key, structuredClone(brief[key])])
  );
}

const refinableSections = [
  "businessCase",
  "technical",
  "executive",
  "stakeholders",
  "gameplan",
  "objections",
];

function assertTargetIsolation(before, after, target, label) {
  for (const section of refinableSections) {
    if (
      section !== target &&
      JSON.stringify(after[section]) !== JSON.stringify(before[section])
    ) {
      throw new Error(`${label} changed non-target section ${section}.`);
    }
  }
}

function assertLiveProvider(result, provider, label) {
  if (result.provider !== provider || result.metadata?.fallbackUsed) {
    throw new Error(
      `${label} did not complete through live ${provider} without fallback.`
    );
  }
}

function assertEvidenceStatusVariety(result, label) {
  const statuses = new Set(
    (result.claims ?? []).map((claim) => claim.evidenceStatus).filter(Boolean)
  );
  const hasSupported =
    statuses.has("supported") || statuses.has("customer-provided");
  if (!hasSupported || !statuses.has("needs-validation")) {
    throw new Error(
      `${label} did not distinguish supported claims from unsupported claims.`
    );
  }
}

function assertNamedStakeholderProfiles(result, input, label) {
  const profiles = (input.decisionMakers ?? []).filter(
    (person) => person.name?.trim() && person.title?.trim()
  );
  const matchedNames = new Set();
  for (const passage of result.stakeholders ?? []) {
    const match = profiles.find(
      (person) =>
        passage.toLowerCase().includes(person.name.toLowerCase()) &&
        passage.toLowerCase().includes(person.title.toLowerCase())
    );
    if (!match && profiles.length >= 4) {
      throw new Error(
        `${label} returned a stakeholder passage without a supplied name and position.`
      );
    }
    if (match) matchedNames.add(match.name);
  }
  if (matchedNames.size !== Math.min(4, profiles.length)) {
    throw new Error(`${label} did not preserve four distinct named profiles.`);
  }
}

const useDirectOutputs = Boolean(
  directApiUrl && directIdentityPoolId && directArtifactBucket
);
const backend = useDirectOutputs ? {} : stackOutputs(backendStack);
const jobs = useDirectOutputs ? {} : stackOutputs(jobsStack);
const apiUrl = directApiUrl || jobs.JobsApiUrl;
const identityPoolId = directIdentityPoolId || backend.DemoIdentityPoolId;
const artifactBucket = directArtifactBucket || backend.ArtifactBucketName;
if (!apiUrl?.startsWith("https://") || !identityPoolId || !artifactBucket) {
  throw new Error("Required backend or Jobs stack outputs are missing.");
}

const cors = await fetch(`${apiUrl}/jobs`, {
  method: "OPTIONS",
  headers: {
    origin,
    "access-control-request-method": "POST",
    "access-control-request-headers":
      "authorization,content-type,x-amz-date,x-amz-security-token",
  },
});
if (
  cors.status !== 204 ||
  cors.headers.get("access-control-allow-origin") !== origin
) {
  throw new Error("Jobs API CORS does not allow the public HTTPS origin.");
}

const unsigned = await fetch(`${apiUrl}/clients`);
if (unsigned.status !== 403) {
  throw new Error(
    `Unsigned Jobs API request returned HTTP ${unsigned.status}, expected 403.`
  );
}

let credentials = await cognitoCredentials(identityPoolId);
const sessionId = `session-smoke-${randomUUID()}`;
const clientId = customScenario ? "custom-demo" : smokePresetClient;
const projectId = clientId;

const unauthorized = await signedFetch(
  `${apiUrl}/jobs`,
  "POST",
  credentials,
  {
    action: "catchup.generate",
    clientId: "outside-demo",
    projectId: "outside-demo",
    sessionId,
    idempotencyKey: `cross-client-${randomUUID()}`,
    input: {
      audienceRole: "New member",
      focus: "Attempt cross-client access.",
      modelPreference: "nova-pro",
    },
  }
);
if (unauthorized.status !== 403) {
  throw new Error(
    `Cross-client Jobs API request returned HTTP ${unauthorized.status}, expected 403.`
  );
}

if (customScenario) {
  const retiredGuestAudio = await signedFetch(
    apiUrl + "/meeting-audio/uploads",
    "POST",
    credentials,
    {
      clientId,
      projectId,
      sessionId,
      scenarioId: "blue-mesa-payments",
      meetingId: "custom-discovery",
      fileName: "custom-meeting.mp3",
      contentType: "audio/mpeg",
      sizeBytes: 1024,
    }
  );
  if (retiredGuestAudio.status !== 404) {
    throw new Error(
      "Retired guest meeting-audio route returned HTTP " +
        retiredGuestAudio.status +
        ", expected 404."
    );
  }

  const unsignedWorkspaceAudio = await fetch(
    apiUrl + "/workspace/meeting-audio/uploads",
    { method: "POST" }
  );
  if (unsignedWorkspaceAudio.status !== 401) {
    throw new Error(
      "Unsigned workspace meeting-audio route returned HTTP " +
        unsignedWorkspaceAudio.status +
        ", expected 401."
    );
  }
}

const briefInput = {
  mode: "prebrief",
  modelPreference: smokeModelPreference,
  company: "PeakCart Retail",
  industry: "Retail",
  meetingType: "Technical Deep Dive",
  companySize: "Enterprise",
  pillars: [
    "Performance Efficiency",
    "Reliability",
    "Cost Optimization",
    "Security",
    "Operational Excellence",
    "Sustainability",
  ],
  pillarRanking: [
    { rank: 1, pillar: "Performance Efficiency" },
    { rank: 2, pillar: "Reliability" },
    { rank: 3, pillar: "Cost Optimization" },
    { rank: 4, pillar: "Security" },
    { rank: 5, pillar: "Operational Excellence" },
    { rank: 6, pillar: "Sustainability" },
  ],
  context:
    "PeakCart Retail already runs its digital storefront, checkout APIs, loyalty services, and analytics workloads on AWS across two Regions. The next holiday campaign is expected to drive four times normal traffic, but the last major promotion caused intermittent checkout latency, overscaled services, delayed inventory events, and six minutes of payment degradation. Leadership needs a bounded improvement plan before the seasonal change freeze. The current estate uses CloudFront, API Gateway, Lambda, ECS, Aurora PostgreSQL, DynamoDB, ElastiCache, EventBridge, and SQS, but ownership, load-test evidence, service-level objectives, and cost attribution are inconsistent across teams.",
  companyValues:
    "Earn customer trust at every interaction, make shopping effortless, protect revenue during peak demand, experiment with evidence, and spend with discipline. Changes must improve customer experience without making launches slower or increasing operational risk.",
  companyValuesUrl:
    "https://www.peakcart.example/about/values",
  additionalDirection:
    "PeakCart is already on AWS. The engagement focuses on holiday traffic readiness, checkout latency, payment resilience, event replay, cost-per-order guardrails, and bounded production-readiness evidence.",
  meetingNotes:
    "Emma Chen wants an executive-ready decision that ties technical changes to checkout conversion, campaign launch confidence, and protected holiday revenue. Luis Ramirez needs load-test baselines, observability ownership, rollback criteria, event-replay validation, dependency limits, and a clear RTO/RPO position before approving production changes. Finance will support a short pilot if the team defines a measurable cost-per-order guardrail and separates immediate peak-readiness work from longer-term platform modernization.",
  decisionMakers: [
    {
      name: "Emma Chen",
      title: "VP, Digital Commerce",
      source: "Customer-approved profile notes",
      context:
        "Owns digital revenue, conversion, loyalty growth, and the customer experience during high-visibility campaigns. She wants a decision in this meeting, not an open-ended architecture exercise.",
    },
    {
      name: "Luis Ramirez",
      title: "Platform Engineering Lead",
      source: "Customer-approved profile notes",
      context:
        "Owns reliability engineering, production readiness, observability, release controls, and rollback confidence. He needs testable acceptance criteria and named owners.",
    },
    {
      name: "Priya Shah",
      title: "Director, FinOps",
      source: "Customer-approved profile notes",
      context:
        "Owns cloud unit economics and wants peak capacity tied to orders, conversion, and revenue rather than a blanket cost-reduction target.",
    },
  ],
  role: "Solutions Architect",
  prompt:
    "Prepare a customer-specific discovery and evidence plan that bridges the commercial outcome, the AWS architecture decisions, and an executable two-week peak-readiness pilot.",
};

const presetOverrides = {
  "apex-mutual": {
    company: "Apex Mutual",
    industry: "Financial Services",
    meetingType: "Executive Briefing",
    context:
      "Apex Mutual is modernizing a customer portal with compliance, identity, payroll and HR integration, audit evidence, data privacy, reconciliation, and phased cutover requirements.",
    companyValues:
      "Customer trust, transparent governance, disciplined modernization, and measurable progress without avoidable service disruption.",
    meetingNotes:
      "The CIO needs an executive-ready modernization path while security validates identity boundaries, evidence, data handling, and a bounded pilot.",
  },
  "bluemesa-payments": {
    company: "BlueMesa Payments",
    industry: "Financial Services",
    meetingType: "Executive Briefing",
    context:
      "BlueMesa Payments already runs on AWS and needs a governed payroll-partner integration with clear reliability, reconciliation, security, and ownership gates.",
    companyValues:
      "Merchant trust, rigorous compliance, low-drama change management, and faster delivery when customer impact stays protected.",
    additionalDirection:
      "BlueMesa is an existing AWS customer. The engagement focuses on payroll integration across mixed API and encrypted-file interfaces, including idempotency, reconciliation, data privacy, retention, partner certification, cutover, and recovery evidence. Replacing the existing ledger is outside scope.",
    meetingNotes:
      "BlueMesa approved discovery for a bounded payroll-partner integration on its existing AWS payment platform. The call must confirm mixed API and encrypted-file interfaces, idempotency, reconciliation ownership, data classification, retention, privileged access, partner certification, payroll-window availability, recovery evidence, named owners, and the next architecture decision gate.",
    decisionMakers: [
      {
        name: "Ariana Cole",
        title: "Chief Digital Officer",
        roleType: "decision-maker",
        source: "Synthetic customer-approved profile notes",
        context:
          "Owns the program commitment and needs a bounded first release tied to merchant trust and faster payroll-partner onboarding.",
      },
      {
        name: "Dev Malik",
        title: "VP Infrastructure and Resilience",
        roleType: "decision-maker",
        source: "Synthetic customer-approved profile notes",
        context:
          "Owns technical direction, integration reliability, replay evidence, observability, rollback readiness, and production approval.",
      },
      {
        name: "Rachel Kim",
        title: "Chief Risk and Compliance Officer",
        roleType: "decision-maker",
        source: "Synthetic customer-approved profile notes",
        context:
          "Owns payroll-data controls, privileged-access evidence, retention decisions, and risk acceptance.",
      },
      {
        name: "Priya Shah",
        title: "Director of Payment Operations",
        roleType: "stakeholder",
        source: "Synthetic customer-approved profile notes",
        context:
          "Influences acceptance through reconciliation, exception handling, payroll cutoffs, and operational ownership.",
      },
    ],
  },
  "northstar-health": {
    company: "Northstar Health",
    industry: "Healthcare",
    meetingType: "Technical Deep Dive",
    context:
      "Northstar Health is consolidating patient scheduling systems and needs resilient integration, stronger disaster recovery, lower support burden, and clear compliance evidence.",
    companyValues:
      "Patient access, continuity of care, responsible data stewardship, and simpler operations for care teams.",
    meetingNotes:
      "Architecture needs RTO and RPO options, data classification, interoperability, and phased cutover patterns while compliance needs explicit evidence paths.",
  },
};

if (!customScenario && presetOverrides[clientId]) {
  Object.assign(briefInput, presetOverrides[clientId]);
}

if (legacyBlueMesaDirection) {
  if (clientId !== "bluemesa-payments" || customScenario) {
    throw new Error(
      "PILLARPREP_SMOKE_LEGACY_BLUE_MESA requires the BlueMesa preset."
    );
  }
  briefInput.additionalDirection =
    "Treat BlueMesa as an existing AWS customer. Make payroll integration, mixed API and encrypted-file interfaces, idempotency, reconciliation, data privacy, retention, partner certification, cutover, and recovery evidence explicit. The existing ledger replacement is out of scope.";
}

if (customScenario) {
  Object.assign(briefInput, {
    company: "HarborLine Workforce",
    industry: "SaaS",
    meetingType: "Discovery Call",
    context:
      "HarborLine Workforce already runs its workforce scheduling platform on AWS. The customer needs a governed integration with its payroll provider before the next enrollment cycle. The team must clarify API and encrypted-file interfaces, reconciliation ownership, failure recovery, privacy controls, implementation sequencing, and measurable acceptance gates.",
    companyValues:
      "Protect employee trust, make payroll changes auditable, deliver integrations incrementally, and assign accountable owners before production launch.",
    companyValuesUrl: "https://harborline.example/company/values",
    additionalDirection:
      "The customer is already on AWS. The brief must cover payroll integration, encrypted-file exchange, reconciliation ownership, privacy controls, cutover planning, failure recovery, and measurable acceptance gates.",
    meetingNotes:
      "The buyer wants a bounded payroll integration decision, the technical lead needs evidence for idempotency and reconciliation, and operations needs a clear escalation path for missed or duplicated payroll events.",
    decisionMakers: [
      {
        name: "Jordan Lee",
        title: "VP, Product",
        source: "Synthetic customer-approved profile notes",
        context:
          "Owns the enrollment-cycle outcome and needs a decision with scope, success measures, and named owners.",
      },
      {
        name: "Morgan Patel",
        title: "Director, Platform Engineering",
        source: "Synthetic customer-approved profile notes",
        context:
          "Owns payroll integration reliability, API contracts, reconciliation, observability, and production readiness.",
      },
    ],
    prompt:
      "Create a customer-specific discovery packet for the governed payroll integration and make the required decisions, evidence, risks, and next steps explicit.",
  });
}

let generation;
let businessRefinement;
let refinement;
let brief;
let approval;
let approved;

if (resumeApproved) {
  let refreshedApproval;
  if (refreshApproval) {
    refreshedApproval = await runJob(
      apiUrl,
      credentials,
      {
        action: "brief.approve",
        clientId,
        projectId,
        sessionId,
        idempotencyKey: `approve-refresh-${randomUUID()}`,
        input: { packetVersion: 3, modelPreference: "nova-pro" },
      },
      "Refresh approved packet"
    );
  }
  const resumeQuery = new URLSearchParams({ projectId, sessionId });
  const resumed = await signedJson(
    `${apiUrl}/clients/${clientId}/latest?${resumeQuery}`,
    "GET",
    credentials,
    undefined,
    "Resume approved packet"
  );
  approved = resumed.body.packet;
  if (
    resumed.body.packetVersion !== 3 ||
    approved?.metadata?.approvalStatus !== "approved"
  ) {
    throw new Error("Resume mode requires approved packet version 3.");
  }
  generation = { result: approved, durationMs: 0, polls: 0 };
  businessRefinement = { result: approved, durationMs: 0, polls: 0 };
  refinement = { result: approved, durationMs: 0, polls: 0 };
  approval = refreshedApproval ?? { result: approved, durationMs: 0, polls: 0 };
  brief = approved;
} else {
  generation = await runJob(
    apiUrl,
    credentials,
    {
      action: "brief.generate",
      clientId,
      projectId,
      sessionId,
      idempotencyKey: `generate-${randomUUID()}`,
      input: briefInput,
    },
    "Brief generation"
  );
  assertLiveProvider(generation.result, "bedrock", "Brief generation");
  assertEvidenceStatusVariety(generation.result, "Brief generation");
  assertNamedStakeholderProfiles(
    generation.result,
    briefInput,
    "Brief generation"
  );
  if (
    generation.result.metadata?.packetVersion !== 1 ||
    generation.result.metadata?.approvalStatus !== "draft" ||
    generation.result.metadata?.refinementTarget ||
    !generation.result.metadata?.artifactKey?.includes("/brief/draft/")
  ) {
    throw new Error(
      "Generated brief was not persisted as a clean scoped draft version 1."
    );
  }

  if (audienceRefinements) {
    if (rotateIdentityBeforeRefinement) {
      credentials = await cognitoCredentials(identityPoolId);
    }
    const cases = [
      {
        target: "executive",
        feedback: "Executive lens: Add ROI and decision criteria",
        notes:
          "Rewrite all four executive passages for the board. Connect protected revenue, decision timing, measurable success, sponsor confidence, and the cost of delay without AWS jargon.",
      },
      {
        target: "stakeholders",
        feedback: "Stakeholder alignment: Clarify influence and approval responsibilities",
        notes:
          "Rewrite all four stakeholder passages. Distinguish decision authority from influence, name the evidence each person needs, identify likely blockers, and add a role-specific question.",
      },
      {
        target: "gameplan",
        feedback: "Meeting execution: Create a tighter meeting agenda",
        notes:
          "Rewrite all four SA game-plan passages into a timed customer-call sequence with an objective, owner, evidence checkpoint, decision gate, and live question in each passage.",
      },
      {
        target: "objections",
        feedback: "Objection handling: Add stronger customer-specific responses",
        notes:
          "Rewrite all four objections around release risk, resilience evidence, cost guardrails, and accountable ownership. Preserve the Concern, Response, and Ask structure.",
      },
    ];
    let current = snapshot(generation.result);
    let currentVersion = 1;
    const timings = {};
    for (const item of cases) {
      const previous = snapshot(current);
      const refined = await runJob(
        apiUrl,
        credentials,
        {
          action: "brief.refine",
          clientId,
          projectId,
          sessionId,
          idempotencyKey: "refine-" + item.target + "-" + randomUUID(),
          input: {
            ...briefInput,
            feedback: [item.feedback],
            feedbackDetails: [
              {
                category: item.feedback.split(":", 1)[0],
                instruction: item.feedback.split(":").slice(1).join(":").trim(),
              },
            ],
            feedbackNotes: item.notes,
            baseBriefVersion: currentVersion,
            refinementTarget: item.target,
            previousBrief: previous,
          },
        },
        item.target + " refinement"
      );
      assertLiveProvider(refined.result, "bedrock", item.target + " refinement");
      assertTargetIsolation(previous, refined.result, item.target, item.target + " refinement");
      if (
        refined.result.metadata?.refinementTarget !== item.target ||
        refined.result.metadata?.refinementIsolationPassed !== true ||
        refined.result.metadata?.refinementCoveragePassed !== true ||
        refined.result[item.target]?.length !== 4 ||
        JSON.stringify(refined.result[item.target]) === JSON.stringify(previous[item.target])
      ) {
        throw new Error(item.target + " refinement did not regenerate its complete selected tab.");
      }
      current = refined.result;
      currentVersion += 1;
      timings[item.target] = refined.durationMs + " ms / " + refined.polls + " polls";
    }
    console.table({
      scenario: customScenario ? "custom-demo" : clientId,
      finalVersion: currentVersion,
      ...timings,
    });
    process.exit(0);
  }

  if (briefOnly) {
    if (
      smokeModelPreference === "claude-sonnet-4.6" &&
      generation.result.metadata?.modelId !== "global.anthropic.claude-sonnet-4-6"
    ) {
      throw new Error("Claude smoke test completed through the wrong Bedrock model.");
    }
    if (
      smokeModelPreference === "claude-sonnet-4.6" &&
      (generation.result.metadata?.generationStrategy !== "section-router" ||
        generation.result.metadata?.generationRoutes?.length !== 3)
    ) {
      throw new Error("Claude smoke test did not complete through all three brief routes.");
    }
    if (customScenario) {
      const customText = JSON.stringify(generation.result).toLowerCase();
      for (const anchor of ["harborline", "payroll"]) {
        if (!customText.includes(anchor)) {
          throw new Error(
            "Custom scenario output did not preserve required context: " + anchor + "."
          );
        }
      }
    }
    const companyAnchor = briefInput.company.toLowerCase().split(/\s+/)[0];
    if (!JSON.stringify(generation.result).toLowerCase().includes(companyAnchor)) {
      throw new Error("Preset output did not preserve the supplied company context.");
    }
    console.table({
      scenario: customScenario ? "custom-demo" : clientId,
      jobsApi: apiUrl,
      requestedModel: smokeModelPreference,
      actualModel: generation.result.metadata?.modelId,
      modelProfile: generation.result.metadata?.modelProfile,
      outputTokens: generation.result.metadata?.outputTokens,
      generation: `${generation.durationMs} ms / ${generation.polls} polls`,
    });
    process.exit(0);
  }

  const baseline = snapshot(generation.result);
  const blueMesaRefinement = clientId === "bluemesa-payments" && !customScenario;
  const businessFeedback = blueMesaRefinement
    ? {
        summary:
          "Business alignment: Add the confirmed payroll-integration success criteria",
        instruction:
          "The bounded validation must prove mixed API and encrypted-file exchange, idempotent replay, reconciliation ownership, payroll-window availability, tested rollback, and privacy-control evidence before partner certification",
        notes:
          "Regenerate the complete Business Case from first principles. Confirm BlueMesa already runs on AWS, remove every on-premises or initial AWS migration assumption, and incorporate payroll integration, mixed API and encrypted-file paths, reconciliation ownership, data handling, partner certification, rollback, and recovery evidence across every relevant outcome, scope, risk, decision, alignment, and next-step field.",
      }
    : {
        summary:
          "Business alignment: Add the newly confirmed peak-readiness success criteria",
        instruction:
          "The two-week validation must establish a p95 checkout-latency baseline, a payment-degradation budget below two minutes, a tested rollback gate, and a cost-per-order guardrail before campaign rehearsal",
        notes:
          "Regenerate the complete Business Case from first principles. Confirm the customer is already on AWS, remove every on-premises or initial AWS migration assumption, and incorporate the newly confirmed success criteria across every relevant outcome, scope, risk, decision, alignment, and next-step field. Sales must frame protected conversion and launch confidence while the SA validates each technical measure.",
      };
  businessRefinement = await runJob(
    apiUrl,
    credentials,
    {
      action: "brief.refine",
      clientId,
      projectId,
      sessionId,
      idempotencyKey: `refine-${randomUUID()}`,
      input: {
        ...briefInput,
        feedback: [
          "Customer context: Customer is already on AWS",
          businessFeedback.summary,
        ],
        feedbackDetails: [
          {
            category: "Customer context",
            instruction: "Customer is already on AWS",
          },
          {
            category: "Business alignment",
            instruction: businessFeedback.instruction,
          },
        ],
        feedbackNotes: businessFeedback.notes,
        baseBriefVersion: 1,
        refinementTarget: "businessCase",
        previousBrief: baseline,
      },
    },
    "Business Case refinement"
  );
  const businessBrief = businessRefinement.result;
  assertLiveProvider(businessBrief, "bedrock", "Business Case refinement");
  const businessText = Object.values(businessBrief.businessCase).join(" ");
  assertTargetIsolation(
    baseline,
    businessBrief,
    "businessCase",
    "Business Case refinement"
  );
  if (
    businessBrief.metadata?.packetVersion !== 2 ||
    businessBrief.metadata?.approvalStatus !== "stale" ||
    businessBrief.metadata?.refinementTarget !== "businessCase" ||
    businessBrief.metadata?.refinementIsolationPassed !== true ||
    businessBrief.metadata?.contradictionValidationPassed !== true ||
    Object.keys(businessBrief.businessCase ?? {}).length !== 13 ||
    JSON.stringify(businessBrief.businessCase) ===
      JSON.stringify(baseline.businessCase) ||
    /\bon[- ]prem(?:ises)?\b/i.test(businessText) ||
    /\bmigrat(?:e|es|ing|ion)\b.{0,80}\bto\s+aws\b/i.test(businessText)
  ) {
    throw new Error(
      "Live refinement did not regenerate only the complete Business Case from corrected facts."
    );
  }

  const beforeObjections = snapshot(businessBrief);
  const objectionInstruction = blueMesaRefinement
    ? "Rebuild every objection around payroll-integration evidence and decision gates"
    : "Rebuild every objection around peak-readiness evidence and decision gates";
  const objectionNotes = blueMesaRefinement
    ? "Regenerate all four Objection Simulator entries. Cover payroll data risk, integration reliability, partner certification, operational ownership, and the evidence required to proceed. Keep the Concern, Response, Ask structure in every entry."
    : "Regenerate all four Objection Simulator entries. Cover release risk, proof of resilience, cost guardrails, and accountable ownership. Keep the Concern, Response, Ask structure in every entry.";
  refinement = await runJob(
    apiUrl,
    credentials,
    {
      action: "brief.refine",
      clientId,
      projectId,
      sessionId,
      idempotencyKey: `refine-objections-${randomUUID()}`,
      input: {
        ...briefInput,
        feedback: [`Objection handling: ${objectionInstruction}`],
        feedbackDetails: [
          {
            category: "Objection handling",
            instruction: objectionInstruction,
          },
        ],
        feedbackNotes: objectionNotes,
        baseBriefVersion: 2,
        refinementTarget: "objections",
        previousBrief: beforeObjections,
      },
    },
    "Objections refinement"
  );
  brief = refinement.result;
  assertLiveProvider(brief, "bedrock", "Objections refinement");
  assertTargetIsolation(
    beforeObjections,
    brief,
    "objections",
    "Objections refinement"
  );
  if (
    brief.metadata?.packetVersion !== 3 ||
    brief.metadata?.approvalStatus !== "stale" ||
    brief.metadata?.refinementTarget !== "objections" ||
    brief.metadata?.refinementIsolationPassed !== true ||
    brief.objections?.length !== 4 ||
    !brief.objections.every(
      (item, index) => item !== beforeObjections.objections[index]
    )
  ) {
    throw new Error(
      "Live refinement did not regenerate only all four Objection Simulator entries."
    );
  }

  approval = await runJob(
    apiUrl,
    credentials,
    {
      action: "brief.approve",
      clientId,
      projectId,
      sessionId,
      idempotencyKey: `approve-${randomUUID()}`,
      input: { packetVersion: 3, modelPreference: "nova-pro" },
    },
    "Brief approval"
  );
  approved = approval.result;
  assertLiveProvider(approved, "bedrock", "Brief approval");
  if (
    approved.metadata?.approvalStatus !== "approved" ||
    approved.metadata?.packetVersion !== 3 ||
    approved.metadata?.approvedPacketVersion !== 3 ||
    approved.metadata?.artifactRetention !== "immutable-approved" ||
    !approved.metadata?.artifactKey?.endsWith("/brief/approved/v000003/packet.json") ||
    !approved.metadata?.docxArtifactKey?.endsWith("/brief/approved/v000003/packet.docx")
  ) {
    throw new Error("Approval did not durably promote the exact packet version.");
  }
}

if (
  (!resumeApproved || refreshApproval) &&
  (approval.result.metadata?.precallHandoffJobId ||
    approval.result.metadata?.precallHandoffStatus !== "idle" ||
    approval.result.metadata?.precallHandoffSourceVersion !==
      approval.result.metadata?.approvedPacketVersion)
) {
  throw new Error(
    "Approval did not stop at the explicit pre-call handoff checkpoint."
  );
}

let handoff;
if (skipHandoff) {
  const storedHandoffQuery = new URLSearchParams({
    clientId,
    projectId,
    sessionId,
    format: "json",
  });
  const storedHandoffArtifact = await signedJson(
    `${apiUrl}/artifacts/handoff?${storedHandoffQuery}`,
    "GET",
    credentials,
    undefined,
    "Stored AgentCore handoff"
  );
  const storedHandoffResponse = await fetch(
    storedHandoffArtifact.body.downloadUrl
  );
  if (!storedHandoffResponse.ok) {
    throw new Error("Stored AgentCore handoff could not be downloaded.");
  }
  const storedHandoff = await storedHandoffResponse.json();
  handoff = {
    result: storedHandoff.packet ?? storedHandoff.response ?? storedHandoff,
    durationMs: 0,
    polls: 0,
  };
} else {
  handoff = await runJob(
    apiUrl,
    credentials,
    {
      action: "handoff.generate",
      clientId,
      projectId,
      sessionId,
      idempotencyKey: `handoff-${randomUUID()}`,
      input: {
        audienceRole: "Solutions Architect",
        focus:
          "Create the implementation handoff, evidence plan, owners, and first two weeks.",
        meetingNotes: briefInput.meetingNotes,
        modelPreference: "nova-pro",
        expectedApprovedPacketVersion: 3,
      },
    },
    "AgentCore handoff"
  );
}
assertLiveProvider(handoff.result, "agentcore", "AgentCore handoff");
if (
  !handoff.result.metadata?.memoryUsed ||
  !handoff.result.metadata?.gatewayUsed ||
  handoff.result.metadata?.approvedPacketVersion !== 3 ||
  !handoff.result.projectArtifacts?.nextSteps?.immediateActions?.length
) {
  throw new Error("AgentCore handoff was not grounded in the approved packet.");
}

const catchup = await runJob(
  apiUrl,
  credentials,
  {
    action: "catchup.generate",
    clientId,
    projectId,
    sessionId,
    idempotencyKey: `catchup-${randomUUID()}`,
    input: {
      audienceRole: "New member",
      focus: "What changed, what is decided, and where should I start?",
      meetingNotes: "",
      modelPreference: "nova-pro",
    },
  },
  "AgentCore catch-up"
);
assertLiveProvider(catchup.result, "agentcore", "AgentCore catch-up");
if (
  catchup.result.metadata?.approvedPacketVersion !== 3 ||
  catchup.result.metadata?.projectVersion !==
    handoff.result.metadata?.projectVersion ||
  catchup.result.metadata?.toolCalls?.some((name) =>
    ["save_project_update", "create_handoff_packet"].includes(name)
  )
) {
  throw new Error("Catch-up was not read-only or used the wrong approved packet.");
}

const clients = await signedJson(
  `${apiUrl}/clients`,
  "GET",
  credentials,
  undefined,
  "Client directory"
);
const client = clients.body.clients?.find((item) => item.clientId === clientId);
if (
  !client?.hasApprovedBrief ||
  !client?.hasHandoff ||
  client.approvedPacketVersion !== 3
) {
  throw new Error("Fresh-browser client discovery is missing latest packet state.");
}

const query = new URLSearchParams({ projectId, sessionId });
const latest = await signedJson(
  `${apiUrl}/clients/${clientId}/latest?${query}`,
  "GET",
  credentials,
  undefined,
  "Latest approved packet"
);
if (
  latest.body.packetVersion !== 3 ||
  latest.body.packet?.metadata?.approvalStatus !== "approved"
) {
  throw new Error("Latest-packet read did not return approved version 3.");
}

const latestDownloadUrl = latest.body.packet?.metadata?.docxDownloadUrl;
if (
  !latestDownloadUrl?.startsWith("https://") ||
  ![
    `${artifactBucket}.s3.amazonaws.com`,
    `${artifactBucket}.s3.${region}.amazonaws.com`,
  ].includes(new URL(latestDownloadUrl).hostname)
) {
  throw new Error("Saved packet did not receive a fresh link from the active artifact bucket.");
}
const latestDownload = await fetch(latestDownloadUrl);
if (
  latestDownload.status !== 200 ||
  !latestDownload.headers.get("content-type")?.includes(
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
  )
) {
  throw new Error(`Saved-packet DOCX returned HTTP ${latestDownload.status}.`);
}
await latestDownload.arrayBuffer();

const artifactQuery = new URLSearchParams({
  clientId,
  projectId,
  sessionId,
  format: "docx",
});
const artifact = await signedJson(
  `${apiUrl}/artifacts/brief?${artifactQuery}`,
  "GET",
  credentials,
  undefined,
  "Brief artifact"
);
if (!artifact.body.downloadUrl?.startsWith("https://")) {
  throw new Error("Artifact route did not return an HTTPS presigned URL.");
}
const download = await fetch(artifact.body.downloadUrl);
if (
  download.status !== 200 ||
  !download.headers
    .get("content-type")
    ?.includes(
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
) {
  const errorBody = await download.text();
  const errorCode = errorBody.match(/<Code>([^<]+)<\/Code>/)?.[1] || "unknown";
  const downloadHost = new URL(artifact.body.downloadUrl).hostname;
  throw new Error(
    `The scoped DOCX download did not succeed (HTTP ${download.status}, ` +
      `content-type ${download.headers.get("content-type") || "missing"}, S3 code ${errorCode}, host ${downloadHost}).`
  );
}

const directKey = artifact.body.artifactKey
  .split("/")
  .map(encodeURIComponent)
  .join("/");
const directS3 = await fetch(
  `https://${artifactBucket}.s3.${region}.amazonaws.com/${directKey}`
);
if (directS3.status !== 403) {
  throw new Error(
    `Direct S3 object access returned HTTP ${directS3.status}, expected 403.`
  );
}

console.table({
  jobsApi: apiUrl,
  unsignedApi: "403",
  crossClient: "403",
  briefProvider: generation.result.provider,
  briefVersion: approved.metadata.packetVersion,
  businessRefinementTarget: businessRefinement.result.metadata.refinementTarget,
  objectionsRefinementTarget: brief.metadata.refinementTarget,
  contradictionCheck: brief.metadata.contradictionValidationPassed,
  approval: approved.metadata.approvalStatus,
  handoffProvider: handoff.result.provider,
  catchupProvider: catchup.result.provider,
  catchupReadOnly: true,
  clientDirectory: "latest approved + handoff",
  savedPacketDownload: "200 from active bucket",
  docxDownload: "200",
  directS3: "403",
  generation: `${generation.durationMs} ms / ${generation.polls} polls`,
  businessRefinement: `${businessRefinement.durationMs} ms / ${businessRefinement.polls} polls`,
  objectionsRefinement: `${refinement.durationMs} ms / ${refinement.polls} polls`,
  handoff: `${handoff.durationMs} ms / ${handoff.polls} polls`,
  catchup: `${catchup.durationMs} ms / ${catchup.polls} polls`,
});
