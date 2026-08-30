import { expect, test, type Page } from "@playwright/test";

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
  ].map((key) => [
    key,
    `Apex Mutual ${key} is grounded in approved customer context, named owners, measurable evidence, and a bounded next decision.`,
  ])
);

const completedBrief = {
  provider: "bedrock",
  generatedAt: "2026-08-13T12:00:00.000Z",
  businessCase,
  technical: Array.from(
    { length: 4 },
    (_, index) =>
      `Technical passage ${index + 1} validates architecture evidence, ownership, constraints, and the next decision. Ask: "Which proof is required?"`
  ),
  executive: Array.from(
    { length: 4 },
    (_, index) =>
      `Executive passage ${index + 1} connects customer value, urgency, measurable outcomes, and sponsor confidence. Ask: "What outcome matters?"`
  ),
  stakeholders: Array.from(
    { length: 4 },
    (_, index) =>
      `Stakeholder passage ${index + 1} confirms influence, evidence, ownership, and approval criteria. Ask: "Who decides?"`
  ),
  gameplan: Array.from(
    { length: 4 },
    (_, index) =>
      `Game plan passage ${index + 1} sequences discovery, evidence review, readback, and the decision gate. Ask: "What happens next?"`
  ),
  objections: Array.from(
    { length: 4 },
    (_, index) =>
      `Concern ${index + 1}: evidence is incomplete. Response: use a bounded validation step with a named owner. Ask: "What proof resolves this?"`
  ),
  projectAnswer:
    "Use the approved packet as the shared handoff, validate assumptions, assign evidence owners, and schedule the next decision checkpoint.",
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
  citations: ["Customer context"],
  evidence: [],
  sourceCatalog: [
    {
      sourceId: "src-customer-context",
      tenantId: "tenant-test",
      clientId: "apex-mutual",
      projectId: "apex-mutual",
      label: "Customer context",
      sourceType: "customer-provided-context",
      title: "Customer context",
      sourceLocation: "protected-workspace-record",
      capturedAt: "2026-08-13T11:55:00.000Z",
      freshness: "current-request",
      approvedBy: "request-author",
      evidenceSnippet: "Apex Mutual approved a bounded modernization discovery focused on customer trust and audit evidence.",
      accessScope: "tenant-private",
      lifecycleStatus: "active",
    },
  ],
  claims: [
    {
      claimId: "claim-business-scenario",
      section: "businessCase",
      itemIndex: 0,
      text: businessCase.scenario,
      sourceIds: ["src-customer-context"],
      evidenceStatus: "customer-provided",
      evidenceSnippet: "Apex Mutual approved a bounded modernization discovery focused on customer trust and audit evidence.",
      validationStatus: "valid-source-reference",
    },
  ],
  evidenceCoverage: {
    materialClaims: 1,
    claimsWithApprovedSources: 1,
    coveragePercent: 100,
    statusCounts: { "customer-provided": 1 },
    meaning: "Percentage of material claims linked to approved sources; not a probability of truth.",
  },
  metadata: {
    projectId: "apex-mutual",
    clientId: "apex-mutual",
    packetVersion: 1,
    modelId: "us.amazon.nova-pro-v1:0",
    modelTier: "nova-pro",
    requestedModelTier: "nova-pro",
    fallbackUsed: false,
    latencyMs: 1200,
    estimatedCostUsd: 0.01,
    artifactRetention: "latest-only",
  },
};

async function mockCognito(page: Page) {
  await page.route("https://cognito-identity.us-east-1.amazonaws.com/**", async (route) => {
    const target = route.request().headers()["x-amz-target"] ?? "";
    const body = target.endsWith("GetId")
      ? { IdentityId: "us-east-1:test-identity" }
      : {
          IdentityId: "us-east-1:test-identity",
          Credentials: {
            AccessKeyId: "ASIAPILARPREPTEST",
            SecretKey: "test-secret-key-for-browser-signing-only",
            SessionToken: "test-session-token",
            Expiration: Math.floor(Date.now() / 1000) + 3600,
          },
        };
    await route.fulfill({
      status: 200,
      contentType: "application/x-amz-json-1.1",
      body: JSON.stringify(body),
    });
  });
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const entries: number[] = [];
    Object.defineProperty(window, "__pilarprepLongTasks", {
      value: entries,
      configurable: true,
    });
    if ("PerformanceObserver" in window) {
      try {
        const observer = new PerformanceObserver((list) => {
          for (const entry of list.getEntries()) entries.push(entry.duration);
        });
        observer.observe({ type: "longtask", buffered: true });
      } catch {
        // Long-task entries are optional browser diagnostics.
      }
    }
  });
  await mockCognito(page);
});

