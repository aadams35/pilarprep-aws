import type {
  ApprovedBriefSnapshot,
  BriefClaim,
  BriefEvidence,
  BriefRequest,
  BriefResponse,
  BusinessCase,
  DecisionMakerContext,
  EvidenceCoverage,
  EvidenceSourceRecord,
  EvidenceStatus,
  ProjectArtifacts,
  RefinementTarget,
} from "./types";

const businessCaseFields: Array<keyof BusinessCase> = [
  "scenario",
  "whyNow",
  "currentSituation",
  "desiredOutcomes",
  "successCriteria",
  "businessRisks",
  "decisionRequired",
  "inScope",
  "outOfScope",
  "assumptionsAndUnknowns",
  "stakeholderAlignment",
  "alignmentStatement",
  "nextStepGuidance",
];

function stableLocalSourceId(label: string) {
  let hash = 2166136261;
  for (const character of label) {
    hash ^= character.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  const slug = label.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 36) || "evidence";
  return `src-${slug}-${(hash >>> 0).toString(16).padStart(8, "0")}`;
}

const evidenceStopWords = new Set([
  "about", "after", "against", "also", "and", "are", "because", "before",
  "brief", "business", "can", "company", "could", "customer", "customers",
  "decision", "decisions", "evidence", "for", "from", "has", "have", "how",
  "into", "meeting", "must", "our", "pilarprep", "should", "that", "the",
  "their", "them", "then", "there", "these", "they", "this", "through",
  "team", "teams", "technical", "use", "using", "validate", "validation",
  "what", "when", "where", "which", "who", "will", "with", "would",
]);

function evidenceTerms(value: string, ignored = new Set<string>()) {
  return new Set(
    (value.toLowerCase().match(/[a-z0-9][a-z0-9-]{1,}/g) ?? []).filter(
      (term) =>
        !evidenceStopWords.has(term) &&
        !ignored.has(term) &&
        (term.length >= 3 || term === "ai" || term === "s3")
    )
  );
}

function localSourceSupportScore(
  text: string,
  source: EvidenceSourceRecord,
  ignored: Set<string>
) {
  const claimTerms = evidenceTerms(text, ignored);
  const sourceTerms = evidenceTerms(`${source.evidenceSnippet} ${source.title}`, ignored);
  const overlap = [...claimTerms].filter((term) => sourceTerms.has(term));
  return overlap.length >= 2 ? overlap.length : 0;
}

function supportingLocalSources(
  text: string,
  catalog: EvidenceSourceRecord[],
  preferredLabels: string[],
  ignored: Set<string>
) {
  const preferred = new Set(preferredLabels);
  return catalog
    .map((source) => ({
      source,
      score: localSourceSupportScore(text, source, ignored),
      preferred: preferred.has(source.label),
    }))
    .filter((match) => match.score >= 3)
    .sort(
      (left, right) =>
        right.score - left.score ||
        Number(right.preferred) - Number(left.preferred) ||
        left.source.label.localeCompare(right.source.label)
    )
    .slice(0, 3);
}

function localClaimStatus(
  section: BriefEvidence["section"],
  itemIndex: number,
  text: string,
  matches: Array<{ score: number }>
): EvidenceStatus {
  const lowered = text.toLowerCase();
  if (/conflicting evidence|sources disagree|conflict between/.test(lowered)) {
    return matches.length ? "conflicting-evidence" : "needs-validation";
  }
  if (
    (section === "businessCase" && itemIndex === 9) ||
    /working assumption|remains an assumption|unknown to validate/.test(lowered)
  ) {
    return "assumption";
  }
  if (section === "objections") return "needs-validation";
  if (!matches.length) return "needs-validation";
  if (
    section === "gameplan" ||
    section === "projectAnswer" ||
    /\b(?:assume|assumed|hypothesis|may|might|recommend|should|propose|evaluate|consider|unknown)\b/.test(lowered)
  ) {
    return "partially-supported";
  }
  return "supported";
}

function attachLocalProvenance(brief: BriefResponse, input: BriefRequest): BriefResponse {
  const people = input.decisionMakers ?? [];
  const sourceText: Record<string, { sourceType: string; snippet: string; location?: string }> = {
    "Customer context": { sourceType: "customer-provided-context", snippet: input.context },
    "AWS Well-Architected pillars": { sourceType: "aws-framework", snippet: rankedPillarsFromInput(input).join(", ") },
    "Company values": { sourceType: "company-values", snippet: input.companyValues ?? "" },
    "Company values page": { sourceType: "approved-public-url", snippet: input.companyValuesUrl ?? "", location: input.companyValuesUrl },
    "Additional direction": { sourceType: "customer-provided-context", snippet: input.additionalDirection ?? "" },
    "Decision-maker notes": { sourceType: "stakeholder-profile", snippet: people.filter((person) => person.roleType !== "stakeholder").map((person) => person.context).filter(Boolean).join(" ") },
    "Stakeholder notes": { sourceType: "stakeholder-profile", snippet: people.filter((person) => person.roleType === "stakeholder").map((person) => person.context).filter(Boolean).join(" ") },
    "Meeting notes": { sourceType: "meeting-transcript-or-notes", snippet: input.meetingNotes ?? "" },
    "Refinement feedback": { sourceType: "customer-correction", snippet: refinementInstructions(input).map((item) => item.instruction).join(" ") },
    "Previous brief version": { sourceType: "approved-brief", snippet: "Previous packet retained for target-isolated refinement." },
    "Approved pre-brief": { sourceType: "approved-brief", snippet: "Human-approved PilarPrep packet." },
  };
  const labels = Array.from(new Set([
    ...brief.citations,
    ...(brief.evidence ?? []).flatMap((item) => item.sources),
  ])).filter(Boolean);
  const capturedAt = brief.generatedAt || new Date().toISOString();
  const sourceCatalog: EvidenceSourceRecord[] = labels.map((label) => {
    const source = sourceText[label] ?? {
      sourceType: "approved-customer-evidence",
      snippet: "Approved source retained with this packet.",
    };
    return {
      sourceId: stableLocalSourceId(label),
      tenantId: "demo",
      clientId: toProjectId(input.company),
      projectId: toProjectId(input.company),
      label,
      sourceType: source.sourceType,
      title: label,
      sourceLocation: source.location || "browser-local-workspace-record",
      capturedAt,
      freshness: "current-request",
      approvedBy: "request-author",
      evidenceSnippet: source.snippet.trim().slice(0, 600),
      accessScope: "synthetic-demo",
      lifecycleStatus: "active",
    };
  });
  const evidenceByItem = new Map(
    (brief.evidence ?? []).map((item) => [`${item.section}:${item.itemIndex}`, item])
  );
  const ignoredEvidenceTerms = evidenceTerms(input.company);
  const rows: Array<{ section: BriefEvidence["section"]; itemIndex: number; text: string }> = [
    ...businessCaseFields.map((field, itemIndex) => ({ section: "businessCase" as const, itemIndex, text: brief.businessCase[field] })),
    ...(["technical", "executive", "stakeholders", "gameplan", "objections"] as const).flatMap((section) =>
      brief[section].map((text, itemIndex) => ({ section, itemIndex, text }))
    ),
    { section: "projectAnswer" as const, itemIndex: 0, text: brief.projectAnswer },
  ].filter((row) => row.text.trim());
  const claims: BriefClaim[] = rows.map((row) => {
    const evidence = evidenceByItem.get(`${row.section}:${row.itemIndex}`);
    const matches = supportingLocalSources(
      row.text,
      sourceCatalog,
      evidence?.sources ?? [],
      ignoredEvidenceTerms
    );
    const evidenceStatus = localClaimStatus(
      row.section,
      row.itemIndex,
      row.text,
      matches
    );
    const sourceIds = evidenceStatus === "assumption" || evidenceStatus === "needs-validation"
      ? []
      : matches.map((match) => match.source.sourceId);
    const validationStatus: Record<EvidenceStatus, string> = {
      supported: "supported-by-approved-source",
      "partially-supported": "partially-supported-by-approved-source",
      "customer-provided": "supported-by-customer-context",
      assumption: "explicit-assumption",
      "conflicting-evidence": "conflicting-evidence",
      "needs-validation": "unsupported-no-matching-source",
    };
    return {
      claimId: `claim-${stableLocalSourceId(`${row.section}-${row.itemIndex}-${row.text}`).slice(-8)}`,
      section: row.section,
      itemIndex: row.itemIndex,
      text: row.text,
      sourceIds,
      evidenceStatus,
      evidenceSnippet: sourceIds.length
        ? sourceCatalog.find((source) => source.sourceId === sourceIds[0])?.evidenceSnippet ?? ""
        : "No approved supporting source is recorded.",
      validationStatus: validationStatus[evidenceStatus],
    };
  });
  const supported = claims.filter((claim) => claim.sourceIds.length).length;
  const statusCounts = claims.reduce<EvidenceCoverage["statusCounts"]>((counts, claim) => {
    counts[claim.evidenceStatus] = (counts[claim.evidenceStatus] ?? 0) + 1;
    return counts;
  }, {});
  const resolvedEvidence = claims.flatMap((claim) => {
    const sources = claim.sourceIds.flatMap((sourceId) => {
      const source = sourceCatalog.find((item) => item.sourceId === sourceId);
      return source ? [source.label] : [];
    });
    return sources.length
      ? [{ section: claim.section, itemIndex: claim.itemIndex, sources }]
      : [];
  });
  return {
    ...brief,
    citations: Array.from(
      new Set([
        ...brief.citations,
        ...resolvedEvidence.flatMap((item) => item.sources),
      ])
    ),
    evidence: resolvedEvidence,
    sourceCatalog,
    claims,
    evidenceCoverage: {
      materialClaims: claims.length,
      claimsWithApprovedSources: supported,
      coveragePercent: claims.length ? Math.round((supported / claims.length) * 100) : 0,
      statusCounts,
      meaning: "Percentage of material claims linked to approved sources; not a probability of truth.",
    },
  };
}

