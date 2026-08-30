export type BriefMode = "prebrief" | "project";
export type ModelPreference =
  | "default"
  | "nova-pro"
  | "nova-micro"
  | "claude-sonnet-4.6";

export type PersonRoleType = "decision-maker" | "stakeholder";
export type PersonInfluence = "high" | "medium" | "low";
export type PersonStance = "champion" | "supportive" | "neutral" | "skeptical" | "blocker";

export type DecisionMakerContext = {
  name: string;
  title: string;
  source?: string;
  context: string;
  roleType?: PersonRoleType;
  organizationalRole?: string;
  influence?: PersonInfluence;
  stance?: PersonStance;
  decisionAuthority?: string;
  priorities?: string;
  concerns?: string;
  successMeasures?: string;
  engagementGuidance?: string;
  approvedNotes?: string;
};

export type PillarRankingItem = {
  rank: number;
  pillar: string;
};

export type BriefEvidenceSection =
  | "businessCase"
  | "technical"
  | "executive"
  | "stakeholders"
  | "gameplan"
  | "objections"
  | "projectAnswer";

export type RefinementTarget = Exclude<BriefEvidenceSection, "projectAnswer">;

export type BriefEvidence = {
  section: BriefEvidenceSection;
  itemIndex: number;
  sources: string[];
};

export type EvidenceStatus =
  | "supported"
  | "partially-supported"
  | "customer-provided"
  | "assumption"
  | "conflicting-evidence"
  | "needs-validation";

export type EvidenceSourceRecord = {
  sourceId: string;
  tenantId?: string;
  clientId?: string;
  projectId?: string;
  label: string;
  sourceType: string;
  title: string;
  sourceLocation: string;
  capturedAt: string;
  freshness: string;
  approvedBy: string;
  evidenceSnippet: string;
  accessScope: string;
  lifecycleStatus: string;
};

export type BriefClaim = {
  claimId: string;
  section: BriefEvidenceSection;
  itemIndex: number;
  text: string;
  sourceIds: string[];
  evidenceStatus: EvidenceStatus;
  evidenceSnippet: string;
  validationStatus: string;
};

export type EvidenceCoverage = {
  materialClaims: number;
  claimsWithApprovedSources: number;
  coveragePercent: number;
  statusCounts: Partial<Record<EvidenceStatus, number>>;
  meaning: string;
};
export type BusinessCase = {
  scenario: string;
  whyNow: string;
  currentSituation: string;
  desiredOutcomes: string;
  successCriteria: string;
  businessRisks: string;
  decisionRequired: string;
  inScope: string;
  outOfScope: string;
  assumptionsAndUnknowns: string;
  stakeholderAlignment: string;
  alignmentStatement: string;
  nextStepGuidance: string;
};


export type ApprovedBriefSnapshot = {
  businessCase: BusinessCase;
  technical: string[];
  executive: string[];
  stakeholders: string[];
  gameplan: string[];
  objections: string[];
  citations?: string[];
  evidence?: BriefEvidence[];
  sourceCatalog?: EvidenceSourceRecord[];
  claims?: BriefClaim[];
  evidenceCoverage?: EvidenceCoverage;
  projectAnswer?: string;
  projectArtifacts?: ProjectArtifacts;
};

export type RefinementInstruction = {
  category: string;
  instruction: string;
};

export type BriefRequest = {
  mode?: BriefMode;
  modelPreference?: ModelPreference;
  qualityTier?: "fast" | "standard" | "premium";
  company: string;
  industry: string;
  meetingType: string;
  companySize: string;
  pillars: string[];
  pillarRanking?: PillarRankingItem[];
  context: string;
  companyValues?: string;
  companyValuesUrl?: string;
  additionalDirection?: string;
  decisionMakers?: DecisionMakerContext[];
  meetingNotes?: string;
  feedback?: string[];
  feedbackDetails?: RefinementInstruction[];
  feedbackNotes?: string;
  baseBriefVersion?: number;
  refinementTarget?: RefinementTarget;
  previousBrief?: ApprovedBriefSnapshot;
  role?: string;
  prompt?: string;
  approvedBrief?: ApprovedBriefSnapshot;
  asyncGeneration?: boolean;
};

export type BriefJobRequest = {
  operation: "getBriefJob";
  jobId: string;
  projectId: string;
};

export type BriefJobAccepted = {
  jobId: string;
  projectId: string;
  status: "queued" | "running";
  pollAfterMs: number;
};

export type AgentAction = "create_handoff" | "generate_catchup";

