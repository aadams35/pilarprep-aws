import { expect, test } from "@playwright/test";

const businessCase = Object.fromEntries(
  [
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
  ].map((key) => [key, `BlueMesa approved ${key} for the AWS payments resilience briefing.`])
);

const approvedBrief = {
  provider: "bedrock",
  generatedAt: "2026-08-20T12:00:00.000Z",
  businessCase,
  technical: ["Validate the current platform and recovery evidence."],
  executive: ["Protect merchant trust while accelerating delivery."],
  stakeholders: ["Dev owns infrastructure and resilience evidence."],
  gameplan: ["Confirm current state, owners, and the next decision gate."],
  objections: ["Concern: disruption. Response: use a bounded validation path."],
  projectAnswer: "The approved brief is ready for meeting follow-through.",
  projectArtifacts: {
    twoWeekPlan: [],
    riskRegister: [],
    stakeholderMap: [],
    followUpEmail: { subject: "", body: "" },
    nextSteps: {
      immediateActions: [],
      openQuestions: [],
      nextMeeting: { purpose: "", timing: "", attendees: [] },
      customerSummary: "",
      internalNotes: "",
    },
  },
  citations: ["Approved BlueMesa context"],
  evidence: [],
  metadata: {
    clientId: "bluemesa-payments",
    projectId: "bluemesa-payments",
    packetVersion: 4,
    approvedPacketVersion: 4,
    approvalStatus: "approved",
    modelId: "us.amazon.nova-pro-v1:0",
    fallbackUsed: false,
  },
};

const meetingResult = {
  provider: "agentcore-strands",
  action: "meeting.process",
  status: "review-ready",
  scenarioId: "blue-mesa-payments",
  meetingId: "blue-mesa-discovery",
  proposalId: "proposal-0001",
  baseBriefVersion: 4,
  transcript: {
    durationSeconds: 82,
    speakerCount: 2,
    text: "Blue Mesa is already on AWS. Payroll integration is in scope.",
    segments: [
      {
        id: "segment-1",
        speakerLabel: "spk_2",
        speaker: "Dev Malik, VP Infrastructure and Resilience",
        timestampStart: 12,
        timestampEnd: 24,
        text: "Blue Mesa is already on AWS. This is not an on-premises migration.",
      },
      {
        id: "segment-2",
        speakerLabel: "spk_3",
        speaker: "Rachel Kim, Chief Risk and Compliance Officer",
        timestampStart: 64,
        timestampEnd: 82,
        text: "Payroll data needs least privilege, log redaction, and attributable access evidence.",
      },
    ],
  },
  analysis: {
    meetingSummary: "Blue Mesa confirmed its existing AWS estate and made payroll integration the bounded first-release objective.",
    confirmedFacts: [
      {
        id: "confirmed-one",
        statement: "BlueMesa already runs the payment platform on AWS.",
        status: "confirmed",
        speaker: "Dev Malik",
        timestampStart: 12,
        timestampEnd: 24,
        evidenceText: "Blue Mesa is already on AWS.",
        confidence: 0.99,
        sourceType: "meeting transcript",
      },
    ],
    correctedAssumptions: [
      {
        id: "corrected-one",
        statement: "One design partner is batch-only rather than API-first.",
        status: "corrected",
        speaker: "Dev Malik",
        timestampStart: 30,
        timestampEnd: 42,
        evidenceText: "One partner is batch only for the pilot.",
        confidence: 0.98,
        sourceType: "meeting transcript",
        previousAssumption: "Both partners prefer API-first integration.",
        meetingCorrection: "One partner is batch-only.",
        affectedBriefSections: ["businessCase", "technical"],
      },
    ],
    decisions: [],
    openQuestions: [
      {
        id: "open-one",
        statement: "The payroll retention schedule is not yet approved.",
        status: "unresolved",
        speaker: "Rachel Kim",
        timestampStart: 64,
        timestampEnd: 82,
        evidenceText: "We have not approved a payroll retention schedule.",
        confidence: 0.97,
        sourceType: "meeting transcript",
      },
    ],
    requirements: [],
    risks: [],
    scopeChanges: [],
    actions: [],
    stakeholderSignals: [],
    proposedHandoffSummary: "Build the payroll integration handoff from reviewed evidence.",
    citations: ["Current AWS environment", "Synthetic meeting transcript"],
  },
  reviewItems: [
    {
      id: "change-one",
      category: "Corrected assumption",
      originalContent: "The customer is preparing an initial AWS migration.",
      proposedUpdate: "Blue Mesa already operates the payment workload family on AWS.",
      speaker: "Dev Malik",
      timestampStart: 12,
      timestampEnd: 24,
      evidenceText: "Blue Mesa is already on AWS. This is not an on-premises migration.",
      confidence: 0.99,
      supportStatus: "corrected",
      required: true,
    },
    {
      id: "change-two",
      category: "Requirement",
      originalContent: "Payroll integration was not yet in scope.",
      proposedUpdate: "Payroll integration must use least privilege, redacted logs, and attributable access evidence.",
      speaker: "Rachel Kim",
      timestampStart: 64,
      timestampEnd: 82,
      evidenceText: "Payroll data needs least privilege, log redaction, and attributable access evidence.",
      confidence: 0.96,
      supportStatus: "new",
      required: true,
    },
  ],
  citations: ["Current AWS environment", "Synthetic meeting transcript"],
  metadata: { writesApplied: false, syntheticDemo: true },
};