function compactList(items: string[]) {
  return items.filter(Boolean).join(", ");
}

function rankedPillarsFromInput(input: BriefRequest) {
  const rankedPillars = input.pillarRanking
    ?.slice()
    .sort((a, b) => a.rank - b.rank)
    .map((item) => item.pillar.trim())
    .filter(Boolean);

  return rankedPillars?.length
    ? rankedPillars
    : input.pillars.map((pillar) => pillar.trim()).filter(Boolean);
}

function compactPillarRanking(items: string[], limit = 3) {
  return items
    .slice(0, limit)
    .map((pillar, index) => `${index + 1}. ${pillar}`)
    .join("; ");
}

function companyValuesSignal(companyValues: string | undefined, companyValuesUrl: string | undefined) {
  const cleanValues = companyValues?.trim();
  const cleanUrl = companyValuesUrl?.trim();

  if (!cleanValues && !cleanUrl) {
    return "";
  }

  if (cleanValues && cleanUrl) {
    return ` Company values to respect in tone and tradeoffs: ${cleanValues}. Source page: ${cleanUrl}`;
  }

  if (cleanValues) {
    return ` Company values to respect in tone and tradeoffs: ${cleanValues}`;
  }

  return ` Reference the customer values page when shaping tone and sales positioning. Source page: ${cleanUrl}`;
}

function industryFocus(industry: string) {
  if (industry === "Financial Services") {
    return "auditability, identity controls, regulatory evidence, and migration risk";
  }

  if (industry === "Healthcare") {
    return "patient access, protected health data, continuity, and compliance evidence";
  }

  if (industry === "Retail") {
    return "traffic elasticity, checkout latency, conversion protection, and unit cost";
  }

  if (industry === "Manufacturing") {
    return "plant continuity, data pipelines, forecasting, and operational uptime";
  }

  if (industry === "Media") {
    return "global delivery, content workflow speed, burst traffic, and monetization";
  }

  if (industry === "SaaS") {
    return "tenant isolation, reliability, platform velocity, and growth efficiency";
  }

  return "modernization, reliability, security, and measurable business outcomes";
}

function toProjectId(company: string) {
  const slug = company
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");

  return slug ? `demo-${slug}` : "demo-customer";
}

function normalizeDecisionMakers(
  decisionMakers: DecisionMakerContext[] | undefined
) {
  return (decisionMakers ?? [])
    .map((person) => ({
      name: person.name?.trim() ?? "",
      title: person.title?.trim() ?? "",
      source: person.source?.trim() ?? "",
      context: person.context?.trim() ?? "",
      roleType: person.roleType === "stakeholder" ? "stakeholder" as const : "decision-maker" as const,
      organizationalRole: person.organizationalRole?.trim() ?? "",
      influence: person.influence,
      stance: person.stance,
      decisionAuthority: person.decisionAuthority?.trim() ?? "",
      priorities: person.priorities?.trim() ?? "",
      concerns: person.concerns?.trim() ?? "",
      successMeasures: person.successMeasures?.trim() ?? "",
      engagementGuidance: person.engagementGuidance?.trim() ?? "",
      approvedNotes: person.approvedNotes?.trim() ?? "",
    }))
    .filter((person) => person.name || person.title || person.context);
}
function approvedBriefContext(input: BriefRequest) {
  const approvedBrief = input.approvedBrief;

  if (!approvedBrief) {
    return "Use the generated brief as the working packet, then confirm what changed in the meeting before treating anything as final.";
  }

  const approvedSignals = [
    approvedBrief.technical?.[0],
    approvedBrief.executive?.[0],
    approvedBrief.stakeholders?.[0],
  ].filter((item): item is string => typeof item === "string" && Boolean(item.trim()));

  if (!approvedSignals.length) {
    return "Use the approved brief as the source packet for the handoff, then turn its assumptions, risks, and stakeholder signals into owned follow-through.";
  }

  return `Approved brief context: ${approvedSignals.slice(0, 2).join(" ")}`;
}

export const refinementTargets: RefinementTarget[] = [
  "businessCase",
  "technical",
  "executive",
  "stakeholders",
  "gameplan",
  "objections",
];

export function isRefinementTarget(value: unknown): value is RefinementTarget {
  return typeof value === "string" && refinementTargets.includes(value as RefinementTarget);
}

function clonePacketValue<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

