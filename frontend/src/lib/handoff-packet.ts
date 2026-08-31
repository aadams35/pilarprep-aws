import type { BriefClaim, BriefResponse, EvidenceCoverage } from "./types";

export type HandoffScope = {
  company: string;
  clientId: string;
  projectId: string;
  packetVersion: number;
  audienceRole: string;
  focus: string;
};

function coverageFor(claims: BriefClaim[]): EvidenceCoverage | undefined {
  if (!claims.length) return undefined;
  const statusCounts: EvidenceCoverage["statusCounts"] = {};
  for (const claim of claims) {
    statusCounts[claim.evidenceStatus] = (statusCounts[claim.evidenceStatus] ?? 0) + 1;
  }
  const linked = claims.filter((claim) => claim.sourceIds.length).length;
  return {
    materialClaims: claims.length,
    claimsWithApprovedSources: linked,
    coveragePercent: Math.round(linked / claims.length * 100),
    statusCounts,
    meaning: "Coverage measures approved source linkage, not probability of truth.",
  };
}

export function handoffAnswerFor(packet: BriefResponse | null, scope: HandoffScope): string {
  const metadata = packet?.metadata;
  if (
    !packet || !metadata ||
    metadata.handoffCompany !== scope.company ||
    metadata.clientId !== scope.clientId ||
    metadata.projectId !== scope.projectId ||
    metadata.approvedPacketVersion !== scope.packetVersion ||
    metadata.packetVersion !== scope.packetVersion ||
    metadata.handoffAudienceRole !== scope.audienceRole ||
    metadata.handoffFocus !== scope.focus ||
    metadata.precallHandoffStatus !== "ready"
  ) return "";
  return packet.projectAnswer;
}

export function mergeHandoffPacket(
  brief: BriefResponse,
  handoff: BriefResponse,
  scope: HandoffScope
): BriefResponse {
  const metadata = handoff.metadata;
  if (
    !handoff.projectAnswer.trim() ||
    (metadata?.clientId && metadata.clientId !== scope.clientId) ||
    (metadata?.projectId && metadata.projectId !== scope.projectId) ||
    (metadata?.approvedPacketVersion !== undefined && metadata.approvedPacketVersion !== scope.packetVersion) ||
    (metadata?.packetVersion !== undefined && metadata.packetVersion !== scope.packetVersion)
  ) throw new Error("The handoff did not match the approved packet. Your current brief was preserved.");

  // A handoff can replace its own answer, never the approved brief or its evidence.
  const sourceCatalog = [...(brief.sourceCatalog ?? [])];
  const sourceIds = new Set(sourceCatalog.map((source) => source.sourceId));
  for (const source of handoff.sourceCatalog ?? []) {
    if (!sourceIds.has(source.sourceId)) {
      sourceCatalog.push(source);
      sourceIds.add(source.sourceId);
    }
  }
  let briefClaims = (brief.claims ?? []).filter((claim) => claim.section !== "projectAnswer");
  const matchingServerBrief = metadata?.clientId === scope.clientId &&
    metadata.projectId === scope.projectId && metadata.approvedPacketVersion === scope.packetVersion &&
    metadata.packetVersion === scope.packetVersion &&
    (["businessCase", "technical", "executive", "stakeholders", "gameplan", "objections"] as const)
      .every((section) => JSON.stringify(brief[section]) === JSON.stringify(handoff[section]));
  // Older handoffs lost their assessments. Restore only evidence for identical, scoped server content.
  if (!briefClaims.length && matchingServerBrief) {
    briefClaims = (handoff.claims ?? []).filter((claim) => claim.section !== "projectAnswer" &&
      claim.sourceIds.every((id) => {
        const stored = sourceCatalog.find((source) => source.sourceId === id);
        const returned = handoff.sourceCatalog?.find((source) => source.sourceId === id);
        return stored && returned && JSON.stringify(stored) === JSON.stringify(returned);
      })
    );
  }
  const claims = [
    ...briefClaims,
    ...(handoff.claims ?? []).filter((claim) =>
      claim.section === "projectAnswer" && claim.itemIndex === 0 &&
      claim.text === handoff.projectAnswer && claim.sourceIds.every((id) => sourceIds.has(id))
    ),
  ];
  return {
    ...brief,
    provider: handoff.provider,
    generatedAt: handoff.generatedAt,
    projectAnswer: handoff.projectAnswer,
    projectArtifacts: handoff.projectArtifacts,
    sourceCatalog,
    claims,
    evidenceCoverage: coverageFor(claims),
    citations: [...new Set([...brief.citations, ...handoff.citations])],
    evidence: [
      ...(brief.evidence ?? []).filter((item) => item.section !== "projectAnswer"),
      ...(handoff.evidence ?? []).filter((item) => item.section === "projectAnswer"),
    ],
    metadata: {
      ...brief.metadata,
      ...metadata,
      clientId: scope.clientId,
      projectId: scope.projectId,
      packetVersion: scope.packetVersion,
      approvedPacketVersion: scope.packetVersion,
      handoffAudienceRole: scope.audienceRole,
      handoffCompany: scope.company,
      handoffFocus: scope.focus,
      precallHandoffStatus: "ready",
      precallHandoffSourceVersion: scope.packetVersion,
    },
  };
}