test("stale Blue Mesa demo direction is migrated before generation", async ({ page }) => {
  const legacyDirection =
    "Treat BlueMesa as an existing AWS customer. Make payroll integration, mixed API and encrypted-file interfaces, idempotency, reconciliation, data privacy, retention, partner certification, cutover, and recovery evidence explicit. The existing ledger replacement is out of scope.";
  let submittedDirection = "";

  await page.addInitScript(
    ({ storageKey, direction }) => {
      window.localStorage.setItem(
        storageKey,
        JSON.stringify({
          scenarioId: "bluemesa",
          additionalDirection: direction,
        })
      );
    },
    {
      storageKey: "pillarprep.workspace.v2",
      direction: legacyDirection,
    }
  );
  await page.route("https://test.execute-api.us-east-1.amazonaws.com/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "GET" && path === "/clients") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ clients: [] }),
      });
      return;
    }
    if (route.request().method() === "POST") {
      const request = route.request().postDataJSON() as {
        input?: { additionalDirection?: string };
      };
      submittedDirection = request.input?.additionalDirection ?? "";
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          jobId: "job-migrated-demo",
          clientId: "bluemesa-payments",
          projectId: "bluemesa-payments",
          status: "queued",
          pollAfterMs: 10,
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        jobId: "job-migrated-demo",
        clientId: "bluemesa-payments",
        projectId: "bluemesa-payments",
        status: "complete",
        result: {
          ...completedBrief,
          metadata: {
            ...completedBrief.metadata,
            clientId: "bluemesa-payments",
            projectId: "bluemesa-payments",
          },
        },
      }),
    });
  });

  await page.goto("/");
  await page.getByRole("button", { name: /Generate AI prebrief/i }).click();
  await expect.poll(() => submittedDirection).toContain("BlueMesa is an existing AWS customer");
  expect(submittedDirection).not.toContain("Treat BlueMesa");
  expect(submittedDirection).toContain("payroll integration");
});