export function refinementInstructions(input: BriefRequest) {
  const structured = (input.feedbackDetails ?? [])
    .map((item) => ({
      category: item.category.trim() || "Additional direction",
      instruction: item.instruction.trim(),
    }))
    .filter((item) => item.instruction);

  const selected = structured.length
    ? structured
    : (input.feedback ?? []).map((value) => {
        const separator = value.indexOf(":");
        return separator > 0
          ? {
              category: value.slice(0, separator).trim(),
              instruction: value.slice(separator + 1).trim(),
            }
          : { category: "Additional direction", instruction: value.trim() };
      });

  if (input.feedbackNotes?.trim()) {
    selected.push({
      category: "Additional direction",
      instruction: input.feedbackNotes.trim(),
    });
  }

  return selected.filter(
    (item, index, items) =>
      item.instruction &&
      items.findIndex(
        (candidate) =>
          candidate.category.toLowerCase() === item.category.toLowerCase() &&
          candidate.instruction.toLowerCase() === item.instruction.toLowerCase()
      ) === index
  );
}

export function refinementAffectedSections(input: BriefRequest) {
  if (
    !previousPacketHasContent(input.previousBrief) ||
    !refinementInstructions(input).length ||
    !isRefinementTarget(input.refinementTarget)
  ) {
    return [];
  }

  return [input.refinementTarget];
}

function previousPacketHasContent(previous: ApprovedBriefSnapshot | undefined) {
  return Boolean(
    previous &&
      (Object.values(previous.businessCase ?? {}).some(Boolean) ||
        previous.technical?.length ||
        previous.executive?.length ||
        previous.stakeholders?.length ||
        previous.gameplan?.length ||
        previous.objections?.length)
  );
}

function applyLocalFactCorrections(value: string, direction: string) {
  const alreadyOnAws =
    /\b(?:already|currently)\s+(?:on|in)\s+aws\b|\bexisting aws (?:environment|estate|footprint)\b/i.test(
      direction
    );
  if (!alreadyOnAws) return value;

  const corrected = value
    .replace(/\bon[- ]prem(?:ises)?\b/gi, "existing AWS environment")
    .replace(
      /\bmigrat(?:e|es|ing|ion)\b.{0,80}\bto\s+(?:aws|the\s+cloud)\b/gi,
      "modernize within AWS"
    )
    .replace(
      /\bmov(?:e|es|ing)\b.{0,80}\bto\s+(?:aws|the\s+cloud)\b/gi,
      "modernize within AWS"
    )
    .replace(/\b(?:data\s*center|datacenter)\s+exit\b/gi, "in-place AWS modernization");

  return /\b(?:already|currently)\b.{0,35}\b(?:on|in|uses?|runs?\s+on|operates?\s+on)\s+aws\b/i.test(
    corrected
  )
    ? corrected
    : corrected + " Confirmed correction: the customer is already on AWS.";
}