export type AgentRequest = {
  action: AgentAction;
  clientId: string;
  projectId: string;
  sessionId: string;
  audienceRole: string;
  focus: string;
  meetingNotes: string;
  modelPreference: Exclude<ModelPreference, "default">;
  confirmWrite: boolean;
  idempotencyKey: string;
  approvedBrief?: ApprovedBriefSnapshot;
  briefRequest: BriefRequest;
};
export type AgentJobRequest = {
  operation: "getAgentJob";
  jobId: string;
  clientId: string;
  projectId: string;
  sessionId: string;
};

export type AgentJobAccepted = {
  jobId: string;
  clientId: string;
  projectId: string;
  status: "queued" | "running";
  pollAfterMs: number;
};

export type PipelineJobAction =
  | "brief.generate"
  | "brief.refine"
  | "brief.approve"
  | "handoff.generate"
  | "catchup.generate"
  | "meeting.process"
  | "meeting.approve"
  | "evidence.ingest"
  | "evidence.delete"
  | "evidence.reindex";

export type PipelineJobState =
  | "queued"
  | "running"
  | "validating"
  | "saving"
  | "waiting_for_scan"
  | "transcribing"
  | "screening"
  | "analyzing"
  | "review-ready"
  | "approved"
  | "complete"
  | "failed";

export type PipelineJobRequest = {
  action: PipelineJobAction;
  clientId: string;
  projectId: string;
  sessionId: string;
  idempotencyKey: string;
  input: Record<string, unknown>;
};

export type PipelineJobAccepted = {
  jobId: string;
  clientId: string;
  projectId: string;
  status: "queued" | "running";
  pollAfterMs: number;
  idempotent?: boolean;
};

export type PipelineJobStatus<TResult = BriefResponse> = {
  jobId: string;
  clientId: string;
  projectId: string;
  action: PipelineJobAction;
  status: PipelineJobState;
  phase?: PipelineJobState;
  retryCount: number;
  traceId?: string;
  pollAfterMs?: number;
  result?: TResult;
  error?: string;

};
export type MeetingTranscriptSegment = {
  id: string;
  speakerLabel: string;
  speaker: string;
  timestampStart: number;
  timestampEnd: number;
  text: string;
};

export type MeetingEvidenceItem = {
  id: string;
  statement: string;
  status: string;
  speaker: string;
  timestampStart: number;
  timestampEnd: number;
  evidenceText: string;
  confidence: number;
  sourceType: string;
  owner?: string;
  targetDate?: string;
  dependency?: string;
  previousAssumption?: string;
  meetingCorrection?: string;
  affectedBriefSections?: string[];
};

export type MeetingAnalysis = {
  meetingSummary: string;
  confirmedFacts: MeetingEvidenceItem[];
  correctedAssumptions: MeetingEvidenceItem[];
  decisions: MeetingEvidenceItem[];
  openQuestions: MeetingEvidenceItem[];
  requirements: MeetingEvidenceItem[];
  risks: MeetingEvidenceItem[];
  scopeChanges: MeetingEvidenceItem[];
  actions: MeetingEvidenceItem[];
  stakeholderSignals: MeetingEvidenceItem[];
  proposedHandoffSummary: string;
  citations: string[];
};

export type MeetingReviewItem = {
  id: string;
  category: string;
  originalContent: string;
  proposedUpdate: string;
  speaker: string;
  timestampStart: number;
  timestampEnd: number;
  evidenceText: string;
  confidence: number;
  supportStatus: string;
  required: boolean;
  owner?: string;
};

export type MeetingProcessResult = {
  provider: "agentcore-strands";
  action: "meeting.process";
  status: "review-ready";
  scenarioId: "blue-mesa-payments";
  meetingId: string;
  proposalId: string;
  baseBriefVersion: number;
  transcript: {
    segments: MeetingTranscriptSegment[];
    durationSeconds: number;
    speakerCount: number;
    text: string;
  };
  analysis: MeetingAnalysis;
  reviewItems: MeetingReviewItem[];
  citations: string[];
  metadata: Record<string, unknown>;
};

export type MeetingReviewDecision = {
  id: string;
  decision: "accepted" | "edited" | "rejected";
  editedStatement?: string;
};

export type AuthorizedClientSummary = {
  clientId: string;
  projectId: string;
  company: string;
  latestApprovedAt?: string;
  latestHandoffAt?: string;
  approvedPacketVersion?: number;
  hasApprovedBrief: boolean;
  hasHandoff: boolean;
};