test("live job keeps the workspace responsive with an in-app clock", async ({ page }) => {
  let postCount = 0;
  let pollCount = 0;
  await page.route("https://test.execute-api.us-east-1.amazonaws.com/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "GET" && path === "/clients") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ clients: [] }),
      });
      return;
    }
    if (route.request().method() === "POST") {
      postCount += 1;
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          jobId: "job-0001",
          clientId: "apex-mutual",
          projectId: "apex-mutual",
          status: "queued",
          pollAfterMs: 750,
        }),
      });
      return;
    }

    pollCount += 1;
    const states = ["queued", "running", "validating", "saving", "complete"] as const;
    const status = states[Math.min(pollCount - 1, states.length - 1)];
    await route.fulfill({
      status: status === "complete" ? 200 : 202,
      contentType: "application/json",
      body: JSON.stringify({
        jobId: "job-0001",
        clientId: "apex-mutual",
        projectId: "apex-mutual",
        status,
        pollAfterMs: 750,
        result: status === "complete" ? completedBrief : undefined,
      }),
    });
  });

  await page.goto("/");
  const generate = page.getByRole("button", {
    name: /Generate AI prebrief/i,
  });
  const duplicateSubmit = page.locator(".workspace-context-action button").last();
  const workspace = page.locator("main");
  const pageView = page.locator(".page-view");
  const workflowNavigation = page.getByRole("navigation", {
    name: "Customer lifecycle",
  });
  const briefNavigation = workflowNavigation.getByRole("button", {
    name: /Insights/,
  });

  await expect(generate).toBeEnabled();
  const actionBoxBefore = await duplicateSubmit.boundingBox();
  await generate.click();
  await expect(duplicateSubmit).toBeDisabled();
  const actionBoxBusy = await duplicateSubmit.boundingBox();
  expect(actionBoxBefore).not.toBeNull();
  expect(actionBoxBusy).not.toBeNull();
  expect(Math.abs((actionBoxBusy?.width ?? 0) - (actionBoxBefore?.width ?? 0))).toBeLessThanOrEqual(1);
  expect(Math.abs((actionBoxBusy?.height ?? 0) - (actionBoxBefore?.height ?? 0))).toBeLessThanOrEqual(1);
  await expect(workspace).toHaveAttribute("aria-busy", "true");
  await expect(page.getByText("Waiting for generation")).toBeVisible();
  await expect(briefNavigation).toBeDisabled();
  const processingIndicator = page.locator(".processing-indicator").first();
  await expect(processingIndicator).toBeVisible();
  await expect(processingIndicator.locator(".processing-clock")).toBeVisible();
  await expect
    .poll(() => pageView.evaluate((element) => getComputedStyle(element).cursor))
    .not.toBe("wait");
  await expect
    .poll(() => pageView.evaluate((element) => getComputedStyle(element).cursor))
    .not.toBe("progress");

  const shellLayout = await page.evaluate(() => {
    const root = document.scrollingElement;
    const briefPane = document.querySelector(".brief-surface");
    return {
      rootVerticalOverflow: root ? root.scrollHeight - window.innerHeight : 0,
      rootHorizontalOverflow: root ? root.scrollWidth - window.innerWidth : 0,
      briefOverflowStyle: briefPane ? getComputedStyle(briefPane).overflowY : "",
      pageY: window.scrollY,
    };
  });
  expect(shellLayout.rootVerticalOverflow).toBeGreaterThan(0);
  expect(shellLayout.rootHorizontalOverflow).toBeLessThanOrEqual(2);
  expect(shellLayout.briefOverflowStyle).toBe("visible");
  expect(shellLayout.pageY).toBeGreaterThanOrEqual(0);

  await expect(page.getByText("Checking quality and safety...", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Saving packet...", { exact: true }).first()).toBeVisible();
  await expect(workspace).toHaveAttribute("aria-busy", "false", {
    timeout: 10_000,
  });
  await expect(page.getByText(businessCase.scenario)).toBeVisible();
  await expect(page.getByText("No brief generated yet")).toHaveCount(0);
  await expect(briefNavigation).toBeEnabled();
  await expect(duplicateSubmit).toBeEnabled();
  await expect
    .poll(() => pageView.evaluate((element) => getComputedStyle(element).cursor))
    .not.toBe("wait");
  expect(postCount).toBe(1);
  expect(pollCount).toBe(5);

  const approvalGap = await page.evaluate(() => {
    const brief = document.querySelector(".brief-surface")?.getBoundingClientRect();
    const approval = document
      .querySelector(".refinement-approve-row")
      ?.getBoundingClientRect();
    if (!brief || !approval) return Number.POSITIVE_INFINITY;
    return Math.round(approval.top - brief.bottom);
  });
  expect(approvalGap).toBeGreaterThanOrEqual(0);
  expect(approvalGap).toBeLessThanOrEqual(20);

  await page.evaluate(() => {
    window.scrollTo({
      top: document.scrollingElement?.scrollHeight ?? document.body.scrollHeight,
      behavior: "auto",
    });
  });
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(0);
  await page.getByRole("button", { name: "Open PilarPrep context" }).click();
  await expect(page.getByRole("heading", { name: "Build the meeting context" })).toBeVisible();
  await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);

  const longestTask = await page.evaluate(
    () =>
      Math.max(
        0,
        ...((window as typeof window & { __pilarprepLongTasks?: number[] })
          .__pilarprepLongTasks ?? [])
      )
  );
  expect(longestTask).toBeLessThan(250);
});