function applyLocalPacketRefinement(
  generated: BriefResponse,
  input: BriefRequest
): BriefResponse {
  const previous = input.previousBrief;
  const instructions = refinementInstructions(input);
  if (!previousPacketHasContent(previous) || !previous || !instructions.length) {
    return generated;
  }

  const affected = new Set(refinementAffectedSections(input));
  const direction = instructions
    .map((item) => item.category + ": " + item.instruction)
    .join("; ")
    .slice(0, 420);
  const result: BriefResponse = {
    ...generated,
    businessCase: clonePacketValue(previous.businessCase),
    technical: [...previous.technical],
    executive: [...previous.executive],
    stakeholders: [...previous.stakeholders],
    gameplan: [...previous.gameplan],
    objections: [...previous.objections],
    projectAnswer: previous.projectAnswer ?? generated.projectAnswer,
    projectArtifacts: clonePacketValue(
      previous.projectArtifacts ?? generated.projectArtifacts
    ),
    citations: Array.from(
      new Set([
        ...(previous.citations ?? []),
        "Previous brief version",
        "Refinement feedback",
      ])
    ),
  };

  const businessActions: Record<keyof BusinessCase, string> = {
    scenario: "Restate why this matters now and separate confirmed inputs from assumptions.",
    whyNow: "Name the business event, timing pressure, consequence of delay, and evidence still needed.",
    currentSituation: "Restate only confirmed current-state facts and make unsupported details explicit unknowns.",
    desiredOutcomes: "Connect commercial outcomes to measurable technical validation.",
    successCriteria: "Define evidence-based measures, owners, blockers, and the next approval checkpoint.",
    businessRisks: "Connect customer, operational, financial, governance, and delivery risks to validation evidence.",
    decisionRequired: "Name the decision this meeting must enable, its owner, and the evidence threshold.",
    inScope: "Cover the decisions, evidence, dependencies, and questions needed in this meeting.",
    outOfScope: "Keep final design, unsupported commitments, and unvalidated claims outside scope.",
    assumptionsAndUnknowns: "Separate confirmed facts, corrected facts, assumptions, and open discovery questions.",
    stakeholderAlignment: "Align sponsor value, SA feasibility, risk approval, and delivery ownership.",
    alignmentStatement: "Give Sales and the SA a concise opening that confirms purpose and scope.",
    nextStepGuidance: "Close with owned actions, timing, dependencies, evidence, and the next decision gate.",
  };
  const passageActions: Record<
    Exclude<RefinementTarget, "businessCase">,
    string[]
  > = {
    technical: [
      "Deepen current-state architecture, identity, data-boundary, constraint, and assumption validation.",
      "Request topology, control, recovery, performance, operating, and cost evidence.",
      "Tie each AWS option to a customer decision, tradeoff, and proof requirement.",
      "Turn unknowns into architecture questions, owners, risks, and decision gates.",
    ],
    executive: [
      "Connect urgency to customer impact, business risk, competitive pressure, cost, or deadline.",
      "Frame value, measurable outcomes, decision confidence, and the consequence of delay without service jargon.",
      "Name sponsor priorities, blockers, approval criteria, and evidence for the next commitment.",
      "Separate near-term proof from later investment and ask an executive decision question.",
    ],
    stakeholders: [
      "Clarify confirmed priorities, hypotheses, influence, and decision responsibility.",
      "Connect the role to required evidence, blockers, dependencies, and a live question.",
      "Identify what this role approves, what changes confidence, and who else participates.",
      "Record the follow-through owner and the signal that permits the next decision.",
    ],
    gameplan: [
      "Open with the business scenario, urgency, outcomes, and alignment check.",
      "Sequence discovery around ranked pillars, assumptions, evidence, and audience-specific questions.",
      "Read back scope, risks, blockers, owners, and measurable criteria before recommending a path.",
      "Close with actions, timing, dependencies, decision gates, and the next meeting.",
    ],
    objections: [
      "Address the concern using customer context, evidence, and a bounded validation step.",
      "Connect the response to business value and the technical proof the SA must collect.",
      "Name the owner, dependency, risk, and approval gate behind the concern.",
      "End with a question that advances the decision without inventing facts.",
    ],
  };

  if (affected.has("businessCase")) {
    result.businessCase = Object.fromEntries(
      (Object.keys(businessActions) as Array<keyof BusinessCase>).map((key) => [
        key,
        applyLocalFactCorrections(
          generated.businessCase[key] +
            " Refinement direction: " +
            businessActions[key] +
            " Apply: " +
            direction,
          direction
        ),
      ])
    ) as BusinessCase;
  }

  const arraySections = ["technical", "executive", "stakeholders", "gameplan", "objections"] as const;
  for (const section of arraySections) {
    if (!affected.has(section)) continue;
    const baseItems = generated[section];
    result[section] = baseItems.map(
      (item, index) =>
        applyLocalFactCorrections(
          item +
            " Refinement direction: " +
            passageActions[section][index % passageActions[section].length] +
            " Apply: " +
            direction,
          direction
        )
    );
  }

  const target = input.refinementTarget as RefinementTarget;
  const previousEvidence = previous.evidence ?? [];
  const targetEvidence = (generated.evidence ?? [])
    .filter((item) => item.section === target)
    .map((item) => ({
      ...item,
      sources: Array.from(
        new Set(["Previous brief version", "Refinement feedback", ...item.sources])
      ).slice(0, 3),
    }));
  result.evidence = [
    ...previousEvidence
      .filter((item) => item.section !== target)
      .map((item) => clonePacketValue(item)),
    ...targetEvidence,
  ];

  result.metadata = {
    ...result.metadata,
    baseBriefVersion: input.baseBriefVersion,
    packetVersion: (input.baseBriefVersion ?? 0) + 1,
    refinementTarget: target,
    refinementSections: [...affected],
    refinementInstructionCount: instructions.length,
    changedSectionIds: [target],
    unauthorizedSectionChanges: 0,
    refinementIsolationPassed: true,
  };
  return result;
}
function buildProjectArtifacts(
  input: BriefRequest,
  company: string,
  pillars: string[],
  decisionMakers: DecisionMakerContext[],
  focus: string,
  valuesSignal: string
): ProjectArtifacts {
  const primaryPillar = pillars[0] ?? "Security";
  const sponsor = decisionMakers[0]?.name || "executive sponsor";

  return {
    twoWeekPlan: [
      {
        title: "Days 1-2: Confirm outcomes",
        owner: "Solutions Architect / Sales",
        status: "Ready",
        detail: `Objective: align ${company} on urgency, desired outcomes, and decision authority. Output: an approved outcome statement and owner map. Dependency: sponsor and technical-owner availability. Exit criterion: the customer confirms the next decision and who can make it.`,
      },
      {
        title: "Days 3-5: Validate current state",
        owner: "Solutions Architect / Customer engineer",
        status: "Ready",
        detail: `Objective: test current-state architecture and the top ${primaryPillar.toLowerCase()} hypotheses. Output: an evidence-backed constraints and unknowns register. Dependency: customer diagrams, metrics, recovery targets, and control artifacts. Exit criterion: confirmed facts are visibly separated from assumptions.`,
      },
      {
        title: "Days 6-8: Shape bounded pilot",
        owner: "Engineer / Solutions Architect",
        status: "Draft",
        detail: "Objective: define the smallest useful validation scope. Output: a pilot design with integration boundaries, rollback, observability, acceptance thresholds, and evidence owners. Dependency: accepted current-state findings. Exit criterion: technical and control owners agree on go, pause, or redirect criteria.",
      },
      {
        title: "Days 9-10: Package decision evidence",
        owner: "PM / Sponsor",
        status: "Draft",
        detail: `Objective: prepare the decision with ${sponsor}. Output: a decision log, risk register, pilot recommendation, and next-meeting agenda. Dependency: completed validation and owner sign-off. Exit criterion: delivery accepts the handoff without reopening completed discovery.`,
      },
    ],    riskRegister: [
      {
        title: "Unvalidated assumption: current-state evidence is complete",
        owner: "Solutions Architect",
        status: "Unvalidated",
        detail: `The generated direction assumes the supplied context captures ${company}'s material dependencies. Obtain customer diagrams, metrics, control evidence, and owner confirmation before treating any recommendation as a design decision.`,
      },      {
        title: `${primaryPillar} ownership gap`,
        owner: "Customer owner",
        status: "Medium",
        detail:
          "The project may stall if the highest-risk pillar lacks a named decision maker and technical owner.",
      },
      {
        title: "Narrative drift",
        owner: "PM",
        status: "Medium",
        detail:
          "Technical and executive tracks can diverge. Keep every architecture task connected to a measurable business outcome.",
      },
      {
        title: "Evidence gap",
        owner: "SA / PM",
        status: "Medium",
        detail:
          "The pilot may stall if architecture, control, cost, or success evidence is not captured in a reusable project record.",
      },
    ],
    stakeholderMap: decisionMakers.length
      ? decisionMakers.map((person) => ({
          title: person.name || "Decision maker",
          owner: person.title || "Role to confirm",
          status: "Validate",
          detail:
            person.context ||
            `Confirm how this stakeholder defines success around ${focus}.`,
        }))
      : [
          {
            title: "Economic buyer",
            owner: "To confirm",
            status: "Needed",
            detail:
              "Identify who owns budget approval, business value, and final prioritization.",
          },
          {
            title: "Technical owner",
            owner: "To confirm",
            status: "Needed",
            detail:
              "Identify who owns current-state validation, architecture decisions, and implementation tradeoffs.",
          },
        ],
    followUpEmail: {
      subject: `Follow-up from PilarPrep briefing for ${company}`,
      body: `Thanks for the conversation. We captured ${focus} as the main business context and ${primaryPillar.toLowerCase()} as the first technical validation area.${valuesSignal}\n\nRecommended next step: run a focused working session to confirm stakeholders, current-state assumptions, success criteria, risks, and pilot scope.\n\nWe will use the approved brief, decision-maker notes, meeting outcomes, and owner list as the shared project context.`,
    },
    nextSteps: {
      immediateActions: [
        {
          action: `Confirm the current-state evidence package for ${company}`,
          owner: decisionMakers[1]?.name || "Customer technical owner",
          timing: "Within 2 business days",
          dependency: "Architecture diagram, dependency inventory, recovery targets, and existing control evidence",
          decisionGate: `Evidence is complete enough to plan the ${primaryPillar.toLowerCase()} validation workshop`,
        },
        {
          action: `Run the ${primaryPillar} validation workshop`,
          owner: "SA and customer technical owner",
          timing: "Within 1 week",
          dependency: "Confirmed owners, ranked risks, and the current-state evidence package",
          decisionGate: "The team agrees on the riskiest assumption, proof method, and rollback boundary",
        },
        {
          action: "Publish the bounded pilot decision memo",
          owner: decisionMakers[0]?.name || "Executive sponsor",
          timing: "By the end of week 1",
          dependency: "Workshop findings, success measures, open risks, and cost boundary",
          decisionGate: "Sponsor approves, pauses, or redirects the pilot with a recorded rationale",
        },
        {
          action: "Schedule the implementation-readiness review",
          owner: "PM / Sales",
          timing: "Within 10 business days",
          dependency: "Named delivery owners, resolved blockers, and an approved pilot decision",
          decisionGate: "Delivery accepts the handoff without requiring repeated discovery",
        },
      ],
      openQuestions: [
        `Who owns final approval for the ${primaryPillar.toLowerCase()} proof and any exception?`,
        "Which customer artifact will validate the highest-risk current-state assumption?",
        "What measurable threshold would stop, redirect, or expand the pilot?",
      ],
      nextMeeting: {
        purpose: `Validate ${company}'s highest-risk assumptions and agree on the bounded pilot decision`,
        timing: "Within 5 business days",
        attendees: Array.from(new Set([
          ...decisionMakers.map((person) => person.name || person.title).filter(Boolean),
          "Solutions Architect",
          "Customer technical owner",
        ])).slice(0, 6),
      },
      customerSummary: `${company} and the account team will reconvene to validate the current-state evidence, confirm owners and success thresholds, and decide whether a bounded ${primaryPillar.toLowerCase()} pilot can proceed without increasing customer or operational risk.`,
      internalNotes: `Keep ${company}'s assumptions explicitly unvalidated until customer evidence is attached. Escalate missing ownership, rollback criteria, or success thresholds before implementation planning begins.`,
    },
  };
}

