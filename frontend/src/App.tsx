"use client";

import type { DragEvent } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  MeetingIntelligence,
  type MeetingDecisionMap,
  type MeetingAudioSelection,
} from "@/components/meeting-intelligence";
import {
  ClientLifecycle,
  type LifecycleStage,
  type LifecycleStageId,
} from "@/components/client-lifecycle";
import {
  OpportunityGates,
  type OpportunityGate,
  type OpportunityGateStatus,
} from "@/components/opportunity-gates";
import { EvidenceWorkspace, type EvidenceUpload } from "@/components/evidence-workspace";
import { EvidenceDrawer } from "@/components/evidence-drawer";
import {
  cognitoIdentityCredentialsProvider,
  signedApiFetch,
} from "@/lib/aws-sigv4";
import {
  bearerApiFetch,
  beginCognitoLogin,
  cognitoAuthConfigured,
  completeCognitoLogin,
  signOutCognito,
  validCognitoIdToken,
  type CognitoAuthConfig,
  type CognitoAuthSession,
} from "@/lib/cognito-auth";
import {
  parseAuthorizedClients,
  parseCurrentPacket,
  parseEvidenceDocuments,
  parseLatestPacket,
  parsePipelineAccepted,
  pipelineApiUrl,
  pollPipelineJob,
} from "@/lib/jobs-client";
import {
  generateBlueMesaBackupBrief,
  generateDemoBrief,
  validateBriefRequest,
} from "@/lib/generator";
import {
  approvalAfterGeneration,
  businessCaseFields,
  businessCasePassages,
  compareBriefVersions,
  comparisonForSelectedRefinement,
  type BriefReviewMode,
} from "@/lib/brief-diff";
import { evidenceStatusLabel } from "@/lib/evidence-status";
import {
  cloneRefinementDrafts,
  createRefinementDrafts,
  normalizeRefinementDrafts,
  refinementDraftChanged,
  refinementTargets,
  toggleRefinementFeedback,
  type RefinementDrafts,
} from "@/lib/refinement";
import { normalizeBriefResponse } from "@/lib/response";
import { handoffAnswerFor, mergeHandoffPacket } from "@/lib/handoff-packet";
import { readApiJson, readRetryDelay } from "@/lib/api-response";
import type {
  BriefRequest,
  BriefResponse,
  DecisionMakerContext,
  EvidenceDocumentRecord,
  MeetingProcessResult,
  MeetingReviewDecision,
  ModelPreference,
  PersonInfluence,
  PersonRoleType,
  PersonStance,
  PipelineJobAction,
  PipelineJobRequest,
  PipelineJobState,
  PipelineJobStatus,
  ProjectArtifactItem,
  RefinementTarget,
} from "@/lib/types";

type BriefTab = RefinementTarget;
type AudienceRole = "Sales" | "Solutions Architect" | "Executive" | "PM" | "Engineer" | "New member";
type RiskLevel = "Low" | "Medium" | "High";
type PeopleView = PersonRoleType;
type GenerationMode = "demo" | "live";
type GenerationStageMode = "prebrief" | "project";
type PrecallHandoffStatus = "idle" | "queued" | "preparing" | "ready" | "failed" | "stale";
type ActivePipelineStatus = Extract<
  PipelineJobState,
  "queued" | "running" | "validating" | "saving"
>;
type LiveModelPreference = Exclude<ModelPreference, "default">;
type ConsolePage = "setup" | "brief" | "project" | "library" | "evidence";
type CatchupFilter = "all" | "approved" | "handoff";
type GateDecision = {
  status: OpportunityGateStatus;
  confirmed: boolean;
};
type Scenario = {
  id: string;
  name: string;
  company: string;
  industry: string;
  meetingType: string;
  companySize: string;
  pillars: string[];
  context: string;
  companyValues: string;
  companyValuesUrl: string;
  additionalDirection: string;
  decisionMakers: DecisionMakerContext[];
  meetingNotes: string;
  challenge: string;
  winTheme: string;
};

type BriefHistoryEntry = {
  id: string;
  scenarioId?: string;
  savedAt: string;
  company: string;
  industry: string;
  meetingType: string;
  companySize: string;
  selectedPillars: string[];
  context: string;
  companyValues: string;
  companyValuesUrl?: string;
  additionalDirection?: string;
  decisionMakers: DecisionMakerContext[];
  meetingNotes: string;
  feedback: string[];
  feedbackNotes?: string;
  refinementTarget?: BriefTab;
  refinementDrafts?: RefinementDrafts;
  appliedRefinementDrafts?: RefinementDrafts;
  modelPreference?: LiveModelPreference;
  briefVersion: number;
  approved: boolean;
  promoted: boolean;
  generatedBrief: BriefResponse;
};

const industries = [
  "Financial Services",
  "Healthcare",
  "Retail",
  "Manufacturing",
  "Media",
  "SaaS",
  "Other",
];

const meetingTypes = [
  "Discovery Call",
  "Technical Deep Dive",
  "Executive Briefing",
];

const companySizes = ["Startup", "Mid-market", "Enterprise"];

const scenarios: Scenario[] = [
  {
    id: "bluemesa",
    name: "Payroll integration program",
    company: "BlueMesa Payments",
    industry: "Financial Services",
    meetingType: "Executive Briefing",
    companySize: "Enterprise",
    pillars: ["Security", "Reliability", "Operational Excellence"],
    context:
      "BlueMesa Payments is a regulated payment processor serving regional banks and payroll platforms. Its production payment APIs already run on Amazon EKS, operational data is stored in Amazon RDS for PostgreSQL, payment events move through Amazon MSK, and evidence archives use Amazon S3. BlueMesa wants to add governed payroll-partner onboarding through real-time APIs and encrypted batch files without replacing its ledger. The initiative must improve partner onboarding while preserving settlement accuracy, reconciliation ownership, privileged-access evidence, and availability during payroll windows.",
    companyValues: "Merchant trust, auditable operations, predictable settlement, accountable ownership, and faster partner onboarding without weakening payment or payroll data protection.",
    companyValuesUrl: "https://www.bluemesa-payments.example/company/values",
    additionalDirection: "BlueMesa is an existing AWS customer. The meeting scope includes payroll integration, mixed API and encrypted-file interfaces, idempotency, reconciliation, data privacy, retention, partner certification, cutover, and recovery evidence. Existing ledger replacement remains outside scope.",
    decisionMakers: [
      {
        name: "Ariana Cole",
        roleType: "decision-maker",
        title: "Chief Digital Officer",
        organizationalRole: "Executive sponsor",
        decisionAuthority: "Executive sponsorship, strategic priority, and final program commitment",
        priorities: "Merchant trust, four-week partner onboarding, faster payment-product launches, and a board-ready risk narrative.",
        concerns: "A payroll launch that creates settlement disruption, customer-visible incidents, or another acquisition-integration delay.",
        successMeasures: "Two design partners onboarded through a bounded first release with measurable controls and no ledger replacement.",
        engagementGuidance: "Lead with partner growth and merchant trust, then connect each technical gate to customer and board confidence.",
        source: "Customer-approved profile notes",
        context: "Executive sponsor for the payroll integration program. She wants visible progress without weakening settlement confidence or creating a peak-period incident.",
      },
      {
        name: "Marcus Vale",
        roleType: "decision-maker",
        title: "Chief Financial Officer",
        organizationalRole: "Economic buyer",
        decisionAuthority: "Funding envelope, commercial approval, and economic acceptance criteria",
        priorities: "Shorter onboarding cycles, predictable delivery cost, partner revenue readiness, and a bounded first release.",
        concerns: "Open-ended platform redesign, unclear unit economics, and duplicated operating costs during rollout.",
        successMeasures: "An approved investment range tied to onboarding lead time, support effort, and partner activation milestones.",
        engagementGuidance: "Use decision options, cost boundaries, and measurable operating outcomes rather than an AWS service tour.",
        source: "Customer-approved profile notes",
        context: "Economic buyer who supports the program if the team can bound scope and show how faster payroll-partner onboarding creates measurable value.",
      },
      {
        name: "Dev Malik",
        roleType: "decision-maker",
        title: "VP Infrastructure and Resilience",
        organizationalRole: "Technical decision-maker",
        decisionAuthority: "Technical direction, reliability acceptance, and production-readiness approval",
        priorities: "Existing AWS architecture reuse, idempotent interfaces, failure testing, operational ownership, and recovery evidence.",
        concerns: "Duplicate payment instructions, brittle replay behavior, unclear MSK ownership, and unsupported RTO or RPO claims.",
        successMeasures: "A validated interface pattern, failure-mode evidence, named owners, and approved production-readiness gates.",
        engagementGuidance: "Bring current-state diagrams and testable tradeoffs. Separate confirmed architecture from hypotheses.",
        source: "Customer-approved profile notes",
        context: "Technical authority for the existing AWS payment platform and the resilience evidence required before payroll interfaces can enter production.",
      },
      {
        name: "Rachel Kim",
        roleType: "decision-maker",
        title: "Chief Risk and Compliance Officer",
        organizationalRole: "Security and compliance decision-maker",
        decisionAuthority: "Control acceptance, retention decision, and risk exception approval",
        priorities: "Least privilege, data minimization, attributable privileged access, retention clarity, and audit-ready evidence.",
        concerns: "Raw payroll data in logs, inherited retention assumptions, broad support access, and compliance evidence created after delivery.",
        successMeasures: "Approved data classification and retention decisions with control evidence attached to the release gate.",
        engagementGuidance: "State what is known, ask for the evidence threshold, and avoid certification or compliance claims.",
        source: "Customer-approved profile notes",
        context: "Control approver who will not accept the launch until payroll-data handling, privileged access, and retention ownership are explicit.",
      },
      {
        name: "Priya Shah",
        roleType: "stakeholder",
        title: "Director of Payment Operations",
        organizationalRole: "Application and operational owner",
        influence: "high",
        stance: "supportive",
        priorities: "Reconciliation accuracy, exception visibility, payroll cutoffs, and unambiguous runbook ownership.",
        concerns: "Late acknowledgements, duplicate files, exception queues without owners, and dashboards that expose sensitive fields.",
        successMeasures: "Exceptions visible before the next payroll cutoff with daily reconciliation outcomes owned by Payment Operations.",
        engagementGuidance: "Validate the operating model and exception workflow before finalizing the integration design.",
        source: "Customer-approved profile notes",
        context: "Operational owner and subject-matter expert. She strongly influences acceptance but does not hold final funding authority.",
      },
      {
        name: "Elena Torres",
        roleType: "stakeholder",
        title: "Payroll Partnerships Lead",
        organizationalRole: "Internal champion and partner owner",
        influence: "high",
        stance: "champion",
        priorities: "Partner certification windows, a consistent status model, clear evidence requests, and faster onboarding.",
        concerns: "Each provider requiring a bespoke process and technical evidence arriving after certification windows close.",
        successMeasures: "Two design partners complete certification with one repeatable onboarding playbook.",
        engagementGuidance: "Use her partner knowledge to shape interface requirements and make her the owner of external coordination.",
        source: "Customer-approved profile notes",
        context: "Internal champion connecting the technical program to partner commitments and certification schedules.",
      },
      {
        name: "Noah Grant",
        roleType: "stakeholder",
        title: "Director of Strategic Procurement",
        organizationalRole: "Finance and procurement reviewer",
        influence: "medium",
        stance: "neutral",
        priorities: "Commercial clarity, vendor accountability, support boundaries, and predictable renewal impact.",
        concerns: "Unplanned tooling commitments, vague support ownership, and partner contracts that do not match technical responsibilities.",
        successMeasures: "Commercial dependencies and vendor responsibilities documented before the pilot commitment.",
        engagementGuidance: "Bring a bounded dependency list and involve procurement before external commitments are made.",
        source: "Customer-approved profile notes",
        context: "Commercial reviewer who can delay the schedule even though he does not approve the target architecture.",
      },
      {
        name: "Omar Fields",
        roleType: "stakeholder",
        title: "Platform Engineering Lead",
        organizationalRole: "Technical evaluator and potential blocker",
        influence: "high",
        stance: "skeptical",
        priorities: "Low operational burden, reusable interface patterns, clear ownership, and credible on-call capacity.",
        concerns: "A rushed deadline, custom adapters for every partner, insufficient replay tooling, and another service without staffing.",
        successMeasures: "A supportable reference pattern with capacity, runbooks, test evidence, and explicit service ownership.",
        engagementGuidance: "Treat objections as design constraints, involve him in the failure-mode workshop, and avoid presenting a finished architecture.",
        source: "Customer-approved profile notes",
        context: "High-influence technical evaluator. He can block implementation readiness but does not own executive funding or risk acceptance.",
      },
    ],
    meetingNotes:
      "BlueMesa has approved discovery for a bounded payroll-partner integration on its existing AWS payment platform. The team must validate mixed API and encrypted-file interfaces, idempotency, status events, reconciliation ownership, data classification, retention, privileged access, partner certification, availability during payroll windows, and recovery evidence. Ledger replacement and broad platform redesign are outside the first release. The upcoming call must confirm measurable outcomes, unresolved control decisions, named owners, and the evidence required for the architecture workshop.",
    challenge: "Governed payroll integration on an existing AWS platform",
    winTheme: "Onboard payroll partners faster without weakening settlement accuracy, operational ownership, or control evidence.",
  },
  {
    id: "apex",
    name: "Financial modernization",
    company: "Apex Mutual",
    industry: "Financial Services",
    meetingType: "Executive Briefing",
    companySize: "Enterprise",
    pillars: ["Security", "Reliability", "Cost Optimization"],
    context:
      "Apex Mutual is modernizing its customer portal, policyholder self-service flows, and document-delivery experience while keeping audit evidence, identity boundaries, and release governance intact. Leadership wants a clearer migration path that avoids peak enrollment disruption, reduces manual exception handling for service teams, and creates a board-ready story around risk reduction, cost transparency, and delivery confidence across the first two migration waves.",
    companyValues: "Customer trust, controlled modernization, visible governance, clean auditability, and predictable change windows. Apex wants faster delivery only when controls, rollback confidence, and executive reporting stay intact, and they care about showing measurable progress without creating noise for policyholders or internal service teams.",
    companyValuesUrl: "https://www.apexmutual.example/about/values",
    additionalDirection: "Include that the portal modernization must interface with payroll and HR systems for employee benefit deductions, identity handoffs, data privacy, reconciliation, and cutover planning.",
    decisionMakers: [
      {
        name: "Lena Ortiz",
        roleType: "decision-maker",
        title: "CIO",
        source: "Customer-approved profile notes",
        context:
          "Prior notes emphasize board visibility, customer trust, modernization governance, a phased migration story, and proving that each release wave improves policyholder experience without introducing an executive-escalation event. She cares about whether the team can explain progress in business language, show that operational noise is going down, and demonstrate that customer-service teams will not be surprised by rollout decisions.",
      },
      {
        name: "Marcus Reed",
        roleType: "decision-maker",
        title: "CISO",
        source: "Customer-approved profile notes",
        context:
          "Security leadership has focused on audit evidence, identity boundaries, data residency, incident readiness, privileged-access review, and proving that any portal modernization preserves clean handoffs between business operations, IAM policy, and compliance reporting. He is especially sensitive to access drift, unclear ownership after cutover, and launch plans that treat audit documentation as a cleanup task instead of a release requirement.",
      },
    ],
    meetingNotes:
      "The CIO wants an executive-ready modernization path with clear pilot scope, success metrics, and board language around customer trust. Security asked for identity boundaries, audit evidence, rollback criteria, and a migration pilot before committing to a broader program. Operations wants fewer manual fulfillment exceptions, cleaner release coordination, and better visibility into where customer-facing friction is created across the current portal stack. Product leaders also want the future-state story to include faster launch cycles for policy updates, fewer service escalations after releases, and a visible way to measure whether each migration wave is actually reducing business drag.",
    challenge: "Risk-sensitive modernization",
    winTheme: "Move faster without weakening trust or auditability.",
  },
  {
    id: "northstar",
    name: "Healthcare continuity",
    company: "Northstar Health",
    industry: "Healthcare",
    meetingType: "Technical Deep Dive",
    companySize: "Enterprise",
    pillars: ["Security", "Reliability", "Operational Excellence"],
    context:
      "Hospital network is consolidating patient scheduling systems and needs stronger disaster recovery, lower support burden, and clear compliance controls.",
    companyValues: "Patient-first reliability, clinical continuity, privacy protection, and simpler frontline operations.",
    companyValuesUrl: "https://www.northstarhealth.example/about/mission-values",
    additionalDirection: "Include integration with patient scheduling, clinical identity, and downstream notification workflows in the technical questions.",
    decisionMakers: [
      {
        name: "Priya Shah",
        roleType: "decision-maker",
        title: "VP Patient Access",
        source: "Customer-approved profile notes",
        context:
          "Public themes center on patient access, scheduling reliability, care team efficiency, and minimizing disruption during system changes.",
      },
      {
        name: "Daniel Brooks",
        roleType: "stakeholder",
        organizationalRole: "Technical evaluator",
        influence: "high",
        stance: "supportive",
        title: "Director of Enterprise Architecture",
        source: "Customer-approved profile notes",
        context:
          "Architecture notes emphasize interoperability, resilient integration patterns, and reducing manual operational support.",
      },
    ],
    meetingNotes:
      "Architecture team needs RTO/RPO options, data classification, and phased cutover patterns. Compliance team wants explicit evidence paths and fewer manual review steps.",
    challenge: "Patient-facing availability",
    winTheme: "Protect patient access while simplifying operations.",
  },
  {
    id: "peakcart",
    name: "Retail peak season",
    company: "PeakCart Retail",
    industry: "Retail",
    meetingType: "Discovery Call",
    companySize: "Mid-market",
    pillars: ["Performance Efficiency", "Cost Optimization", "Reliability"],
    context:
      "Digital commerce team is preparing for peak season. They need better elasticity, fewer checkout incidents, and a clearer cost story for executive sponsors.",
    companyValues: "Fast customer experience, confident launches, revenue protection, and disciplined spend.",
    companyValuesUrl: "https://www.peakcart.example/company/values",
    additionalDirection: "Include loyalty platform integration, promotion launch timing, and checkout reconciliation as explicit business dependencies.",
    decisionMakers: [
      {
        name: "Emma Chen",
        roleType: "decision-maker",
        title: "VP Digital",
        source: "Customer-approved profile notes",
        context:
          "Recent themes focus on conversion, faster campaign launches, loyalty growth, and protecting customer experience during peak traffic.",
      },
      {
        name: "Luis Ramirez",
        roleType: "stakeholder",
        organizationalRole: "Technical evaluator and implementation champion",
        influence: "high",
        stance: "champion",
        title: "Platform Engineering Lead",
        source: "Customer-approved profile notes",
        context:
          "Engineering priorities include rollback confidence, load-test evidence, observability, and predictable cloud spend.",
      },
    ],
    meetingNotes:
      "VP of Digital cares about conversion and launch speed. Engineering wants load-test targets, rollback patterns, and cost controls before seasonal traffic ramps.",
    challenge: "Elastic customer experience",
    winTheme: "Keep checkout fast, reliable, and cost-aware during traffic spikes.",
  },
];

const pillars = [
  {
    id: "Operational Excellence",
    short: "Ops",
    tone: "Improve operating rhythm and measurable ownership.",
    risk: "Medium" as RiskLevel,
    color: "bg-cyan-500",
  },
  {
    id: "Security",
    short: "Security",
    tone: "Protect identities, data, and customer trust.",
    risk: "High" as RiskLevel,
    color: "bg-red-500",
  },
  {
    id: "Reliability",
    short: "Reliability",
    tone: "Recover quickly and reduce customer-facing disruption.",
    risk: "High" as RiskLevel,
    color: "bg-amber-500",
  },
  {
    id: "Performance Efficiency",
    short: "Performance",
    tone: "Keep latency low while demand changes.",
    risk: "Medium" as RiskLevel,
    color: "bg-blue-500",
  },
  {
    id: "Cost Optimization",
    short: "Cost",
    tone: "Connect spend to outcomes and unit economics.",
    risk: "High" as RiskLevel,
    color: "bg-sky-600",
  },
  {
    id: "Sustainability",
    short: "Sustainability",
    tone: "Right-size resources and reduce waste.",
    risk: "Low" as RiskLevel,
    color: "bg-slate-500",
  },
];

function normalizePillarRanking(items: string[] | undefined) {
  const knownPillars = new Set(pillars.map((pillar) => pillar.id));
  const ranked: string[] = [];

  for (const item of items ?? []) {
    if (knownPillars.has(item) && !ranked.includes(item)) {
      ranked.push(item);
    }
  }

  for (const pillar of pillars) {
    if (!ranked.includes(pillar.id)) {
      ranked.push(pillar.id);
    }
  }

  return ranked;
}

function buildPillarRanking(items: string[]) {
  return items.map((pillar, index) => ({
    rank: index + 1,
    pillar,
  }));
}

function buildApprovedBriefSnapshot(brief: BriefResponse | null): BriefRequest["approvedBrief"] {
  if (!brief) {
    return undefined;
  }

  return {
    businessCase: { ...brief.businessCase },
    technical: [...brief.technical],
    executive: [...brief.executive],
    stakeholders: [...brief.stakeholders],
    gameplan: [...brief.gameplan],
    objections: [...brief.objections],
    citations: [...brief.citations],
    evidence: brief.evidence?.map((item) => ({ ...item, sources: [...item.sources] })),
    sourceCatalog: brief.sourceCatalog?.map((source) => ({ ...source })),
    claims: brief.claims?.map((claim) => ({ ...claim, sourceIds: [...claim.sourceIds] })),
    evidenceCoverage: brief.evidenceCoverage
      ? {
          ...brief.evidenceCoverage,
          statusCounts: { ...brief.evidenceCoverage.statusCounts },
        }
      : undefined,
    projectAnswer: brief.projectAnswer,
    projectArtifacts: brief.projectArtifacts
      ? JSON.parse(JSON.stringify(brief.projectArtifacts))
      : undefined,
  };
}

function structuredFeedback(items: string[]) {
  return items.map((value) => {
    const separator = value.indexOf(":");
    return separator > 0
      ? {
          category: value.slice(0, separator).trim(),
          instruction: value.slice(separator + 1).trim(),
        }
      : { category: "Additional direction", instruction: value.trim() };
  });
}
const feedbackCategories = [
  {
    title: "Executive lens",
    description: "Make the brief sharper for business sponsors and decision makers.",
    options: [
      "Make it more board-ready",
      "Reduce AWS jargon",
      "Add ROI and decision criteria",
      "Tighten the executive summary",
    ],
  },
  {
    title: "Technical depth",
    description: "Push the technical brief toward architecture validation and tradeoffs.",
    options: [
      "Add stronger technical depth",
      "Ask deeper architecture questions",
      "Show likely current-state assumptions",
      "Name AWS services with rationale",
    ],
  },
  {
    title: "Risk and compliance",
    description: "Emphasize controls, evidence, resilience, and approval blockers.",
    options: [
      "Lead with security and evidence",
      "Add compliance validation questions",
      "Strengthen RTO and RPO discovery",
      "Surface migration risk and rollback",
    ],
  },
  {
    title: "Cost and value",
    description: "Make the output stronger on economics and measurable outcomes.",
    options: [
      "Add cost angle",
      "Add time-to-value framing",
      "Include success metrics",
      "Separate quick wins from later bets",
    ],
  },
  {
    title: "Customer context",
    description: "Tell the model how to frame the customer starting point.",
    options: [
      "Customer is already on AWS",
      "Customer is migrating from on-prem",
      "Customer has a hybrid environment",
      "Customer has executive urgency",
    ],
  },
  {
    title: "Meeting execution",
    description: "Improve what the SA can actually say and ask live.",
    options: [
      "Improve discovery questions",
      "Add objection handling",
      "Create a tighter meeting agenda",
      "Clarify next-step owners",
    ],
  },
];

const technicalFeedbackCategories = [
  {
    title: "Current architecture",
    description: "Correct the starting point and identify the systems that already exist.",
    options: [
      "Customer is already on AWS",
      "Add current AWS services",
      "Add existing non-AWS systems",
      "Clarify cloud, hybrid, or legacy boundaries",
    ],
  },
  {
    title: "Integrations and data",
    description: "Make interfaces, ownership, and data movement explicit.",
    options: [
      "Add integrations and data flows",
      "Add API and file interface questions",
      "Add identity and data-boundary questions",
      "Add reconciliation and cutover dependencies",
    ],
  },
  {
    title: "Security and resilience",
    description: "Deepen controls, evidence, availability, and recovery discovery.",
    options: [
      "Add security and compliance requirements",
      "Strengthen RTO and RPO discovery",
      "Add failure-mode and rollback questions",
      "Add observability and evidence ownership",
    ],
  },
  {
    title: "Scale and decisions",
    description: "Connect workload behavior to constraints and the next technical gate.",
    options: [
      "Add performance and scaling constraints",
      "Add architecture tradeoffs",
      "Add technical unknowns requiring discovery",
      "Clarify the next technical decision gate",
    ],
  },
];

const legacyFeedbackMap: Record<string, string> = {
  "Make it more executive": "Executive lens: Make it more board-ready",
  "Add stronger technical depth": "Technical depth: Add stronger technical depth",
  "Reduce AWS jargon": "Executive lens: Reduce AWS jargon",
  "Focus on security": "Risk and compliance: Lead with security and evidence",
  "Add cost angle": "Cost and value: Add cost angle",
  "Improve discovery questions": "Meeting execution: Improve discovery questions",
  "Customer is already on AWS": "Customer context: Customer is already on AWS",
  "Customer is migrating from on-prem": "Customer context: Customer is migrating from on-prem",
};

const defaultFeedback = [
  "Executive lens: Make it more board-ready",
  "Risk and compliance: Lead with security and evidence",
];

function normalizeFeedback(items: unknown) {
  if (!Array.isArray(items)) {
    return defaultFeedback;
  }

  const normalized = items
    .filter((item): item is string => typeof item === "string" && Boolean(item.trim()))
    .map((item) => legacyFeedbackMap[item] ?? item)
    .filter((item, index, list) => list.indexOf(item) === index);

  return normalized.length ? normalized : defaultFeedback;
}
const defaultRole: AudienceRole = "PM";
const workspaceStorageKey = "pillarprep.workspace.v2";
const legacyWorkspaceStorageKey = "pillarprep.workspace.v1";
const legacyBlueMesaAdditionalDirection =
  "Treat BlueMesa as an existing AWS customer. Make payroll integration, mixed API and encrypted-file interfaces, idempotency, reconciliation, data privacy, retention, partner certification, cutover, and recovery evidence explicit. The existing ledger replacement is out of scope.";
