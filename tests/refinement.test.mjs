import assert from "node:assert/strict";
import test from "node:test";

import {
  cloneRefinementDrafts,
  createRefinementDrafts,
  normalizeRefinementDrafts,
  refinementDraftChanged,
  refinementTargets,
  toggleRefinementFeedback,
} from "../frontend/src/lib/refinement.ts";

test("each tab keeps an independent feedback draft", () => {
  const drafts = createRefinementDrafts();
  const next = {
    ...drafts,
    businessCase: {
      feedback: ["Cost and value: Add cost and value framing"],
      feedbackNotes: "Tie outcomes to the sponsor decision.",
    },
    technical: {
      feedback: ["Technical depth: Ask deeper architecture questions"],
      feedbackNotes: "Include RTO and RPO evidence.",
    },
  };

  assert.deepEqual(next.businessCase.feedback, ["Cost and value: Add cost and value framing"]);
  assert.deepEqual(next.technical.feedback, ["Technical depth: Ask deeper architecture questions"]);
  for (const target of refinementTargets.slice(2)) {
    assert.deepEqual(next[target], { feedback: [], feedbackNotes: "" }, target);
  }
});

test("legacy feedback migrates only into its selected tab", () => {
  const restored = normalizeRefinementDrafts(
    undefined,
    "executive",
    ["Executive lens: Sharpen the executive framing"],
    "Keep the language free of AWS jargon.",
  );

  assert.deepEqual(restored.executive.feedback, ["Executive lens: Sharpen the executive framing"]);
  assert.equal(restored.executive.feedbackNotes, "Keep the language free of AWS jargon.");
  for (const target of refinementTargets.filter((item) => item !== "executive")) {
    assert.deepEqual(restored[target], { feedback: [], feedbackNotes: "" }, target);
  }
});

test("draft comparisons and clones remain tab scoped", () => {
  const applied = createRefinementDrafts();
  const drafts = cloneRefinementDrafts(applied);
  drafts.stakeholders.feedback.push("Customer context: Add stakeholder priorities");

  assert.equal(refinementDraftChanged(drafts.stakeholders, applied.stakeholders), true);
  assert.equal(refinementDraftChanged(drafts.gameplan, applied.gameplan), false);
  assert.deepEqual(applied.stakeholders.feedback, []);
});

test("customer-context feedback supports multiple simultaneous selections", () => {
  const first = toggleRefinementFeedback(
    createRefinementDrafts(),
    "businessCase",
    "Customer context: Customer is already on AWS",
  );
  const second = toggleRefinementFeedback(
    first,
    "businessCase",
    "Customer context: Customer has executive urgency",
  );

  assert.deepEqual(second.businessCase.feedback, [
    "Customer context: Customer is already on AWS",
    "Customer context: Customer has executive urgency",
  ]);
  assert.deepEqual(second.technical.feedback, []);

  const third = toggleRefinementFeedback(
    second,
    "businessCase",
    "Customer context: Customer is already on AWS",
  );
  assert.deepEqual(third.businessCase.feedback, [
    "Customer context: Customer has executive urgency",
  ]);
});