export function generateDemoBrief(input: BriefRequest): BriefResponse {
  const company = input.company.trim() || "the customer";
  const pillars = rankedPillarsFromInput(input);
  const rankedPillars = pillars.length ? pillars : ["Security", "Reliability"];
  const topPillars = rankedPillars.slice(0, 3);
  const primaryPillar = rankedPillars[0] ?? "Security";
  const rankingSummary = compactPillarRanking(topPillars.length ? topPillars : rankedPillars);
  const decisionMakers = normalizeDecisionMakers(input.decisionMakers);
  const decisionAuthorities = decisionMakers.filter((person) => person.roleType === "decision-maker");
  const influentialStakeholders = decisionMakers.filter((person) => person.roleType === "stakeholder");
  const stakeholderLead = decisionAuthorities[0] ?? influentialStakeholders[0];
  const feedback = input.feedback?.length
    ? `Refinements applied: ${compactList(input.feedback)}.`
    : "No extra refinement feedback applied yet.";
  const focus = industryFocus(input.industry);
  const valuesSignal = companyValuesSignal(input.companyValues, input.companyValuesUrl);
  const approvedContext = approvedBriefContext(input);
  const stakeholderText = stakeholderLead
    ? `${stakeholderLead.roleType === "decision-maker" ? "Decision-authority" : "Stakeholder-influence"} angle: anchor the conversation to ${stakeholderLead.name || "the primary stakeholder"}${stakeholderLead.title ? ` (${stakeholderLead.title})` : ""} and validate the priorities captured in the approved profile notes.`
    : "People angle: add approved decision-maker and stakeholder notes to tailor the opening, questions, and objection handling.";
  const projectArtifacts = buildProjectArtifacts(
    input,
    company,
    rankedPillars,
    decisionMakers,
    focus,
    valuesSignal
  );
  const stakeholderNames = decisionMakers
    .map((person) => person.name || person.title)
    .filter(Boolean)
    .slice(0, 3)
    .join(", ");
  const valuesContext = input.companyValues?.trim()
    ? `The account team should frame the value in ${company}'s stated principles: ${input.companyValues.trim()}`
    : `The account team should ask which customer principles must govern the recommendation before positioning value.`;
  const additionalDirection = input.additionalDirection?.trim();
  const directionSignal = additionalDirection
    ? `Additional direction the team must cover: ${additionalDirection}`
    : "No additional customer direction was supplied.";
  const refinementDirection = input.feedback?.length
    ? `The latest SA refinement also asks the team to emphasize: ${compactList(input.feedback)}.`
    : "No additional SA refinement has been applied, so the team should validate emphasis during the meeting.";

  const businessCase = {
    scenario: `${company} is preparing for a ${input.meetingType.toLowerCase()} because ${focus} now needs a shared business and technical decision path. Customer-provided context states: ${input.context.trim()} This is the known starting point, not a complete current-state assessment. ${directionSignal} The working hypothesis is that unresolved ownership, evidence, or architecture constraints could slow the initiative or create avoidable customer impact; the SA must validate that hypothesis before recommending a design. ${valuesContext} ${stakeholderNames ? `Approved notes identify ${stakeholderNames} as important voices in the decision path.` : "The economic buyer, technical owner, and control approver still need to be confirmed."}`,
    whyNow: `${company} needs to establish the event that makes action timely before Sales frames value or the SA proposes a path. The supplied context points to ${focus}, but the customer must confirm the deadline, customer consequence, operational pressure, competitive exposure, or cost of waiting that creates urgency. The meeting should test whether delay creates material risk and whether the requested decision is needed now. If no time-bound driver is confirmed, record urgency as an unknown and ask which event should govern sequencing and investment.` ,
    currentSituation: `The authoritative starting point is limited to the customer-provided context: ${input.context.trim()} ${input.meetingNotes?.trim() ? `Meeting notes add: ${input.meetingNotes.trim()}` : "No prior meeting outcomes were supplied."} Everything beyond those statements remains a working assumption. The SA should validate current architecture, ownership, operating constraints, evidence quality, dependencies, and the status of any existing AWS environment before using migration, modernization, or target-state language. Corrected feedback takes precedence over the original generated packet.` ,
    desiredOutcomes: `${company} should leave with business outcomes and technical proof connected in one plan. Sales can frame the value as protecting customer experience, reducing decision latency and rework, and creating a controlled path to the next commitment. The SA must translate that framing and any additional direction into testable outcomes: validate the rank 1 ${primaryPillar.toLowerCase()} risk, agree on current-state evidence, define a bounded pilot and rollback boundary, and name owners for each dependency. Measures must be confirmed with the customer rather than invented; ask which service, risk, cost, delivery, or governance indicators will prove meaningful progress.`,
    successCriteria: `The meeting is successful when Sales and the SA can restate the same business problem, ${company} corrects material context gaps, and the group agrees which outcomes matter and how they will be measured. The team should leave with confirmed scope, a visible list of unvalidated assumptions, named owners for architecture and control evidence, a defined rank 1 ${primaryPillar.toLowerCase()} validation gate, and a scheduled working session with the required sponsor and technical approvers. No implementation handoff should begin until the decision owner and evidence threshold are clear.`,
    businessRisks: `The main business risk is making a recommendation before ${company} confirms the facts that drive value and feasibility. That could create avoidable rework, delay risk approval, weaken sponsor confidence, or expose customer-facing operations to poorly bounded change. The team should also validate the consequence of doing nothing, ownership gaps, competing priorities, capacity limits, and any governance or cost boundary. Each risk needs an owner, evidence source, mitigation choice, and decision checkpoint instead of a generic severity label.`,
    decisionRequired: `This conversation should enable one bounded decision: whether ${company} has enough aligned business context and technical evidence to proceed to a focused validation step. The sponsor must confirm the outcome and urgency, the technical owner must confirm what can be tested, and the relevant risk owner must define acceptable evidence. The group should explicitly choose go, pause, or redirect, name who owns that choice, and record which unresolved fact would change it.`,
    alignmentStatement: `Before we discuss solutions, we want to confirm that ${company}'s goal is to address ${focus}, validate the highest-risk ${primaryPillar.toLowerCase()} assumptions, and leave with agreed evidence, owners, and a next decision. We will separate what your team has confirmed from what we still need to test. Is that the right outcome for today's conversation?`,
    inScope: `We will connect the commercial reason for action to the technical discovery needed to support it. That includes the urgency and consequence of delay, desired business and customer outcomes, ranked discovery priorities (${rankingSummary}), current-state constraints, additional direction, stakeholder decision criteria, security and operational evidence, measurable pilot acceptance and rollback conditions, and ownership of the next decision. ${refinementDirection} The conversation should end with explicit questions for any missing information instead of silently filling gaps with AI assumptions.`,
    outOfScope: `We will not use this meeting to finalize a production architecture, promise savings or delivery dates, certify compliance, select every AWS service, or approve a broad migration. We will also avoid treating decision-maker notes or generated architecture hypotheses as confirmed facts. Those commitments remain deferred until ${company}'s technical and control owners provide the relevant artifacts, validate dependencies and operating responsibilities, and agree that the proposed pilot evidence is sufficient for a go, pause, or redirect decision.`,
    assumptionsAndUnknowns: `Confirmed facts are limited to the customer-entered context, company values, approved stakeholder notes, meeting notes, and explicit corrections. Working assumptions include the completeness of current-state evidence, the ownership model, the primary source of risk, and whether a bounded validation step is feasible. Unknowns include the business deadline, baseline measures, architecture constraints, approval path, available capacity, and required artifacts. The SA should convert each unknown into a live question and avoid carrying superseded language into the recommendation.`,
    stakeholderAlignment: `${stakeholderNames ? `${stakeholderNames} are named in the supplied people context, but their priorities, influence, and decision authority still need customer confirmation.` : "The sponsor, economic buyer, technical owner, risk approver, and project driver still need to be identified."} Sales should own the value narrative and consequence of delay; the SA should own feasibility questions and evidence; customer owners should confirm risk tolerance and the decision gate. The meeting should expose disagreements early and finish with one owner for each open question and next action.`,
    nextStepGuidance: `Close by reading back ${company}'s confirmed scenario, corrections, desired outcomes, measures, scope, risks, assumptions, and the decision reached. Create three to six immediate actions with named owners, timing, dependencies, and evidence requirements, then schedule the next technical or sponsor checkpoint. The first follow-on session should resolve the highest-risk ${primaryPillar.toLowerCase()} unknown rather than reopen the entire discovery. Promote only the approved packet and meeting outcomes into the handoff used by Sales, the SA, and delivery.`,
  };
  const peopleForStakeholderBriefing = [
    ...decisionAuthorities.slice(0, 2),
    ...influentialStakeholders.slice(0, 2),
    ...decisionAuthorities.slice(2),
    ...influentialStakeholders.slice(2),
  ].slice(0, 4);
  const stakeholderBriefing = [
    ...peopleForStakeholderBriefing.map((person) => {
      const title = person.title ? ` (${person.title})` : "";
      const roleLabel = person.roleType === "decision-maker" ? "Decision-maker" : "Stakeholder";
      const authority = person.roleType === "decision-maker"
        ? "Confirm the approval, funding, risk-acceptance, or technical decision they own."
        : `Do not imply approval authority; validate how they shape requirements, evidence, adoption, or resistance.${person.influence ? ` Influence: ${person.influence}.` : ""}${person.stance ? ` Current stance: ${person.stance}.` : ""}`;
      const signal = person.context
        ? `Use the approved signal as a hypothesis: ${person.context}`
        : `Use the ranked pillar list as the hypothesis, starting with ${primaryPillar}.`;

      return `${person.name || roleLabel}${title} — ${roleLabel}: tailor the opening to their confirmed role. ${authority} ${signal} Ask: "What outcome matters from your seat, what evidence would increase your confidence, and who owns the final decision?"`;
    }),
    `Economic buyer to confirm: identify who owns budget, value, and final prioritization for ${company}. Ask: "What business metric will prove this was worth doing, and what date or event is creating urgency?"`,
    `Technical owner to confirm: identify who owns current-state architecture, implementation feasibility, and operating model decisions. Ask: "Where are the highest-risk dependencies, what evidence do you need before approving the target pattern, and what rollback expectation is non-negotiable?"`,
    `Influence path to confirm: identify champions, reviewers, evaluators, application owners, and potential blockers who shape the recommendation without granting final approval. Ask: "Whose evidence, adoption, or objection could materially change the decision?"`,
  ].slice(0, 4);
  const sourceLabels = [
    "Customer context",
    ...(input.companyValues?.trim() ? ["Company values"] : []),
    ...(additionalDirection ? ["Additional direction"] : []),
    ...(decisionAuthorities.length ? ["Decision-maker notes"] : []),
    ...(influentialStakeholders.length ? ["Stakeholder notes"] : []),
    ...(input.meetingNotes?.trim() ? ["Meeting notes"] : []),
    "AWS Well-Architected pillars",
  ];
  const evidence: BriefEvidence[] = (["technical", "executive", "stakeholders", "gameplan", "objections"] as const).flatMap(
    (section) => Array.from({ length: 4 }, (_, itemIndex) => ({
      section,
      itemIndex,
      sources:
        section === "technical"
          ? sourceLabels.filter((label) => label === "Customer context" || label === "Meeting notes" || label === "AWS Well-Architected pillars")
          : section === "stakeholders"
            ? sourceLabels.filter((label) => (label === "Decision-maker notes" || label === "Stakeholder notes" || label === "Customer context"))
            : sourceLabels.filter((label) => label !== "AWS Well-Architected pillars").slice(0, 3),
    }))
  );
  evidence.unshift(
    ...Array.from({ length: Object.keys(businessCase).length }, (_, itemIndex) => ({
      section: "businessCase" as const,
      itemIndex,
      sources: sourceLabels.filter((label) =>
        label === "Customer context" ||
        label === "Company values" ||
        label === "Decision-maker notes" ||
        label === "Meeting notes" ||
        label === "AWS Well-Architected pillars"
      ).slice(0, 4),
    }))
  );
  evidence.push({
    section: "projectAnswer",
    itemIndex: 0,
    sources: sourceLabels.filter((label) => label === "Customer context" || label === "Meeting notes" || label === "Company values"),
  });
  const projectAnswer = input.role === "Solutions Architect"
    ? `${company}'s supplied context establishes the working business scenario and urgency, while every current-state architecture statement remains an AI hypothesis until the customer validates it. Start with the desired outcomes and ranked pillars (${rankingSummary}), then request the current topology, identity and data boundaries, RTO/RPO, compliance scope, workload metrics, cost baseline, incident history, deployment process, and operational ownership needed to test security, reliability, performance, cost, and operations constraints. Use AWS services only as evaluated options tied to a decision: for example, CloudWatch when telemetry evidence is missing, S3 when approved artifacts need durable retention, and DynamoDB when project state needs controlled continuity. Record risks, dependencies, evidence owners, timing, and the approval gate for each recommendation, and keep customer-confirmed facts visibly separate from unvalidated assumptions. Recommend a focused technical validation session within five business days with the Solutions Architect, ${decisionMakers.map((person) => person.name).filter(Boolean).slice(0, 3).join(", ") || "the customer technical owner and relevant control approver"}; the meeting should decide whether the bounded pilot has enough evidence to proceed.`
    : `For ${input.role ?? "the project team"}, use the approved brief as the starting project model, not as final truth. ${approvedContext}${valuesSignal} The next useful move is a two-week sprint for ${company}: confirm stakeholders, validate rank 1 ${primaryPillar.toLowerCase()} assumptions, review current-state evidence, turn meeting notes into owners and risks, and publish a decision log that sales, SA, engineering, and the sponsor can all reuse. For the prompt "${input.prompt ?? "What should we do next?"}", answer with concrete owner-based actions, the evidence needed to proceed, and the blocker that should be escalated first${stakeholderLead ? ` with ${stakeholderLead.name || "the primary stakeholder"}` : ""}.`;
  const generated: BriefResponse = {
    provider: "demo",
    generatedAt: new Date().toISOString(),
    metadata: {
      projectId: toProjectId(company),
      clientId: toProjectId(company),
      artifactRetention: "browser-local",
      packetVersion: 1,
    },
    businessCase,
    technical: [
      `${company} should be framed around the ranked Well-Architected priorities (${rankingSummary}), with rank 1 treated as the first discovery lens instead of a generic checkbox. Current-state validation should focus on how ${primaryPillar.toLowerCase()} shows up in the architecture today: identity boundaries, data movement, failure modes, operating ownership, and evidence the customer already has. Ask: "Which current-state assumption would be most dangerous if we got it wrong, and what artifact can we review to validate it before proposing a target design?"`,
      `For a ${input.companySize.toLowerCase()} ${input.industry.toLowerCase()} customer, the SA should convert the meeting into measurable acceptance criteria instead of broad cloud recommendations. Confirm RTO/RPO, compliance scope, latency or throughput targets, incident response ownership, release/change process, and dependency constraints that could shape the first pilot. Ask: "What has to be true for your technical leads, security team, and business sponsor to all call this safe enough to proceed?"`,
      `The AWS path should be discussed as an implementation option only after the customer's risks are clear: API Gateway and Lambda for controlled orchestration, Bedrock for generation, S3 for artifacts, DynamoDB for project state, CloudWatch for observability, and Knowledge Bases for approved project memory. Tie every service mention to a customer decision, not a feature tour. Ask: "Which decision do you need AWS to make easier: reducing risk, speeding delivery, proving compliance, improving reliability, or controlling cost?"`,
      `Use the ranked pillar order to shape the proof plan for ${company}: rank 1 gets the deepest evidence review, ranks 2 and 3 become tradeoff checks, and lower-ranked pillars stay visible so they are not ignored. Capture which artifacts are missing, who owns each artifact, and how a pilot would prove the riskiest assumption. Ask: "What proof would let us move from discussion to a small approved pilot?"`,
    ],
    executive: [
      `${company} is balancing speed with risk control, so the executive conversation should start with ${focus} rather than architecture diagrams. ${valuesSignal ? `Use the customer values signal when framing the recommendation.${valuesSignal} ` : ""}The strongest framing is that PilarPrep improves decision quality before the meeting and preserves follow-through after the meeting, reducing the chance that good discovery turns into scattered notes. Ask: "What business outcome would make this meeting a success 30 days from now?"`,
      `The business case should emphasize fewer missed risks, faster alignment across sales/SA/project teams, and a clearer path from discussion to pilot. Avoid AWS jargon unless an executive asks how it works; describe the result as a repeatable way to prepare, validate assumptions, and turn meeting outcomes into owners, risks, and next actions. Ask: "Where do projects like this usually slow down: funding, security approval, technical uncertainty, or lack of ownership?"`,
      `For the sponsor, the important decision is whether to approve a bounded validation sprint with clear success measures, decision owners, and evidence checkpoints. ${stakeholderText} ${feedback} Ask: "What would make you comfortable saying yes to the next step, and what evidence would you need before scaling beyond a pilot?"`,
      `Frame the ROI for ${company} as decision speed and rework reduction: better prep should reduce repeated discovery, unclear handoffs, and late risk surprises. The executive sponsor does not need a service tour; they need confidence that the team can move in a controlled way and know when to stop, pivot, or expand. Ask: "Which delay costs more right now: waiting for perfect information, or moving forward without enough evidence?"`,
    ],
    stakeholders: stakeholderBriefing,
    gameplan: [
      `Open with a tight purpose statement: "We are here to validate the assumptions behind ${company}'s ${input.meetingType.toLowerCase()} and agree on the evidence needed for a safe next step." Then confirm the business event driving urgency, the decision owner, and the ranked pillar order before going deep. Ask: "Is ${primaryPillar} really the first priority, or should we reorder the conversation based on what is most likely to block approval?"`,
      stakeholderLead
        ? `Use ${stakeholderLead.name || "the primary stakeholder"} as the first anchor, but do not overfit to one person. Confirm what success looks like, what risk would slow approval, who else needs to be in the decision, and what proof would change their confidence level. Ask: "Which result would increase your confidence enough to approve the next step?" Then map the answer back to the ranked pillars so the technical discussion stays connected to sponsor value.`
        : `Identify the economic buyer, technical owner, security approver, and project driver before going deep. Ask each role a different question: the buyer gets value and timing, the technical owner gets constraints and evidence, security gets control requirements, and the project driver gets owners and next steps. Use the answers to decide whether the meeting should stay at discovery level or move into architecture detail.` ,
      `Spend the middle of the meeting on rank 1 ${primaryPillar.toLowerCase()} tradeoffs, then use ranks 2 and 3 to shape secondary discovery. Ask: "Which unresolved question is most likely to delay approval if we do not answer it this week?"`,
      `Close by reading back the agreed success measure, owner list, risks, unresolved questions, timeline, and how the Project Brain handoff will be used after the call. Do the readback while customer stakeholders are still present so corrections become shared truth immediately. Ask: "What should we capture now so the implementation team does not have to rediscover it later?"`,
    ],
    objections: [
      `Concern: "We do not have enough reliable context." Response: agree and make that the operating model: every generated recommendation is a hypothesis until the customer validates it with artifacts, owner confirmation, or meeting notes. Ask: "Which assumption should we validate first because it would change the plan the most?"`,
      `Concern: "This feels too AWS-heavy." Response: separate the executive story from the technical implementation path; lead with outcomes, risks, decision speed, and ownership, then use AWS services only where they make a specific decision easier. Ask: "Would it be more useful to compare business outcomes first and leave service mapping for the technical deep dive?"`,
      stakeholderLead
        ? `Concern: "${stakeholderLead.name || "The sponsor"} may not see why this is relevant." Response: connect the recommendation to the approved stakeholder signal, then ask what has changed since those notes were captured. Ask: "Which priority should we retire, update, or elevate based on today's business reality?"`
        : `Concern: "We do not know what the decision makers care about." Response: capture approved stakeholder context before the follow-up and use Project Brain to refresh the plan from known notes, not guessed profile data. Ask: "Who must approve the business case, technical plan, security posture, and funding path?"`,
      `Concern: "The generated brief may be wrong." Response: agree, then position the brief as a structured hypothesis map that speeds validation rather than replacing customer discovery. Ask: "Which assumption should we mark as highest risk until your team confirms it?"`,
    ],
    projectAnswer,
    projectArtifacts,
    citations: sourceLabels,
    evidence,
  };
  return attachLocalProvenance(applyLocalPacketRefinement(generated, input), input);
}