for (const responseMode of ["normal", "throttled", "legacy-edge", "blocked"] as const) {
test(`meeting intelligence handles ${responseMode} requests without losing review`, async ({ page }) => {
  if (responseMode === "legacy-edge") await page.clock.install();
  await page.addInitScript((brief) => {
    sessionStorage.setItem(
      "pillarprep.auth.session.v1",
      JSON.stringify({
        idToken: "test-id-token",
        accessToken: "test-access-token",
        expiresAt: Date.now() + 3_600_000,
        subject: "workspace-user-1",
        email: "owner@example.com",
        name: "Demo Owner",
      })
    );
    localStorage.setItem(
      "pillarprep.workspace.v2",
      JSON.stringify({
        scenarioId: "bluemesa",
        company: "BlueMesa Payments",
        industry: "Financial Services",
        meetingType: "Executive Briefing",
        companySize: "Enterprise",
        selectedPillars: ["Security", "Reliability", "Operational Excellence"],
        context: "BlueMesa payments modernization context.",
        companyValues: "Merchant trust and controlled change.",
        companyValuesUrl: "https://www.bluemesa-payments.example/company/values",
        additionalDirection: "Make settlement and reconciliation dependencies visible.",
        decisionMakers: [],
        meetingNotes: "",
        activeTab: "businessCase",
        briefVersion: 4,
        approved: true,
        approvalStale: false,
        promoted: true,
        generatedBrief: brief,
        briefHistory: [],
        role: "Solutions Architect",
        activePrompt: "What changed since the last meeting?",
      })
    );
  }, approvedBrief);

  await page.route("https://cognito-identity.us-east-1.amazonaws.com/**", async (route) => {
    const target = route.request().headers()["x-amz-target"] ?? "";
    await route.fulfill({
      status: 200,
      contentType: "application/x-amz-json-1.1",
      body: JSON.stringify(
        target.endsWith("GetId")
          ? { IdentityId: "us-east-1:test-identity" }
          : {
              IdentityId: "us-east-1:test-identity",
              Credentials: {
                AccessKeyId: "ASIAPILARPREPTEST",
                SecretKey: "test-secret-key-for-browser-signing-only",
                SessionToken: "test-session-token",
                Expiration: Math.floor(Date.now() / 1000) + 3600,
              },
            }
      ),
    });
  });

  let scanPoll = 0;
  let poll = 0;
  let jobSubmissions = 0;
  await page.route("https://test.execute-api.us-east-1.amazonaws.com/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (
      route.request().method() === "GET" &&
      (path === "/clients" || path === "/workspace/clients")
    ) {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ clients: [] }) });
      return;
    }
    if (
      route.request().method() === "GET" &&
      path === "/workspace/meeting-audio/demo"
    ) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          downloadUrl: "https://meeting-download.test/BlueMesa.mp3",
          fileName: "PilarPrep-BlueMesa-Discovery-Meeting.mp3",
          expiresIn: 900,
        }),
      });
      return;
    }
    if (route.request().method() === "POST" && path === "/private-upload") {
      await route.fulfill({ status: 204, body: "" });
      return;
    }
    if (
      route.request().method() === "POST" &&
      path === "/workspace/meeting-audio/uploads"
    ) {
      expect(route.request().headers().authorization).toBe("Bearer test-id-token");
      expect(route.request().postDataJSON()).toMatchObject({
        consentAcknowledged: true,
      });
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          uploadId: "upload-meeting-0001",
          uploadUrl: "https://test.execute-api.us-east-1.amazonaws.com/private-upload",
          uploadFields: { key: "private/meeting.mp3", policy: "test-policy" },
        }),
      });
      return;
    }
    if (
      route.request().method() === "GET" &&
      path === "/workspace/meeting-audio/uploads/upload-meeting-0001"
    ) {
      scanPoll += 1;
      if (responseMode === "throttled" && scanPoll === 1) {
        await route.fulfill({
          status: 429,
          headers: { "retry-after": "1", "access-control-expose-headers": "Retry-After" },
          contentType: "application/json",
          body: JSON.stringify({ error: "Please wait briefly.", code: "rate_limited", retryAfterSeconds: 1 }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          uploadId: "upload-meeting-0001",
          status: scanPoll === 1 ? "pending_scan" : "clean",
        }),
      });
      return;
    }
    if (route.request().method() === "POST") {
      jobSubmissions += 1;
      expect(route.request().postDataJSON().action).toBe("meeting.process");
      if (responseMode === "blocked") {
        await route.fulfill({
          status: 403,
          contentType: "text/html",
          body: '<!DOCTYPE HTML><HTML><BODY><H1>403 ERROR</H1>Request blocked. Generated by cloudfront</BODY></HTML>',
        });
        return;
      }
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          jobId: "meeting-job-0001",
          clientId: "bluemesa-payments",
          projectId: "bluemesa-payments",
          status: "queued",
          pollAfterMs: 750,
        }),
      });
      return;
    }
    poll += 1;
    if (responseMode === "legacy-edge" && poll === 2) {
      await route.fulfill({
        status: 403,
        contentType: "text/html",
        body: '<!DOCTYPE HTML><HTML><BODY><H1>403 ERROR</H1>Request blocked. Generated by cloudfront</BODY></HTML>',
      });
      return;
    }
    if (responseMode === "throttled" && poll === 2) {
      await route.fulfill({
        status: 429,
        headers: { "retry-after": "2", "access-control-expose-headers": "Retry-After" },
        contentType: "application/json",
        body: JSON.stringify({ error: "Please wait briefly.", code: "rate_limited", retryAfterSeconds: 2 }),
      });
      return;
    }
    const progressPoll = (responseMode === "throttled" || responseMode === "legacy-edge") && poll > 2 ? poll - 1 : poll;
    const status = progressPoll === 1 ? "transcribing" : progressPoll === 2 ? "analyzing" : "review-ready";
    await route.fulfill({
      status: status === "review-ready" ? 200 : 202,
      contentType: "application/json",
      body: JSON.stringify({
        jobId: "meeting-job-0001",
        clientId: "bluemesa-payments",
        projectId: "bluemesa-payments",
        action: "meeting.process",
        status,
        phase: status,
        retryCount: 0,
        pollAfterMs: 750,
        result: status === "review-ready" ? meetingResult : undefined,
      }),
    });
  });

  await page.goto("/");
  await page.getByRole("navigation", { name: "Customer lifecycle" }).getByRole("button", { name: /Meet/ }).click();
  const workspace = page.locator(".meeting-intelligence");
  await expect(workspace).toBeVisible();
  await expect(workspace.getByText("Synthetic demo", { exact: true })).toHaveCount(0);
  await expect(workspace.getByRole("button", { name: "Get demo MP3" })).toHaveCount(0);
  await expect(page.getByText("Synthetic demo data only", { exact: true })).toHaveCount(0);
  await expect(workspace.getByRole("button", { name: "Choose audio" })).toBeDisabled();
  await workspace.getByRole("checkbox").check();
  await expect(workspace.getByRole("button", { name: "Choose audio" })).toBeEnabled();
  await expect(workspace.getByText("Full transcript", { exact: true })).toBeVisible();
  await expect(workspace.getByText("Content safety", { exact: true })).toBeVisible();

  await workspace.locator('input[type="file"]').setInputFiles({
    name: "PilarPrep-Blue-Mesa-Discovery-Meeting.mp3",
    mimeType: "audio/mpeg",
    buffer: Buffer.from("synthetic meeting audio"),
  });
  await expect(workspace.getByText("Scanning for malware", { exact: false })).toBeVisible();
  await expect(workspace.getByText("MB · Ready to process", { exact: false })).toBeVisible({ timeout: 10_000 });

  await page.getByRole("button", { name: "Process meeting audio" }).click();
  if (responseMode === "blocked") {
    await expect(workspace.getByText(/Meeting processing failed: The site blocked this request/)).toBeVisible();
    await expect(workspace).toHaveAttribute("aria-busy", "false");
    await expect(page.locator("body")).not.toContainText("<!DOCTYPE");
    await expect(page.locator("body")).not.toContainText("Generated by cloudfront");
    await expect(page.getByRole("button", { name: "Process meeting audio" })).toBeEnabled();
    expect(jobSubmissions).toBe(1);
    expect(poll).toBe(0);
    return;
  }
  await expect(workspace).toHaveAttribute("aria-busy", "true");
  await expect(page.locator(".meeting-processing-state .processing-clock")).toBeVisible();
  await expect.poll(() => workspace.evaluate((element) => getComputedStyle(element).cursor)).not.toBe("wait");
  if (responseMode === "throttled" || responseMode === "legacy-edge") {
    await expect(workspace.getByText("Processing continues. Reconnecting to meeting status...")).toBeVisible();
    await expect(workspace.locator(".meeting-primary-action")).toBeDisabled();
    await expect(page.locator("body")).not.toContainText("<!DOCTYPE");
    if (responseMode === "legacy-edge") await page.clock.fastForward(60_001);
  }

  await expect(page.getByText("Proposed project updates")).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText("Confirmed by the call")).toBeVisible();
  await expect(page.getByText("Corrected by the call")).toBeVisible();
  await expect(page.getByText("Still unresolved")).toBeVisible();
  await expect(page.getByText("One partner is batch-only.")).toBeVisible();
  await expect(page.getByText("Blue Mesa already operates the payment workload family on AWS.")).toBeVisible();
  await expect(page.getByText("Rachel Kim, Chief Risk and Compliance Officer")).toBeVisible();
  expect(jobSubmissions).toBe(1);
  await expect(workspace.getByText("Processing continues. Reconnecting to meeting status...")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Approve Next-Step Handoff" })).toBeDisabled();

  const reviewItems = page.locator(".meeting-review-item");
  await page.getByRole("button", { name: "Accept all reviewed changes" }).click();
  await expect(reviewItems.getByRole("button", { name: "Accept" }).first()).toHaveClass(/is-selected/);
  await expect(reviewItems.getByRole("button", { name: "Accept" }).last()).toHaveClass(/is-selected/);
  await expect(page.getByRole("button", { name: "Approve Next-Step Handoff" })).toBeEnabled();
  await page.screenshot({ path: `test-results/meeting-intelligence-${responseMode}.png`, fullPage: true });
});
}