const hostedJobsUrl = (import.meta.env.VITE_PILLARPREP_JOBS_API_URL ?? "").trim();
const hostedWorkspaceUrl = (
  import.meta.env.VITE_PILLARPREP_WORKSPACE_API_URL ?? hostedJobsUrl
).trim();
const hostedBackendRegion = (import.meta.env.VITE_PILLARPREP_BACKEND_REGION ?? "us-east-1").trim();
const hostedIdentityPoolId = (import.meta.env.VITE_PILLARPREP_COGNITO_IDENTITY_POOL_ID ?? "").trim();
const hostedUserPoolClientId = (
  import.meta.env.VITE_PILLARPREP_COGNITO_USER_POOL_CLIENT_ID ?? ""
).trim();
const hostedLoginDomain = (
  import.meta.env.VITE_PILLARPREP_COGNITO_LOGIN_DOMAIN ?? ""
).trim();
const workspaceLoginAvailable = Boolean(hostedUserPoolClientId && hostedLoginDomain);
const hostedJobsMode = Boolean(hostedJobsUrl && hostedIdentityPoolId);
const liveModeAvailable = hostedJobsMode;
const rolePrompts: Record<AudienceRole, string[]> = {
  Sales: [
    "What should we say in the follow-up email?",
    "Which outcome should we lead with?",
    "What objections should we prepare for?",
  ],
  "Solutions Architect": [
    "What architecture assumptions must I validate?",
    "Which customer evidence should I request?",
    "What should the next technical session decide?",
  ],
  Executive: [
    "Summarize the project in 60 seconds.",
    "What business risks are we reducing?",
    "What decisions need sponsor alignment?",
  ],
  PM: [
    "Create the first two-week plan.",
    "What dependencies should I track?",
    "Which decisions are still open?",
  ],
  Engineer: [
    "What should we build first?",
    "What AWS services are in scope?",
    "What assumptions need validation?",
  ],
  "New member": [
    "What is this project about?",
    "What did the customer care about?",
    "Where should I start?",
  ],
};


const packetOutputs = [
  {
    title: "Business case",
    key: "businessCase",
    detail: "Scenario, desired outcomes, meeting scope, alignment language, and success criteria.",
  },
  {
    title: "Technical brief",
    key: "technical",
    detail: "Architecture assumptions, risk areas, service references, and deep-dive questions.",
  },
  {
    title: "Executive brief",
    key: "executive",
    detail: "Business context, outcome framing, success criteria, and low-jargon questions.",
  },
  {
    title: "Decision-maker lens",
    key: "stakeholders",
    detail: "Approved stakeholder context, likely priorities, tailored questions, and influence notes.",
  },
  {
    title: "SA game plan",
    key: "gameplan",
    detail: "Meeting objective, talk track, likely objections, and closeout checklist.",
  },
  {
    title: "Project handoff",
    key: "handoff",
    detail: "Notes, decisions, owners, risks, timeline, and role-aware follow-on answers.",
  },
] as const;

const lifecycleStageIds: LifecycleStageId[] = [
  "research",
  "insights",
  "discovery",
  "meeting-prep",
  "follow-up",
];

const lifecycleRoutes: Record<
  LifecycleStageId,
  { page: ConsolePage; sectionId: string }
> = {
  research: { page: "setup", sectionId: "setup" },
  insights: { page: "brief", sectionId: "brief-review-section" },
  discovery: { page: "brief", sectionId: "brief-review-section" },
  "meeting-prep": { page: "project", sectionId: "project-meeting-prep-section" },
  "follow-up": { page: "project", sectionId: "project-follow-up-section" },
};

function restoreLifecycleStage(value: unknown): LifecycleStageId | null {
  if (typeof value !== "string") return null;
  if (lifecycleStageIds.includes(value as LifecycleStageId)) return value as LifecycleStageId;
  return {
    prepare: "research",
    refine: "insights",
    "sa-ready": "meeting-prep",
    meet: "meeting-prep",
    update: "follow-up",
    advance: "follow-up",
  }[value] as LifecycleStageId | undefined ?? null;
}

const opportunityGateDefinitions = [
  { id: "business", name: "Business alignment" },
  { id: "technical", name: "Technical validation" },
  { id: "security", name: "Security and compliance" },
  { id: "integration", name: "Data and integration readiness" },
  { id: "commercial", name: "Commercial and procurement" },
  { id: "executive", name: "Executive sponsorship" },
  { id: "implementation", name: "Implementation readiness" },
] as const;

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

function activePipelineStatus(
  status: PipelineJobState
): ActivePipelineStatus | null {
  return status === "queued" ||
    status === "running" ||
    status === "validating" ||
    status === "saving"
    ? status
    : null;
}

function ProcessingIndicator({
  label,
  tone = "light",
  announce = true,
  compact = false,
}: {
  label: string;
  tone?: "light" | "dark";
  announce?: boolean;
  compact?: boolean;
}) {
  return (
    <span
      className={cx(
        "processing-indicator",
        tone === "dark" && "processing-indicator-dark",
        compact && "processing-indicator-compact"
      )}
      role={announce ? "status" : undefined}
      aria-live={announce ? "polite" : undefined}
    >
      <svg className="processing-clock" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
        <circle cx="12" cy="12" r="8.5" />
        <path className="processing-clock-hour" d="M12 12V7.5" />
        <path className="processing-clock-minute" d="M12 12h4" />
        <circle className="processing-clock-pin" cx="12" cy="12" r="1" />
      </svg>
      <strong>{label}</strong>
    </span>
  );
}

function providerLabel(provider: BriefResponse["provider"]) {
  if (provider === "bedrock") {
    return "Bedrock AI";
  }

  if (provider === "agentcore") {
    return "AgentCore + Strands";
  }

  if (provider === "strands") {
    return "Strands agent";
  }

  return "Local fallback";
}

function fallbackBriefForRequest(request: BriefRequest) {
  const response =
    request.company.trim().toLowerCase() === "bluemesa payments"
      ? generateBlueMesaBackupBrief(request)
      : generateDemoBrief(request);
  return normalizeBriefResponse(response, "demo");
}
function isPublicDemoAccessError(error: unknown) {
  const message = error instanceof Error ? error.message.toLowerCase() : String(error).toLowerCase();
  return (
    message.includes("unauthorized") ||
    message.includes("forbidden") ||
    message.includes("not assigned to this client") ||
    message.includes("iam") ||
    message.includes("cognito") ||
    message.includes("credentials") ||
    message.includes("failed to fetch") ||
    message.includes("networkerror")
  );
}

function publicDemoFallbackNotice(error: unknown) {
  const detail = error instanceof Error ? error.message : "Live AWS access was unavailable.";
  return (
    "Live AWS access was unavailable for this viewer, so PilarPrep generated the safe public demo packet locally. " +
    "Authenticated users still use the IAM-signed AWS pipeline. Detail: " +
    detail
  );
}
function briefTabLabel(tab: BriefTab) {
  if (tab === "businessCase") {
    return "Business case";
  }

  if (tab === "gameplan") {
    return "SA game plan";
  }

  if (tab === "stakeholders") {
    return "Stakeholder lens";
  }

  if (tab === "objections") {
    return "Objection simulator";
  }

  return tab === "technical" ? "Technical brief" : "Executive brief";
}

const briefSectionHeadings: Record<Exclude<BriefTab, "businessCase">, string[]> = {
  technical: [
    "Current architecture and assumptions",
    "Requirements, constraints, and evidence",
    "AWS options and tradeoffs",
    "Discovery questions and decision gates",
  ],
  executive: [
    "Business context and urgency",
    "Value and measurable outcomes",
    "Sponsor priorities and approval criteria",
    "Investment framing and next decision",
  ],
  stakeholders: [
    "Decision authority and priorities",
    "Influence, evidence, and blockers",
    "Alignment and approval path",
    "Engagement plan and next action",
  ],
  gameplan: [
    "Open and align",
    "Discover and validate",
    "Read back decisions and risks",
    "Close with owners and next steps",
  ],
  objections: [
    "Missing context and evidence",
    "AWS relevance and business value",
    "Stakeholder priorities and confidence",
    "Generated-content accuracy",
  ],
};

function briefSectionHeading(tab: BriefTab, index: number) {
  if (tab === "businessCase") {
    return businessCaseFields[index]?.label ?? `Business case item ${index + 1}`;
  }

  return briefSectionHeadings[tab][index] ?? `${briefTabLabel(tab)} section ${index + 1}`;
}

function cloneDecisionMakers(decisionMakers: DecisionMakerContext[]): DecisionMakerContext[] {
  return decisionMakers.map((person) => ({
    ...person,
    roleType: (person.roleType === "stakeholder" ? "stakeholder" : "decision-maker") as PersonRoleType,
  }));
}