export function generateBlueMesaBackupBrief(
  request: Partial<BriefRequest> = {},
): BriefResponse {
  const defaults: BriefRequest = {
    mode: "project",
    modelPreference: "nova-pro",
    company: "BlueMesa Payments",
    industry: "Financial Services",
    meetingType: "Executive Briefing",
    companySize: "Enterprise",
    pillars: ["Security", "Reliability", "Operational Excellence"],
    pillarRanking: [
      { rank: 1, pillar: "Security" },
      { rank: 2, pillar: "Reliability" },
      { rank: 3, pillar: "Operational Excellence" },
    ],
    context: "BlueMesa Payments is a regulated payment processor whose production payment APIs already run on Amazon EKS, with operational data in Amazon RDS for PostgreSQL, events on Amazon MSK, and evidence archives in Amazon S3. BlueMesa wants governed payroll-partner onboarding through real-time APIs and encrypted batch files without replacing its ledger.",
    companyValues: "Merchant trust, auditable operations, predictable settlement, accountable ownership, and faster partner onboarding without weakening payment or payroll data protection.",
    companyValuesUrl: "https://www.bluemesa-payments.example/company/values",
    additionalDirection: "BlueMesa is an existing AWS customer. The meeting scope includes payroll integration, mixed API and encrypted-file interfaces, idempotency, reconciliation, data privacy, retention, partner certification, cutover, and recovery evidence. Ledger replacement remains outside scope.",
    decisionMakers: [
      { name: "Ariana Cole", roleType: "decision-maker", title: "Chief Digital Officer", organizationalRole: "Executive sponsor", decisionAuthority: "Strategic priority and final program commitment", source: "Customer-approved profile notes", context: "Sponsors merchant trust, payroll-partner growth, and a bounded first release." },
      { name: "Marcus Vale", roleType: "decision-maker", title: "Chief Financial Officer", organizationalRole: "Economic buyer", decisionAuthority: "Funding and commercial approval", source: "Customer-approved profile notes", context: "Requires a bounded investment tied to onboarding speed and partner activation." },
      { name: "Dev Malik", roleType: "decision-maker", title: "VP Infrastructure and Resilience", organizationalRole: "Technical decision-maker", decisionAuthority: "Technical direction and production readiness", source: "Customer-approved profile notes", context: "Owns integration reliability, replay evidence, observability, and rollback readiness." },
      { name: "Rachel Kim", roleType: "decision-maker", title: "Chief Risk and Compliance Officer", organizationalRole: "Control approver", decisionAuthority: "Control acceptance and risk exceptions", source: "Customer-approved profile notes", context: "Owns payroll-data handling, privileged-access evidence, retention, and compliance approval." },
      { name: "Priya Shah", roleType: "stakeholder", title: "Director of Payment Operations", organizationalRole: "Operational owner", influence: "high", stance: "supportive", source: "Customer-approved profile notes", context: "Influences acceptance through reconciliation, exception handling, payroll cutoffs, and runbook ownership." },
      { name: "Elena Torres", roleType: "stakeholder", title: "Payroll Partnerships Lead", organizationalRole: "Internal champion", influence: "high", stance: "champion", source: "Customer-approved profile notes", context: "Connects partner certification windows and onboarding commitments to technical requirements." },
      { name: "Noah Grant", roleType: "stakeholder", title: "Director of Strategic Procurement", organizationalRole: "Commercial reviewer", influence: "medium", stance: "neutral", source: "Customer-approved profile notes", context: "Can delay the pilot until vendor responsibilities and commercial dependencies are clear." },
      { name: "Omar Fields", roleType: "stakeholder", title: "Platform Engineering Lead", organizationalRole: "Technical evaluator", influence: "high", stance: "skeptical", source: "Customer-approved profile notes", context: "Can block implementation readiness if replay tooling, ownership, and operating capacity are weak." },
    ],
    meetingNotes: "Confirmed context: BlueMesa already runs the payment platform on AWS, targets two payroll design partners and a twelve-to-four-week onboarding improvement, expects mixed API and encrypted-file interfaces, and excludes ledger replacement. Working assumptions to validate in the call: both partners prefer API-first integration, the seven-year settlement retention rule applies to payroll data, Platform Engineering has delivery capacity, and implementation funding is approved. Open discovery: exact retention, RTO/RPO, partner certification dates, and the investment gate.",
    feedback: ["Make owners and evidence checkpoints explicit", "Keep executive language free of AWS jargon"],
    role: "PM",
    prompt: "Create the first two-week plan and identify the first approval gate.",
  };
  const backup = generateDemoBrief({
    ...defaults,
    ...request,
    pillars: request.pillars?.length ? request.pillars : defaults.pillars,
    pillarRanking: request.pillarRanking?.length
      ? request.pillarRanking
      : defaults.pillarRanking,
    decisionMakers:
      request.decisionMakers !== undefined
        ? request.decisionMakers
        : defaults.decisionMakers,
    feedback: request.feedback ?? defaults.feedback,
  });

  return {
    ...backup,
    generatedAt: "2026-08-12T12:00:00.000Z",
    metadata: {
      ...backup.metadata,
      agentMode: "prepared-demo-backup",
      fallbackUsed: true,
      fallbackReason: "Prepared BlueMesa backup loaded because the live AI path was unavailable.",
    },
  };
}