test("signed-out users can see Step 4 but cannot upload private meeting audio", async ({ page }) => {
  await page.addInitScript((brief) => {
    localStorage.setItem(
      "pillarprep.workspace.v2",
      JSON.stringify({
        scenarioId: "bluemesa",
        company: "BlueMesa Payments",
        industry: "Financial Services",
        meetingType: "Executive Briefing",
        companySize: "Enterprise",
        selectedPillars: ["Security", "Reliability", "Operational Excellence"],
        context: "BlueMesa payments modernization context.",
        companyValues: "Merchant trust and controlled change.",
        companyValuesUrl: "https://www.bluemesa-payments.example/company/values",
        additionalDirection: "Make settlement and reconciliation dependencies visible.",
        decisionMakers: [],
        meetingNotes: "",
        activeTab: "businessCase",
        briefVersion: 4,
        approved: true,
        approvalStale: false,
        promoted: true,
        generatedBrief: brief,
        briefHistory: [],
        role: "Solutions Architect",
        activePrompt: "What changed since the last meeting?",
      })
    );
  }, approvedBrief);

  await page.route("https://cognito-identity.us-east-1.amazonaws.com/**", async (route) => {
    const target = route.request().headers()["x-amz-target"] ?? "";
    await route.fulfill({
      status: 200,
      contentType: "application/x-amz-json-1.1",
      body: JSON.stringify(
        target.endsWith("GetId")
          ? { IdentityId: "us-east-1:test-identity" }
          : {
              IdentityId: "us-east-1:test-identity",
              Credentials: {
                AccessKeyId: "ASIAPILARPREPTEST",
                SecretKey: "test-secret-key-for-browser-signing-only",
                SessionToken: "test-session-token",
                Expiration: Math.floor(Date.now() / 1000) + 3600,
              },
            }
      ),
    });
  });
  await page.route("https://test.execute-api.us-east-1.amazonaws.com/**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ clients: [] }),
    });
  });

  await page.goto("/");
  await page.getByRole("navigation", { name: "Customer lifecycle" }).getByRole("button", { name: /Meet/ }).click();
  const workspace = page.locator(".meeting-intelligence");
  await expect(workspace.getByText("Sign in before uploading meeting audio")).toBeVisible();
  await expect(workspace.getByRole("button", { name: "Sign in" })).toBeEnabled();
  await expect(workspace.getByRole("button", { name: "Get demo MP3" })).toHaveCount(0);
  await expect(page.getByText("Synthetic demo data only", { exact: true })).toHaveCount(0);
  await expect(workspace.getByRole("button", { name: "Choose audio" })).toBeDisabled();
  await expect(workspace.getByRole("checkbox")).toBeDisabled();
});