test("approval waits for an explicit pre-call handoff action", async ({ page }) => {
  const submittedActions: Array<{
    action?: string;
    input?: { packetVersion?: number };
  }> = [];
  const approvedBrief = {
    ...completedBrief,
    metadata: {
      ...completedBrief.metadata,
      packetVersion: 2,
      approvedPacketVersion: 2,
      approvalStatus: "approved",
      precallHandoffStatus: "idle",
      precallHandoffSourceVersion: 2,
    },
  };
  const handoffBrief = {
    ...approvedBrief,
    provider: "agentcore",
    projectArtifacts: {
      twoWeekPlan: [
        {
          day: "Day 1",
          action: "Validate payroll interface evidence",
          owner: "Solutions Architect",
          exitCriteria: "Evidence owner confirmed",
        },
      ],
      riskRegister: [
        {
          risk: "Payroll cutover risk",
          impact: "Settlement confidence",
          mitigation: "Bounded validation",
          owner: "Platform lead",
        },
      ],
      stakeholderMap: [
        {
          name: "Maya Chen",
          role: "Executive sponsor",
          priorities: "Visible progress",
          engagement: "Weekly decision readout",
        },
      ],
      followUpEmail: {
        subject: "BlueMesa pre-call alignment",
        body: "The approved packet is ready for the call team.",
      },
      nextSteps: {
        immediateActions: ["Confirm evidence owners"],
        openQuestions: ["Which payroll interfaces are in scope?"],
        nextMeeting: {
          purpose: "Validate architecture evidence",
          timing: "Next week",
          attendees: ["Sales", "Solutions Architect"],
        },
        customerSummary: "BlueMesa is preparing a bounded payroll integration.",
        internalNotes: "Use the approved packet as the source of truth.",
      },
    },
    metadata: {
      ...approvedBrief.metadata,
      docxArtifactKey: "handoff/latest.docx",
      stateKey: "HANDOFF#LATEST",
      precallHandoffStatus: "ready",
    },
  };

  await page.route("https://test.execute-api.us-east-1.amazonaws.com/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() === "GET" && path === "/clients") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ clients: [] }),
      });
      return;
    }
    if (request.method() === "POST") {
      const payload = request.postDataJSON() as {
        action?: string;
        input?: { packetVersion?: number };
      };
      submittedActions.push(payload);
      const approval = payload.action === "brief.approve";
      const handoff = payload.action === "handoff.generate";
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          jobId: approval
            ? "job-approve-v1"
            : handoff
              ? "job-handoff-v1"
              : "job-generate-v1",
          clientId: "apex-mutual",
          projectId: "apex-mutual",
          status: "queued",
          pollAfterMs: 10,
        }),
      });
      return;
    }

    const approval = path.endsWith("/job-approve-v1");
    const handoff = path.endsWith("/job-handoff-v1");
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        jobId: approval
          ? "job-approve-v1"
          : handoff
            ? "job-handoff-v1"
            : "job-generate-v1",
        clientId: "apex-mutual",
        projectId: "apex-mutual",
        status: "complete",
        result: approval ? approvedBrief : handoff ? handoffBrief : completedBrief,
      }),
    });
  });

  await page.goto("/");
  await page.getByRole("button", { name: /Generate AI prebrief/i }).click();
  await expect(page.getByText(businessCase.scenario)).toBeVisible();

  const approve = page.getByRole("button", { name: "Approve pre-call packet" });
  await expect(approve).toBeEnabled();
  await approve.click();

  await expect(
    page.getByRole("heading", { name: "Prepare the team for the customer call" })
  ).toBeVisible();
  await expect(page.getByText("Ready to prepare", { exact: true })).toBeVisible();
  const buildHandoff = page.getByRole("button", {
    name: "Build pre-call handoff",
  });
  await expect(buildHandoff).toBeEnabled();
  expect(submittedActions.map((request) => request.action)).toEqual([
    "brief.generate",
    "brief.approve",
  ]);
  expect(submittedActions[1]?.input?.packetVersion).toBe(1);
  await buildHandoff.click();
  await expect(
    page.getByText("The shared handoff is ready for the call team.")
  ).toBeVisible();
  expect(submittedActions.map((request) => request.action)).toEqual([
    "brief.generate",
    "brief.approve",
    "handoff.generate",
  ]);
});

