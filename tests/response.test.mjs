import assert from "node:assert/strict";
import test from "node:test";

import { generateBlueMesaBackupBrief } from "../frontend/src/lib/generator.ts";
import { normalizeBriefResponse } from "../frontend/src/lib/response.ts";

test("response normalization preserves refinement provenance", () => {
  const brief = generateBlueMesaBackupBrief();
  const normalized = normalizeBriefResponse(
    {
      ...brief,
      metadata: {
        ...brief.metadata,
        baseBriefVersion: 7,
        packetVersion: 8,
        refinementTarget: "technical",
        refinementSections: ["technical"],
        refinementInstructionCount: 2,
        changedSectionIds: ["technical"],
        unauthorizedSectionChanges: 0,
        refinementIsolationPassed: true,
        refinementChangedPassages: 4,
        refinementMinimumChangedPassages: 2,
        refinementCoveragePassed: true,
        refinementLatencyMs: 4321,
      },
    },
    "bedrock",
  );

  assert.equal(normalized.metadata?.baseBriefVersion, 7);
  assert.equal(normalized.metadata?.packetVersion, 8);
  assert.equal(normalized.metadata?.refinementTarget, "technical");
  assert.deepEqual(normalized.metadata?.refinementSections, ["technical"]);
  assert.equal(normalized.metadata?.refinementInstructionCount, 2);
  assert.deepEqual(normalized.metadata?.changedSectionIds, ["technical"]);
  assert.equal(normalized.metadata?.unauthorizedSectionChanges, 0);
  assert.equal(normalized.metadata?.refinementIsolationPassed, true);
  assert.equal(normalized.metadata?.refinementChangedPassages, 4);
  assert.equal(normalized.metadata?.refinementMinimumChangedPassages, 2);
  assert.equal(normalized.metadata?.refinementCoveragePassed, true);
  assert.equal(normalized.metadata?.refinementLatencyMs, 4321);
});

test("response normalization keeps only claims linked to real catalog sources", () => {
  const brief = generateBlueMesaBackupBrief();
  const validSource = brief.sourceCatalog[0];
  const normalized = normalizeBriefResponse(
    {
      ...brief,
      claims: [
        {
          ...brief.claims[0],
          sourceIds: [validSource.sourceId, "src-invented"],
        },
      ],
    },
    "bedrock",
  );

  assert.deepEqual(normalized.claims[0].sourceIds, [validSource.sourceId]);
  assert.ok(normalized.sourceCatalog.some((source) => source.sourceId === validSource.sourceId));
  assert.equal(
    normalized.evidenceCoverage?.meaning,
    "Percentage of material claims linked to approved sources; not a probability of truth.",
  );
});

test("legacy packets migrate without fabricated provenance", () => {
  const normalized = normalizeBriefResponse(
    {
      businessCase: { scenario: "Legacy scenario" },
      technical: ["Legacy technical brief"],
      executive: ["Legacy executive brief"],
    },
    "demo",
  );

  assert.deepEqual(normalized.sourceCatalog, []);
  assert.deepEqual(normalized.claims, []);
  assert.equal(normalized.evidenceCoverage, undefined);
});