test("advance keeps AI gate recommendations human-confirmed and the full handoff collapsed", async ({ page }) => {
  await page.addInitScript((brief) => {
    const advanceBrief = {
      ...brief,
      projectArtifacts: {
        twoWeekPlan: [
          {
            title: "Bounded validation",
            detail: "Validate the first implementation path and its exit criteria.",
            owner: "Solutions Architect",
            status: "Ready",
          },
        ],
        riskRegister: [
          {
            title: "Security evidence",
            detail: "The control owner and required evidence must be confirmed.",
            owner: "Security lead",
            status: "Open",
          },
        ],
        stakeholderMap: [
          {
            title: "Executive sponsor",
            detail: "Confirms business alignment and escalation path.",
            owner: "Rachel Kim",
            status: "Active",
          },
        ],
        followUpEmail: { subject: "Next decision", body: "Confirm owners and evidence." },
        nextSteps: {
          immediateActions: [
            {
              action: "Confirm the current-state evidence package.",
              owner: "Account team",
              timing: "Before the next call",
              dependency: "Customer owner",
              decisionGate: "Business alignment",
            },
          ],
          openQuestions: ["Who owns the final security decision?"],
          nextMeeting: {
            purpose: "Validate evidence and make the next decision.",
            timing: "Next week",
            attendees: ["Sales", "Solutions Architect", "Customer sponsor"],
          },
          customerSummary: "The team is aligned on the bounded next decision.",
          internalNotes: "Keep unvalidated assumptions out of customer-facing claims.",
        },
      },
      metadata: {
        ...brief.metadata,
        meetingApprovalStatus: "approved",
        meetingApprovedAt: "2026-08-21T12:00:00.000Z",
      },
    };
    localStorage.setItem(
      "pillarprep.workspace.v2",
      JSON.stringify({
        scenarioId: "bluemesa",
        company: "BlueMesa Payments",
        industry: "Financial Services",
        meetingType: "Executive Briefing",
        companySize: "Enterprise",
        selectedPillars: ["Security", "Reliability", "Operational Excellence"],
        context: "BlueMesa payments modernization context.",
        companyValues: "Merchant trust and controlled change.",
        companyValuesUrl: "https://www.bluemesa-payments.example/company/values",
        additionalDirection: "Keep settlement and reconciliation dependencies visible.",
        decisionMakers: [],
        meetingNotes: "Accepted meeting evidence is ready for the next-step handoff.",
        activeTab: "businessCase",
        briefVersion: 4,
        approved: true,
        approvalStale: false,
        promoted: true,
        generatedBrief: advanceBrief,
        briefHistory: [],
        role: "Solutions Architect",
        activePrompt: "What changed since the last meeting?",
        selectedLifecycleStage: "advance",
        gateDecisions: {
          business: { status: "complete", confirmed: true },
        },
      })
    );
  }, approvedBrief);

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Turn decisions into the next move" })).toBeVisible();

  const gates = page.locator(".opportunity-gate");
  await expect(gates).toHaveCount(7);
  await expect(page.locator(".project-handoff-details")).not.toHaveAttribute("open", "");

  const technicalGate = gates.filter({ hasText: "Technical validation" });
  await technicalGate.getByRole("combobox").selectOption("complete");
  await technicalGate.getByRole("button", { name: "Confirm" }).click();
  await expect(technicalGate).toHaveClass(/opportunity-gate-confirmed/);

  await expect.poll(() =>
    page.evaluate(() => {
      const saved = JSON.parse(localStorage.getItem("pillarprep.workspace.v2") || "{}");
      return saved.gateDecisions?.technical?.confirmed;
    })
  ).toBe(true);

  const gateListOverflow = await page.locator(".opportunity-gate-list").evaluate(
    (element) => getComputedStyle(element).overflowY
  );
  expect(gateListOverflow).toBe("visible");
  await expect(page.getByRole("button", { name: "Prepare next call" })).toBeVisible();
});