test("claim citations open an accessible authorized evidence drawer", async ({ page }) => {
  await page.route("https://test.execute-api.us-east-1.amazonaws.com/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "GET" && path === "/clients") {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ clients: [] }) });
      return;
    }
    if (route.request().method() === "POST") {
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          jobId: "job-evidence-drawer",
          clientId: "apex-mutual",
          projectId: "apex-mutual",
          status: "queued",
          pollAfterMs: 10,
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        jobId: "job-evidence-drawer",
        clientId: "apex-mutual",
        projectId: "apex-mutual",
        status: "complete",
        result: completedBrief,
      }),
    });
  });

  await page.goto("/");
  await page.getByRole("button", { name: /Generate AI prebrief/i }).click();
  await expect(page.getByText(businessCase.scenario)).toBeVisible();
  await page.getByRole("button", { name: "[Customer context]" }).first().click();

  const drawer = page.getByRole("dialog", { name: "Customer context" });
  await expect(drawer).toBeVisible();
  await expect(drawer).toContainText("Customer provided");
  await expect(drawer).toContainText("bounded modernization discovery");
  await expect(drawer).toContainText("not a probability that a claim is true");
  await drawer.getByRole("button", { name: "Close evidence details" }).click();
  await expect(drawer).toHaveCount(0);
});

test("failed generation replaces the empty state with an actionable error", async ({ page }) => {
  let pollCount = 0;
  await page.route("https://test.execute-api.us-east-1.amazonaws.com/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "GET" && path === "/clients") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ clients: [] }),
      });
      return;
    }
    if (route.request().method() === "POST") {
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          jobId: "job-failed-0001",
          clientId: "bluemesa-payments",
          projectId: "bluemesa-payments",
          status: "queued",
          pollAfterMs: 750,
        }),
      });
      return;
    }

    pollCount += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        jobId: "job-failed-0001",
        clientId: "bluemesa-payments",
        projectId: "bluemesa-payments",
        status: "failed",
        error:
          "PilarPrep could not process part of the supplied content. Describe customer facts and desired outcomes without instructions to ignore, override, or reveal AI behavior.",
      }),
    });
  });

  await page.goto("/");
  await page.getByRole("button", { name: /BlueMesa Payments/ }).click();
  await page.getByRole("button", { name: /Generate AI prebrief/i }).click();

  const alert = page.getByRole("alert");
  await expect(alert).toContainText("Brief generation could not complete");
  await expect(alert).toContainText("Describe customer facts and desired outcomes");
  await expect(alert).toHaveCSS("background-color", "rgb(255, 247, 245)");
  await expect(alert).toHaveCSS("border-color", "rgb(231, 180, 172)");
  await expect(page.getByText("No brief generated yet")).toHaveCount(0);
  await expect(page.locator("main")).toHaveAttribute("aria-busy", "false");
  await alert.getByRole("button", { name: "Review inputs" }).click();
  await expect(page.getByRole("heading", { name: "Build the meeting context" })).toBeVisible();
  expect(pollCount).toBe(1);
});