export default function App() {
  const [scenarioId, setScenarioId] = useState("apex");
  const activeScenario =
    scenarios.find((scenario) => scenario.id === scenarioId) ?? scenarios[0];
  const [company, setCompany] = useState(activeScenario.company);
  const [industry, setIndustry] = useState(activeScenario.industry);
  const [meetingType, setMeetingType] = useState(activeScenario.meetingType);
  const [companySize, setCompanySize] = useState(activeScenario.companySize);
  const [selectedPillars, setSelectedPillars] = useState(() =>
    normalizePillarRanking(activeScenario.pillars)
  );
  const [draggedPillar, setDraggedPillar] = useState<string | null>(null);
  const [context, setContext] = useState(activeScenario.context);
  const [companyValues, setCompanyValues] = useState(activeScenario.companyValues);
  const [companyValuesUrl, setCompanyValuesUrl] = useState(activeScenario.companyValuesUrl);
  const [additionalDirection, setAdditionalDirection] = useState(activeScenario.additionalDirection);
  const [decisionMakers, setDecisionMakers] = useState<DecisionMakerContext[]>(
    () => cloneDecisionMakers(activeScenario.decisionMakers)
  );
  const [peopleView, setPeopleView] = useState<PeopleView>("decision-maker");
  const [meetingNotes, setMeetingNotes] = useState(
    activeScenario.meetingNotes
  );
  const [activeTab, setActiveTab] = useState<BriefTab>("businessCase");
  const [reviewMode, setReviewMode] = useState<BriefReviewMode>("clean");
  const [briefVersion, setBriefVersion] = useState(0);
  const [refinementDrafts, setRefinementDrafts] = useState(createRefinementDrafts);
  const [appliedRefinementDrafts, setAppliedRefinementDrafts] = useState(createRefinementDrafts);
  const [refiningTarget, setRefiningTarget] = useState<BriefTab | null>(null);
  const [approved, setApproved] = useState(false);
  const [approvalStale, setApprovalStale] = useState(false);
  const [promoted, setPromoted] = useState(false);
  const [precallHandoffStatus, setPrecallHandoffStatus] =
    useState<PrecallHandoffStatus>("idle");
  const [precallHandoffError, setPrecallHandoffError] = useState("");
  const [role, setRole] = useState<AudienceRole>(defaultRole);
  const [activePrompt, setActivePrompt] = useState(rolePrompts[defaultRole][0]);
  const [generatedBrief, setGeneratedBrief] = useState<BriefResponse | null>(null);
  const [briefHistory, setBriefHistory] = useState<BriefHistoryEntry[]>([]);
  const [serverBriefHistory, setServerBriefHistory] = useState<BriefHistoryEntry[]>([]);
  const [isLibraryLoading, setIsLibraryLoading] = useState(hostedJobsMode);
  const [selectedHistoryId, setSelectedHistoryId] = useState<string | null>(null);
  const [isGenerating, setIsGenerating] = useState(false);
  const [generationStageMode, setGenerationStageMode] = useState<GenerationStageMode>("prebrief");
  const [pipelineJobStatus, setPipelineJobStatus] = useState<ActivePipelineStatus | null>(null);
  const [generationError, setGenerationError] = useState("");
  const [generationNotice, setGenerationNotice] = useState("");
  const [workspaceLoaded, setWorkspaceLoaded] = useState(false);
  const [generationMode, setGenerationMode] = useState<GenerationMode>(
    liveModeAvailable ? "live" : "demo"
  );
  const [modelPreference, setModelPreference] = useState<LiveModelPreference>("nova-pro");
  const authConfig = useMemo<CognitoAuthConfig | null>(() => {
    if (!workspaceLoginAvailable || typeof window === "undefined") {
      return null;
    }
    return {
      domain: hostedLoginDomain,
      clientId: hostedUserPoolClientId,
      redirectUri: window.location.origin,
      logoutUri: window.location.origin,
    };
  }, []);
  const [authSession, setAuthSession] = useState<CognitoAuthSession | null>(null);
  const [authReady, setAuthReady] = useState(!workspaceLoginAvailable);
  const [authError, setAuthError] = useState("");
  const [evidenceDocuments, setEvidenceDocuments] = useState<EvidenceDocumentRecord[]>([]);
  const [selectedEvidenceSourceId, setSelectedEvidenceSourceId] = useState("");
  const [selectedEvidenceClaimId, setSelectedEvidenceClaimId] = useState("");
  const [isEvidenceLoading, setIsEvidenceLoading] = useState(false);
  const [evidenceBusyDocumentId, setEvidenceBusyDocumentId] = useState("");
  const [evidenceError, setEvidenceError] = useState("");
  const [evidenceNotice, setEvidenceNotice] = useState("");

  const hostedCredentials = useMemo(
    () =>
      hostedJobsMode
        ? cognitoIdentityCredentialsProvider({
            region: hostedBackendRegion,
            identityPoolId: hostedIdentityPoolId,
          })
        : null,
    []
  );
  const [copiedLabel, setCopiedLabel] = useState("");
  const copyFeedbackTimeoutRef = useRef<number | null>(null);
  const historyEntryCounterRef = useRef(0);
  const agentSessionIdRef = useRef("");
  const generationRequestRef = useRef(false);
  const pipelineAbortRef = useRef<AbortController | null>(null);
  const packetRequestEpochRef = useRef(0);
  const activeTabRef = useRef<BriefTab>("businessCase");
  const catchupRequestRef = useRef(false);
  const catchupAbortRef = useRef<AbortController | null>(null);
  const [activePage, setActivePage] = useState<ConsolePage>("setup");
  const [pendingSectionId, setPendingSectionId] = useState<string | null>(null);
  const [selectedLifecycleStage, setSelectedLifecycleStage] =
    useState<LifecycleStageId>("research");
  const [gateDecisions, setGateDecisions] = useState<Record<string, GateDecision>>({});
  const [catchupFilter, setCatchupFilter] = useState<CatchupFilter>("all");
  const [catchupAnswer, setCatchupAnswer] = useState("");
  const [catchupError, setCatchupError] = useState("");
  const [catchupSource, setCatchupSource] = useState("");
  const [isCatchupGenerating, setIsCatchupGenerating] = useState(false);
  const [catchupJobStatus, setCatchupJobStatus] = useState<ActivePipelineStatus | null>(null);
  const [meetingResult, setMeetingResult] = useState<MeetingProcessResult | null>(null);
  const [meetingAudio, setMeetingAudio] = useState<MeetingAudioSelection>({
    fileName: "",
    sizeBytes: 0,
    status: "empty",
  });
  const [meetingAudioUploadId, setMeetingAudioUploadId] = useState("");
  const [meetingDecisions, setMeetingDecisions] = useState<MeetingDecisionMap>({});
  const [meetingJobStatus, setMeetingJobStatus] = useState<PipelineJobState | null>(null);
  const [meetingNotice, setMeetingNotice] = useState("");
  const [meetingError, setMeetingError] = useState("");
  const [isMeetingProcessing, setIsMeetingProcessing] = useState(false);
  const [isMeetingApproving, setIsMeetingApproving] = useState(false);
  const meetingRequestRef = useRef(false);
  const meetingAbortRef = useRef<AbortController | null>(null);
  const meetingUploadAbortRef = useRef<AbortController | null>(null);
  const previousActivePageRef = useRef<ConsolePage>(activePage);

  useEffect(() => {
    if (previousActivePageRef.current === activePage) {
      return;
    }

    previousActivePageRef.current = activePage;
    const frame = window.requestAnimationFrame(() => {
      window.scrollTo({ top: 0, left: 0, behavior: "auto" });
      document
        .querySelectorAll<HTMLElement>(".page-view, .refinement-panel, .brief-surface")
        .forEach((element) => {
          element.scrollTop = 0;
          element.scrollLeft = 0;
        });
    });

    return () => window.cancelAnimationFrame(frame);
  }, [activePage]);

  const activeRefinementDraft = refinementDrafts[activeTab];
  const feedback = activeRefinementDraft.feedback;
  const feedbackNotes = activeRefinementDraft.feedbackNotes;
  const activeFeedbackCategories =
    activeTab === "technical" ? technicalFeedbackCategories : feedbackCategories;

  function clearCopyFeedback() {
    if (copyFeedbackTimeoutRef.current !== null) {
      window.clearTimeout(copyFeedbackTimeoutRef.current);
      copyFeedbackTimeoutRef.current = null;
    }

    setCopiedLabel("");
  }

  useEffect(() => {
    if (!authConfig) {
      return;
    }
    let cancelled = false;
    void completeCognitoLogin(authConfig)
      .then((session) => {
        if (!cancelled) {
          setAuthSession(session);
          setAuthError("");
          setAuthReady(true);
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setAuthError(error instanceof Error ? error.message : "Sign-in failed.");
          setAuthReady(true);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [authConfig]);

  useEffect(() => {
    return () => {
      if (copyFeedbackTimeoutRef.current !== null) {
        window.clearTimeout(copyFeedbackTimeoutRef.current);
      }
      pipelineAbortRef.current?.abort(
        new DOMException("The PilarPrep workspace closed.", "AbortError")
      );
      catchupAbortRef.current?.abort(
        new DOMException("The PilarPrep workspace closed.", "AbortError")
      );
      meetingAbortRef.current?.abort(
        new DOMException("The PilarPrep workspace closed.", "AbortError")
      );
      meetingUploadAbortRef.current?.abort(
        new DOMException("The PilarPrep workspace closed.", "AbortError")
      );
    };
  }, []);

  useEffect(() => {
    activeTabRef.current = activeTab;
  }, [activeTab]);

  useEffect(() => {
    // Restore the saved workspace once after mounting.
    try {
      const rawWorkspace = window.localStorage.getItem(workspaceStorageKey);

      if (!rawWorkspace) {
        window.localStorage.removeItem(legacyWorkspaceStorageKey);
        return;
      }

      const saved = JSON.parse(rawWorkspace) as Record<string, unknown>;
      const savedScenarioId =
        typeof saved.scenarioId === "string" &&
        scenarios.some((scenario) => scenario.id === saved.scenarioId)
          ? saved.scenarioId
          : scenarioId;
      const savedRole =
        typeof saved.role === "string" && saved.role in rolePrompts
          ? (saved.role as AudienceRole)
          : defaultRole;
      const savedActiveTab =
        typeof saved.activeTab === "string" &&
        refinementTargets.includes(saved.activeTab as BriefTab)
          ? (saved.activeTab as BriefTab)
          : "businessCase";
      const savedScenario =
        scenarios.find((scenario) => scenario.id === savedScenarioId) ??
        activeScenario;
      const savedAdditionalDirection =
        typeof saved.additionalDirection === "string"
          ? saved.additionalDirection
          : savedScenario.additionalDirection;
      const restoredAdditionalDirection =
        savedScenarioId === "bluemesa" &&
        savedAdditionalDirection.trim() === legacyBlueMesaAdditionalDirection
          ? savedScenario.additionalDirection
          : savedAdditionalDirection;

      setScenarioId(savedScenarioId);
      setCompany(typeof saved.company === "string" ? saved.company : company);
      setIndustry(typeof saved.industry === "string" ? saved.industry : industry);
      setMeetingType(
        typeof saved.meetingType === "string" ? saved.meetingType : meetingType
      );
      setCompanySize(
        typeof saved.companySize === "string" ? saved.companySize : companySize
      );
      setSelectedPillars(
        Array.isArray(saved.selectedPillars)
          ? normalizePillarRanking(
              saved.selectedPillars.filter(
                (pillar): pillar is string => typeof pillar === "string"
              )
            )
          : normalizePillarRanking(selectedPillars)
      );
      setContext(typeof saved.context === "string" ? saved.context : context);
      setCompanyValues(typeof saved.companyValues === "string" ? saved.companyValues : activeScenario.companyValues);
      setCompanyValuesUrl(typeof saved.companyValuesUrl === "string" ? saved.companyValuesUrl : activeScenario.companyValuesUrl);
      setAdditionalDirection(restoredAdditionalDirection);
      setDecisionMakers(
        Array.isArray(saved.decisionMakers)
          ? saved.decisionMakers
              .filter(
                (person): person is Record<string, unknown> =>
                  typeof person === "object" && person !== null
              )
              .map((person) => ({
                name: typeof person.name === "string" ? person.name : "",
                title: typeof person.title === "string" ? person.title : "",
                source: typeof person.source === "string" ? person.source : "",
                context:
                  typeof person.context === "string" ? person.context : "",
                roleType: person.roleType === "stakeholder" ? "stakeholder" as const : "decision-maker" as const,
                organizationalRole:
                  typeof person.organizationalRole === "string" ? person.organizationalRole : "",
                influence:
                  person.influence === "high" || person.influence === "medium" || person.influence === "low"
                    ? person.influence
                    : undefined,
                stance:
                  person.stance === "champion" || person.stance === "supportive" || person.stance === "neutral" || person.stance === "skeptical" || person.stance === "blocker"
                    ? person.stance
                    : undefined,
                decisionAuthority:
                  typeof person.decisionAuthority === "string" ? person.decisionAuthority : "",
                priorities:
                  typeof person.priorities === "string" ? person.priorities : "",
                concerns:
                  typeof person.concerns === "string" ? person.concerns : "",
                successMeasures:
                  typeof person.successMeasures === "string" ? person.successMeasures : "",
                engagementGuidance:
                  typeof person.engagementGuidance === "string" ? person.engagementGuidance : "",
                approvedNotes:
                  typeof person.approvedNotes === "string" ? person.approvedNotes : "",
              }))
          : decisionMakers
      );
      setMeetingNotes(
        typeof saved.meetingNotes === "string" ? saved.meetingNotes : meetingNotes
      );
      setActiveTab(savedActiveTab);
      setReviewMode(saved.reviewMode === "changes" ? "changes" : "clean");
      setBriefVersion(
        typeof saved.briefVersion === "number" && saved.briefVersion > 0
          ? saved.briefVersion
          : saved.generatedBrief
            ? 1
            : 0
      );
      const restoredDrafts = normalizeRefinementDrafts(
        saved.refinementDrafts,
        savedActiveTab,
        normalizeFeedback(saved.feedback),
        saved.feedbackNotes
      );
      setRefinementDrafts(restoredDrafts);
      setAppliedRefinementDrafts(
        saved.appliedRefinementDrafts
          ? normalizeRefinementDrafts(
              saved.appliedRefinementDrafts,
              savedActiveTab
            )
          : cloneRefinementDrafts(restoredDrafts)
      );
      setModelPreference(
        saved.modelPreference === "nova-micro" || saved.modelPreference === "claude-sonnet-4.6"
          ? saved.modelPreference
          : "nova-pro"
      );
      setApproved(Boolean(saved.approved));
      setApprovalStale(Boolean(saved.approvalStale));
      setPromoted(Boolean(saved.promoted));
      setRole(savedRole);
      setActivePrompt(
        typeof saved.activePrompt === "string"
          ? saved.activePrompt
          : rolePrompts[savedRole][0]
      );
      setGeneratedBrief(
        typeof saved.generatedBrief === "object" && saved.generatedBrief !== null
          ? (saved.generatedBrief as BriefResponse)
          : null
      );
      setBriefHistory(
        Array.isArray(saved.briefHistory)
          ? saved.briefHistory.filter(
              (entry): entry is BriefHistoryEntry =>
                typeof entry === "object" && entry !== null
            )
          : []
      );
      setSelectedHistoryId(
        typeof saved.selectedHistoryId === "string" ? saved.selectedHistoryId : null
      );
      const savedBrief =
        typeof saved.generatedBrief === "object" && saved.generatedBrief !== null
          ? (saved.generatedBrief as BriefResponse)
          : null;
      const savedMeetingApproved =
        savedBrief?.metadata?.meetingApprovalStatus === "approved";
      let restoredStage =
        restoreLifecycleStage(saved.selectedLifecycleStage) ??
        (saved.promoted
          ? savedMeetingApproved
            ? "follow-up"
            : "meeting-prep"
          : saved.approved
            ? "meeting-prep"
            : saved.generatedBrief
              ? "insights"
              : "research");
      if (restoredStage === "follow-up" && !savedMeetingApproved) {
        restoredStage = "meeting-prep";
      }
      setSelectedLifecycleStage(restoredStage);
      setActivePage(lifecycleRoutes[restoredStage].page);
      if (typeof saved.gateDecisions === "object" && saved.gateDecisions !== null) {
        const restoredGates: Record<string, GateDecision> = {};
        for (const [id, value] of Object.entries(saved.gateDecisions)) {
          if (typeof value !== "object" || value === null) continue;
          const candidate = value as Record<string, unknown>;
          const status = candidate.status;
          if (
            status === "not-started" ||
            status === "in-progress" ||
            status === "blocked" ||
            status === "ready" ||
            status === "complete"
          ) {
            restoredGates[id] = { status, confirmed: Boolean(candidate.confirmed) };
          }
        }
        setGateDecisions(restoredGates);
      }
    } catch {
      window.localStorage.removeItem(workspaceStorageKey);
    } finally {
      setWorkspaceLoaded(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!hostedJobsMode || !workspaceLoaded) {
      return;
    }
    let cancelled = false;

    async function loadAuthorizedClientPackets() {
      try {
        const sessionId = agentSessionId();
        const clients = parseAuthorizedClients(
          await signedPipelineRequest("clients", "GET")
        );
        const entries: BriefHistoryEntry[] = [];
        for (const client of clients.filter((item) => item.hasApprovedBrief)) {
          const latest = parseLatestPacket(
            await signedPipelineRequest(
              `clients/${encodeURIComponent(client.clientId)}/latest?projectId=${encodeURIComponent(client.projectId)}&sessionId=${encodeURIComponent(sessionId)}`,
              "GET"
            )
          );
          const request = latest.requestContext as Partial<BriefRequest>;
          const packet = normalizeBriefResponse(
            latest.packet,
            latest.packet.provider === "agentcore" ? "agentcore" : "bedrock"
          );
          const selected = Array.isArray(request.pillars)
            ? normalizePillarRanking(
                request.pillars.filter(
                  (item): item is string => typeof item === "string"
                )
              )
            : normalizePillarRanking([]);
          const decisionMakerContext = Array.isArray(request.decisionMakers)
            ? request.decisionMakers.filter(
                (item): item is DecisionMakerContext =>
                  typeof item === "object" && item !== null
              )
            : [];
          entries.push({
            id: `server-${client.clientId}-${latest.packetVersion}`,
            savedAt: latest.approvedAt || packet.generatedAt,
            scenarioId: "",
            company: request.company || client.company,
            industry: request.industry || "Other",
            meetingType: request.meetingType || "Discovery Call",
            companySize: request.companySize || "Enterprise",
            selectedPillars: selected,
            context: request.context || "",
            companyValues: request.companyValues || "",
            companyValuesUrl: request.companyValuesUrl || "",
            additionalDirection: request.additionalDirection || "",
            decisionMakers: cloneDecisionMakers(decisionMakerContext),
            meetingNotes: request.meetingNotes || "",
            feedback: [],
            feedbackNotes: "",
            refinementDrafts: createRefinementDrafts(),
            appliedRefinementDrafts: createRefinementDrafts(),
            modelPreference:
              request.modelPreference === "nova-micro" ||
              request.modelPreference === "claude-sonnet-4.6"
                ? request.modelPreference
                : "nova-pro",
            briefVersion: latest.packetVersion,
            approved: true,
            promoted: client.hasHandoff,
            generatedBrief: packet,
          });
        }
        if (!cancelled) {
          setServerBriefHistory(entries);
        }
      } catch (error) {
        if (!cancelled) {
          setServerBriefHistory([]);
          setGenerationNotice(
            isPublicDemoAccessError(error)
              ? "Public demo mode is available. Sign-in-only saved packets are hidden for this viewer."
              : error instanceof Error
                ? `Saved client packets are temporarily unavailable: ${error.message}`
                : "Saved client packets are temporarily unavailable."
          );
        }
      } finally {
        if (!cancelled) {
          setIsLibraryLoading(false);
        }
      }
    }

    void loadAuthorizedClientPackets();
    return () => {
      cancelled = true;
    };
    // The signed helper and credential provider are stable for this static build.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceLoaded]);
  useEffect(() => {
    if (!workspaceLoaded) {
      return;
    }

    window.localStorage.setItem(
      workspaceStorageKey,
      JSON.stringify({
        scenarioId,
        company,
        industry,
        meetingType,
        companySize,
        selectedPillars,
        context,
        companyValues,
        companyValuesUrl,
    additionalDirection,
    decisionMakers,
        meetingNotes,
        activeTab,
        reviewMode,
        approvalStale,
        briefVersion,
        feedback,
        feedbackNotes,
        refinementDrafts,
        appliedRefinementDrafts,
        modelPreference,
        approved,
        promoted,
        role,
        activePrompt,
        generatedBrief,
        briefHistory,
        selectedHistoryId,
        selectedLifecycleStage,
        gateDecisions,
      })
    );
  }, [
    activePrompt,
    activeTab,
    appliedRefinementDrafts,
    approved,
    approvalStale,
    reviewMode,
    briefHistory,
    briefVersion,
    company,
    companySize,
    context,
    companyValues,
    companyValuesUrl,
    additionalDirection,
    decisionMakers,
    feedback,
    feedbackNotes,
    generatedBrief,
    gateDecisions,
    industry,
    modelPreference,
    meetingNotes,
    meetingType,
    promoted,
    refinementDrafts,
    role,
    scenarioId,
    selectedLifecycleStage,
    selectedHistoryId,
    selectedPillars,
    workspaceLoaded,
  ]);

  const selectedPillarDetails = useMemo(
    () =>
      selectedPillars.flatMap((pillarId) => {
        const pillar = pillars.find((candidate) => candidate.id === pillarId);
        return pillar ? [pillar] : [];
      }),
    [selectedPillars]
  );
  const topRankedPillars = useMemo(
    () => selectedPillars.slice(0, 3),
    [selectedPillars]
  );
  const pillarRanking = useMemo(
    () => buildPillarRanking(selectedPillars),
    [selectedPillars]
  );
  const usableDecisionMakers = useMemo(
    () =>
      decisionMakers
        .map((person) => ({
          name: person.name.trim(),
          title: person.title.trim(),
          source: person.source?.trim() ?? "",
          context: person.context.trim(),
          roleType: person.roleType === "stakeholder" ? "stakeholder" as const : "decision-maker" as const,
          organizationalRole: person.organizationalRole?.trim() ?? "",
          influence: person.influence,
          stance: person.stance,
        }))
        .filter((person) => person.name || person.title || person.context),
    [decisionMakers]
  );
  const peopleCounts = useMemo(
    () => ({
      "decision-maker": decisionMakers.filter((person) => person.roleType !== "stakeholder").length,
      stakeholder: decisionMakers.filter((person) => person.roleType === "stakeholder").length,
    }),
    [decisionMakers]
  );
  const peopleSummaryProfiles = useMemo(
    () => [
      ...usableDecisionMakers.filter((person) => person.roleType === "decision-maker").slice(0, 2),
      ...usableDecisionMakers.filter((person) => person.roleType === "stakeholder").slice(0, 2),
    ],
    [usableDecisionMakers]
  );
  const visiblePeople = useMemo(
    () =>
      decisionMakers
        .map((person, index) => ({ person, index }))
        .filter(({ person }) =>
          peopleView === "stakeholder"
            ? person.roleType === "stakeholder"
            : person.roleType !== "stakeholder"
        ),
    [decisionMakers, peopleView]
  );
const primaryConcern = selectedPillarDetails[0]?.id ?? "Discovery";
const industryFocus = useMemo(() => {
    if (industry === "Financial Services") {
      return "compliance, auditability, customer trust, and modernization risk";
    }

    if (industry === "Healthcare") {
      return "patient data protection, availability, interoperability, and compliance";
    }

    if (industry === "Retail") {
      return "seasonal scale, personalization, latency, and unit economics";
    }

    if (industry === "Manufacturing") {
      return "plant continuity, IoT data pipelines, forecasting, and uptime";
    }

    if (industry === "Media") {
      return "content workflows, burst traffic, monetization, and global delivery";
    }

    if (industry === "SaaS") {
      return "tenant isolation, reliability, growth efficiency, and platform velocity";
    }

    return "modernization, reliability, security, and measurable business outcomes";
  }, [industry]);
  const blankBriefContent: Record<BriefTab, string[]> = {
    businessCase: [],
    technical: [],
    executive: [],
    stakeholders: [],
    gameplan: [],
    objections: [],
  };

  const fallbackBriefContent = {
    businessCase: [
      `${company || "The customer"} is entering this ${meetingType.toLowerCase()} to align on ${activeScenario.winTheme.toLowerCase()} and agree on the evidence needed before committing to implementation.`,
      "Desired outcomes: confirm measurable business and technical outcomes, reduce decision risk, and leave with named owners for the next validation step.",
      "Alignment statement: confirm the purpose, constraints, decision makers, and evidence threshold before moving into architecture recommendations.",
      `In scope: customer outcomes, ranked priorities, current-state constraints, material risks, and the next decision for ${company || "the customer"}.`,
      "Out of scope: final production architecture, fixed implementation dates, guaranteed savings, or compliance certification before customer validation.",
      "Success criteria: corrected assumptions, agreed scope, named owners, measurable proof, and a scheduled decision checkpoint.",
    ],
    technical: [
      `${company || "The customer"} likely needs a secure landing zone, governed identity model, observable application path, and migration pattern that reduces production risk.`,
      `Discovery should validate current architecture, data classification, RTO/RPO, incident response, network dependencies, and ownership across ${companySize.toLowerCase()} teams.`,
      `Recommended AWS references: Amazon Bedrock for generation, AWS Lambda and API Gateway for orchestration, Amazon S3 for artifacts, Amazon DynamoDB for project state, Amazon CloudWatch for observability, and AWS Well-Architected Tool for pillar alignment.`,
    ],
    executive: [
      `${company || "The customer"} is balancing modernization speed with risk control. The conversation should stay centered on ${industryFocus}.`,
      "Position AWS as a way to improve decision quality, reduce operational drag, and make progress measurable without forcing a risky all-at-once transformation.",
      `Business framing: ${activeScenario.winTheme}`,
    ],
    stakeholders: usableDecisionMakers.length
      ? usableDecisionMakers.map(
          (person) =>
            `${person.name || "Decision maker"}${person.title ? `, ${person.title}` : ""}: connect the opening to ${primaryConcern.toLowerCase()} and ask which outcome, risk, or blocker matters most from their seat. ${person.context ? `Signal: ${person.context}` : ""}`
        )
      : [
          "Add approved stakeholder notes to tailor the opening, questions, and objection handling.",
          `For ${company || "the customer"}, identify the economic buyer, technical owner, security approver, and project driver before the follow-up.`,
          "Use pasted customer-approved context only; treat all profile-based insight as a hypothesis to validate.",
        ],
    gameplan: [
      "Open by confirming the business event driving urgency, then map technical unknowns to business impact.",
      `Spend the first half on the ranked priorities (${topRankedPillars.join(", ").toLowerCase()}) and use the final ten minutes to agree on success measures and next steps.`,
      "Close with a crisp handoff: confirmed goals, known risks, unanswered questions, owners, timeline, and how the project workspace should be used.",
    ],
    objections: [
      "Customer pushback: We cannot risk disruption during this program.",
      `Response: propose a bounded pilot around ${selectedPillars[0]?.toLowerCase() || "the top priority"}, define rollback criteria, and connect each technical checkpoint to business continuity.`,
      "Customer pushback: This sounds expensive. Response: start with unit-cost visibility, right-sizing, and a decision checkpoint before scaling the implementation.",
    ],
  };

  const activeGenerationStageLabel =
    pipelineJobStatus === "queued"
      ? "Queued securely in AWS"
      : pipelineJobStatus === "validating"
        ? "Checking packet quality and safety"
        : pipelineJobStatus === "saving"
          ? "Saving the latest customer packet"
          : refiningTarget
            ? `Regenerating ${briefTabLabel(refiningTarget)} with Bedrock`
            : generationStageMode === "project"
              ? "Building the team handoff with AgentCore"
              : "Generating the customer packet with Bedrock";

  const activeProcessingLabel =
    pipelineJobStatus === "queued"
      ? "Queued securely in AWS..."
      : pipelineJobStatus === "validating"
        ? "Checking quality and safety..."
        : pipelineJobStatus === "saving"
          ? "Saving packet..."
          : refiningTarget
            ? `Applying feedback to ${briefTabLabel(refiningTarget)}...`
            : generationStageMode === "project"
              ? "Building handoff..."
              : "Generating brief...";

  const catchupProcessingLabel =
    catchupJobStatus === "queued"
      ? "Catch-up queued..."
      : catchupJobStatus === "validating"
        ? "Checking catch-up quality..."
        : catchupJobStatus === "saving"
          ? "Saving catch-up..."
          : "Preparing catch-up...";

  function claimRecord(section: BriefTab | "projectAnswer", itemIndex: number) {
    return generatedBrief?.claims?.find(
      (item) => item.section === section && item.itemIndex === itemIndex
    );
  }

  function claimSourceRecords(section: BriefTab | "projectAnswer", itemIndex: number) {
    const claim = claimRecord(section, itemIndex);
    const allowed = new Set(claim?.sourceIds ?? []);
    return (generatedBrief?.sourceCatalog ?? []).filter((source) => allowed.has(source.sourceId));
  }

  function openEvidenceSource(sourceId: string, claimId = "") {
    setSelectedEvidenceSourceId(sourceId);
    setSelectedEvidenceClaimId(claimId);
  }

  const selectedEvidenceSource =
    generatedBrief?.sourceCatalog?.find((source) => source.sourceId === selectedEvidenceSourceId) ?? null;
  const selectedEvidenceClaim =
    generatedBrief?.claims?.find((claim) => claim.claimId === selectedEvidenceClaimId) ?? null;

  const briefContent = isGenerating && !generatedBrief
    ? blankBriefContent
    : generatedBrief
      ? {
          businessCase: businessCasePassages(generatedBrief.businessCase),
          technical: generatedBrief.technical,
          executive: generatedBrief.executive,
          stakeholders: generatedBrief.stakeholders?.length
            ? generatedBrief.stakeholders
            : fallbackBriefContent.stakeholders,
          gameplan: generatedBrief.gameplan,
          objections: generatedBrief.objections,
        }
      : blankBriefContent;

  const currentBriefHistoryIndex = selectedHistoryId
    ? briefHistory.findIndex((entry) => entry.id === selectedHistoryId)
    : -1;
  const savedPacketInputs = briefHistory[currentBriefHistoryIndex]
    ?? serverBriefHistory.find((entry) => entry.id === selectedHistoryId);
  const peopleInputKey = (people: DecisionMakerContext[]) => JSON.stringify(people.map((person) => [
    person.name.trim(), person.title.trim(), person.context.trim(), person.source?.trim() ?? "",
    person.roleType ?? "decision-maker", person.organizationalRole?.trim() ?? "", person.influence, person.stance,
  ]));
  const pendingIntakeChanges = Boolean(generatedBrief && savedPacketInputs && (
    company !== savedPacketInputs.company || industry !== savedPacketInputs.industry ||
    meetingType !== savedPacketInputs.meetingType || companySize !== savedPacketInputs.companySize ||
    context !== savedPacketInputs.context || companyValues !== savedPacketInputs.companyValues ||
    companyValuesUrl !== (savedPacketInputs.companyValuesUrl ?? "") ||
    additionalDirection !== (savedPacketInputs.additionalDirection ?? "") ||
    JSON.stringify(selectedPillars) !== JSON.stringify(normalizePillarRanking(savedPacketInputs.selectedPillars)) ||
    peopleInputKey(usableDecisionMakers) !== peopleInputKey(savedPacketInputs.decisionMakers)
  ));
  const activeComparison = useMemo(
    () =>
      comparisonForSelectedRefinement(
        briefHistory,
        currentBriefHistoryIndex,
        activeTab
      ),
    [activeTab, briefHistory, currentBriefHistoryIndex]
  );
  const visibleReviewMode: BriefReviewMode = activeComparison
    ? reviewMode
    : "clean";
  const activePassageChanges =
    activeComparison?.changes.filter((item) => item.section === activeTab) ?? [];
  const activeRemovedPassages =
    activeComparison?.removed.filter((item) => item.section === activeTab) ?? [];
  const activeChangedPassages =
    activePassageChanges.length + activeRemovedPassages.length;
  const unresolvedRefinement = Boolean(
    generatedBrief &&
      refinementDraftChanged(
        activeRefinementDraft,
        appliedRefinementDrafts[activeTab]
      )
  );
  const approvalReady = Boolean(generatedBrief && !isGenerating && !unresolvedRefinement && !pendingIntakeChanges);

  const activeBriefText = [
    `${company || "Customer"} - ${briefTabLabel(activeTab)}`,
    "",
    ...briefContent[activeTab].map(
      (item, index) => `${briefSectionHeading(activeTab, index)}: ${item}`
    ),
    "",
    `Sources: ${generatedBrief?.sourceCatalog?.length ? generatedBrief.sourceCatalog.map((source) => source.title).join(", ") : "Evidence not recorded"}`,
  ].join("\n");

  const followUpEmailText = promoted && approved && generatedBrief?.projectArtifacts?.followUpEmail
    ? `Subject: ${generatedBrief.projectArtifacts.followUpEmail.subject}\n\n${generatedBrief.projectArtifacts.followUpEmail.body}`
    : "";
  const isProjectGenerating = isGenerating && generationStageMode === "project";
  const displayedProjectAnswer = approved && promoted && !isProjectGenerating
    ? handoffAnswerFor(generatedBrief, {
        company,
        clientId: pipelineClientIdentifier(company),
        projectId: pipelineClientIdentifier(company),
        packetVersion: currentPacketVersion(),
        audienceRole: role,
        focus: activePrompt,
      })
    : "";
  const handoffPacketText = (() => {
    const metadata = generatedBrief?.metadata;
    const sources = generatedBrief?.sourceCatalog?.map((source) => source.title) ?? [];
    const artifactList = (title: string, items: ProjectArtifactItem[] | undefined) => [
      title,
      ...(items?.length
        ? items.map((item, index) => {
            const owner = item.owner ? ` | Owner: ${item.owner}` : "";
            const status = item.status ? ` | Status: ${item.status}` : "";
            return `${index + 1}. ${item.title}${owner}${status}\n   ${item.detail}`;
          })
        : ["Not generated yet."]),
    ].join("\n");
    const briefSection = (title: string, items: string[]) => [
      title,
      ...items.map((item, index) => `${index + 1}. ${item}`),
    ].join("\n");
    const businessCaseSection = [
      "Business case",
      ...businessCaseFields.map(
        ({ key, label }) =>
          `${label}: ${generatedBrief?.businessCase?.[key] ?? "Not generated yet."}`
      ),
    ].join("\n");
    const nextSteps = generatedBrief?.projectArtifacts?.nextSteps;
    const nextStepsSection = [
      "Next steps",
      ...(nextSteps?.immediateActions?.length
        ? nextSteps.immediateActions.map(
            (item, index) =>
              `${index + 1}. ${item.action}\n   Owner: ${item.owner} | Timing: ${item.timing}\n   Dependency: ${item.dependency}\n   Decision gate: ${item.decisionGate}`
          )
        : ["Not generated yet."]),
      "",
      "Open questions",
      ...(nextSteps?.openQuestions?.length
        ? nextSteps.openQuestions.map((item, index) => `${index + 1}. ${item}`)
        : ["None captured yet."]),
      "",
      "Next meeting",
      nextSteps
        ? `${nextSteps.nextMeeting.purpose} | ${nextSteps.nextMeeting.timing} | Attendees: ${nextSteps.nextMeeting.attendees.join(", ")}`
        : "Not scheduled yet.",
      "",
      "Customer-facing summary",
      nextSteps?.customerSummary ?? "Not generated yet.",
      "",
      "Internal notes",
      nextSteps?.internalNotes ?? "Not generated yet.",
    ].join("\n");

    return [
      `PilarPrep handoff packet - ${company || "Customer"}`,
      `Meeting: ${meetingType} | Industry: ${industry} | Size: ${companySize}`,
      `Generation path: ${generatedBrief ? providerLabel(generatedBrief.provider) : "Not generated yet"}`,
      metadata?.artifactKey ? `S3 JSON: ${metadata.artifactKey}` : "S3 JSON: Not saved yet",
      metadata?.docxArtifactKey ? `S3 DOCX: ${metadata.docxArtifactKey}` : "S3 DOCX: Not saved yet",
      metadata?.docxDownloadUrl ? `DOCX download: ${metadata.docxDownloadUrl}` : "DOCX download: Not generated yet",
      metadata?.stateKey ? `DynamoDB state: ${metadata.stateKey}` : "DynamoDB state: Not saved yet",
      "",
      "Ranked AWS priorities",
      ...selectedPillars.map((pillar, index) => `${index + 1}. ${pillar}`),
      "",
      "Customer context",
      context || "No customer context captured yet.",
      "Company values",
      companyValues || "No company values captured yet.",
      "Company values page",
      companyValuesUrl || "No company values page captured yet.",
      "",
      businessCaseSection,
      "",
      briefSection("Technical brief", briefContent.technical),
      "",
      briefSection("Executive brief", briefContent.executive),
      "",
      briefSection("Stakeholder lens", briefContent.stakeholders),
      "",
      briefSection("SA game plan", briefContent.gameplan),
      "",
      briefSection("Objection handling", briefContent.objections),
      "",
      "Team handoff answer",
      displayedProjectAnswer,
      "",
      artifactList("Two-week implementation plan", generatedBrief?.projectArtifacts?.twoWeekPlan),
      "",
      artifactList("Risk register", generatedBrief?.projectArtifacts?.riskRegister),
      "",
      artifactList("Stakeholder map", generatedBrief?.projectArtifacts?.stakeholderMap),
      "",
      nextStepsSection,
      "",
      "Follow-up email",
      followUpEmailText,
      "",
      `Sources: ${sources.join(", ")}`,
    ].join("\n");
  })();

  const handoffItems = [
    {
      title: "Final brief",
      status: approved ? "Ready" : "Draft",
      detail: `v${briefVersion} with ${feedback.length} refinements`,
    },
    {
      title: "Stakeholder lens",
      status: usableDecisionMakers.length ? "Captured" : "Needs context",
      detail: usableDecisionMakers.length
        ? `${usableDecisionMakers.length} decision-maker signals`
        : "Approved profile notes and priorities",
    },
    {
      title: "Known customer context",
      status: meetingNotes.length > 80 ? "Captured" : "Needs notes",
      detail: "Discovery, commitments, sensitivities, and unknowns",
    },
    {
      title: "Project memory",
      status: promoted ? "Live" : "Waiting",
      detail: "Brief, notes, risks, actions, and decisions",
    },
    {
      title: "Next artifacts",
      status: promoted ? "Generated" : "Queued",
      detail: "Plan, risk list, exec summary, onboarding",
    },
  ];

  const projectNextSteps = generatedBrief?.projectArtifacts?.nextSteps;
  const projectTimeline = useMemo(
    () => generatedBrief?.projectArtifacts?.twoWeekPlan ?? [],
    [generatedBrief]
  );
  const projectRiskRegister = useMemo(
    () => generatedBrief?.projectArtifacts?.riskRegister ?? [],
    [generatedBrief]
  );
  const projectAssumptions = useMemo(
    () => projectRiskRegister.filter((item) =>
      /assumption|hypothesis|unvalidated/i.test(`${item.title} ${item.status ?? ""}`)
    ),
    [projectRiskRegister]
  );
  const projectRisks = useMemo(
    () => projectRiskRegister.filter((item) => !projectAssumptions.includes(item)),
    [projectAssumptions, projectRiskRegister]
  );
  const projectStakeholders = useMemo(
    () => generatedBrief?.projectArtifacts?.stakeholderMap ?? [],
    [generatedBrief]
  );
  const handoffPersistenceReady = Boolean(
    generatedBrief?.provider === "demo" ||
      (generatedBrief?.metadata?.docxArtifactKey && generatedBrief?.metadata?.stateKey)
  );
  const handoffReady = Boolean(
    generatedBrief &&
      promoted &&
      handoffPersistenceReady &&
      generatedBrief.projectArtifacts?.twoWeekPlan?.length &&
      generatedBrief.projectArtifacts?.riskRegister?.length &&
      generatedBrief.projectArtifacts?.stakeholderMap?.length &&
      generatedBrief.projectArtifacts?.nextSteps?.immediateActions?.length
  );
  const meetingUpdateApproved = Boolean(
    meetingJobStatus === "approved" ||
      generatedBrief?.metadata?.meetingApprovalStatus === "approved"
  );
  const opportunityGates = useMemo<OpportunityGate[]>(() => {
    const actions = projectNextSteps?.immediateActions ?? [];
    const primaryOwner =
      usableDecisionMakers[0]?.name || projectStakeholders[0]?.owner || "Account team";
    const evidenceByGate: Record<string, string> = {
      business:
        generatedBrief?.businessCase?.decisionRequired ||
        "Confirm the business decision, desired outcomes, and success measures.",
      technical:
        generatedBrief?.technical?.[0] ||
        "Validate the current architecture, constraints, and required proof.",
      security:
        projectRisks.find((item) => /security|compliance|identity|audit/i.test(item.title + " " + item.detail))?.detail ||
        "Confirm the security owner, control evidence, and risk-acceptance path.",
      integration:
        generatedBrief?.technical?.find((item) => /data|integration|interface|api|payroll/i.test(item)) ||
        "Confirm system boundaries, data ownership, interfaces, and cutover dependencies.",
      commercial:
        projectNextSteps?.openQuestions?.find((item) => /commercial|contract|procurement|budget|cost/i.test(item)) ||
        "Confirm funding, procurement, and commercial dependencies.",
      executive:
        generatedBrief?.stakeholders?.[0] ||
        "Confirm the executive sponsor, decision authority, and escalation path.",
      implementation:
        projectTimeline[0]?.detail ||
        "Confirm the first bounded implementation step, owner, and exit criteria.",
    };

    return opportunityGateDefinitions.map((definition, index) => {
      const action = actions[index] ?? actions[0];
      const decision = gateDecisions[definition.id];
      const suggestedStatus: OpportunityGateStatus = meetingUpdateApproved
        ? definition.id === "business" || definition.id === "executive"
          ? "ready"
          : "in-progress"
        : handoffReady
          ? "in-progress"
          : "not-started";

      return {
        id: definition.id,
        name: definition.name,
        status: decision?.status ?? suggestedStatus,
        owner: action?.owner || projectStakeholders[index]?.owner || primaryOwner,
        evidence: evidenceByGate[definition.id],
        nextAction:
          action?.action ||
          action?.decisionGate ||
          "Assign an owner and agree on the evidence required to advance.",
        confirmed: decision?.confirmed ?? false,
      };
    });
  }, [
    gateDecisions,
    generatedBrief,
    handoffReady,
    meetingUpdateApproved,
    projectNextSteps,
    projectRisks,
    projectStakeholders,
    projectTimeline,
    usableDecisionMakers,
  ]);
  const confirmedGateCount = opportunityGates.filter((gate) => gate.confirmed).length;
  const currentLifecycleStage = useMemo<LifecycleStageId>(() => {
    if (!generatedBrief) return "research";
    if (!approved || approvalStale) {
      return activeTab === "businessCase" || activeTab === "executive"
        ? "insights"
        : "discovery";
    }
    if (!promoted || !meetingUpdateApproved) return "meeting-prep";
    return "follow-up";
  }, [activeTab, approved, approvalStale, generatedBrief, meetingUpdateApproved, promoted]);
  const lifecycleStages = useMemo<LifecycleStage[]>(() => {
    const completed: Record<LifecycleStageId, boolean> = {
      research: Boolean(generatedBrief),
      insights: Boolean(generatedBrief),
      discovery: Boolean(approved && !approvalStale),
      "meeting-prep": meetingUpdateApproved,
      "follow-up": meetingUpdateApproved && confirmedGateCount === opportunityGateDefinitions.length,
    };
    const available: Record<LifecycleStageId, boolean> = {
      research: true,
      insights: Boolean(generatedBrief),
      discovery: Boolean(generatedBrief),
      "meeting-prep": Boolean(approved && !approvalStale),
      "follow-up": meetingUpdateApproved,
    };
    const definitions: Array<Omit<LifecycleStage, "status">> = [
      { id: "research", label: "Research", shortLabel: "Research", detail: "Capture approved customer facts, people, values, and sources." },
      { id: "insights", label: "Insights", shortLabel: "Insights", detail: "Review the business scenario, outcomes, risks, and stakeholder signals." },
      { id: "discovery", label: "Discovery", shortLabel: "Discovery", detail: "Validate assumptions, evidence gaps, questions, and architecture considerations." },
      { id: "meeting-prep", label: "Meet", shortLabel: "Meet", detail: "Align the team, capture the call, and review what the evidence changes." },
      { id: "follow-up", label: "Follow-up", shortLabel: "Follow-up", detail: "Use approved meeting evidence to prepare the next customer move." },
    ];

    return definitions.map((stage) => ({
      ...stage,
      status: completed[stage.id]
        ? "complete"
        : stage.id === currentLifecycleStage
          ? approvalStale || opportunityGates.some((gate) => gate.status === "blocked")
            ? "attention"
            : "current"
          : available[stage.id]
            ? "available"
            : "locked",
    }));
  }, [
    approvalStale,
    approved,
    confirmedGateCount,
    currentLifecycleStage,
    generatedBrief,
    meetingUpdateApproved,
    opportunityGates,
  ]);
  const currentLifecycleLabel =
    lifecycleStages.find((stage) => stage.id === currentLifecycleStage)?.label || "Prepare";
  const nextLifecycleActionLabel: Record<LifecycleStageId, string> = {
    research: meetingUpdateApproved ? "Prepare next call" : "Generate prebrief",
    insights: "Review business insights",
    discovery: "Resolve discovery gaps",
    "meeting-prep": promoted ? "Open meeting workspace" : "Prepare team handoff",
    "follow-up": "Review next-step gates",
  };
  const projectStagePresentation =
    selectedLifecycleStage === "follow-up"
      ? { eyebrow: "Follow-on motion", title: "Turn decisions into the next move", detail: "Review the accepted meeting evidence, confirm opportunity gates, and prepare the next customer call." }
      : promoted
        ? meetingResult
          ? { eyebrow: "Change review", title: "Decide what becomes project truth", detail: "Accept, edit, or reject meeting-derived updates before they enter the next-step handoff." }
          : { eyebrow: "Customer call", title: "Capture the customer conversation", detail: "Upload the call and compare it with the approved brief before anything becomes project truth." }
        : { eyebrow: "Meeting preparation", title: "Prepare the team for the customer call", detail: "Share the approved context, assumptions, questions, owners, and meeting goals across everyone joining the call." };
  const isFollowUpStage = selectedLifecycleStage === "follow-up";
  const isNextStepFollowUp = isFollowUpStage && meetingUpdateApproved;
  const isMeetingStage =
    selectedLifecycleStage === "meeting-prep" && promoted && !meetingUpdateApproved;
  const evidenceCoverageLabel = generatedBrief?.evidenceCoverage
    ? `${generatedBrief.evidenceCoverage.coveragePercent}% linked`
    : "Evidence not recorded";
  const validationNeedCount = (generatedBrief?.claims ?? []).filter((claim) =>
    claim.evidenceStatus === "assumption" ||
    claim.evidenceStatus === "needs-validation" ||
    claim.evidenceStatus === "conflicting-evidence"
  ).length;
  const latestApprovedOutput = meetingUpdateApproved
    ? "Approved meeting outcome"
    : promoted
      ? "Pre-call handoff"
      : approved && !approvalStale
        ? `Approved brief v${briefVersion}`
        : generatedBrief
          ? `Draft brief v${briefVersion}`
          : "No packet yet";
  const nextBestAction = currentLifecycleStage === "research"
    ? "Confirm approved sources and customer context"
    : currentLifecycleStage === "insights"
      ? "Align the business scenario and desired outcomes"
      : currentLifecycleStage === "discovery"
        ? validationNeedCount
          ? `Resolve ${validationNeedCount} evidence or assumption gap${validationNeedCount === 1 ? "" : "s"}`
          : "Approve the customer meeting packet"
        : currentLifecycleStage === "meeting-prep"
          ? promoted
            ? authSession
              ? "Upload the call and review every proposed update"
              : "Sign in to upload the private meeting audio"
            : "Align owners and evidence requests before the call"
          : meetingUpdateApproved
            ? "Confirm gates and prepare the next customer meeting"
            : "Upload the call and review every proposed update";
  const latestHistoryByClient = useMemo(() => {
    const grouped = new Map<string, BriefHistoryEntry>();

    for (const entry of [...briefHistory, ...serverBriefHistory]) {
      if (!entry.approved && !entry.promoted) {
        continue;
      }
      const key = entry.company.trim().toLowerCase() || entry.id;

      if (!grouped.has(key)) {
        grouped.set(key, entry);
      }
    }

    return Array.from(grouped.values()).sort((left, right) => {
      const leftTime = Date.parse(left.savedAt);
      const rightTime = Date.parse(right.savedAt);
      return (Number.isNaN(rightTime) ? 0 : rightTime) - (Number.isNaN(leftTime) ? 0 : leftTime);
    });
  }, [briefHistory, serverBriefHistory]);

  const catchupClientCards = useMemo(
    () =>
      latestHistoryByClient.map((entry) => {
        const handoffComplete = Boolean(
          entry.promoted &&
            entry.generatedBrief.metadata?.docxArtifactKey &&
            entry.generatedBrief.metadata?.stateKey &&
            entry.generatedBrief.projectArtifacts?.twoWeekPlan?.length &&
            entry.generatedBrief.projectArtifacts?.riskRegister?.length &&
            entry.generatedBrief.projectArtifacts?.stakeholderMap?.length &&
            entry.generatedBrief.projectArtifacts?.nextSteps?.immediateActions?.length
        );
        const filterKey: CatchupFilter = handoffComplete ? "handoff" : "approved";

        return {
          id: entry.id,
          company: entry.company,
          savedAt: entry.savedAt,
          meetingType: entry.meetingType,
          industry: entry.industry,
          companySize: entry.companySize,
          topPilar: entry.selectedPillars[0] ?? "Not set",
          status: handoffComplete
            ? "Latest handoff"
            : "Latest approved brief",
          filterKey,
        };
      }),
    [latestHistoryByClient]
  );

  const filteredCatchupClientCards = useMemo(
    () =>
      catchupClientCards.filter((entry) => {
        if (catchupFilter === "handoff") {
          return entry.filterKey === "handoff";
        }

        if (catchupFilter === "approved") {
          return entry.filterKey === "approved" || entry.filterKey === "handoff";
        }

        return true;
      }),
    [catchupClientCards, catchupFilter]
  );

  const selectedHistoryEntry = useMemo(
    () =>
      latestHistoryByClient.find((entry) => entry.id === selectedHistoryId && filteredCatchupClientCards.some((card) => card.id === entry.id)) ??
      latestHistoryByClient.find((entry) => filteredCatchupClientCards.some((card) => card.id === entry.id)) ??
      null,
    [filteredCatchupClientCards, latestHistoryByClient, selectedHistoryId]
  );

  const hasFilteredCatchupClient = filteredCatchupClientCards.length > 0;
  const liveCatchupFallback =
    latestHistoryByClient.length === 0 && approved ? generatedBrief : null;
  const catchupBrief = selectedHistoryEntry?.generatedBrief ?? liveCatchupFallback;
  const catchupCompany = selectedHistoryEntry?.company ?? (liveCatchupFallback ? company : latestHistoryByClient.length && !hasFilteredCatchupClient ? "No client in this view" : "");
  const catchupIndustry = selectedHistoryEntry?.industry ?? (liveCatchupFallback ? industry : "");
  const catchupMeetingType = selectedHistoryEntry?.meetingType ?? (liveCatchupFallback ? meetingType : "");
  const catchupCompanySize = selectedHistoryEntry?.companySize ?? (liveCatchupFallback ? companySize : "");
  const catchupTopPilar = selectedHistoryEntry?.selectedPillars?.[0] ?? (liveCatchupFallback ? selectedPillars[0] ?? "Set ranking" : "Set ranking");
  const catchupNotes = selectedHistoryEntry?.meetingNotes ?? (liveCatchupFallback ? meetingNotes : "");
  const catchupContext = selectedHistoryEntry?.context ?? (liveCatchupFallback ? context : "");
  const catchupValues = selectedHistoryEntry?.companyValues ?? (liveCatchupFallback ? companyValues : "");
  const catchupStatus = selectedHistoryEntry
    ? selectedHistoryEntry.promoted &&
      selectedHistoryEntry.generatedBrief.metadata?.docxArtifactKey &&
      selectedHistoryEntry.generatedBrief.metadata?.stateKey &&
      selectedHistoryEntry.generatedBrief.projectArtifacts?.twoWeekPlan?.length &&
      selectedHistoryEntry.generatedBrief.projectArtifacts?.riskRegister?.length &&
      selectedHistoryEntry.generatedBrief.projectArtifacts?.stakeholderMap?.length &&
      selectedHistoryEntry.generatedBrief.projectArtifacts?.nextSteps?.immediateActions?.length
      ? "Delivery-ready handoff"
      : selectedHistoryEntry.promoted
        ? "Handoff building"
        : selectedHistoryEntry.approved
          ? "Approved brief ready for handoff"
          : "Brief generated and in review"
    : latestHistoryByClient.length && !hasFilteredCatchupClient
      ? "No clients in this view"
      : handoffReady
        ? "Delivery-ready handoff"
      : promoted
        ? "Handoff building"
        : approved
          ? "Approved brief ready for handoff"
          : "Approve a brief to make it available here";

  const selectedHistoryPacketItems = useMemo(
    () =>
      packetOutputs.map((packet) => ({
        ...packet,
        status: selectedHistoryEntry ? "Saved" : catchupBrief ? "Live" : "Empty",
      })),
    [catchupBrief, selectedHistoryEntry]
  );

  const libraryPreviewCards = useMemo(() => {
    if (!catchupBrief) {
      return [];
    }

    return [
      {
        title: "Business case",
        detail: catchupBrief.businessCase?.scenario ?? catchupBrief.executive[0] ?? catchupContext ?? "No customer summary available yet.",
      },
      {
        title: "Technical focus",
        detail: catchupBrief.technical[0] ?? "No technical brief saved yet.",
      },
      {
        title: "Who matters most",
        detail: catchupBrief.stakeholders[0] ?? "No stakeholder detail saved yet.",
      },
      {
        title: "What happens next",
        detail:
          catchupBrief.projectArtifacts?.nextSteps?.immediateActions?.[0]?.action ??
          catchupBrief.projectArtifacts?.twoWeekPlan?.[0]?.detail ??
          catchupBrief.gameplan[0] ??
          "No handoff detail saved yet.",
      },
    ];
  }, [catchupBrief, catchupContext]);

  const catchupSummaryCards = useMemo(
    () => [
      { label: "Client", value: catchupCompany || "No client selected" },
      { label: "Meeting", value: catchupMeetingType || "Meeting not set" },
      { label: "Top pilar", value: catchupTopPilar },
      { label: "Current state", value: catchupStatus },
    ],
    [catchupCompany, catchupMeetingType, catchupStatus, catchupTopPilar]
  );

  const catchupActionItems = useMemo(() => {
    const nextSteps = catchupBrief?.projectArtifacts?.nextSteps;
    const firstAction = nextSteps?.immediateActions?.[0];

    return [
      {
        title: "Immediate next step",
        detail: firstAction
          ? `${firstAction.action} Owner: ${firstAction.owner}. Timing: ${firstAction.timing}.`
          : "Generate the handoff to create an owned next action.",
      },
      {
        title: "Decision gate",
        detail: firstAction?.decisionGate ?? "No decision gate captured yet.",
      },
      {
        title: "Next meeting",
        detail: nextSteps
          ? `${nextSteps.nextMeeting.purpose} ${nextSteps.nextMeeting.timing}. Attendees: ${nextSteps.nextMeeting.attendees.join(", ")}.`
          : "No next meeting captured yet.",
      },
      {
        title: "Open question",
        detail: nextSteps?.openQuestions?.[0] ?? "No open questions captured yet.",
      },
    ];
  }, [catchupBrief]);

  const packetPreviewItems = useMemo(
    () =>
      packetOutputs.map((packet) => {
        if (packet.key === "technical" || packet.key === "executive") {
          return {
            ...packet,
            status: generatedBrief ? (approved ? "Approved" : "Generated") : "Queued",
          };
        }

        if (packet.key === "stakeholders") {
          return {
            ...packet,
            status: usableDecisionMakers.length
              ? generatedBrief
                ? "Tailored"
                : "Context ready"
              : "Needs context",
          };
        }

        if (packet.key === "gameplan") {
          return {
            ...packet,
            status: generatedBrief ? "Generated" : "Queued",
          };
        }

        return {
          ...packet,
          status: handoffReady
            ? "Ready"
            : promoted
              ? "Building"
              : generatedBrief
                ? "Next step"
                : "Queued",
        };
      }),
    [approved, generatedBrief, handoffReady, promoted, usableDecisionMakers.length]
  );

  const liveModelSelectionEnabled = hostedJobsMode;
  const selectedModelLabel =
    modelPreference === "claude-sonnet-4.6"
      ? "Claude Sonnet 4.6"
      : modelPreference === "nova-pro"
        ? "Nova Pro"
        : "Nova Micro";
  const selectedQualityTier: "fast" | "standard" | "premium" =
    modelPreference === "claude-sonnet-4.6"
      ? "premium"
      : modelPreference === "nova-micro"
        ? "fast"
        : "standard";

  function resetMeetingWorkspace() {
    meetingAbortRef.current?.abort(new DOMException("Scenario changed.", "AbortError"));
    meetingUploadAbortRef.current?.abort(
      new DOMException("Scenario changed.", "AbortError")
    );
    meetingRequestRef.current = false;
    setMeetingResult(null);
    setMeetingAudio({ fileName: "", sizeBytes: 0, status: "empty" });
    setMeetingAudioUploadId("");
    setMeetingDecisions({});
    setMeetingJobStatus(null);
    setMeetingError("");
    setMeetingNotice("");
    setIsMeetingProcessing(false);
    setIsMeetingApproving(false);
  }

  function loadScenario(nextScenario: Scenario) {
    cancelPacketRequest();
    setScenarioId(nextScenario.id);
    setCompany(nextScenario.company);
    setIndustry(nextScenario.industry);
    setMeetingType(nextScenario.meetingType);
    setCompanySize(nextScenario.companySize);
    setSelectedPillars(normalizePillarRanking(nextScenario.pillars));
    setContext(nextScenario.context);
    setCompanyValues(nextScenario.companyValues);
    setCompanyValuesUrl(nextScenario.companyValuesUrl);
    setAdditionalDirection(nextScenario.additionalDirection);
    setDecisionMakers(cloneDecisionMakers(nextScenario.decisionMakers));
    setMeetingNotes(nextScenario.meetingNotes);
    setRefinementDrafts(createRefinementDrafts());
    setAppliedRefinementDrafts(createRefinementDrafts());
    setBriefVersion(0);
    setApproved(false);
    setApprovalStale(false);
    setPromoted(false);
    setActiveTab("businessCase");
    setReviewMode("clean");
    setGeneratedBrief(null);
    setGenerationError("");
    setGenerationNotice("");
    setSelectedHistoryId(null);
    setCatchupAnswer("");
    setCatchupError("");
    setCatchupSource("");
    resetMeetingWorkspace();
    setDraggedPillar(null);
    clearCopyFeedback();
  }

  function startCustomScenario() {
    cancelPacketRequest();
    setScenarioId("custom");
    setCompany("");
    setIndustry("Other");
    setMeetingType("Discovery Call");
    setCompanySize("Mid-market");
    setSelectedPillars(normalizePillarRanking(["Security", "Reliability", "Cost Optimization"]));
    setContext("");
    setCompanyValues("");
    setCompanyValuesUrl("");
    setAdditionalDirection("");
    setDecisionMakers([
      {
        name: "",
        title: "",
        source: "Customer-approved notes",
        context: "",
        roleType: "decision-maker",
      },
    ]);
    setMeetingNotes("");
    setRefinementDrafts(createRefinementDrafts());
    setAppliedRefinementDrafts(createRefinementDrafts());
    setBriefVersion(0);
    setApproved(false);
    setApprovalStale(false);
    setPromoted(false);
    setActiveTab("businessCase");
    setReviewMode("clean");
    setGeneratedBrief(null);
    setGenerationError("");
    setGenerationNotice("Custom scenario mode is ready. Add the customer context, then generate a fresh packet.");
    setSelectedHistoryId(null);
    setCatchupAnswer("");
    setCatchupError("");
    setCatchupSource("");
    resetMeetingWorkspace();
    setDraggedPillar(null);
    clearCopyFeedback();
  }

  function reorderPillar(pillar: string, targetIndex: number) {
    setSelectedPillars((current) => {
      const ranked = normalizePillarRanking(current);
      const currentIndex = ranked.indexOf(pillar);
      const boundedTargetIndex = Math.min(
        Math.max(targetIndex, 0),
        ranked.length - 1
      );

      if (currentIndex < 0 || currentIndex === boundedTargetIndex) {
        return ranked;
      }

      const nextRanking = [...ranked];
      const [movedPillar] = nextRanking.splice(currentIndex, 1);
      nextRanking.splice(boundedTargetIndex, 0, movedPillar);

      return nextRanking;
    });
  }

  function promotePillar(pillar: string) {
    reorderPillar(pillar, 0);
  }

  function handlePillarDragOver(
    event: DragEvent<HTMLDivElement>,
    targetPillar: string
  ) {
    event.preventDefault();

    if (!draggedPillar || draggedPillar === targetPillar) {
      return;
    }

    const targetIndex = selectedPillars.indexOf(targetPillar);
    reorderPillar(draggedPillar, targetIndex);
  }

  function toggleFeedback(option: string) {
    setRefinementDrafts((current) =>
      toggleRefinementFeedback(current, activeTab, option)
    );
  }

  function updateFeedbackNotes(value: string) {
    setRefinementDrafts((current) => ({
      ...current,
      [activeTab]: {
        ...current[activeTab],
        feedbackNotes: value,
      },
    }));
  }

  function clearActiveRefinementDraft() {
    setRefinementDrafts((current) => ({
      ...current,
      [activeTab]: { feedback: [], feedbackNotes: "" },
    }));
  }

  function selectBriefTab(tab: BriefTab) {
    setActiveTab(tab);
    if (generatedBrief?.metadata?.refinementTarget !== tab) {
      setReviewMode("clean");
    }
  }

  function updateDecisionMaker(
    index: number,
    field: keyof DecisionMakerContext,
    value: string
  ) {
    setDecisionMakers((current) =>
      current.map((person, personIndex) =>
        personIndex === index ? { ...person, [field]: value } : person
      )
    );
  }

  function addPerson(roleType: PersonRoleType) {
    setPeopleView(roleType);
    setDecisionMakers((current) => [
      ...current,
      {
        name: "",
        title: "",
        source: "Customer-approved profile notes",
        context: "",
        roleType,
        organizationalRole: "",
        influence: roleType === "stakeholder" ? "medium" : undefined,
        stance: roleType === "stakeholder" ? "neutral" : undefined,
      },
    ]);
  }

  function reclassifyPerson(index: number, roleType: PersonRoleType) {
    setDecisionMakers((current) =>
      current.map((person, personIndex) =>
        personIndex === index
          ? {
              ...person,
              roleType,
              influence: roleType === "stakeholder" ? person.influence ?? "medium" : undefined,
              stance: roleType === "stakeholder" ? person.stance ?? "neutral" : undefined,
            }
          : person
      )
    );
    setPeopleView(roleType);
  }

  function removeDecisionMaker(index: number) {
    setDecisionMakers((current) =>
      current.filter((_, personIndex) => personIndex !== index)
    );
  }
  function projectIdentifier(value: string) {
    return value
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 64) || "client";
  }

  function pipelineClientIdentifier(value: string) {
    const knownScenario = scenarios.some(
      (scenario) => projectIdentifier(scenario.company) === projectIdentifier(value)
    );
    return knownScenario ? projectIdentifier(value) : "custom-demo";
  }

  function currentPacketVersion() {
    return generatedBrief?.metadata?.packetVersion || briefVersion;
  }

  function agentSessionId() {
    if (agentSessionIdRef.current) {
      return agentSessionIdRef.current;
    }

    const storageKey = "pillarprep.agent-session.v1";
    const stored = window.localStorage.getItem(storageKey);
    if (stored && /^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$/.test(stored)) {
      agentSessionIdRef.current = stored;
      return stored;
    }

    const created = "session-" + window.crypto.randomUUID();
    agentSessionIdRef.current = created;
    window.localStorage.setItem(storageKey, created);
    return created;
  }

  function pipelineIdempotencyKey(action: PipelineJobAction) {
    return action.replace(/\./g, "-") + "-" + window.crypto.randomUUID();
  }

  async function startWorkspaceLogin() {
    if (!authConfig || !cognitoAuthConfigured(authConfig)) {
      setAuthError("Authenticated workspace login is not configured.");
      return;
    }
    setAuthError("");
    await beginCognitoLogin(authConfig);
  }

  function leaveWorkspace() {
    if (!authConfig) {
      return;
    }
    setAuthSession(null);
    setEvidenceDocuments([]);
    setEvidenceError("");
    setEvidenceNotice("");
    setEvidenceBusyDocumentId("");
    setIsEvidenceLoading(false);
    removeMeetingAudio();
    signOutCognito(authConfig);
  }

  async function signedPipelineRequest(
    path: string,
    method: "GET" | "POST",
    payload?: unknown,
    signal?: AbortSignal
  ) {
    if (!hostedJobsMode || !hostedCredentials) {
      throw new Error("The unified AWS job pipeline is not configured.");
    }
    let response: Response;
    if (authSession && authConfig) {
      const valid = await validCognitoIdToken(authConfig, authSession);
      if (valid.session.expiresAt !== authSession.expiresAt) {
        setAuthSession(valid.session);
      }
      response = await bearerApiFetch(
        pipelineApiUrl(hostedWorkspaceUrl, "workspace/" + path),
        valid.token,
        { method, payload, signal }
      );
    } else {
      response = await signedApiFetch(
        pipelineApiUrl(hostedJobsUrl, path),
        hostedCredentials,
        hostedBackendRegion,
        { method, payload, signal }
      );
    }
    return readApiJson(response);
  }

  async function workspacePipelineRequest(
    path: string,
    method: "GET" | "POST",
    payload?: unknown,
    signal?: AbortSignal
  ) {
    if (!hostedJobsMode || !authConfig || !authSession) {
      throw new Error("Sign in to the private workspace to use meeting audio.");
    }
    const valid = await validCognitoIdToken(authConfig, authSession);
    if (valid.session.expiresAt !== authSession.expiresAt) {
      setAuthSession(valid.session);
    }
    const response = await bearerApiFetch(
      pipelineApiUrl(hostedWorkspaceUrl, "workspace/" + path),
      valid.token,
      { method, payload, signal }
    );
    return readApiJson(response);
  }

  async function requestPipelineJob<TResult = BriefResponse>(
    action: PipelineJobAction,
    input: Record<string, unknown>,
    selectedCompany = company,
    options: {
      signal?: AbortSignal;
      onStatus?: (status: PipelineJobState) => void;
      onProgress?: (status: PipelineJobStatus) => void;
      onRetry?: (delayMs: number) => void;
      timeoutMs?: number;
    } = {}
  ) {
    const { timeoutMs = 720_000, ...pollOptions } = options;
    const clientId = pipelineClientIdentifier(selectedCompany);
    const projectId = clientId;
    const sessionId = agentSessionId();
    const request: PipelineJobRequest = {
      action,
      clientId,
      projectId,
      sessionId,
      idempotencyKey: pipelineIdempotencyKey(action),
      input,
    };
    const accepted = parsePipelineAccepted(
      await signedPipelineRequest("jobs", "POST", request, pollOptions.signal),
      { clientId, projectId }
    );
    const result = await pollPipelineJob<TResult>(
      accepted,
      (pollSignal) =>
        signedPipelineRequest(
          `jobs/${accepted.jobId}?clientId=${encodeURIComponent(clientId)}&projectId=${encodeURIComponent(projectId)}&sessionId=${encodeURIComponent(sessionId)}`,
          "GET",
          undefined,
          pollSignal
        ),
      (milliseconds) =>
        new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds)),
      timeoutMs,
      pollOptions
    );
    if (action === "meeting.process" || action === "meeting.approve" || action.startsWith("evidence.")) {
      return result;
    }
    const expectedProvider =
      action === "handoff.generate" || action === "catchup.generate"
        ? "agentcore"
        : "bedrock";
    const normalized = normalizeBriefResponse(result, expectedProvider);
    if (normalized.metadata?.fallbackUsed) {
      throw new Error("The live AWS job did not complete through its required provider.");
    }
    return normalized as TResult;
  }

  async function recoverCurrentBrief(error: unknown, operation: "refinement" | "approval") {
    const message = error instanceof Error ? error.message : "";
    if (!/brief changed before (?:refinement|approval)/i.test(message)) {
      return false;
    }
    const clientId = pipelineClientIdentifier(company);
    const projectId = clientId;
    const sessionId = agentSessionId();
    const current = parseCurrentPacket(
      await signedPipelineRequest(
        `clients/${encodeURIComponent(clientId)}/current?projectId=${encodeURIComponent(projectId)}&sessionId=${encodeURIComponent(sessionId)}`,
        "GET"
      ),
      { clientId, projectId }
    );
    const packet = normalizeBriefResponse(
      current.packet,
      current.packet.provider === "agentcore" ? "agentcore" : "bedrock"
    );
    const isApproved = current.approvalStatus === "approved";
    setGeneratedBrief(packet);
    setBriefVersion(current.packetVersion);
    setApproved(isApproved);
    setApprovalStale(current.approvalStatus === "stale");
    setPromoted(false);
    setSelectedHistoryId(null);
    setReviewMode("clean");
    setPrecallHandoffStatus(isApproved ? "idle" : "stale");
    setPrecallHandoffError("");
    setGenerationError("");
    setGenerationNotice(
      `A newer saved brief v${current.packetVersion} was loaded. Review it, then ${
        operation === "refinement" ? "apply the pending feedback again" : "approve this version"
      }.`
    );
    return true;
  }


  async function loadEvidenceDocuments() {
    if (!authSession) {
      setEvidenceDocuments([]);
      setEvidenceError("Sign in before opening private customer evidence.");
      return;
    }
    const clientId = pipelineClientIdentifier(company);
    const projectId = clientId;
    const sessionId = agentSessionId();
    setIsEvidenceLoading(true);
    setEvidenceError("");
    try {
      const response = await signedPipelineRequest(
        "evidence?clientId=" +
          encodeURIComponent(clientId) +
          "&projectId=" +
          encodeURIComponent(projectId) +
          "&sessionId=" +
          encodeURIComponent(sessionId),
        "GET"
      );
      setEvidenceDocuments(parseEvidenceDocuments(response));
    } catch (error) {
      setEvidenceError(
        error instanceof Error
          ? error.message
          : "Approved evidence could not be loaded."
      );
    } finally {
      setIsEvidenceLoading(false);
    }
  }

  async function uploadEvidence(input: EvidenceUpload) {
    if (!authSession) {
      setEvidenceError("Sign in before adding private customer evidence.");
      return;
    }
    setEvidenceBusyDocumentId("upload");
    setEvidenceError("");
    setEvidenceNotice("");
    try {
      await requestPipelineJob<EvidenceDocumentRecord>(
        "evidence.ingest",
        input,
        company
      );
      setEvidenceNotice(
        "Evidence accepted for " + company + " and queued for Knowledge Base indexing."
      );
      await loadEvidenceDocuments();
    } catch (error) {
      setEvidenceError(
        error instanceof Error ? error.message : "Evidence could not be added."
      );
      throw error;
    } finally {
      setEvidenceBusyDocumentId("");
    }
  }

  async function mutateEvidence(
    action: "evidence.reindex" | "evidence.delete",
    documentId: string
  ) {
    if (!authSession) {
      setEvidenceError("Sign in before changing private customer evidence.");
      return;
    }
    setEvidenceBusyDocumentId(documentId);
    setEvidenceError("");
    setEvidenceNotice("");
    try {
      const result = await requestPipelineJob<EvidenceDocumentRecord>(
        action,
        { documentId },
        company
      );
      setEvidenceNotice(
        action === "evidence.delete"
          ? result.status === "DELETION_PENDING"
            ? "Deletion is pending the next Knowledge Base synchronization."
            : "Evidence deletion started."
          : "Evidence re-indexing started."
      );
      await loadEvidenceDocuments();
    } catch (error) {
      setEvidenceError(
        error instanceof Error ? error.message : "Evidence could not be updated."
      );
    } finally {
      setEvidenceBusyDocumentId("");
    }
  }

  function pushBriefHistory(
    nextBrief: BriefResponse,
    nextBriefVersion: number,
    nextApproved: boolean,
    nextPromoted: boolean,
    nextRefinementTarget: BriefTab | undefined,
    nextAppliedDrafts: RefinementDrafts
  ) {
    historyEntryCounterRef.current += 1;
    const historyEntryId = `${(company || "client").toLowerCase().replace(/[^a-z0-9]+/g, "-") || "client"}-${nextBriefVersion}-${historyEntryCounterRef.current}`;
    const historyEntry: BriefHistoryEntry = {
      id: historyEntryId,
      savedAt: nextBrief.generatedAt || new Date().toISOString(),
      scenarioId,
      company,
      industry,
      meetingType,
      companySize,
      selectedPillars: [...selectedPillars],
      context,
      companyValues,
      companyValuesUrl,
      additionalDirection,
      decisionMakers: cloneDecisionMakers(usableDecisionMakers),
      meetingNotes,
      feedback: nextRefinementTarget
        ? [...refinementDrafts[nextRefinementTarget].feedback]
        : [],
      feedbackNotes: nextRefinementTarget
        ? refinementDrafts[nextRefinementTarget].feedbackNotes
        : "",
      refinementTarget: nextRefinementTarget,
      refinementDrafts: cloneRefinementDrafts(refinementDrafts),
      appliedRefinementDrafts: cloneRefinementDrafts(nextAppliedDrafts),
      modelPreference,
      briefVersion: nextBriefVersion,
      approved: nextApproved,
      promoted: nextPromoted,
      generatedBrief: nextBrief,
    };

    setBriefHistory((current) => [historyEntry, ...current].slice(0, 8));
    setSelectedHistoryId(historyEntry.id);
  }

  function loadBriefHistoryEntry(entry: BriefHistoryEntry) {
    cancelPacketRequest();
    const matchedScenario = scenarios.find(
      (scenario) =>
        scenario.id === entry.scenarioId ||
        (scenario.company === entry.company &&
          scenario.industry === entry.industry &&
          scenario.meetingType === entry.meetingType)
    );

    setScenarioId(matchedScenario?.id ?? scenarioId);
    setCompany(entry.company);
    setIndustry(entry.industry);
    setMeetingType(entry.meetingType);
    setCompanySize(entry.companySize);
    setSelectedPillars(normalizePillarRanking(entry.selectedPillars));
    setContext(entry.context);
    setCompanyValues(entry.companyValues);
    setCompanyValuesUrl(entry.companyValuesUrl ?? "");
    setAdditionalDirection(entry.additionalDirection ?? "");
    setDecisionMakers(cloneDecisionMakers(entry.decisionMakers));
    setMeetingNotes(entry.meetingNotes);
    const entryTarget = entry.refinementTarget ?? "businessCase";
    const entryDrafts = normalizeRefinementDrafts(
      entry.refinementDrafts,
      entryTarget,
      entry.feedback,
      entry.feedbackNotes
    );
    setRefinementDrafts(entryDrafts);
    setAppliedRefinementDrafts(
      entry.appliedRefinementDrafts
        ? normalizeRefinementDrafts(
            entry.appliedRefinementDrafts,
            entryTarget
          )
        : cloneRefinementDrafts(entryDrafts)
    );
    setModelPreference(
      entry.modelPreference === "nova-micro" || entry.modelPreference === "claude-sonnet-4.6"
        ? entry.modelPreference
        : "nova-pro"
    );
    setBriefVersion(entry.briefVersion);
    setApproved(entry.approved);
    setPromoted(entry.promoted);
    setGeneratedBrief(entry.generatedBrief);
    const savedRole = entry.generatedBrief.metadata?.handoffAudienceRole as AudienceRole | undefined;
    const savedFocus = entry.generatedBrief.metadata?.handoffFocus;
    if (savedRole && rolePrompts[savedRole] && savedFocus) {
      setRole(savedRole);
      setActivePrompt(savedFocus);
    }
    setSelectedHistoryId(entry.id);
    setGenerationError("");
    setGenerationNotice("");
    setActiveTab(entryTarget);
    setReviewMode("clean");
    setApprovalStale(false);
    clearCopyFeedback();
    setSelectedLifecycleStage(
      entry.generatedBrief.metadata?.meetingApprovalStatus === "approved"
        ? "follow-up"
        : entry.promoted
          ? "meeting-prep"
          : entry.approved
            ? "meeting-prep"
            : "insights"
    );
    setActivePage("brief");
  }

  function resetWorkspace() {
    pipelineAbortRef.current?.abort(
      new DOMException("The workspace was reset.", "AbortError")
    );
    catchupAbortRef.current?.abort(
      new DOMException("The workspace was reset.", "AbortError")
    );
    window.localStorage.removeItem(workspaceStorageKey);
    window.localStorage.removeItem(legacyWorkspaceStorageKey);
    setBriefHistory([]);
    setSelectedHistoryId(null);
    setRefinementDrafts(createRefinementDrafts());
    setAppliedRefinementDrafts(createRefinementDrafts());
    setRole(defaultRole);
    setActivePrompt(rolePrompts[defaultRole][0]);
    setActivePage("setup");
    setSelectedLifecycleStage("research");
    setGateDecisions({});
    setPendingSectionId(null);
    setCatchupFilter("all");
    setCatchupAnswer("");
    setCatchupError("");
    setCatchupSource("");
    agentSessionIdRef.current = "";
    window.localStorage.removeItem("pillarprep.agent-session.v1");
    window.localStorage.removeItem("pillarprep.demo-workspace.v1");
    setModelPreference("nova-pro");
    clearCopyFeedback();
    setGenerationNotice("");
    loadScenario(scenarios[0]);
  }

  function cancelPacketRequest() {
    packetRequestEpochRef.current += 1;
    pipelineAbortRef.current?.abort(new DOMException("The selected packet changed.", "AbortError"));
    pipelineAbortRef.current = null;
    generationRequestRef.current = false;
    setPipelineJobStatus(null);
    setIsGenerating(false);
    setRefiningTarget(null);
    setPrecallHandoffStatus("idle");
    setPrecallHandoffError("");
  }

  async function requestBrief(
    mode: "prebrief" | "project" = "prebrief",
    options: { forceNew?: boolean } = {}
  ) {
    if (generationRequestRef.current) {
      return;
    }

    if (mode === "project" && (!generatedBrief || !approved)) {
      setGenerationNotice("");
      setGenerationError("Approve the current brief before generating the handoff.");
      setActivePage("brief");
      return;
    }
    if (mode === "project" && pendingIntakeChanges) {
      setGenerationError("The customer inputs have changed. Update the brief before building its handoff.");
      return;
    }
    const requestRole = role;
    const requestPrompt = activePrompt;
    const requestEpoch = packetRequestEpochRef.current;
    const approvedBrief = buildApprovedBriefSnapshot(generatedBrief);
    const isRefinement = mode === "prebrief" && Boolean(generatedBrief) && !options.forceNew;
    const requestRefinementTarget = isRefinement ? activeTab : undefined;
    const requestBaseVersion = currentPacketVersion();
    const submittedDraft = requestRefinementTarget
      ? refinementDrafts[requestRefinementTarget]
      : { feedback: [], feedbackNotes: "" };
    const approvalTransition = approvalAfterGeneration(mode, approved);
    const preserveExistingOutput = mode === "project" || isRefinement;
    const briefRequest: BriefRequest = {
      mode,
      modelPreference,
      qualityTier: selectedQualityTier,
      company,
      industry,
      meetingType,
      companySize,
      pillars: selectedPillars,
      pillarRanking,
      context,
      companyValues,
      companyValuesUrl,
      additionalDirection,
      meetingNotes,
      feedback: isRefinement ? [...submittedDraft.feedback] : [],
      feedbackDetails: isRefinement ? structuredFeedback(submittedDraft.feedback) : [],
      feedbackNotes: isRefinement ? submittedDraft.feedbackNotes.trim() : "",
      baseBriefVersion: isRefinement ? requestBaseVersion : undefined,
      refinementTarget: requestRefinementTarget,
      previousBrief: isRefinement ? approvedBrief : undefined,
      decisionMakers: usableDecisionMakers,
      role: requestRole,
      prompt: requestPrompt,
      approvedBrief:
        mode === "project" || (mode === "prebrief" && options.forceNew && meetingUpdateApproved)
          ? approvedBrief
          : undefined,
    };
    const validationError = validateBriefRequest(briefRequest);

    if (validationError) {
      setGenerationNotice("");
      setGenerationError(validationError);
      return;
    }

    generationRequestRef.current = true;
    const controller = new AbortController();
    pipelineAbortRef.current = controller;
    setGenerationStageMode(mode);
    setPipelineJobStatus(hostedJobsMode ? "queued" : null);
    setIsGenerating(true);
    setRefiningTarget(requestRefinementTarget ?? null);
    setGenerationError("");
    setGenerationNotice("");
    clearCopyFeedback();
    if (mode === "project") {
      setPrecallHandoffStatus("preparing");
      setPrecallHandoffError("");
    }

    if (!preserveExistingOutput) {
      setGeneratedBrief(null);
      setPromoted(false);
      setApproved(approvalTransition.approved);
      setApprovalStale(approvalTransition.stale);
    }

    try {
      let nextBrief: BriefResponse;
      let liveFallbackNotice = "";
      if (hostedJobsMode) {
        const pipelineAction: PipelineJobAction =
          mode === "project"
            ? "handoff.generate"
            : isRefinement
              ? "brief.refine"
              : "brief.generate";
        const pipelineInput: Record<string, unknown> =
          mode === "project"
            ? {
                audienceRole: requestRole,
                focus: requestPrompt,
                meetingNotes,
                modelPreference,
                qualityTier: selectedQualityTier,
                expectedApprovedPacketVersion: requestBaseVersion,
              }
            : { ...briefRequest };
        try {
          nextBrief = await requestPipelineJob(
            pipelineAction,
            pipelineInput,
            briefRequest.company,
            {
              signal: controller.signal,
              onStatus: (status) =>
                setPipelineJobStatus(activePipelineStatus(status)),
            }
          );
          if (controller.signal.aborted || requestEpoch !== packetRequestEpochRef.current) return;
          setGenerationMode("live");
        } catch (liveError) {
          if (!isPublicDemoAccessError(liveError)) {
            throw liveError;
          }
          nextBrief = fallbackBriefForRequest(briefRequest);
          liveFallbackNotice = publicDemoFallbackNotice(liveError);
          setPipelineJobStatus(null);
          setGenerationMode("demo");
        }
      } else {
        nextBrief = fallbackBriefForRequest(briefRequest);
        setGenerationMode("demo");
      }

      if (controller.signal.aborted || requestEpoch !== packetRequestEpochRef.current) return;

      let refinementComparison = null;
      if (requestRefinementTarget && generatedBrief) {
        refinementComparison = compareBriefVersions(generatedBrief, nextBrief);
        const unauthorizedSections =
          refinementComparison.changedSectionNames.filter(
            (section) => section !== requestRefinementTarget
          );
        if (unauthorizedSections.length) {
          throw new Error(
            "Refinement was rejected because it changed content outside " +
              briefTabLabel(requestRefinementTarget) +
              ". The current packet was preserved."
          );
        }
        if (
          nextBrief.metadata?.refinementTarget &&
          nextBrief.metadata.refinementTarget !== requestRefinementTarget
        ) {
          throw new Error("Refinement target mismatch. The current packet was preserved.");
        }
        if (nextBrief.metadata?.refinementIsolationPassed === false) {
          throw new Error("Refinement isolation validation failed. The current packet was preserved.");
        }
      }

      const nextApproved = mode === "project";
      const nextPromoted = mode === "project";
      const nextBriefVersion =
        mode === "project"
          ? requestBaseVersion
          : nextBrief.metadata?.packetVersion || requestBaseVersion + 1;
      const nextAppliedDrafts = cloneRefinementDrafts(appliedRefinementDrafts);
      if (requestRefinementTarget) {
        nextAppliedDrafts[requestRefinementTarget] = {
          feedback: [...submittedDraft.feedback],
          feedbackNotes: submittedDraft.feedbackNotes,
        };
      }

      if (mode === "project") {
        nextBrief = mergeHandoffPacket(generatedBrief!, nextBrief, {
          company,
          clientId: pipelineClientIdentifier(company),
          projectId: pipelineClientIdentifier(company),
          packetVersion: requestBaseVersion,
          audienceRole: requestRole,
          focus: requestPrompt,
        });
        setPrecallHandoffStatus("ready");
        setPrecallHandoffError("");
      }
      setGeneratedBrief(nextBrief);
      if (liveFallbackNotice && !requestRefinementTarget) {
        setGenerationNotice(liveFallbackNotice);
      }
      if (requestRefinementTarget && refinementComparison) {
        const passageLabel =
          refinementComparison.changedPassages === 1 ? " passage" : " passages";
        setGenerationNotice(
          refinementComparison.changedPassages
            ? String(refinementComparison.changedPassages) +
                passageLabel +
                " updated in " +
                briefTabLabel(requestRefinementTarget) +
                ". Every other brief was preserved."
            : "The model returned no material changes for this tab. Add more specific feedback and apply it again."
        );
        if (activeTabRef.current === requestRefinementTarget) {
          setReviewMode("changes");
        }
      } else if (!requestRefinementTarget) {
        setReviewMode("clean");
      }
      pushBriefHistory(
        nextBrief,
        nextBriefVersion,
        nextApproved,
        nextPromoted,
        requestRefinementTarget,
        nextAppliedDrafts
      );
      setAppliedRefinementDrafts(nextAppliedDrafts);
      setPromoted(nextPromoted);

      if (mode === "project") {
        setApproved(true);
        setApprovalStale(false);
      } else {
        setBriefVersion(nextBriefVersion);
        setApproved(false);
        setApprovalStale(Boolean(requestRefinementTarget));
      }
    } catch (error) {
      if (requestEpoch !== packetRequestEpochRef.current) return;
      if (error instanceof DOMException && error.name === "AbortError") {
        setGenerationNotice("The AI request was cancelled. Your current packet was preserved.");
        if (mode === "project") {
          setPrecallHandoffStatus("idle");
        }
      } else if (await recoverCurrentBrief(error, "refinement")) {
        // The pending feedback remains in the editor and can be applied to the recovered version.
      } else {
        const message = error instanceof Error ? error.message : "Brief generation failed";
        setGenerationError(message);
        if (mode === "project") {
          setPrecallHandoffStatus("failed");
          setPrecallHandoffError(message);
        }
      }
    } finally {
      if (pipelineAbortRef.current === controller) {
        pipelineAbortRef.current = null;
        generationRequestRef.current = false;
        setPipelineJobStatus(null);
        setIsGenerating(false);
        setRefiningTarget(null);
      }
    }
  }

  function generateBrief() {
    setSelectedLifecycleStage("insights");
    setActivePage("brief");
    void requestBrief("prebrief", { forceNew: true });
  }

  function refineBrief() {
    setSelectedLifecycleStage(
      activeTab === "businessCase" || activeTab === "executive"
        ? "insights"
        : "discovery"
    );
    setActivePage("brief");
    void requestBrief("prebrief");
  }

  async function approveBrief() {
    if (!approvalReady) {
      return;
    }

    if (hostedJobsMode) {
      if (generationRequestRef.current) {
        return;
      }
      generationRequestRef.current = true;
      const requestEpoch = packetRequestEpochRef.current;
      const controller = new AbortController();
      pipelineAbortRef.current = controller;
      setPipelineJobStatus("queued");
      setIsGenerating(true);
      setGenerationError("");
      setGenerationNotice("");
      try {
        const approvedBrief = await requestPipelineJob(
          "brief.approve",
          {
            packetVersion:
              generatedBrief?.metadata?.packetVersion || briefVersion,
          },
          company,
          {
            signal: controller.signal,
            onStatus: (status) =>
              setPipelineJobStatus(activePipelineStatus(status)),
          }
        );
        if (controller.signal.aborted || requestEpoch !== packetRequestEpochRef.current) return;
        setGeneratedBrief(approvedBrief);
        setBriefVersion(approvedBrief.metadata?.packetVersion || briefVersion);
        setApproved(true);
        setApprovalStale(false);
        setSelectedLifecycleStage("meeting-prep");
        setActivePage("project");
        setBriefHistory((current) =>
          current.map((entry) =>
            entry.id === selectedHistoryId
              ? { ...entry, approved: true, generatedBrief: approvedBrief }
              : entry
          )
        );
        setPrecallHandoffStatus("idle");
        setPrecallHandoffError("");
        setGenerationNotice(
          "Approved for the meeting. Build the pre-call handoff when the team is ready."
        );
      } catch (error) {
        if (requestEpoch !== packetRequestEpochRef.current) return;
        if (error instanceof DOMException && error.name === "AbortError") {
          setGenerationNotice("Approval was cancelled. The draft remains unchanged.");
        } else if (await recoverCurrentBrief(error, "approval")) {
          // Require a human to review the recovered server version before approving again.
        } else {
          setGenerationError(
            error instanceof Error
              ? `Brief approval failed: ${error.message}`
              : "Brief approval failed."
          );
        }
      } finally {
        if (pipelineAbortRef.current === controller) {
          pipelineAbortRef.current = null;
          generationRequestRef.current = false;
          setPipelineJobStatus(null);
          setIsGenerating(false);
        }
      }
      return;
    }

    setApproved(true);
    setApprovalStale(false);
    setPrecallHandoffStatus("idle");
    setPrecallHandoffError("");
    setSelectedLifecycleStage("meeting-prep");
    setActivePage("project");
    setBriefHistory((current) =>
      current.map((entry) =>
        entry.id === selectedHistoryId ? { ...entry, approved: true } : entry
      )
    );
  }

  function refreshProjectModel() {
    setSelectedLifecycleStage("meeting-prep");
    setActivePage("project");
    void requestBrief("project");
  }


  function setMeetingDecision(decision: MeetingReviewDecision) {
    setMeetingDecisions((current) => ({ ...current, [decision.id]: decision }));
    setMeetingNotice("");
    setMeetingError("");
  }

  function acceptAllMeetingUpdates() {
    if (!meetingResult || isMeetingProcessing || isMeetingApproving) return;
    const accepted = Object.fromEntries(
      meetingResult.reviewItems.map((item) => [
        item.id,
        { id: item.id, decision: "accepted" as const },
      ])
    );
    setMeetingDecisions(accepted);
    setMeetingNotice("All proposed changes are selected. Review them before final approval.");
    setMeetingError("");
  }

  async function waitForMeetingAudioScan(
    uploadId: string,
    clientId: string,
    projectId: string,
    sessionId: string,
    signal: AbortSignal
  ): Promise<"clean" | "blocked" | "scan_failed"> {
    const deadline = Date.now() + 300_000;
    let pollAfterMs = 1800;
    let failures = 0;
    let pendingScans = 0;
    while (Date.now() < deadline) {
      if (signal.aborted) {
        throw signal.reason ?? new DOMException("Audio scan cancelled.", "AbortError");
      }
      await new Promise<void>((resolve, reject) => {
        const timer = window.setTimeout(() => {
          signal.removeEventListener("abort", cancel);
          resolve();
        }, Math.min(pollAfterMs, Math.max(0, deadline - Date.now())));
        const cancel = () => {
          window.clearTimeout(timer);
          signal.removeEventListener("abort", cancel);
          reject(signal.reason ?? new DOMException("Audio scan cancelled.", "AbortError"));
        };
        signal.addEventListener("abort", cancel, { once: true });
      });
      if (Date.now() >= deadline) break;
      let response: unknown;
      try {
        response = await workspacePipelineRequest(
          "meeting-audio/uploads/" +
            encodeURIComponent(uploadId) +
            "?clientId=" +
            encodeURIComponent(clientId) +
            "&projectId=" +
            encodeURIComponent(projectId) +
            "&sessionId=" +
            encodeURIComponent(sessionId),
          "GET",
          undefined,
          signal
        );
        failures = 0;
      } catch (error) {
        const retryDelay = readRetryDelay(error, ++failures);
        if (retryDelay === undefined) throw error;
        pollAfterMs = retryDelay;
        setMeetingNotice("The malware scan is continuing. Reconnecting to its status...");
        continue;
      }
      if (typeof response !== "object" || response === null) {
        throw new Error("The audio malware scan returned an invalid status.");
      }
      const status = (response as Record<string, unknown>).status;
      if (status === "pending_scan") {
        pollAfterMs = Math.min(5000, 1800 + ++pendingScans * 300);
        setMeetingNotice("Audio uploaded securely. The malware scan is running.");
        continue;
      }
      if (status === "clean" || status === "processing") return "clean";
      if (status === "blocked" || status === "scan_failed") return status;
      throw new Error("The audio malware scan returned an unknown status.");
    }
    throw new Error(
      "The audio malware scan is taking longer than expected. Remove the file and try again."
    );
  }

  async function uploadMeetingAudio(file: File, consentAcknowledged: boolean) {
    const extension = file.name.split(".").pop()?.toLowerCase() || "";
    const contentTypes: Record<string, string> = {
      mp3: "audio/mpeg",
      wav: "audio/wav",
      m4a: "audio/mp4",
    };
    if (!contentTypes[extension] || file.size < 1 || file.size > 25 * 1024 * 1024) {
      setMeetingAudio({ fileName: file.name, sizeBytes: file.size, status: "failed" });
      setMeetingError("Choose an MP3, WAV, or M4A file no larger than 25 MB.");
      return;
    }
    if (scenarioId !== "bluemesa" || !approved || !hostedJobsMode) {
      setMeetingError("Select and approve the Blue Mesa packet before uploading meeting audio.");
      return;
    }
    if (!authSession) {
      setMeetingError("Sign in to the private workspace before uploading meeting audio.");
      return;
    }
    meetingUploadAbortRef.current?.abort(
      new DOMException("A new audio file was selected.", "AbortError")
    );
    const uploadController = new AbortController();
    meetingUploadAbortRef.current = uploadController;
    setMeetingAudio({ fileName: file.name, sizeBytes: file.size, status: "uploading" });
    setMeetingAudioUploadId("");
    setMeetingError("");
    setMeetingNotice("");
    try {
      const clientId = pipelineClientIdentifier(company);
      const projectId = clientId;
      const sessionId = agentSessionId();
      const upload = await workspacePipelineRequest(
        "meeting-audio/uploads",
        "POST",
        {
          clientId,
          projectId,
          sessionId,
          scenarioId: "blue-mesa-payments",
          meetingId: "blue-mesa-discovery",
          fileName: file.name,
          contentType: contentTypes[extension],
          sizeBytes: file.size,
          consentAcknowledged,
        }
      );
      if (
        typeof upload !== "object" ||
        upload === null ||
        typeof (upload as Record<string, unknown>).uploadId !== "string" ||
        typeof (upload as Record<string, unknown>).uploadUrl !== "string" ||
        typeof (upload as Record<string, unknown>).uploadFields !== "object"
      ) {
        throw new Error("AWS did not return a valid private upload target.");
      }
      const record = upload as {
        uploadId: string;
        uploadUrl: string;
        uploadFields: Record<string, string>;
      };
      const form = new FormData();
      Object.entries(record.uploadFields).forEach(([key, value]) => form.append(key, value));
      form.append("file", file);
      const uploaded = await fetch(record.uploadUrl, { method: "POST", body: form });
      if (!uploaded.ok) {
        throw new Error("The audio could not be uploaded to the private meeting workspace.");
      }
      setMeetingAudioUploadId(record.uploadId);
      setMeetingAudio({ fileName: file.name, sizeBytes: file.size, status: "scanning" });
      setMeetingNotice("Audio uploaded securely. The malware scan is running.");
      const scanStatus = await waitForMeetingAudioScan(
        record.uploadId,
        clientId,
        projectId,
        sessionId,
        uploadController.signal
      );
      if (scanStatus === "clean") {
        setMeetingAudio({ fileName: file.name, sizeBytes: file.size, status: "ready" });
        setMeetingNotice("Malware scan complete. The audio is ready to process.");
      } else if (scanStatus === "blocked") {
        setMeetingAudio({ fileName: file.name, sizeBytes: file.size, status: "blocked" });
        setMeetingNotice("");
        setMeetingError("This audio upload was blocked. Remove it and upload a new file.");
      } else {
        setMeetingAudio({ fileName: file.name, sizeBytes: file.size, status: "scan_failed" });
        setMeetingNotice("");
        setMeetingError("The audio malware scan could not complete. Remove it and upload again.");
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        return;
      }
      setMeetingAudio({ fileName: file.name, sizeBytes: file.size, status: "failed" });
      setMeetingNotice("");
      setMeetingError(
        error instanceof TypeError
          ? "The browser could not reach the private audio workspace. Refresh and try the upload again."
          : error instanceof Error
            ? error.message
            : "Meeting audio upload failed."
      );
    } finally {
      if (meetingUploadAbortRef.current === uploadController) {
        meetingUploadAbortRef.current = null;
      }
    }
  }

  function removeMeetingAudio() {
    if (isMeetingProcessing) return;
    meetingUploadAbortRef.current?.abort(
      new DOMException("Audio upload removed.", "AbortError")
    );
    meetingUploadAbortRef.current = null;
    setMeetingAudio({ fileName: "", sizeBytes: 0, status: "empty" });
    setMeetingAudioUploadId("");
    setMeetingNotice("");
    setMeetingError("");
  }


  async function processSyntheticMeeting() {
    if (meetingRequestRef.current) return;
    if (scenarioId !== "bluemesa") {
      setMeetingError("Select the Blue Mesa Payments scenario before processing the synthetic meeting.");
      return;
    }
    if (!approved || !generatedBrief) {
      setMeetingError("Approve the Blue Mesa briefing packet before processing the meeting.");
      return;
    }
    if (!hostedJobsMode) {
      setMeetingError("The live AWS jobs endpoint is required for meeting processing.");
      return;
    }
    if (
      !meetingAudioUploadId ||
      !["scanning", "ready"].includes(meetingAudio.status)
    ) {
      setMeetingError("Upload the synthetic meeting audio before processing it.");
      return;
    }

    meetingRequestRef.current = true;
    const controller = new AbortController();
    meetingAbortRef.current = controller;
    setIsMeetingProcessing(true);
    setMeetingJobStatus("queued");
    setMeetingError("");
    setMeetingNotice("");
    const approvedPacketVersion =
      generatedBrief.metadata?.approvedPacketVersion ||
      generatedBrief.metadata?.packetVersion ||
      briefVersion;

    try {
      const result = await requestPipelineJob<MeetingProcessResult>(
        "meeting.process",
        {
          scenarioId: "blue-mesa-payments",
          meetingId: "blue-mesa-discovery",
          audioUploadId: meetingAudioUploadId,
          expectedApprovedPacketVersion: approvedPacketVersion,
        },
        company,
        {
          signal: controller.signal,
          timeoutMs: 600_000,
          onStatus: (status) => {
            setMeetingJobStatus(status);
            setMeetingNotice("");
          },
          onRetry: () => setMeetingNotice("Processing continues. Reconnecting to meeting status..."),
        }
      );
      if (
        result.status !== "review-ready" ||
        result.scenarioId !== "blue-mesa-payments" ||
        !result.reviewItems?.length
      ) {
        throw new Error("AWS returned an incomplete meeting review.");
      }
      setMeetingResult(result);
      setMeetingDecisions({});
      setMeetingJobStatus("review-ready");
      setMeetingNotice("Transcript analysis is ready. Review every proposed update before approval.");
      setSelectedLifecycleStage("meeting-prep");
    } catch (error) {
      setMeetingJobStatus("failed");
      setMeetingNotice("");
      setMeetingError(
        error instanceof DOMException && error.name === "AbortError"
          ? "Meeting processing was cancelled. The approved brief is unchanged."
          : error instanceof Error
            ? `Meeting processing failed: ${error.message}`
            : "Meeting processing failed. The approved brief is unchanged."
      );
    } finally {
      if (meetingAbortRef.current === controller) meetingAbortRef.current = null;
      meetingRequestRef.current = false;
      setIsMeetingProcessing(false);
    }
  }

  async function approveMeetingUpdates() {
    if (meetingRequestRef.current || !meetingResult) return;
    const dispositions = meetingResult.reviewItems.map((item) => meetingDecisions[item.id]);
    if (dispositions.some((item) => !item)) {
      setMeetingError("Accept, edit, or reject every proposed update before approval.");
      return;
    }
    if (
      dispositions.some(
        (item) => item.decision === "edited" && !item.editedStatement?.trim()
      )
    ) {
      setMeetingError("Edited updates need a project statement before approval.");
      return;
    }
    if (!dispositions.some((item) => item.decision !== "rejected")) {
      setMeetingError("Accept or edit at least one meeting update to build the handoff.");
      return;
    }

    meetingRequestRef.current = true;
    const controller = new AbortController();
    meetingAbortRef.current = controller;
    setIsMeetingApproving(true);
    setMeetingJobStatus("queued");
    setMeetingError("");
    setMeetingNotice("");

    try {
      const rawHandoff = await requestPipelineJob<BriefResponse>(
        "meeting.approve",
        {
          scenarioId: "blue-mesa-payments",
          meetingId: meetingResult.meetingId,
          proposalId: meetingResult.proposalId,
          expectedApprovedPacketVersion: meetingResult.baseBriefVersion,
          dispositions,
        },
        company,
        {
          signal: controller.signal,
          timeoutMs: 480_000,
          onStatus: setMeetingJobStatus,
        }
      );
      if (controller.signal.aborted) return;
      const handoff = mergeHandoffPacket(generatedBrief!, normalizeBriefResponse(rawHandoff, "agentcore"), {
        company,
        clientId: pipelineClientIdentifier(company),
        projectId: pipelineClientIdentifier(company),
        packetVersion: meetingResult.baseBriefVersion,
        audienceRole: role,
        focus: activePrompt,
      });
      if (handoff.metadata?.fallbackUsed) {
        throw new Error("The approved handoff did not complete through AgentCore.");
      }
      setGeneratedBrief(handoff);
      setPromoted(true);
      setApproved(true);
      setApprovalStale(false);
      setMeetingJobStatus("approved");
      setMeetingNotes(
        [
          meetingResult.analysis.meetingSummary,
          ...meetingResult.reviewItems
            .filter((item) => meetingDecisions[item.id]?.decision !== "rejected")
            .map((item) => meetingDecisions[item.id]?.editedStatement || item.proposedUpdate),
        ].join("\n\n")
      );
      setBriefHistory((current) =>
        current.map((entry) =>
          entry.id === selectedHistoryId
            ? { ...entry, generatedBrief: handoff, approved: true, promoted: true }
            : entry
        )
      );
      setMeetingNotice("Meeting updates approved. The project handoff and catch-up context now use only the accepted evidence.");
      setSelectedLifecycleStage("follow-up");
    } catch (error) {
      setMeetingJobStatus("failed");
      setMeetingError(
        error instanceof DOMException && error.name === "AbortError"
          ? "Meeting approval was cancelled. No proposed changes were applied."
          : error instanceof Error
            ? `Meeting approval failed: ${error.message}`
            : "Meeting approval failed. No proposed changes were applied."
      );
    } finally {
      if (meetingAbortRef.current === controller) meetingAbortRef.current = null;
      meetingRequestRef.current = false;
      setIsMeetingApproving(false);
    }
  }
  async function generateRoleAwareCatchup() {
    if (catchupRequestRef.current) {
      return;
    }

    if (!catchupBrief) {
      setCatchupError("Select a client with an approved brief first.");
      return;
    }

    const selectedPillarsForCatchup =
      selectedHistoryEntry?.selectedPillars ?? selectedPillars;
    const selectedDecisionMakers =
      selectedHistoryEntry?.decisionMakers ?? usableDecisionMakers;
    const catchupRequest: BriefRequest = {
      mode: "project",
      modelPreference,
      qualityTier: selectedQualityTier,
      company: catchupCompany || company,
      industry: catchupIndustry || industry,
      meetingType: catchupMeetingType || meetingType,
      companySize: catchupCompanySize || companySize,
      pillars: selectedPillarsForCatchup,
      pillarRanking: selectedPillarsForCatchup.map((pillar, index) => ({
        rank: index + 1,
        pillar,
      })),
      context: catchupContext,
      companyValues: catchupValues,
      companyValuesUrl:
        selectedHistoryEntry?.companyValuesUrl ?? companyValuesUrl,
      additionalDirection: selectedHistoryEntry?.additionalDirection ?? additionalDirection,
      decisionMakers: selectedDecisionMakers,
      meetingNotes: catchupNotes,
      feedback: selectedHistoryEntry?.feedback ?? feedback,
      role,
      prompt: activePrompt,
      approvedBrief: buildApprovedBriefSnapshot(catchupBrief),
    };

    catchupRequestRef.current = true;
    const controller = new AbortController();
    catchupAbortRef.current = controller;
    setCatchupJobStatus(hostedJobsMode ? "queued" : null);
    setIsCatchupGenerating(true);
    setCatchupError("");
    setCatchupSource("");
    setCatchupAnswer("");

    try {
      let response: BriefResponse;
      if (hostedJobsMode) {
        response = await requestPipelineJob(
          "catchup.generate",
          {
            audienceRole: role,
            focus: activePrompt,
            meetingNotes: catchupNotes,
            modelPreference,
            qualityTier: selectedQualityTier,
          },
          catchupCompany || company,
          {
            signal: controller.signal,
            onStatus: (status) =>
              setCatchupJobStatus(activePipelineStatus(status)),
          }
        );
      } else {
        response = normalizeBriefResponse(
          generateDemoBrief(catchupRequest),
          "demo"
        );
      }

      setCatchupAnswer(response.projectAnswer);
      setCatchupSource(
        hostedJobsMode
          ? "AgentCore via durable AWS job"
          : response.metadata?.fallbackUsed
          ? "Existing Bedrock Lambda fallback"
          : providerLabel(response.provider)
      );
    } catch (error) {
      setCatchupAnswer("");
      setCatchupSource("");
      setCatchupError(
        error instanceof DOMException && error.name === "AbortError"
          ? "Catch-up was cancelled. The approved packet is unchanged."
          : error instanceof Error
            ? `Catch-up could not be generated: ${error.message} The approved packet is unchanged.`
            : "Catch-up generation is temporarily unavailable. The approved packet is unchanged."
      );
    } finally {
      if (catchupAbortRef.current === controller) {
        catchupAbortRef.current = null;
      }
      catchupRequestRef.current = false;
      setCatchupJobStatus(null);
      setIsCatchupGenerating(false);
    }
  }

  async function copyText(label: string, textToCopy: string) {
    if (!textToCopy.trim()) {
      return;
    }

    try {
      await navigator.clipboard.writeText(textToCopy);
      clearCopyFeedback();
      setCopiedLabel(label);
      copyFeedbackTimeoutRef.current = window.setTimeout(() => {
        setCopiedLabel((current) => (current === label ? "" : current));
        copyFeedbackTimeoutRef.current = null;
      }, 1800);
    } catch {
      setGenerationError("Copy was blocked by the browser. The text is still visible on screen.");
    }
  }

  function copyActiveBrief() {
    void copyText(briefTabLabel(activeTab), activeBriefText);
  }

  function copyFollowUpEmail() {
    void copyText("Follow-up email", followUpEmailText);
  }

  function copyHandoffPacket() {
    void copyText("Handoff packet", handoffPacketText);
  }

  function copyDocxPath() {
    void copyText("DOCX path", generatedBrief?.metadata?.docxArtifactKey ?? "");
  }

  function openLifecycleStage(stageId: LifecycleStageId) {
    const route = lifecycleRoutes[stageId];
    if (stageId === "insights") setActiveTab("businessCase");
    if (stageId === "discovery") setActiveTab("technical");
    setSelectedLifecycleStage(stageId);
    navigateToPage(route.page);
    setPendingSectionId(
      stageId === "meeting-prep" && promoted
        ? "meeting-workspace-section"
        : route.sectionId
    );
  }

  function updateGateStatus(id: string, status: OpportunityGateStatus) {
    setGateDecisions((current) => ({
      ...current,
      [id]: { status, confirmed: false },
    }));
  }

  function confirmGate(id: string) {
    const gate = opportunityGates.find((item) => item.id === id);
    if (!gate) return;
    setGateDecisions((current) => ({
      ...current,
      [id]: { status: gate.status, confirmed: true },
    }));
  }

  function prepareNextCall() {
    setSelectedLifecycleStage("research");
    setActivePage("setup");
    setGenerationError("");
    setGenerationNotice(
      "The approved packet and meeting history are preserved. Update the customer context, then generate the follow-on prebrief."
    );
  }

  useEffect(() => {
    if (!pendingSectionId) {
      return;
    }

    const scrollToSection = () => {
      const section = document.getElementById(pendingSectionId);

      if (!section) {
        return false;
      }

      section.scrollIntoView({ behavior: "smooth", block: "start" });
      return true;
    };

    if (scrollToSection()) {
      const frame = window.requestAnimationFrame(() => {
        setPendingSectionId(null);
      });

      return () => window.cancelAnimationFrame(frame);
    }
    const timeout = window.setTimeout(() => {
      if (scrollToSection()) {
        setPendingSectionId(null);
      }
    }, 120);

    return () => window.clearTimeout(timeout);
  }, [activePage, generatedBrief, pendingSectionId, promoted]);

  function navigateToPage(page: ConsolePage) {
    if (page !== activePage) {
      pipelineAbortRef.current?.abort(
        new DOMException("The user opened another workspace page.", "AbortError")
      );
      catchupAbortRef.current?.abort(
        new DOMException("The user opened another workspace page.", "AbortError")
      );
      setPipelineJobStatus(null);
      setCatchupJobStatus(null);
      setIsGenerating(false);
      setIsCatchupGenerating(false);
      setRefiningTarget(null);
    }
    setActivePage(page);
  }

  function continueWorkflow() {
    if (currentLifecycleStage === "research" && !generatedBrief) {
      generateBrief();
      return;
    }

    openLifecycleStage(currentLifecycleStage);
  }
  return (
    <main className="app-shell min-h-screen text-[#111827]" aria-busy={isGenerating || isCatchupGenerating}>
      <header className="app-header">
        <div className="app-header-inner">
          <button className="product-brand" type="button" onClick={() => navigateToPage("setup")} aria-label="Open PilarPrep context">
            <span className="product-mark">P</span>
            <span>PilarPrep</span>
          </button>

          <ClientLifecycle
            stages={lifecycleStages}
            selectedStage={selectedLifecycleStage}
            onSelect={openLifecycleStage}
          />

          <div className="utility-nav">
            <button
              className={cx("utility-button", activePage === "library" && "utility-button-active")}
              type="button"
              onClick={() => navigateToPage("library")}
              aria-label="Open catch-up workspace"
              title="Catch-up workspace"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15.5a2.5 2.5 0 0 0-2.5-2.5H4z" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round"/>
                <path d="M6.5 3v15.5M9 7h7M9 11h7M9 15h5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"/>
              </svg>
              <span>Catch-up</span>
              {briefHistory.length ? <small>{briefHistory.length}</small> : null}
            </button>
            {workspaceLoginAvailable ? (
              <>
            <button
              className={cx("utility-button", activePage === "evidence" && "utility-button-active")}
              type="button"
              onClick={() => {
                setActivePage("evidence");
                setEvidenceNotice("");
                setEvidenceError("");
                if (authSession) void loadEvidenceDocuments();
              }}
              aria-label="Open approved customer evidence"
              title="Approved customer evidence"
            >
              <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
                <path d="M5 4.5h10.5L19 8v11.5H5z" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round"/>
                <path d="M15.5 4.5V8H19M8 12h8M8 15.5h6" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round"/>
              </svg>
              <span>Evidence</span>
              {evidenceDocuments.length ? <small>{evidenceDocuments.length}</small> : null}
            </button>
              <button
                className={cx("auth-button", authSession && "auth-button-private")}
                type="button"
                onClick={() =>
                  authSession ? leaveWorkspace() : void startWorkspaceLogin()
                }
                disabled={!authReady}
                title={
                  authSession
                    ? "Sign out of the private workspace"
                    : "Sign in with a verified email"
                }
              >
                <span>{authSession ? "Private" : "Guest"}</span>
                <strong>{authSession?.name || "Sign in"}</strong>
              </button>
              </>
            ) : null}
          </div>
        </div>

        <div className="workspace-context-bar">
          <div className="workspace-context-main">
            <span className="workspace-avatar">{(company || "P").slice(0, 1).toUpperCase()}</span>
            <div>
              <small>Client workspace</small>
              <strong>{company || "Select a client"}</strong>
            </div>
          </div>
          <div className="workspace-context-meta" aria-label="Current client context">
            <span>{meetingType}</span>
            <span>{industry}</span>
            <span>Top pilar: {selectedPillars[0] ?? "Set ranking"}</span>
            {authSession ? <span className="access-mode access-mode-private">Private workspace</span> : null}
          </div>
          <div className="workspace-context-action">
            <span className={cx("stage-state", currentLifecycleStage === "follow-up" && meetingUpdateApproved && "stage-state-complete")}>
              {currentLifecycleLabel}
            </span>
            <button type="button" onClick={continueWorkflow} disabled={isGenerating}>
              {isGenerating
                ? <ProcessingIndicator label={activeProcessingLabel} announce={false} compact />
                : nextLifecycleActionLabel[currentLifecycleStage]}
            </button>
          </div>
        </div>
        <div className="journey-context-strip" aria-label="Current journey context">
          <span><small>Evidence status</small><strong>{evidenceCoverageLabel}</strong></span>
          <span><small>Validation needs</small><strong>{generatedBrief?.claims?.length ? validationNeedCount : "Not assessed"}</strong></span>
          <span><small>Latest output</small><strong>{latestApprovedOutput}</strong></span>
          <span className="journey-context-next"><small>Next recommended action</small><strong>{nextBestAction}</strong></span>
        </div>

        {authError ? (
          <div className="auth-error" role="alert">{authError}</div>
        ) : null}
        <div className="sr-only">PilarPrep workspace</div>
      </header>

      {activePage === "evidence" ? (
        <EvidenceWorkspace
          authenticated={Boolean(authSession)}
          company={company}
          documents={evidenceDocuments}
          loading={isEvidenceLoading}
          busyDocumentId={evidenceBusyDocumentId}
          error={evidenceError}
          notice={evidenceNotice}
          onSignIn={() => void startWorkspaceLogin()}
          onRefresh={loadEvidenceDocuments}
          onUpload={uploadEvidence}
          onReindex={(documentId) => mutateEvidence("evidence.reindex", documentId)}
          onDelete={(documentId) => mutateEvidence("evidence.delete", documentId)}
        />
      ) : null}

      {activePage === "library" ? (
        <div className="page-view">
          <section className="mx-auto max-w-[1500px] px-5 pt-5">
            <div className="library-shell" aria-busy={isLibraryLoading}>
              <div className="library-titlebar">
                <div>
                  <p>Catch-up workspace</p>
                  <h1>Get a new teammate up to speed</h1>
                  <span>See one latest packet per client so sales, delivery, and new teammates can catch up without digging through version history.</span>
                </div>
                <button className="small-action primary-small-action" type="button" onClick={() => navigateToPage("setup")}>
                  New brief
                </button>
              </div>

              <div className="catchup-filter-bar" role="tablist" aria-label="Catch-up filters">
                {([
                  { id: "all", label: "All" },
                  { id: "approved", label: "Approved only" },
                  { id: "handoff", label: "Handoff ready" },
                ] as Array<{ id: CatchupFilter; label: string }>).map((filter) => (
                  <button
                    key={filter.id}
                    className={cx("catchup-filter-button", catchupFilter === filter.id && "catchup-filter-button-active")}
                    type="button"
                    role="tab"
                    aria-selected={catchupFilter === filter.id}
                    onClick={() => {
                      setCatchupFilter(filter.id);
                      setCatchupAnswer("");
                      setCatchupError("");
                      setCatchupSource("");
                    }}
                  >
                    {filter.label}
                  </button>
                ))}
              </div>

              <div className="catchup-summary-grid">
                {catchupSummaryCards.map((card) => (
                  <div key={card.label} className="catchup-summary-card">
                    <span>{card.label}</span>
                    <strong>{card.value}</strong>
                  </div>
                ))}
              </div>

              {isLibraryLoading ? (
                <div className="library-loading">
                  <ProcessingIndicator label="Loading saved packets..." />
                </div>
              ) : latestHistoryByClient.length || catchupBrief ? (
                <div className="library-grid">
                  <div className="library-list">
                    {filteredCatchupClientCards.length ? filteredCatchupClientCards.map((entry) => (
                      <button
                        key={entry.id}
                        className={cx("library-entry", selectedHistoryEntry?.id === entry.id && "library-entry-active")}
                        type="button"
                        onClick={() => {
                          setSelectedHistoryId(entry.id);
                          setCatchupAnswer("");
                          setCatchupError("");
                          setCatchupSource("");
                        }}
                      >
                        <div className="library-entry-head">
                          <strong>{entry.company}</strong>
                          <span>{new Date(entry.savedAt).toLocaleString()}</span>
                        </div>
                        <p>{entry.meetingType} / {entry.industry} / {entry.companySize}</p>
                        <div className="library-entry-meta">
                          <span>Top pilar: {entry.topPilar}</span>
                          <span>{entry.status}</span>
                        </div>
                      </button>
                    )) : (
                      <div className="catchup-list-empty">
                        <strong>{latestHistoryByClient.length ? "No clients match this filter" : "Current workspace only"}</strong>
                        <p>{latestHistoryByClient.length ? "Try a broader filter or switch back to All to see the latest client packets." : "The latest working packet will appear here after you generate the first brief."}</p>
                      </div>
                    )}
                  </div>

                  <div className="library-detail">
                    {catchupBrief ? (
                      <>
                        <div className="library-detail-head">
                          <div>
                            <p>Latest client packet</p>
                            <h3>{catchupCompany || "Client workspace"}</h3>
                            <span>{catchupMeetingType} / {catchupIndustry} / {catchupCompanySize}</span>
                          </div>
                          <div className="library-detail-actions">
                            <button className="small-action primary-small-action" type="button" onClick={() => selectedHistoryEntry ? loadBriefHistoryEntry(selectedHistoryEntry) : navigateToPage("brief")}>
                              {selectedHistoryEntry ? "Load into workspace" : "Open live workspace"}
                            </button>
                            {catchupBrief?.metadata?.docxDownloadUrl ? (
                              <a
                                className="setup-packet-link"
                                href={catchupBrief.metadata.docxDownloadUrl}
                                target="_blank"
                                rel="noreferrer"
                              >
                                Download DOCX
                              </a>
                            ) : (
                      <div className="catchup-detail-empty">
                        <strong>No client selected for this view</strong>
                        <p>Switch the filter or choose another client to open the latest packet.</p>
                      </div>
                    )}
                          </div>
                        </div>
                        <div className="packet-grid library-packet-grid">
                          {selectedHistoryPacketItems.map((packet, index) => (
                            <div key={packet.title} className="packet-tile">
                              <span>{index + 1}</span>
                              <small>{packet.status}</small>
                              <strong>{packet.title}</strong>
                              <p>{packet.detail}</p>
                            </div>
                          ))}
                        </div>
                        <div className="library-preview-grid">
                          {libraryPreviewCards.map((card) => (
                            <div key={card.title} className="library-preview-card">
                              <span>{card.title}</span>
                              <p>{card.detail}</p>
                            </div>
                          ))}
                        </div>
                        <section className="catchup-agent-panel" aria-label="Role-aware AI catch-up">
                          <div className="catchup-agent-head">
                            <div>
                              <span>Project continuity</span>
                              <strong>Generate a role-aware catch-up</strong>
                              <p>Uses the latest approved brief, governed project state, and this project session.</p>
                            </div>
                            <button
                              className="small-action primary-small-action"
                              type="button"
                              disabled={isCatchupGenerating || !catchupBrief}
                              onClick={generateRoleAwareCatchup}
                            >
                              {isCatchupGenerating
                                ? <ProcessingIndicator label={catchupProcessingLabel} announce={false} compact />
                                : "Generate catch-up"}
                            </button>
                          </div>
                          <div className="catchup-role-row" role="group" aria-label="Catch-up audience">
                            {(["Sales", "Solutions Architect", "Executive", "PM", "Engineer", "New member"] as AudienceRole[]).map((item) => (
                              <button
                                key={item}
                                className={cx("catchup-role-button", role === item && "catchup-role-button-active")}
                                type="button"
                                onClick={() => {
                                  setRole(item);
                                  setActivePrompt(rolePrompts[item][0]);
                                  setCatchupAnswer("");
                                  setCatchupError("");
                                  setCatchupSource("");
                                }}
                              >
                                {item}
                              </button>
                            ))}
                          </div>
                          <div
                            className={cx("catchup-agent-answer", isCatchupGenerating && "catchup-agent-answer-busy")}
                            aria-busy={isCatchupGenerating}
                          >
                            <div>
                              <span>{catchupSource || "Ready for " + role}</span>
                              <strong>{activePrompt}</strong>
                            </div>
                            {isCatchupGenerating ? (
                              <div className="processing-panel-status">
                                <ProcessingIndicator label={catchupProcessingLabel} />
                                <p>{catchupJobStatus === "queued"
                                  ? "You can keep reading this packet while the secure AWS job starts."
                                  : "AgentCore is grounding the response in this client's latest approved packet."}</p>
                              </div>
                            ) : (
                              <p>{catchupAnswer || "Generate a catch-up to turn this saved packet into a focused starting point for the selected teammate."}</p>
                            )}
                          </div>
                          {catchupError ? <p className="catchup-agent-note">{catchupError}</p> : null}
                        </section>
                        <div className="catchup-action-grid">
                          {catchupActionItems.map((card) => (
                            <div key={card.title} className="catchup-action-card">
                              <span>{card.title}</span>
                              <p>{card.detail}</p>
                            </div>
                          ))}
                        </div>
                      </>
                    ) : null}
                  </div>
                </div>
              ) : (
                <div className="library-empty-state">
                  <strong>No brief to catch up on yet</strong>
                  <p>Generate the first packet and this space will turn into a clean handoff view for sales, delivery, and new teammates.</p>
                </div>
              )}
            </div>
          </section>
        </div>
      ) : null}

      <section className="linear-workflow mx-auto max-w-[1500px] px-5 py-5">
        {activePage === "setup" ? (
          <div className="page-view">
        <div className="page-titlebar" id="setup">
          <div className="page-title-copy">
            <span className="page-number">01</span>
            <div>
              <p>Customer preparation</p>
              <h1>{meetingUpdateApproved ? "Prepare the follow-on meeting context" : "Build the meeting context"}</h1>
              <span>Choose a starting scenario, confirm what matters, and rank the AWS pillars that should shape the conversation.</span>
            </div>
          </div>
          <div className="page-title-status">
            <small>Next outcome</small>
            <strong>{meetingUpdateApproved ? "A refreshed brief grounded in the last call" : "A tailored technical and executive brief"}</strong>
          </div>
        </div>

        <section className={cx("packet-glance", generatedBrief && "packet-glance-ready")} aria-label="Generated packet">
          <div className="packet-glance-intro">
            <span>Generated packet</span>
            <strong>{generatedBrief ? "Your packet is ready to review" : "One input creates six reusable outputs"}</strong>
          </div>
          <div className="packet-glance-items">
            {packetPreviewItems.map((packet, index) => (
              <button
                key={packet.title}
                type="button"
                onClick={() =>
                  packet.key === "handoff"
                    ? openLifecycleStage(promoted ? "follow-up" : "meeting-prep")
                    : openLifecycleStage(
                        packet.key === "businessCase" || packet.key === "executive"
                          ? "insights"
                          : "discovery"
                      )
                }
                disabled={!generatedBrief}
              >
                <span>{index + 1}</span>
                <div>
                  <strong>{packet.title}</strong>
                  <small>{packet.status}</small>
                </div>
              </button>
            ))}
          </div>
          {generatedBrief?.metadata?.docxDownloadUrl ? (
            <a href={generatedBrief.metadata.docxDownloadUrl} target="_blank" rel="noreferrer">
              Download DOCX
            </a>
          ) : null}
        </section>

        <div className="setup-grid">
          <section className="rounded-lg border border-[#d7dee8] bg-white shadow-sm">
            <div className="scenario-panel-header border-b border-[#e0e7ef] p-5">
              <div>
                <p className="text-xs font-bold uppercase tracking-[0.18em] text-[#446076]">
                  Customer scenarios
                </p>
                <h2 className="mt-1 text-xl font-black">Start from a customer scenario</h2>
              </div>
              <button className="create-scenario-button" type="button" onClick={startCustomScenario}>
                Create your own
              </button>
            </div>
            <div className="grid gap-2 p-5">
              <button
                className={cx("scenario-button scenario-button-custom", scenarioId === "custom" && "scenario-button-active")}
                onClick={startCustomScenario}
                type="button"
              >
                <span>Custom scenario</span>
                <strong>Start from blank inputs</strong>
              </button>
              {scenarios.map((scenario) => (
                <button
                  key={scenario.id}
                  className={cx("scenario-button", scenarioId === scenario.id && "scenario-button-active")}
                  onClick={() => loadScenario(scenario)}
                  type="button"
                >
                  <span>{scenario.company}</span>
                  <strong>{scenario.challenge}</strong>
                </button>
              ))}
            </div>
          </section>

          <section className="rounded-lg border border-[#d7dee8] bg-white shadow-sm">
            <div className="border-b border-[#e0e7ef] p-5">
              <p className="text-xs font-bold uppercase tracking-[0.18em] text-[#446076]">
                Brief inputs
              </p>
              <h2 className="mt-1 text-xl font-black">Customer context</h2>
              <p className="mt-2 text-sm leading-6 text-[#526070]">Known context, company values, and stakeholder notes shape how the brief is framed.</p>
            </div>

            <div className="brief-input-grid p-5">
              <div className="brief-input-panel brief-input-panel-main">
                <div className="brief-input-panel-head">
                  <span>1</span>
                  <div>
                    <strong>Brief inputs</strong>
                    <small>Customer, priorities, context, and values</small>
                  </div>
                </div>

                <div className="brief-input-panel-grid">
                  <div className="brief-input-row brief-input-row-primary">
                    <div className="brief-input-column brief-input-basics">
                      <div className="brief-input-column-head">
                        <span>1A</span>
                        <div>
                          <strong>Customer</strong>
                          <small>Who is in the meeting</small>
                        </div>
                      </div>
                      <label className="block">
                        <span className="field-label">Company name</span>
                        <input
                          className="field"
                          value={company}
                          onChange={(event) => setCompany(event.target.value)}
                        />
                      </label>

                      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-1">
                        <label className="block">
                          <span className="field-label">Industry</span>
                          <select
                            className="field"
                            value={industry}
                            onChange={(event) => setIndustry(event.target.value)}
                          >
                            {industries.map((item) => (
                              <option key={item}>{item}</option>
                            ))}
                          </select>
                        </label>

                        <label className="block">
                          <span className="field-label">Meeting type</span>
                          <select
                            className="field"
                            value={meetingType}
                            onChange={(event) => setMeetingType(event.target.value)}
                          >
                            {meetingTypes.map((item) => (
                              <option key={item}>{item}</option>
                            ))}
                          </select>
                        </label>
                      </div>

                      <div>
                        <span className="field-label">Company size</span>
                        <div className="segmented">
                          {companySizes.map((size) => (
                            <button
                              key={size}
                              className={cx(
                                "segment",
                                companySize === size && "segment-active"
                              )}
                              onClick={() => setCompanySize(size)}
                              type="button"
                            >
                              {size}
                            </button>
                          ))}
                        </div>
                      </div>
                    </div>

                    <div className="brief-input-column brief-input-priorities">
                      <div className="brief-input-column-head">
                        <span>1B</span>
                        <div>
                          <strong>Priorities</strong>
                          <small>Drag to rank the discovery lens</small>
                        </div>
                      </div>

                      <div>
                        <span className="field-label">AWS pillar ranking</span>
                        <div className="pillar-ranking-list" aria-label="AWS Well-Architected pillar ranking">
                          {selectedPillarDetails.map((pillar, index) => (
                            <div
                              aria-label={`${pillar.id} ranked ${index + 1}`}
                              aria-roledescription="draggable ranking item"
                              draggable
                              key={pillar.id}
                              onDragEnd={() => setDraggedPillar(null)}
                              onDragOver={(event) => handlePillarDragOver(event, pillar.id)}
                              onDragStart={(event) => {
                                event.dataTransfer.effectAllowed = "move";
                                event.dataTransfer.setData("text/plain", pillar.id);
                                setDraggedPillar(pillar.id);
                              }}
                              onDrop={(event) => {
                                event.preventDefault();
                                setDraggedPillar(null);
                              }}
                              className={cx(
                                "pillar-rank-card",
                                index === 0 && "pillar-rank-card-primary",
                                draggedPillar === pillar.id && "pillar-rank-card-dragging"
                              )}
                            >
                              <button
                                aria-label={`Drag ${pillar.id} priority`}
                                className="pillar-rank-grip"
                                tabIndex={-1}
                                type="button"
                              />
                              <span className="pillar-rank-number">{index + 1}</span>
                              <span className={cx("h-2.5 w-2.5 rounded-full", pillar.color)} />
                              <div className="pillar-rank-copy">
                                <strong>{pillar.short}</strong>
                                <p>{pillar.id}</p>
                              </div>
                              <button
                                className="pillar-rank-promote"
                                disabled={index === 0}
                                onClick={() => promotePillar(pillar.id)}
                                type="button"
                              >
                                {index === 0 ? "Top" : "Make top"}
                              </button>
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>
                  </div>

                  <div className="brief-input-row brief-input-context-row">
                    <label className="block">
                      <span className="field-label">Known context</span>
                      <textarea
                        className="field min-h-28 resize-none"
                        value={context}
                        onChange={(event) => setContext(event.target.value)}
                      />
                    </label>
                    <label className="block">
                      <span className="field-label">Company values</span>
                      <textarea
                        className="field min-h-24 resize-none"
                        value={companyValues}
                        onChange={(event) => setCompanyValues(event.target.value)}
                        placeholder="Paste stated values, principles, or reference notes from the customer site."
                      />
                      <small className="field-helper">
                        Paste the values or source notes you want the brief to reflect.
                      </small>
                    </label>
                    <label className="block brief-input-span-full">
                      <span className="field-label">Company values page</span>
                      <input
                        className="field"
                        type="url"
                        value={companyValuesUrl}
                        onChange={(event) => setCompanyValuesUrl(event.target.value)}
                        placeholder="https://customer.com/about/values"
                      />
                      <small className="field-helper">
                        Optional link sales can use to connect the pitch back to the customer&apos;s stated principles.
                      </small>
                    </label>
                    <label className="block brief-input-span-full">
                      <span className="field-label">Additional direction</span>
                      <textarea
                        className="field min-h-24 resize-none"
                        value={additionalDirection}
                        onChange={(event) => setAdditionalDirection(event.target.value)}
                        placeholder="Example: include payroll integration, data reconciliation, and cutover ownership in the business case."
                      />
                      <small className="field-helper">
                        Use this for must-cover guidance the AI should not treat as optional.
                      </small>
                    </label>
                  </div>
                </div>
              </div>

              <div className="brief-input-panel brief-input-panel-stakeholders">
                <div className="brief-input-panel-head">
                  <span>2</span>
                  <div>
                    <strong>People and influence</strong>
                    <small>Capture who decides, who influences, and how to engage them.</small>
                  </div>
                </div>

                <div className="brief-input-column brief-input-stakeholders">
                  <div className="people-type-tabs" role="tablist" aria-label="People profile type">
                    <button
                      aria-controls="people-profile-panel"
                      aria-selected={peopleView === "decision-maker"}
                      className={cx("people-type-tab", peopleView === "decision-maker" && "people-type-tab-active")}
                      onClick={() => setPeopleView("decision-maker")}
                      role="tab"
                      type="button"
                    >
                      <span>2A</span>
                      <strong>Decision-makers</strong>
                      <small>{peopleCounts["decision-maker"]}</small>
                    </button>
                    <button
                      aria-controls="people-profile-panel"
                      aria-selected={peopleView === "stakeholder"}
                      className={cx("people-type-tab", peopleView === "stakeholder" && "people-type-tab-active")}
                      onClick={() => setPeopleView("stakeholder")}
                      role="tab"
                      type="button"
                    >
                      <span>2B</span>
                      <strong>Stakeholders</strong>
                      <small>{peopleCounts.stakeholder}</small>
                    </button>
                  </div>

                  <div className="decision-context-panel" id="people-profile-panel" role="tabpanel">
                    <div className="decision-context-head">
                      <div>
                        <span className="field-label">
                          {peopleView === "decision-maker" ? "Formal decision authority" : "Influence without final approval"}
                        </span>
                        <h3>{peopleView === "decision-maker" ? "Decision-makers" : "Stakeholders"}</h3>
                      </div>
                      <button
                        className="small-action"
                        type="button"
                        onClick={() => addPerson(peopleView)}
                      >
                        {peopleView === "decision-maker" ? "Add decision-maker" : "Add stakeholder"}
                      </button>
                    </div>
                    <p className="decision-context-note">
                      Customer-approved notes only. No automated LinkedIn scraping.
                    </p>
                    <div className="decision-maker-list">
                      {visiblePeople.length ? visiblePeople.map(({ person, index }, visibleIndex) => (
                        <div key={`${index}-${person.roleType ?? "decision-maker"}`} className="decision-maker-card">
                          <div className="decision-maker-card-head">
                            <strong>
                              {peopleView === "decision-maker" ? "Decision-maker" : "Stakeholder"} {visibleIndex + 1}
                            </strong>
                            <button
                              className="text-action"
                              type="button"
                              onClick={() => removeDecisionMaker(index)}
                            >
                              Remove
                            </button>
                          </div>
                          <div className="decision-maker-grid">
                            <label className="block">
                              <span className="field-label">Name</span>
                              <input
                                className="field"
                                value={person.name}
                                onChange={(event) => updateDecisionMaker(index, "name", event.target.value)}
                              />
                            </label>
                            <label className="block">
                              <span className="field-label">Title</span>
                              <input
                                className="field"
                                value={person.title}
                                onChange={(event) => updateDecisionMaker(index, "title", event.target.value)}
                              />
                            </label>
                          </div>
                          <details className="person-profile-details">
                            <summary>
                              <span>{person.organizationalRole || "Profile details"}</span>
                              <small>
                                {person.roleType === "stakeholder"
                                  ? `${person.influence ?? "medium"} influence · ${person.stance ?? "neutral"}`
                                  : person.decisionAuthority || "Decision authority and engagement details"}
                              </small>
                            </summary>
                            <div className="person-profile-details-body">
                          <div className="person-classification-grid">
                            <label className="block">
                              <span className="field-label">Profile type</span>
                              <select
                                className="field"
                                value={person.roleType === "stakeholder" ? "stakeholder" : "decision-maker"}
                                onChange={(event) => reclassifyPerson(index, event.target.value as PersonRoleType)}
                              >
                                <option value="decision-maker">Decision-maker</option>
                                <option value="stakeholder">Stakeholder</option>
                              </select>
                            </label>
                            <label className="block">
                              <span className="field-label">Organizational role</span>
                              <input
                                className="field"
                                placeholder="Reviewer, champion, app owner"
                                value={person.organizationalRole ?? ""}
                                onChange={(event) => updateDecisionMaker(index, "organizationalRole", event.target.value)}
                              />
                            </label>
                          </div>
                          {peopleView === "stakeholder" ? (
                            <div className="person-classification-grid">
                              <label className="block">
                                <span className="field-label">Influence</span>
                                <select
                                  className="field"
                                  value={person.influence ?? "medium"}
                                  onChange={(event) => updateDecisionMaker(index, "influence", event.target.value as PersonInfluence)}
                                >
                                  <option value="high">High</option>
                                  <option value="medium">Medium</option>
                                  <option value="low">Low</option>
                                </select>
                              </label>
                              <label className="block">
                                <span className="field-label">Current stance</span>
                                <select
                                  className="field"
                                  value={person.stance ?? "neutral"}
                                  onChange={(event) => updateDecisionMaker(index, "stance", event.target.value as PersonStance)}
                                >
                                  <option value="champion">Champion</option>
                                  <option value="supportive">Supportive</option>
                                  <option value="neutral">Neutral</option>
                                  <option value="skeptical">Skeptical</option>
                                  <option value="blocker">Potential blocker</option>
                                </select>
                              </label>
                            </div>
                          ) : null}
                          <label className="block">
                            <span className="field-label">Source label</span>
                            <input
                              className="field"
                              value={person.source ?? ""}
                              onChange={(event) => updateDecisionMaker(index, "source", event.target.value)}
                            />
                          </label>
                          <label className="block">
                            <span className="field-label">Approved profile / engagement notes</span>
                            <textarea
                              className="field min-h-24 resize-none"
                              value={person.context}
                              onChange={(event) => updateDecisionMaker(index, "context", event.target.value)}
                            />
                          </label>
                          <details className="person-engagement-details">
                            <summary>Priorities, concerns, and engagement plan</summary>
                            <div className="person-engagement-grid">
                              {peopleView === "decision-maker" ? (
                                <label className="block">
                                  <span className="field-label">Decision authority</span>
                                  <input
                                    className="field"
                                    placeholder="Budget, risk, architecture, final commitment"
                                    value={person.decisionAuthority ?? ""}
                                    onChange={(event) => updateDecisionMaker(index, "decisionAuthority", event.target.value)}
                                  />
                                </label>
                              ) : null}
                              <label className="block">
                                <span className="field-label">Priorities</span>
                                <textarea
                                  className="field"
                                  value={person.priorities ?? ""}
                                  onChange={(event) => updateDecisionMaker(index, "priorities", event.target.value)}
                                />
                              </label>
                              <label className="block">
                                <span className="field-label">Concerns and blockers</span>
                                <textarea
                                  className="field"
                                  value={person.concerns ?? ""}
                                  onChange={(event) => updateDecisionMaker(index, "concerns", event.target.value)}
                                />
                              </label>
                              <label className="block">
                                <span className="field-label">Success measures</span>
                                <textarea
                                  className="field"
                                  value={person.successMeasures ?? ""}
                                  onChange={(event) => updateDecisionMaker(index, "successMeasures", event.target.value)}
                                />
                              </label>
                              <label className="block">
                                <span className="field-label">Engagement guidance</span>
                                <textarea
                                  className="field"
                                  value={person.engagementGuidance ?? ""}
                                  onChange={(event) => updateDecisionMaker(index, "engagementGuidance", event.target.value)}
                                />
                              </label>
                            </div>
                          </details>
                            </div>
                          </details>
                        </div>
                      )) : (
                        <div className="people-empty-state">
                          <strong>No {peopleView === "decision-maker" ? "decision-makers" : "stakeholders"} added</strong>
                          <p>
                            {peopleView === "decision-maker"
                              ? "Add the people who can approve, fund, accept risk, or commit the customer."
                              : "Add champions, reviewers, technical evaluators, application owners, or potential blockers."}
                          </p>
                          <button className="small-action" type="button" onClick={() => addPerson(peopleView)}>
                            {peopleView === "decision-maker" ? "Add decision-maker" : "Add stakeholder"}
                          </button>
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              </div>
              <details className="advanced-generation-settings">
                <summary>Advanced generation settings</summary>
              <div className="model-selector-row">
                <div className="model-selector-copy">
                  <span className="field-label">AI quality preference (server routed)</span>
                  <strong>{selectedModelLabel} requested</strong>
                  <p>
                    {liveModelSelectionEnabled
                      ? "PilarPrep selects the final model from task complexity and your verified access tier. Nova Pro remains the standard packet model."
                      : "Live Bedrock is not active right now, so this selector will apply the next time live AI mode is available."}
                  </p>
                </div>
                <div className="segmented model-selector-control" role="group" aria-label="AI model selection">
                  <button
                    className={cx("segment", modelPreference === "nova-pro" && "segment-active")}
                    type="button"
                    onClick={() => setModelPreference("nova-pro")}
                    disabled={!liveModelSelectionEnabled || isGenerating}
                    aria-pressed={modelPreference === "nova-pro"}
                  >
                    Nova Pro
                  </button>
                  <button
                    className={cx("segment", modelPreference === "nova-micro" && "segment-active")}
                    type="button"
                    onClick={() => setModelPreference("nova-micro")}
                    disabled={!liveModelSelectionEnabled || isGenerating}
                    aria-pressed={modelPreference === "nova-micro"}
                  >
                    Nova Micro
                  </button>
                  <button
                    className={cx("segment", modelPreference === "claude-sonnet-4.6" && "segment-active")}
                    type="button"
                    onClick={() => setModelPreference("claude-sonnet-4.6")}
                    disabled={!liveModelSelectionEnabled || isGenerating}
                    aria-pressed={modelPreference === "claude-sonnet-4.6"}
                  >
                    Claude Sonnet 4.6
                  </button>
                </div>
              </div>
              </details>

              <div className="action-row">
                <button
                  className="primary-button"
                  type="button"
                  disabled={isGenerating}
                  onClick={generateBrief}
                >
                  <span className="button-icon">+</span>
                  {isGenerating
                    ? <ProcessingIndicator label={activeProcessingLabel} announce={false} compact />
                    : meetingUpdateApproved
                      ? "Generate follow-on prebrief"
                      : generationMode === "live"
                        ? "Generate AI prebrief"
                        : "Generate prebrief"}
                </button>
                <button
                  className="secondary-link"
                  type="button"
                  disabled={!generatedBrief}
                  onClick={() => navigateToPage("brief")}
                >
                  {generatedBrief ? "Review generated brief" : "Review after generation"}
                </button>
              </div>
              {isGenerating ? (
                <div className="generation-status">
                  <ProcessingIndicator label={activeProcessingLabel} />
                </div>
              ) : null}
              <div className="provider-note">
                <span>Generation path</span>
                <strong>
                  {generatedBrief
                    ? `${providerLabel(generatedBrief.provider)} - ${new Date(generatedBrief.generatedAt).toLocaleTimeString()}`
                    : generationMode === "live"
                      ? "IAM-signed AWS job pipeline ready"
                      : "Local demo generator ready"}
                </strong>
              </div>
              <div className="provider-note">
                <span>Selected AI model</span>
                <strong>
                  {generatedBrief?.metadata?.modelId
                    ? generatedBrief.metadata.modelId
                    : generationMode === "live"
                      ? selectedModelLabel
                      : `${selectedModelLabel} queued for live mode`}
                </strong>
              </div>
              <div className="provider-note">
                <span>Saved packet</span>
                <strong>{generatedBrief?.metadata?.docxArtifactKey ?? generatedBrief?.metadata?.artifactKey ?? "Not saved yet"}</strong>
              </div>
              {generatedBrief?.metadata?.projectId || generatedBrief?.metadata?.stateKey ? (
                <div className="evidence-tray">
                  {generatedBrief.metadata.clientId || generatedBrief.metadata.projectId ? <span>Client {generatedBrief.metadata.clientId ?? generatedBrief.metadata.projectId}</span> : null}
                  {generatedBrief.metadata.stateKey ? <span>DynamoDB {generatedBrief.metadata.stateKey}</span> : null}
                  {generatedBrief.metadata.modelId ? <span>{generatedBrief.metadata.modelId}</span> : null}
                  {generatedBrief.metadata.totalTokens !== undefined ? (
                    <span>
                      {generatedBrief.metadata.totalTokens} tokens
                      {generatedBrief.metadata.tokenUsageSource === "estimated" ? " (estimated)" : ""}
                    </span>
                  ) : null}
                  {generatedBrief.metadata.estimatedModelCostUsd !== undefined ? (
                    <span>{'$'}{generatedBrief.metadata.estimatedModelCostUsd.toFixed(4)} estimated model cost</span>
                  ) : null}
                  {generatedBrief.metadata.latencyMs !== undefined ? <span>{generatedBrief.metadata.latencyMs} ms</span> : null}
                  {generatedBrief.metadata.storageWarning ? <span>{generatedBrief.metadata.storageWarning}</span> : null}
                </div>
              ) : null}
              <div className="artifact-actions">
                {generatedBrief?.metadata?.docxDownloadUrl ? (
                  <a
                    className="artifact-action artifact-action-primary"
                    href={generatedBrief.metadata.docxDownloadUrl}
                    target="_blank"
                    rel="noreferrer"
                  >
                    Download DOCX
                  </a>
                ) : null}
                <button
                  className="artifact-action"
                  type="button"
                  disabled={!generatedBrief?.metadata?.docxArtifactKey}
                  onClick={copyDocxPath}
                >
                  Copy DOCX path
                </button>
                {copiedLabel === "DOCX path" ? <span className="copy-state">DOCX path copied</span> : null}
              </div>
              <div className="workspace-tools">
                <span>Workspace saves locally in this browser</span>
                <button className="text-action" type="button" onClick={resetWorkspace}>
                  Reset workspace
                </button>
              </div>
              {generationNotice ? (
                <p className="notice-note">{generationNotice}</p>
              ) : null}
              {generationError ? (
                <p className="error-note">{generationError}</p>
              ) : null}
            </div>
          </section>
        </div>
          </div>
        ) : null}
        {activePage === "brief" ? (
          <div className="page-view">
        <div className="page-titlebar page-titlebar-brief" id="brief">
          <div className="page-title-copy">
            <span className="page-number">2</span>
            <div>
              <p>Refine</p>
              <h1>{meetingUpdateApproved ? "Shape the next customer conversation" : "Review and approve the prebrief"}</h1>
              <span>Work one audience view at a time, compare readable changes, and approve the exact version the team will use.</span>
            </div>
          </div>
          <div className="page-title-actions">
            <span className={cx("approval-state", approved && "approval-state-done", approvalStale && "approval-state-stale")}>
              {approvalStale ? `Review updated v${briefVersion}` : approved ? `Approved v${briefVersion}` : generatedBrief ? `Draft v${briefVersion}` : "Waiting for generation"}
            </span>
          </div>
        </div>

        <div className="space-y-5">          <section id="brief-review-section" className="brief-review-layout">
            <div className="brief-workspace-shell">
              <div className="flex flex-col gap-4 border-b border-[#e0e7ef] p-5 lg:flex-row lg:items-center lg:justify-between">
                <div>
                  <p className="text-xs font-bold uppercase tracking-[0.18em] text-[#446076]">
                    Final pre-brief workspace
                  </p>
                  <h2 className="mt-1 text-xl font-black">
                    {company || "Customer"} {meetingType}
                  </h2>
                  <p className="mt-1 text-sm text-[#526070]">
                    {scenarioId === "custom" ? "Custom opportunity" : activeScenario.name} · Top pilar: {selectedPillars[0] ?? "Not ranked"}
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  {refinementTargets.map((tab) => (
                    <button
                      key={tab}
                      className={cx(
                        "tab-button",
                        activeTab === tab && "tab-active"
                      )}
                      onClick={() => selectBriefTab(tab)}
                      type="button"
                    >
                      {briefTabLabel(tab)}
                    </button>
                  ))}
                </div>
              </div>

              <div className="brief-workspace-main">
                <div className="space-y-4">
                  {generatedBrief && generationNotice ? (
                    <p className="notice-note" role="status">{generationNotice}</p>
                  ) : null}
                  {generatedBrief && generationError ? (
                    <p className="error-note" role="alert">{generationError}</p>
                  ) : null}
                  {pendingIntakeChanges ? (
                    <p className="notice-note" role="status">
                      Customer inputs have changed. The content and evidence assessment below belong to saved brief v{briefVersion} until you generate or refine it.
                    </p>
                  ) : null}
                  <div className="brief-review-primary">
                  <div
                    className={cx("brief-surface", isGenerating && (!refiningTarget || refiningTarget === activeTab) && "brief-surface-busy")}
                    aria-busy={isGenerating && (!refiningTarget || refiningTarget === activeTab)}
                  >
                    <div className="flex items-center justify-between gap-3">
                      <p className="text-xs font-bold uppercase tracking-[0.18em] text-[#446076]">
                        {briefTabLabel(activeTab)}
                      </p>
                      <div className="copy-actions copy-actions-inline">
                        <span className="status-pill">
                          {refiningTarget === activeTab
                            ? `Refining: ${briefTabLabel(activeTab)}`
                            : approved
                              ? "Approved"
                              : "Draft"}
                        </span>
                        <button className="copy-button" type="button" onClick={copyActiveBrief}>
                          Copy tab
                        </button>
                        <button
                          className="copy-button"
                          type="button"
                          disabled={!generatedBrief}
                          onClick={copyHandoffPacket}
                        >
                          Copy packet
                        </button>
                        {copiedLabel === "Handoff packet" ? (
                          <span className="copy-state">Packet copied</span>
                        ) : null}
                        {copiedLabel === briefTabLabel(activeTab) ? (
                          <span className="copy-state">Copied</span>
                        ) : null}
                      </div>
                    </div>
                    <div className="brief-review-tools">
                      <div className="review-mode-control" role="group" aria-label="Brief review mode">
                        <button
                          className={cx(visibleReviewMode === "clean" && "review-mode-active")}
                          type="button"
                          aria-pressed={visibleReviewMode === "clean"}
                          onClick={() => setReviewMode("clean")}
                        >
                          Clean
                        </button>
                        <button
                          className={cx(visibleReviewMode === "changes" && "review-mode-active")}
                          type="button"
                          aria-pressed={visibleReviewMode === "changes"}
                          disabled={!activeComparison}
                          title={activeComparison ? "Show changes to this tab" : "Refine this tab to compare versions"}
                          onClick={() => setReviewMode("changes")}
                        >
                          Changes highlighted
                        </button>
                      </div>
                      <p className="brief-change-summary" aria-live="polite">
                        {activeComparison
                          ? `${activeChangedPassages} passage${activeChangedPassages === 1 ? "" : "s"} updated in ${briefTabLabel(activeTab)}`
                          : generatedBrief
                            ? "Refine this tab to create a marked-up comparison."
                            : "Version comparison will appear after generation."}
                      </p>
                      {activeComparison ? (
                        <p className="brief-change-sections">
                          Other briefs preserved
                        </p>
                      ) : null}
                    </div>
                    <div className="mt-4 space-y-3 brief-output-canvas">
                      {briefContent[activeTab].length ? (
                        briefContent[activeTab].map((item, index) => {
                          const claim = claimRecord(activeTab, index);
                          const sources = claimSourceRecords(activeTab, index);
                          const change =
                            visibleReviewMode === "changes"
                              ? activePassageChanges.find((entry) => entry.itemIndex === index)
                              : undefined;
                          const fieldLabel = briefSectionHeading(activeTab, index);
                          return (
                            <div
                              className={cx(
                                "brief-claim",
                                change?.kind === "modified" && "brief-claim-modified",
                                change?.kind === "added" && "brief-claim-added"
                              )}
                              key={`${activeTab}-${index}`}
                            >
                              <h3 className="brief-field-label">{fieldLabel}</h3>
                              {change?.kind === "modified" ? (
                                <div className="brief-change-copy">
                                  <span className="brief-change-badge">Updated</span>
                                  <p className="brief-line">
                                    {change.segments.map((segment, segmentIndex) =>
                                      segment.kind === "added" ? (
                                        <mark key={`${activeTab}-${index}-segment-${segmentIndex}`}>{segment.text}</mark>
                                      ) : (
                                        <span key={`${activeTab}-${index}-segment-${segmentIndex}`}>{segment.text}</span>
                                      )
                                    )}
                                  </p>
                                  <details className="brief-previous-wording">
                                    <summary>View previous wording</summary>
                                    <p>{change.previous}</p>
                                  </details>
                                </div>
                              ) : (
                                <>
                                  {change?.kind === "added" ? <span className="brief-change-badge">Added</span> : null}
                                  <p className="brief-line">{item}</p>
                                </>
                              )}
                              <div className="brief-claim-sources" aria-label={`Evidence for paragraph ${index + 1}`}>
                                {claim ? (
                                  <span className={`claim-evidence-status claim-evidence-status-${claim.evidenceStatus}`}>
                                    {evidenceStatusLabel(claim.evidenceStatus)}
                                  </span>
                                ) : (
                                  <span className="claim-evidence-status claim-evidence-status-not-recorded">Evidence not recorded</span>
                                )}
                                {sources.map((source) => (
                                  <button
                                    key={source.sourceId}
                                    type="button"
                                    onClick={() => openEvidenceSource(source.sourceId, claim?.claimId)}
                                  >
                                    [{source.title}]
                                  </button>
                                ))}
                              </div>
                            </div>
                          );
                        })
                      ) : (
                        <div
                          className={cx(
                            "brief-empty-state",
                            generationError && "brief-empty-state-error"
                          )}
                          role={generationError ? "alert" : "status"}
                          aria-live={generationError ? "assertive" : "polite"}
                        >
                          <strong>
                            {isGenerating
                              ? activeGenerationStageLabel
                              : generationError
                                ? "Brief generation could not complete"
                                : "No brief generated yet"}
                          </strong>
                          <p>
                            {isGenerating
                              ? "The completed packet will appear here automatically."
                              : generationError
                                ? generationError
                                : "Return to Customer Context and generate the first packet."}
                          </p>
                          {generationError ? (
                            <button
                              className="small-action primary-small-action"
                              type="button"
                              onClick={() => navigateToPage("setup")}
                            >
                              Review inputs
                            </button>
                          ) : null}
                        </div>
                      )}
                      {visibleReviewMode === "changes"
                        ? activeRemovedPassages.map((item) => (
                            <div className="brief-claim brief-claim-removed" key={`removed-${activeTab}-${item.itemIndex}`}>
                              <h3 className="brief-field-label">{briefSectionHeading(activeTab, item.itemIndex)}</h3>
                              <span className="brief-change-badge">Removed</span>
                              <details className="brief-previous-wording">
                                <summary>View removed passage</summary>
                                <p>{item.previous}</p>
                              </details>
                            </div>
                          ))
                        : null}
                    </div>
                    <div className="evidence-tray">
                      {generatedBrief?.sourceCatalog?.length ? (
                        generatedBrief.sourceCatalog.slice(0, 10).map((source) => (
                          <button key={source.sourceId} type="button" onClick={() => openEvidenceSource(source.sourceId)}>
                            {source.title}
                          </button>
                        ))
                      ) : (
                        <span>Evidence not recorded</span>
                      )}
                    </div>
                    {generatedBrief?.metadata ? (
                      <details className="brief-run-details">
                        <summary>Generation details</summary>
                        <div>
                          <span>{providerLabel(generatedBrief.provider)}</span>
                          {generatedBrief.metadata.totalTokens !== undefined ? (
                            <span>
                              {generatedBrief.metadata.totalTokens} tokens
                              {generatedBrief.metadata.tokenUsageSource === "estimated" ? " (estimated)" : ""}
                            </span>
                          ) : null}
                          {generatedBrief.metadata.estimatedModelCostUsd !== undefined ? (
                            <span>{'$'}{generatedBrief.metadata.estimatedModelCostUsd.toFixed(4)} estimated model cost</span>
                          ) : null}
                          {generatedBrief.metadata.latencyMs !== undefined ? (
                            <span>{generatedBrief.metadata.latencyMs} ms</span>
                          ) : null}
                        </div>
                      </details>
                    ) : null}
                  </div>

                    <div
                      className={cx(
                        "refinement-approve-row",
                        approved && !unresolvedRefinement && "refinement-approve-row-done",
                        approvalStale && "refinement-approve-row-stale"
                      )}
                      id="brief-approve-section"
                    >
                      <div className="approval-copy">
                        <span>Final quality gate</span>
                        <strong>
                          {unresolvedRefinement
                            ? `Apply feedback to ${briefTabLabel(activeTab)}`
                            : approvalStale
                              ? `Updated brief v${briefVersion} is ready`
                              : approved
                                ? `Brief v${briefVersion} approved for team use`
                                : generatedBrief
                                  ? `Brief v${briefVersion} is ready for approval`
                                  : "Generate the first brief to begin review"}
                        </strong>
                        <p id="approval-readiness-copy">
                          {unresolvedRefinement
                            ? `The ${briefTabLabel(activeTab)} draft has unapplied feedback. Other briefs will remain unchanged.`
                            : approvalStale
                              ? "Feedback created a new version. Review the highlighted changes, then approve the version the team should use."
                              : approved
                                ? "This exact version anchors the delivery handoff, project state, and saved packet."
                                : "Confirm the business case, audience views, questions, scope, and evidence before handing the packet to the team."}
                        </p>
                        <div className="approval-readiness">
                          <span>Version {briefVersion}</span>
                          <span>{activeComparison ? `${activeComparison.changedPassages} changes reviewed` : "Clean copy ready"}</span>
                          <span>{unresolvedRefinement ? `Feedback pending: ${briefTabLabel(activeTab)}` : "Feedback applied"}</span>
                        </div>
                      </div>
                      <div className="approval-actions">
                        {unresolvedRefinement ? (
                          <button
                            className="approval-apply-action"
                            type="button"
                            disabled={isGenerating}
                            onClick={refineBrief}
                          >
                            {isGenerating
                              ? <ProcessingIndicator label="Applying feedback..." announce={false} compact />
                              : `Apply feedback to ${briefTabLabel(activeTab)}`}
                          </button>
                        ) : null}
                        <button
                          className="approval-primary-action"
                          type="button"
                          aria-describedby="approval-readiness-copy"
                          disabled={!approvalReady || approved}
                          onClick={approveBrief}
                        >
                          {approved ? "Approved for meeting" : approvalStale ? "Approve updated pre-call packet" : "Approve pre-call packet"}
                        </button>
                      </div>
                    </div>
                  </div>
                  <div className="refinement-panel" id="brief-refine-section">
                    <div className="refinement-header">
                      <div>
                        <h3 className="text-sm font-black">Refine: {briefTabLabel(activeTab)}</h3>
                        <p>Refining: {briefTabLabel(activeTab)} | {feedback.length} selected</p>
                      </div>
                      <div className="refinement-actions">
                        <button
                          className="small-action"
                          type="button"
                          disabled={(!feedback.length && !feedbackNotes.trim()) || isGenerating}
                          onClick={clearActiveRefinementDraft}
                        >
                          Clear
                        </button>
                        <button
                          className="small-action primary-small-action"
                          type="button"
                          disabled={isGenerating || !generatedBrief || (!feedback.length && !feedbackNotes.trim())}
                          onClick={refineBrief}
                        >
                          {refiningTarget === activeTab
                            ? <ProcessingIndicator label="Applying feedback..." announce={false} compact />
                            : `Apply feedback to ${briefTabLabel(activeTab)}`}
                        </button>
                      </div>
                    </div>
                    <div className="feedback-category-grid">
                      {activeFeedbackCategories.map((category) => {
                        const categoryValues = category.options.map(
                          (option) => `${category.title}: ${option}`
                        );
                        const selectedCount = categoryValues.filter((option) =>
                          feedback.includes(option)
                        ).length;

                        return (
                          <section className="feedback-category" key={category.title}>
                            <div className="feedback-category-title">
                              <div>
                                <strong>{category.title}</strong>
                                <span>{category.description}</span>
                              </div>
                              <em>{selectedCount}/{category.options.length}</em>
                            </div>
                            <div className="feedback-chip-row">
                              {category.options.map((option) => {
                                const value = `${category.title}: ${option}`;
                                return (
                                  <button
                                    key={value}
                                    className={cx(
                                      "feedback-chip",
                                      feedback.includes(value) && "feedback-chip-active"
                                    )}
                                    onClick={() => toggleFeedback(value)}
                                    type="button"
                                  >
                                    {option}
                                  </button>
                                );
                              })}
                            </div>
                          </section>
                        );
                      })}
                    </div>
                    <label className="feedback-notes-field">
                      <span>
                        {activeTab === "technical"
                          ? "Additional technical context"
                          : "Additional direction"}
                      </span>
                      <textarea
                        value={feedbackNotes}
                        onChange={(event) => updateFeedbackNotes(event.target.value)}
                        rows={4}
                        maxLength={1500}
                        placeholder={
                          activeTab === "technical"
                            ? "Add architecture details, corrected assumptions, integrations, constraints, security requirements, or technical questions."
                            : `Example: Add customer-specific depth and stronger questions to ${briefTabLabel(activeTab)} only.`
                        }
                      />
                      <small>Applied throughout this tab only. Every other brief remains unchanged.</small>
                    </label>
                  </div>
                </div>

                <div className="space-y-4">
                  <div className="summary-panel stakeholder-summary">
                    <p className="text-xs font-bold uppercase tracking-[0.16em] text-[#446076]">
                      People captured
                    </p>
                    <div className="people-summary-counts">
                      <span>{peopleCounts["decision-maker"]} decision-makers</span>
                      <span>{peopleCounts.stakeholder} stakeholders</span>
                    </div>
                    <div className="mt-4 space-y-2">
                      {peopleSummaryProfiles.length ? (
                        peopleSummaryProfiles.map((person) => (
                          <div key={`${person.roleType}-${person.name}-${person.title}`} className="stakeholder-mini">
                            <strong>{person.name || (person.roleType === "stakeholder" ? "Stakeholder" : "Decision-maker")}</strong>
                            <span>
                              {person.title || "Role to confirm"} · {person.roleType === "stakeholder" ? "Stakeholder" : "Decision-maker"}
                            </span>
                          </div>
                        ))
                      ) : (
                        <div className="stakeholder-mini stakeholder-mini-empty">
                          <strong>Context needed</strong>
                          <span>Approved notes unlock tailored questions</span>
                        </div>
                      )}
                    </div>
                  </div>

                  <div className="summary-panel">
                    <p className="text-xs font-bold uppercase tracking-[0.16em] text-[#446076]">
                      Ranked pillars
                    </p>
                    <div className="mt-4 space-y-2">
                      {selectedPillarDetails.slice(0, 4).map((pillar, index) => (
                        <div key={pillar.id} className="rank-summary-item">
                          <span>{index + 1}</span>
                          <div>
                            <strong>{pillar.id}</strong>
                            <p>{index === 0 ? "Primary discovery lens" : pillar.tone}</p>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>


                </div>
              </div>
            </div>

            <div className="space-y-5">
              <section className="rounded-lg border border-[#d7dee8] bg-white shadow-sm">
                <div className="border-b border-[#e0e7ef] p-5">
                  <p className="text-xs font-bold uppercase tracking-[0.18em] text-[#446076]">
                    Ranked pillar narrative
                  </p>
                  <h2 className="mt-1 text-xl font-black">Why this matters</h2>
                </div>
                <div className="grid gap-3 p-5">
                  {selectedPillarDetails.map((pillar, index) => (
                    <div key={pillar.id} className="pillar-note">
                      <span className="pillar-note-rank">{index + 1}</span>
                      <span className={cx("h-2.5 w-2.5 rounded-full", pillar.color)} />
                      <div>
                        <strong>{pillar.id}</strong>
                        <p>{pillar.tone}</p>
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            </div>
          </section>


          </div>
          </div>
        ) : null}


          {activePage === "project" ? (
            <div className="page-view">
          <div className="page-titlebar page-titlebar-handoff" id="project-brain">
            <div className="page-title-copy">
              <span className="page-number">{selectedLifecycleStage === "meeting-prep" ? "4" : "5"}</span>
              <div>
                <p>{projectStagePresentation.eyebrow}</p>
                <h1>{projectStagePresentation.title}</h1>
                <span>{projectStagePresentation.detail}</span>
              </div>
            </div>
            <div className="page-title-status">
              <small>Current client</small>
              <strong>{company || "Select a client"} · Brief v{briefVersion || "-"}</strong>
            </div>
          </div>

          {isMeetingStage ? (
          <div id="meeting-workspace-section" className="project-stage-panel">
          <MeetingIntelligence
            isBlueMesa={scenarioId === "bluemesa"}
            isApproved={approved && !approvalStale}
            isHosted={hostedJobsMode}
            isAuthenticated={Boolean(authSession)}
            authAvailable={workspaceLoginAvailable}
            result={meetingResult}
            decisions={meetingDecisions}
            status={meetingJobStatus}
            error={meetingError}
            notice={meetingNotice}
            isProcessing={isMeetingProcessing}
            isApproving={isMeetingApproving}
            audio={meetingAudio}
            onSignIn={() => void startWorkspaceLogin()}
            onUploadAudio={(file, consentAcknowledged) =>
              void uploadMeetingAudio(file, consentAcknowledged)
            }
            onRemoveAudio={removeMeetingAudio}
            onProcess={() => void processSyntheticMeeting()}
            onDecision={setMeetingDecision}
            onAcceptAll={acceptAllMeetingUpdates}
            onApprove={() => void approveMeetingUpdates()}
          />
          </div>
          ) : null}

          {selectedLifecycleStage === "meeting-prep" || isNextStepFollowUp ? (
          <div id={isNextStepFollowUp ? "project-follow-up-section" : "project-meeting-prep-section"} className="project-stage-panel">
          <div className={cx("handoff-ready-card", handoffReady && "handoff-ready-card-done")}>
            <div>
              <span>
                {isNextStepFollowUp
                  ? "Next-step handoff"
                  : handoffReady
                    ? "Pre-call handoff ready"
                    : approved
                      ? "Approved brief ready"
                      : "Waiting for brief approval"}
              </span>
              <h3>
                {isNextStepFollowUp
                  ? `${company || "Customer"} is ready for the next decision`
                  : handoffReady
                    ? `${company || "Customer"} is ready for team alignment`
                    : `Build the pre-call handoff for ${company || "this customer"}`}
              </h3>
              <p>
                {isNextStepFollowUp
                  ? "Use the accepted meeting evidence, owned actions, and gate decisions to prepare the next customer move."
                  : handoffReady
                    ? "Sales context, technical assumptions, stakeholders, discovery questions, and meeting goals are aligned for the full call team."
                    : "The approved packet is ready. Build the shared pre-call handoff when your team is ready."}
              </p>
              {selectedLifecycleStage === "meeting-prep" && approved ? (
                <div className={`precall-handoff-status precall-handoff-status-${precallHandoffStatus}`} role="status" aria-live="polite">
                  <strong>{precallHandoffStatus === "ready" ? "Ready" : precallHandoffStatus === "failed" ? "Needs attention" : precallHandoffStatus === "stale" ? "Out of date" : precallHandoffStatus === "idle" ? "Ready to prepare" : "Preparing"}</strong>
                  <span>
                    {precallHandoffStatus === "ready"
                      ? "The shared handoff is ready for the call team."
                      : precallHandoffStatus === "failed"
                        ? precallHandoffError || "The pre-call handoff could not be prepared."
                      : precallHandoffStatus === "stale"
                          ? "Approve the latest brief version to prepare a current handoff."
                          : precallHandoffStatus === "idle"
                            ? "Start the handoff when you want AgentCore to prepare the team packet."
                            : precallHandoffError || "You can keep working while AgentCore builds the handoff."}
                  </span>
                </div>
              ) : null}
            </div>
            <div className="handoff-ready-actions">
              {selectedLifecycleStage === "meeting-prep" ? (
                <button
                  className="handoff-ready-action handoff-ready-action-primary"
                  type="button"
                  disabled={isGenerating || precallHandoffStatus === "queued" || precallHandoffStatus === "preparing" || !approved || !generatedBrief}
                  onClick={refreshProjectModel}
                >
                  {precallHandoffStatus === "queued" || precallHandoffStatus === "preparing"
                    ? <ProcessingIndicator label="Preparing pre-call handoff..." announce={false} compact />
                    : precallHandoffStatus === "failed"
                      ? "Try pre-call handoff again"
                      : handoffReady
                        ? "Refresh pre-call handoff"
                        : "Build pre-call handoff"}
                </button>
              ) : null}
              {generatedBrief?.metadata?.docxDownloadUrl ? (
                <a
                  className={cx("handoff-ready-action", isNextStepFollowUp && "handoff-ready-action-primary")}
                  href={generatedBrief.metadata.docxDownloadUrl}
                  target="_blank"
                  rel="noreferrer"
                >
                  Download DOCX
                </a>
              ) : null}

              <button
                className="handoff-ready-action"
                type="button"
                disabled={!generatedBrief}
                onClick={copyHandoffPacket}
              >
                Copy packet
              </button>
            </div>
          </div>

          {isNextStepFollowUp ? (
            <OpportunityGates
              gates={opportunityGates}
              disabled={!meetingUpdateApproved}
              onStatusChange={updateGateStatus}
              onConfirm={confirmGate}
            />
          ) : null}

          <details
            className={cx(
              "project-handoff-details",
              selectedLifecycleStage === "meeting-prep" && !promoted && "project-handoff-details-always-open"
            )}
            open={selectedLifecycleStage === "meeting-prep" && !promoted ? true : undefined}
          >
            {isNextStepFollowUp ? (
              <summary className="advance-handoff-summary">
                <span>
                  <strong>Full next-step handoff</strong>
                  <small>Team answers, delivery plan, assumptions, risks, stakeholders, and follow-up email</small>
                </span>
              </summary>
            ) : null}

          <section className="rounded-lg border border-[#d7dee8] bg-[#111827] text-white shadow-sm">
            <div className="grid gap-0 2xl:grid-cols-[380px_1fr]">
              <div className="border-b border-white/10 p-5 2xl:border-b-0 2xl:border-r">
                <p className="text-xs font-bold uppercase tracking-[0.18em] text-[#7dd3fc]">
                  {isNextStepFollowUp ? "Next-step handoff" : "Pre-call handoff"}
                </p>
                <h2 className="mt-1 text-xl font-black">
                  {isNextStepFollowUp ? "Follow-on team context" : "Pre-call context"}
                </h2>
                <p className="mt-3 text-sm leading-6 text-white/70">
                  {isNextStepFollowUp
                    ? "Give Sales, the SA, and delivery one view of what changed, who owns the next action, and which decision is next."
                    : "Give Sales, SAs, executives, PMs, and delivery leads the shared context, assumptions, evidence needs, and questions required for the customer call."}
                </p>

                <div className="mt-5 grid grid-cols-2 gap-2">
                  {(
                    [
                      "Sales",
                      "Solutions Architect",
                      "Executive",
                      "PM",
                      "Engineer",
                      "New member",
                    ] as AudienceRole[]
                  ).map((item) => (
                    <button
                      key={item}
                      className={cx(
                        "role-button",
                        role === item && "role-active"
                      )}
                      onClick={() => {
                        setRole(item);
                        setActivePrompt(rolePrompts[item][0]);
                      }}
                      type="button"
                    >
                      {item}
                    </button>
                  ))}
                </div>
              </div>

              <div className="p-5">
                <div className="grid gap-5 xl:grid-cols-[1fr_340px]">
                  <div>
                    <div className="mb-4 flex flex-wrap gap-2">
                      {rolePrompts[role].map((prompt) => (
                        <button
                          key={prompt}
                          className={cx(
                            "prompt-chip",
                            activePrompt === prompt && "prompt-chip-active"
                          )}
                          onClick={() => setActivePrompt(prompt)}
                          type="button"
                        >
                          {prompt}
                        </button>
                      ))}
                    </div>

                    <div
                      className={cx("project-answer", isProjectGenerating && "project-answer-busy")}
                      aria-busy={isProjectGenerating}
                    >
                      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
                        <div>
                          <p className="text-xs font-bold uppercase tracking-[0.18em] text-[#7dd3fc]">
                            Answer for {role}
                          </p>
                          <h3 className="mt-1 text-lg font-black">
                            {activePrompt}
                          </h3>
                        </div>
                        <div className="project-answer-actions">
                          <span
                            className={cx(
                              "project-state",
                              promoted
                                ? "project-state-live"
                                : "project-state-waiting"
                            )}
                          >
                            {displayedProjectAnswer ? "Team handoff ready" : isProjectGenerating ? "Preparing handoff" : approved ? "Ready to generate" : "Handoff pending"}
                          </span>
                          <button
                            className="copy-button copy-button-dark"
                            type="button"
                            onClick={copyFollowUpEmail}
                            disabled={!displayedProjectAnswer || !followUpEmailText}
                          >
                            Copy email
                          </button>
                          <button
                            className="copy-button copy-button-dark"
                            type="button"
                            disabled={!displayedProjectAnswer}
                            onClick={copyHandoffPacket}
                          >
                            Copy packet
                          </button>
                          {copiedLabel === "Handoff packet" ? (
                            <span className="copy-state copy-state-dark">Packet copied</span>
                          ) : null}
                          {copiedLabel === "Follow-up email" ? (
                            <span className="copy-state copy-state-dark">Copied</span>
                          ) : null}
                        </div>
                      </div>
                      {isProjectGenerating ? (
                        <div className="generation-status generation-status-dark">
                          <ProcessingIndicator label={activeProcessingLabel} tone="dark" />
                        </div>
                      ) : null}
                      <p className="mt-5 text-base leading-7 text-white/82">
                        {displayedProjectAnswer}
                      </p>
                      {displayedProjectAnswer && claimRecord("projectAnswer", 0) ? (
                        <div className="project-answer-sources">
                          <span className={`claim-evidence-status claim-evidence-status-${claimRecord("projectAnswer", 0)?.evidenceStatus}`}>
                            {evidenceStatusLabel(claimRecord("projectAnswer", 0)?.evidenceStatus ?? "needs-validation")}
                          </span>
                          {claimSourceRecords("projectAnswer", 0).map((source) => (
                            <button
                              key={source.sourceId}
                              type="button"
                              onClick={() => openEvidenceSource(source.sourceId, claimRecord("projectAnswer", 0)?.claimId)}
                            >
                              [{source.title}]
                            </button>
                          ))}
                        </div>
                      ) : displayedProjectAnswer ? <div className="project-answer-sources"><span>Not assessed</span></div> : null}
                    </div>

                    {isNextStepFollowUp ? (
                    <>
                    <section className="next-steps-panel" aria-label="Handoff next steps">
                      <div className="next-steps-heading">
                        <div>
                          <p>Handoff priority</p>
                          <h3>Owned next steps and decision gates</h3>
                        </div>
                        <span>
                          {projectNextSteps?.immediateActions?.length
                            ? `${projectNextSteps.immediateActions.length} actions ready`
                            : "Waiting for handoff"}
                        </span>
                      </div>

                      {projectNextSteps?.immediateActions?.length ? (
                        <div className="next-step-action-list">
                          {projectNextSteps.immediateActions.map((item, index) => (
                            <article key={`${item.action}-${index}`}>
                              <span className="next-step-number">{index + 1}</span>
                              <div>
                                <strong>{item.action}</strong>
                                <dl>
                                  <div><dt>Owner</dt><dd>{item.owner}</dd></div>
                                  <div><dt>Timing</dt><dd>{item.timing}</dd></div>
                                  <div><dt>Dependency</dt><dd>{item.dependency}</dd></div>
                                  <div><dt>Decision gate</dt><dd>{item.decisionGate}</dd></div>
                                </dl>
                              </div>
                            </article>
                          ))}
                        </div>
                      ) : (
                        <p className="next-steps-empty">
                          Generate the approved handoff to create named owners, timing, dependencies, and decision gates.
                        </p>
                      )}

                      <div className="next-steps-detail-grid">
                        <div>
                          <strong>Open questions</strong>
                          {projectNextSteps?.openQuestions?.length ? (
                            <ol>
                              {projectNextSteps.openQuestions.map((item) => <li key={item}>{item}</li>)}
                            </ol>
                          ) : (
                            <p>No open questions captured yet.</p>
                          )}
                        </div>
                        <div>
                          <strong>Next meeting</strong>
                          <p>{projectNextSteps?.nextMeeting.purpose ?? "Purpose will be generated with the handoff."}</p>
                          <span>{projectNextSteps?.nextMeeting.timing ?? "Timing to confirm"}</span>
                          <small>{projectNextSteps?.nextMeeting.attendees.join(", ") ?? "Attendees to confirm"}</small>
                        </div>
                      </div>

                      {projectNextSteps ? (
                        <div className="next-steps-summaries">
                          <p><strong>Customer summary</strong>{projectNextSteps.customerSummary}</p>
                          <p><strong>Internal note</strong>{projectNextSteps.internalNotes}</p>
                        </div>
                      ) : null}
                    </section>

                    <section id="project-plan-section" className="canonical-handoff" aria-label="Canonical team handoff">
                      <header className="canonical-handoff-header">
                        <div>
                          <p>Next-step handoff</p>
                          <h3>Delivery path, assumptions, risks, and owners</h3>
                        </div>
                        <span>
                          {projectTimeline.length
                            ? `${projectTimeline.length} timeline stages`
                            : "Waiting for handoff"}
                        </span>
                      </header>

                      <div className="handoff-timeline">
                        <div className="handoff-section-heading">
                          <strong>Delivery timeline</strong>
                          <span>Objectives and exit criteria by stage</span>
                        </div>
                        {projectTimeline.length ? (
                          <ol>
                            {projectTimeline.map((item, index) => (
                              <li key={`${item.title}-${index}`}>
                                <span>{index + 1}</span>
                                <div>
                                  <strong>{item.title}</strong>
                                  <p>{item.detail}</p>
                                  <small>{item.owner ?? "Owner to confirm"} · {item.status ?? "Planned"}</small>
                                </div>
                              </li>
                            ))}
                          </ol>
                        ) : (
                          <p className="canonical-handoff-empty">
                            Generate the approved handoff to build a sequenced delivery plan.
                          </p>
                        )}
                      </div>

                      <div className="handoff-detail-grid">
                        <div className="handoff-detail-section">
                          <div className="handoff-section-heading">
                            <strong>Assumptions to validate</strong>
                            <span>AI hypotheses, never customer facts</span>
                          </div>
                          {projectAssumptions.length ? (
                            <ul>
                              {projectAssumptions.map((item, index) => (
                                <li key={`${item.title}-${index}`}>
                                  <strong>{item.title}</strong>
                                  <p>{item.detail}</p>
                                  <small>{item.owner ?? "Owner to confirm"} · {item.status ?? "Unvalidated"}</small>
                                </li>
                              ))}
                            </ul>
                          ) : (
                            <p className="canonical-handoff-empty">No unvalidated assumptions are listed in this handoff.</p>
                          )}
                        </div>

                        <div className="handoff-detail-section">
                          <div className="handoff-section-heading">
                            <strong>Risks and blockers</strong>
                            <span>Mitigation owners and current status</span>
                          </div>
                          {projectRisks.length ? (
                            <ul>
                              {projectRisks.map((item, index) => (
                                <li key={`${item.title}-${index}`}>
                                  <strong>{item.title}</strong>
                                  <p>{item.detail}</p>
                                  <small>{item.owner ?? "Owner to confirm"} · {item.status ?? "Open"}</small>
                                </li>
                              ))}
                            </ul>
                          ) : (
                            <p className="canonical-handoff-empty">No separate delivery risks are listed yet.</p>
                          )}
                        </div>
                      </div>

                      <div className="handoff-stakeholders">
                        <div className="handoff-section-heading">
                          <strong>Stakeholders and decision owners</strong>
                          <span>Who validates, approves, and acts</span>
                        </div>
                        {projectStakeholders.length ? (
                          <div className="handoff-stakeholder-list">
                            {projectStakeholders.map((item, index) => (
                              <div key={`${item.title}-${index}`}>
                                <strong>{item.owner ?? item.title}</strong>
                                <span>{item.title}</span>
                                <p>{item.detail}</p>
                                <small>{item.status ?? "Role to confirm"}</small>
                              </div>
                            ))}
                          </div>
                        ) : (
                          <p className="canonical-handoff-empty">Generate the handoff to map approvers and delivery owners.</p>
                        )}
                      </div>
                    </section>
                    <section className="mt-5 rounded-2xl border border-white/10 bg-white/5 p-4">
                      <div>
                        <p className="text-[11px] font-bold uppercase tracking-[0.18em] text-[#7dd3fc]">Customer follow-through</p>
                        <h3 className="mt-1 text-base font-black text-white">
                          {generatedBrief?.projectArtifacts?.followUpEmail?.subject ?? "Follow-up email will appear after handoff generation"}
                        </h3>
                      </div>
                      <p className="mt-4 whitespace-pre-line text-sm leading-6 text-white/72">
                        {generatedBrief?.projectArtifacts?.followUpEmail?.body ?? "Generate the handoff to draft the customer-ready follow-up and internal continuity note."}
                      </p>
                    </section>
                    </>
                    ) : null}
                  </div>

                  {selectedLifecycleStage === "meeting-prep" ? (
                    <div className="meeting-panel" id="project-notes-section">
                      <label className="block">
                        <span className="dark-label">Known customer context</span>
                        <textarea
                          aria-label="Sales-to-SA customer context"
                          className="dark-field min-h-36 resize-none"
                          value={meetingNotes}
                          onChange={(event) => setMeetingNotes(event.target.value)}
                          placeholder="Add sales discovery, customer commitments, sensitivities, or context the SA should carry into the call."
                        />
                      </label>
                      <div className="mt-4 grid gap-2">
                        {handoffItems.map((item) => (
                          <div key={item.title} className="handoff-item">
                            <div>
                              <strong>{item.title}</strong>
                              <p>{item.detail}</p>
                            </div>
                            <span>{item.status}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  ) : null}
                </div>
              </div>
            </div>
          </section>
          </details>

          {isNextStepFollowUp ? (
            <div className="advance-action-bar">
              <div>
                <p>Continue the account cycle</p>
                <strong>Prepare the next customer conversation</strong>
                <span>
                  {confirmedGateCount} of {opportunityGates.length} gate decisions confirmed. Open decisions and accepted meeting context carry into the next prebrief.
                </span>
              </div>
              <button
                type="button"
                disabled={!meetingUpdateApproved}
                onClick={prepareNextCall}
              >
                Prepare next call
              </button>
            </div>
          ) : null}
          </div>
        ) : null}
          </div>
        ) : null}
      </section>
      <EvidenceDrawer
        source={selectedEvidenceSource}
        claim={selectedEvidenceClaim}
        onClose={() => {
          setSelectedEvidenceSourceId("");
          setSelectedEvidenceClaimId("");
        }}
      />
    </main>
  );
}