export function validateBriefRequest(input: Partial<BriefRequest>) {
  if (!input.company?.trim()) {
    return "company is required";
  }

  if (!input.industry?.trim()) {
    return "industry is required";
  }

  if (!input.meetingType?.trim()) {
    return "meetingType is required";
  }

  if (!input.companySize?.trim()) {
    return "companySize is required";
  }

  if (!Array.isArray(input.pillars)) {
    return "pillars must be an array";
  }

  if (
    input.pillarRanking !== undefined &&
    !Array.isArray(input.pillarRanking)
  ) {
    return "pillarRanking must be an array";
  }

  if (
    input.decisionMakers !== undefined &&
    !Array.isArray(input.decisionMakers)
  ) {
    return "decisionMakers must be an array";
  }

  if (
    input.decisionMakers?.some((person) =>
      person.roleType !== undefined &&
      person.roleType !== "decision-maker" &&
      person.roleType !== "stakeholder"
    )
  ) {
    return "decisionMakers roleType must be decision-maker or stakeholder";
  }

  if (
    input.decisionMakers?.some((person) =>
      person.influence !== undefined &&
      !["high", "medium", "low"].includes(person.influence)
    )
  ) {
    return "decisionMakers influence must be high, medium, or low";
  }

  if (
    input.decisionMakers?.some((person) =>
      person.stance !== undefined &&
      !["champion", "supportive", "neutral", "skeptical", "blocker"].includes(person.stance)
    )
  ) {
    return "decisionMakers stance is invalid";
  }

  if (!input.context?.trim()) {
    return "context is required";
  }

  if (input.companyValues !== undefined && typeof input.companyValues !== "string") {
    return "companyValues must be a string";
  }

  if (input.companyValuesUrl !== undefined && typeof input.companyValuesUrl !== "string") {
    return "companyValuesUrl must be a string";
  }

  if (
    input.modelPreference !== undefined &&
    input.modelPreference !== "default" &&
    input.modelPreference !== "nova-pro" &&
    input.modelPreference !== "nova-micro" &&
    input.modelPreference !== "claude-sonnet-4.6"
  ) {
    return "modelPreference must be default, nova-pro, nova-micro, or claude-sonnet-4.6";
  }

  const hasRefinementEnvelope =
    input.previousBrief !== undefined ||
    input.baseBriefVersion !== undefined ||
    input.refinementTarget !== undefined;
  if (hasRefinementEnvelope) {
    if (!input.previousBrief || typeof input.previousBrief !== "object") {
      return "previousBrief is required for refinement";
    }

    if (!isRefinementTarget(input.refinementTarget)) {
      return "refinementTarget must be businessCase, technical, executive, stakeholders, gameplan, or objections";
    }

    const hasStructuredFeedback =
      Array.isArray(input.feedbackDetails) &&
      input.feedbackDetails.some(
        (item) =>
          typeof item?.instruction === "string" &&
          Boolean(item.instruction.trim())
      );
    const hasLegacyFeedback =
      Array.isArray(input.feedback) &&
      input.feedback.some(
        (item) => typeof item === "string" && Boolean(item.trim())
      );
    if (
      !hasStructuredFeedback &&
      !hasLegacyFeedback &&
      !input.feedbackNotes?.trim()
    ) {
      return "refinement feedback is required";
    }
  }

  return null;
}
