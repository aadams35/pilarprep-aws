import assert from "node:assert/strict";
import test from "node:test";

import {
  evidenceStatusHeading,
  evidenceStatusLabel,
} from "../frontend/src/lib/evidence-status.ts";

test("evidence labels distinguish supported, partial, and unsupported claims", () => {
  assert.equal(evidenceStatusLabel("supported"), "Supported");
  assert.equal(evidenceStatusLabel("customer-provided"), "Supported");
  assert.equal(evidenceStatusLabel("partially-supported"), "Partially supported");
  assert.equal(evidenceStatusLabel("needs-validation"), "Unsupported");
  assert.match(
    evidenceStatusHeading("needs-validation"),
    /No matching approved evidence/,
  );
});