export type EvidenceDocumentRecord = {
  documentId: string;
  fileName: string;
  sourceTitle: string;
  documentType: string;
  source?: string;
  approvalStatus: "approved";
  status:
    | "STORED"
    | "INGESTION_PENDING"
    | "INGESTING"
    | "AVAILABLE"
    | "DELETION_PENDING"
    | "DELETING"
    | "DELETION_FAILED"
    | "INGESTION_FAILED";
  version: number;
  checksumSha256?: string;
  createdAt?: string;
  updatedAt?: string;
  approvedAt?: string;
  ingestionJobId?: string;
  ingestionStatus?: string;
  failureReasons?: string[];
  sourceId?: string;
  sourceType?: string;
  sourceLocation?: string;
  capturedAt?: string;
  freshness?: string;
  approvedBy?: string;
  accessScope?: string;
  lifecycleStatus?: string;
};

export type ProjectArtifactItem = {
  title: string;
  detail: string;
  owner?: string;
  status?: string;
};

export type FollowUpEmailArtifact = {
  subject: string;
  body: string;
};

export type NextStepAction = {
  action: string;
  owner: string;
  timing: string;
  dependency: string;
  decisionGate: string;
};

export type ProjectNextSteps = {
  immediateActions: NextStepAction[];
  openQuestions: string[];
  nextMeeting: {
    purpose: string;
    timing: string;
    attendees: string[];
  };
  customerSummary: string;
  internalNotes: string;
};

export type ProjectArtifacts = {
  twoWeekPlan: ProjectArtifactItem[];
  riskRegister: ProjectArtifactItem[];
  stakeholderMap: ProjectArtifactItem[];
  followUpEmail: FollowUpEmailArtifact;
  nextSteps: ProjectNextSteps;
};

export type BriefResponse = {
  provider: "demo" | "bedrock" | "strands" | "agentcore";
  generatedAt: string;
  metadata?: {
    projectId?: string;
    clientId?: string;
    artifactKey?: string;
    docxArtifactKey?: string;
    docxDownloadUrl?: string;
    artifactRetention?: string;
    stateKey?: string;
    storageWarning?: string;
    guardrailId?: string;
    guardrailVersion?: string;
    modelId?: string;
    inputTokens?: number;
    outputTokens?: number;
    totalTokens?: number;
    tokenUsageSource?: "reported" | "estimated";
    estimatedModelCostUsd?: number;
    latencyMs?: number;
    agentSessionId?: string;
    agentTraceId?: string;
    agentMode?: string;
    memoryUsed?: boolean;
    gatewayUsed?: boolean;
    fallbackUsed?: boolean;
    fallbackReason?: string;
    modelStopReason?: string;
    meetingApprovalId?: string;
    meetingProposalId?: string;
    meetingId?: string;
    meetingApprovalStatus?: "approved";
    meetingApprovedAt?: string;
    meetingAcceptedChangeCount?: number;
    meetingRejectedChangeCount?: number;
    meetingApprovalArtifactKey?: string;
    performanceLatency?: "standard" | "optimized";
    projectVersion?: number;
    baseBriefVersion?: number;
    packetVersion?: number;
    refinementTarget?: RefinementTarget;
    refinementSections?: string[];
    refinementInstructionCount?: number;
    changedSectionIds?: RefinementTarget[];
    unauthorizedSectionChanges?: number;
    refinementIsolationPassed?: boolean;
    refinementChangedPassages?: number;
    changedPassageIds?: string[];
    refinementMinimumChangedPassages?: number;
    refinementCoveragePassed?: boolean;
    refinementLatencyMs?: number;
    appliedFeedback?: RefinementInstruction[];
    supersededFacts?: string[];
    contradictionValidationPassed?: boolean;
    contradictionFindings?: string[];
    generationAttempts?: number;
    retryReason?: string;
    toolCalls?: string[];
    rag?: {
      enabled: boolean;
      mode: string;
      resultCount: number;
      maxResults?: number;
    };
    approvalStatus?: "draft" | "stale" | "approved";
    precallHandoffJobId?: string;
    precallHandoffStatus?: "idle" | "queued" | "preparing" | "ready" | "failed" | "stale";
    precallHandoffSourceVersion?: number;
    precallHandoffError?: string;
    approvedAt?: string;
    approvedPacketVersion?: number;
  };
  businessCase: BusinessCase;
  technical: string[];
  executive: string[];
  stakeholders: string[];
  gameplan: string[];
  objections: string[];
  projectAnswer: string;
  projectArtifacts?: ProjectArtifacts;
  citations: string[];
  evidence?: BriefEvidence[];
  sourceCatalog?: EvidenceSourceRecord[];
  claims?: BriefClaim[];
  evidenceCoverage?: EvidenceCoverage;
};