test("navigation aborts a running poll and immediately restores interaction", async ({ page }) => {
  let pollCount = 0;
  await page.route("https://test.execute-api.us-east-1.amazonaws.com/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "GET" && path === "/clients") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ clients: [] }),
      });
      return;
    }
    if (route.request().method() === "POST") {
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          jobId: "job-0002",
          clientId: "apex-mutual",
          projectId: "apex-mutual",
          status: "queued",
          pollAfterMs: 750,
        }),
      });
      return;
    }
    pollCount += 1;
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        jobId: "job-0002",
        clientId: "apex-mutual",
        projectId: "apex-mutual",
        status: "running",
        pollAfterMs: 750,
      }),
    });
  });

  await page.goto("/");
  await page
    .getByRole("button", { name: /Generate AI prebrief/i })
    .click();
  await expect(page.locator("main")).toHaveAttribute("aria-busy", "true");

  await page
    .getByRole("button", { name: "Open catch-up workspace" })
    .click();

  await expect(page.locator("main")).toHaveAttribute("aria-busy", "false");
  await expect(page.getByText("Get a new teammate up to speed")).toBeVisible();
  await expect
    .poll(() =>
      page.locator(".page-view").evaluate((element) => getComputedStyle(element).cursor)
    )
    .not.toBe("wait");
  expect(pollCount).toBeLessThanOrEqual(1);
});
test("desktop and laptop layouts avoid horizontal overflow and preserve visible focus", async ({ page }) => {
  await page.route("https://test.execute-api.us-east-1.amazonaws.com/**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ clients: [] }),
    });
  });

  for (const viewport of [
    { width: 1280, height: 800 },
    { width: 1440, height: 900 },
    { width: 1920, height: 1080 },
  ]) {
    await page.setViewportSize(viewport);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Build the meeting context" })).toBeVisible();

    const layout = await page.evaluate(() => {
      const root = document.scrollingElement;
      const sections = Array.from(
        document.querySelectorAll<HTMLElement>(".setup-grid > section")
      ).map((element) => element.getBoundingClientRect());
      return {
        horizontalOverflow: root ? root.scrollWidth - window.innerWidth : 0,
        sections: sections.map((box) => ({
          left: box.left,
          right: box.right,
          top: box.top,
          bottom: box.bottom,
        })),
      };
    });

    expect(layout.horizontalOverflow).toBeLessThanOrEqual(2);
    expect(layout.sections).toHaveLength(2);
    const [scenarioPanel, intakePanel] = layout.sections;
    const sideBySide = Math.abs(scenarioPanel.top - intakePanel.top) <= 2;
    if (sideBySide) {
      expect(scenarioPanel.right).toBeLessThanOrEqual(intakePanel.left + 1);
    } else {
      expect(scenarioPanel.bottom).toBeLessThanOrEqual(intakePanel.top + 1);
    }

    await page.keyboard.press("Tab");
    const focusStyle = await page.evaluate(() => {
      const active = document.activeElement;
      if (!(active instanceof HTMLElement)) return null;
      const style = getComputedStyle(active);
      return {
        tagName: active.tagName,
        outlineStyle: style.outlineStyle,
        outlineWidth: style.outlineWidth,
      };
    });
    expect(focusStyle).not.toBeNull();
    expect(focusStyle?.tagName).not.toBe("BODY");
    expect(focusStyle?.outlineStyle).not.toBe("none");
    expect(focusStyle?.outlineWidth).not.toBe("0px");

    await page.screenshot({
      path: "test-results/audit-after-local-" + viewport.width + ".png",
      fullPage: true,
    });
  }
});

test("processing clock becomes static when reduced motion is requested", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.route("https://test.execute-api.us-east-1.amazonaws.com/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (route.request().method() === "GET" && path === "/clients") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ clients: [] }),
      });
      return;
    }
    if (route.request().method() === "POST") {
      await route.fulfill({
        status: 202,
        contentType: "application/json",
        body: JSON.stringify({
          jobId: "job-reduced-motion",
          clientId: "apex-mutual",
          projectId: "apex-mutual",
          status: "queued",
          pollAfterMs: 750,
        }),
      });
      return;
    }
    await route.fulfill({
      status: 202,
      contentType: "application/json",
      body: JSON.stringify({
        jobId: "job-reduced-motion",
        clientId: "apex-mutual",
        projectId: "apex-mutual",
        status: "running",
        pollAfterMs: 750,
      }),
    });
  });

  await page.goto("/");
  await page.getByRole("button", { name: /Generate AI prebrief/i }).click();
  const indicator = page.locator(".processing-indicator").first();
  await expect(indicator).toBeVisible();
  await expect(indicator.locator(".processing-clock-hour")).toHaveCSS("animation-name", "none");
  await expect(indicator.locator(".processing-clock-minute")).toHaveCSS("animation-name", "none");
  await page.getByRole("button", { name: "Open catch-up workspace" }).click();
  await expect(page.locator("main")).toHaveAttribute("aria-busy", "false");
});
