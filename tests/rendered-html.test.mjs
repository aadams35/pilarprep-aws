import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test, { after, before } from "node:test";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { createServer } from "vite";
import { GET, POST } from "../frontend/dev/brief-api.ts";

let server;
before(async () => {
  server = await createServer({
    configFile: fileURLToPath(new URL("../vite.config.ts", import.meta.url)),
    server: { middlewareMode: true },
    appType: "custom",
    logLevel: "error",
  });
});
after(async () => { await server?.close(); });

async function render() {
  return fetchWorker("/");
}

async function fetchWorker(path, init) {
  if (path === "/api/brief") {
    return init?.method === "POST" ? POST(new Request(`http://localhost${path}`, init)) : GET();
  }
  const { default: App } = await server.ssrLoadModule("/src/App.tsx");
  const document = await readFile(new URL("../frontend/index.html", import.meta.url), "utf8");
  const html = document.replace('<div id="root"></div>', `<div id="root">${renderToString(createElement(App))}</div>`);
  return new Response(html, { headers: { "content-type": "text/html; charset=utf-8" } });
}

test("renders the PilarPrep app with the production entry point", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.ok((response.headers.get("content-type") ?? "").startsWith("text/html"));

  const html = await response.text();
  assert.match(html, /PilarPrep/);
  assert.match(html, /PilarPrep workspace/);
  assert.match(html, /aria-label="Customer lifecycle"/);
  assert.match(html, />Research</);
  assert.match(html, />Insights</);
  assert.match(html, />Discovery</);
  assert.match(html, />Meet</);
  assert.match(html, />Follow-up</);
  assert.doesNotMatch(html, />Prepare</);
  assert.doesNotMatch(html, />Refine</);
  assert.doesNotMatch(html, />Advance</);
  assert.match(html, /Build the meeting context/);
  assert.match(html, /Generated packet/);
  assert.match(html, /Customer scenarios/);
  assert.match(html, /Company values page/);
  assert.match(html, /People and influence/);
  assert.match(html, />1A</);
  assert.match(html, />1B</);
  assert.match(html, />2A</);
  assert.match(html, />2B</);
  assert.match(html, /Decision-makers/);
  assert.match(html, /Stakeholders/);
  assert.doesNotMatch(html, /Decision-maker context|<span>03<\/span>/);
  assert.match(html, /Create your own/);
  assert.match(html, /Generate (AI )?prebrief/);
  assert.match(html, /Risk-sensitive modernization/);
  assert.match(html, /Catch-up/);
  assert.doesNotMatch(html, /Presenter|Ask Project Brain|Ask Project model|Promote to Project|Lifecycle progress|Quality gate|Pillar heatmap|Run Presenter Guide|Demo state|PilarPrep demo console/);
});

test("keeps the customer workflow and public app metadata connected", async () => {
  const [page, layout, entry] = await Promise.all([
    readFile(new URL("../frontend/src/App.tsx", import.meta.url), "utf8"),
    readFile(new URL("../frontend/index.html", import.meta.url), "utf8"),
    readFile(new URL("../frontend/src/main.tsx", import.meta.url), "utf8"),
  ]);

  assert.match(page, /Team handoff|Handoff workspace/);
  assert.ok(page.includes("Prepare team handoff"));
  assert.ok(page.includes("Next-step handoff"));
  assert.ok(page.includes("Copy packet"));
  assert.ok(page.includes("Catch-up workspace"));
  assert.ok(page.includes("Get a new teammate up to speed"));
  assert.ok(page.includes("Generate a role-aware catch-up"));
  assert.ok(page.includes("VITE_PILLARPREP_JOBS_API_URL"));
  assert.ok(page.includes("requestPipelineJob"));
  assert.ok(page.includes("pipelineAbortRef"));
  assert.ok(page.includes("aria-busy"));
  assert.ok(page.includes("Approve the current brief before generating the handoff."));
  assert.ok(page.includes('setPrecallHandoffStatus("ready")'));
  assert.ok(page.includes("The browser could not reach the private audio workspace."));
  assert.ok(page.includes('disabled={isGenerating || precallHandoffStatus === "queued" || precallHandoffStatus === "preparing" || !approved || !generatedBrief}'));
  assert.ok(page.includes("Company values page"));
  assert.ok(page.includes('workspaceStorageKey = "pillarprep.workspace.v2"'));
  assert.ok(page.includes('legacyWorkspaceStorageKey = "pillarprep.workspace.v1"'));
  assert.ok(page.includes("Current architecture and assumptions"));
  assert.ok(page.includes("Value and measurable outcomes"));
  assert.ok(page.includes("Open and align"));
  assert.ok(page.includes("Generated-content accuracy"));
  assert.ok(page.includes("peopleSummaryProfiles"));
  assert.ok(page.includes("Priya Shah"));
  assert.ok(page.includes("Elena Torres"));
  assert.doesNotMatch(page, /singleSelectCategory|withoutConflictingState/);
  assert.ok(page.includes("brief-surface-busy"));
  assert.ok(page.includes("const briefContent = isGenerating"));
  assert.ok(page.includes("setGeneratedBrief(null);"));
  assert.ok(page.includes("const displayedProjectAnswer = approved && promoted && !isProjectGenerating"));
  assert.ok(page.includes("handoffAnswerFor(generatedBrief"));
  assert.doesNotMatch(page, /const projectAnswer = useMemo/);
  assert.ok(page.includes("Changes"));
  assert.ok(page.includes("refinement-approve-row"));
  assert.ok(page.includes("Final quality gate"));
  assert.ok(page.includes("Apply feedback to"));
  assert.ok(page.includes("Refining:"));
  assert.ok(page.includes("Changes highlighted"));
  assert.ok(page.includes("Owned next steps and decision gates"));
  assert.ok(page.includes("Decision gate"));
  assert.ok(page.includes("Customer summary"));
  assert.ok(page.includes("OpportunityGates"));
  assert.ok(page.includes("Prepare next call"));
  assert.ok(page.includes("selectedLifecycleStage"));
  assert.doesNotMatch(page, /VITE_PILLARPREP_AGENT_URL|VITE_PILLARPREP_BACKEND_URL|requestLiveAgent|requestLiveBrief/);
  assert.doesNotMatch(page, /Presenter|Ask Project Brain|Ask Project model|Promote to Project|Backend-ready map|AWS-native architecture|hero-progress|quality-bar|telemetry-bar|Run Presenter Guide|Demo state|PilarPrep demo console|AWS Product Console|AI-backed AWS workload/);
  assert.ok(layout.includes("PilarPrep | AWS SA Briefing Copilot"));
  assert.ok(entry.includes("product.css"));
  assert.ok(entry.includes("<App />"));
  assert.ok(layout.includes("/src/main.tsx"));
});
test("generates a demo brief through the API contract", async () => {
  const response = await fetchWorker("/api/brief", {
    method: "POST",
    headers: {
      accept: "application/json",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      company: "Apex Mutual",
      industry: "Financial Services",
      meetingType: "Executive Briefing",
      companySize: "Enterprise",
      pillars: ["Security", "Reliability", "Cost Optimization"],
      pillarRanking: [
        { rank: 1, pillar: "Security" },
        { rank: 2, pillar: "Reliability" },
        { rank: 3, pillar: "Cost Optimization" },
      ],
      context: "Modernizing a customer portal with audit and migration risk.",
      companyValuesUrl: "https://www.apexmutual.example/about/values",
      decisionMakers: [
        {
          name: "Lena Ortiz",
          title: "CIO",
          source: "Customer-approved profile notes",
          context: "Modernization governance and board visibility.",
        },
      ],
    }),
  });

  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.provider, "demo");
  assert.match(payload.technical.join("\n"), /Apex Mutual/);
  assert.match(payload.businessCase.scenario, /Apex Mutual/);
  assert.equal(Object.keys(payload.businessCase).length, 13);
  assert.equal(payload.claims.filter((item) => item.section === "businessCase").length, 13);
  assert.ok(payload.evidence.filter((item) => item.section === "businessCase").length < 13);
  assert.ok(payload.evidenceCoverage.statusCounts["partially-supported"] > 0);
  assert.ok(payload.evidenceCoverage.statusCounts["needs-validation"] > 0);
  assert.ok(payload.projectArtifacts.nextSteps.immediateActions.length >= 3);
  assert.match(payload.technical.join("\n"), /ranked Well-Architected priorities/i);
  assert.match(payload.executive.join("\n"), /auditability/);
  assert.match(payload.executive.join("\n"), /Source page: https:\/\/www\.apexmutual\.example\/about\/values/);
  assert.match(payload.stakeholders.join("\n"), /Lena Ortiz/);
  assert.ok(payload.projectArtifacts.twoWeekPlan.length >= 3);
  assert.ok(payload.projectArtifacts.riskRegister.length >= 2);
  assert.ok(
    payload.projectArtifacts.nextSteps.immediateActions.every((item) =>
      ["action", "owner", "timing", "dependency", "decisionGate"].every((field) => item[field]),
    ),
  );
  assert.match(payload.projectAnswer, /latest brief|two-week sprint|decision log/i);
});

test("generates a role-aware project model answer", async () => {
  const response = await fetchWorker("/api/brief", {
    method: "POST",
    headers: {
      accept: "application/json",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      mode: "project",
      company: "Apex Mutual",
      industry: "Financial Services",
      meetingType: "Executive Briefing",
      companySize: "Enterprise",
      pillars: ["Security", "Reliability", "Cost Optimization"],
      pillarRanking: [
        { rank: 1, pillar: "Security" },
        { rank: 2, pillar: "Reliability" },
        { rank: 3, pillar: "Cost Optimization" },
      ],
      context: "Modernizing a customer portal with audit and migration risk.",
      meetingNotes: "CIO approved a pilot if security evidence is clear.",
      decisionMakers: [
        {
          name: "Lena Ortiz",
          title: "CIO",
          source: "Customer-approved profile notes",
          context: "Modernization governance and board visibility.",
        },
      ],
      role: "Sales",
      prompt: "What should we say in the follow-up email?",
    }),
  });

  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.provider, "demo");
  assert.match(payload.projectArtifacts.followUpEmail.subject, /Apex Mutual/);
  assert.ok(payload.projectArtifacts.nextSteps.openQuestions.length >= 2);
  assert.ok(payload.projectArtifacts.nextSteps.nextMeeting.attendees.length >= 2);
  assert.match(payload.projectArtifacts.nextSteps.customerSummary, /Apex Mutual/);
  assert.match(payload.projectAnswer, /Sales|Lena Ortiz|stakeholder/i);
});

test("rejects incomplete brief API requests", async () => {
  const response = await fetchWorker("/api/brief", {

    method: "POST",
    headers: {
      accept: "application/json",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      company: "",
    }),
  });

  assert.equal(response.status, 400);
  const payload = await response.json();
  assert.match(payload.error, /company is required/);
});

test("rejects malformed company values url", async () => {
  const response = await fetchWorker("/api/brief", {
    method: "POST",
    headers: {
      accept: "application/json",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      company: "Apex Mutual",
      industry: "Financial Services",
      meetingType: "Executive Briefing",
      companySize: "Enterprise",
      pillars: ["Security", "Reliability", "Cost Optimization"],
      pillarRanking: [
        { rank: 1, pillar: "Security" },
        { rank: 2, pillar: "Reliability" },
        { rank: 3, pillar: "Cost Optimization" },
      ],
      context: "Modernizing a customer portal with audit and migration risk.",
      companyValuesUrl: 42,
    }),
  });

  assert.equal(response.status, 400);
  const payload = await response.json();
  assert.match(payload.error, /companyValuesUrl must be a string/);
});
test("rejects malformed decision maker context", async () => {
  const response = await fetchWorker("/api/brief", {
    method: "POST",
    headers: {
      accept: "application/json",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      company: "Apex Mutual",
      industry: "Financial Services",
      meetingType: "Executive Briefing",
      companySize: "Enterprise",
      pillars: ["Security", "Reliability", "Cost Optimization"],
      pillarRanking: [
        { rank: 1, pillar: "Security" },
        { rank: 2, pillar: "Reliability" },
        { rank: 3, pillar: "Cost Optimization" },
      ],
      context: "Modernizing a customer portal with audit and migration risk.",
      decisionMakers: "not an array",
    }),
  });

  assert.equal(response.status, 400);
  const payload = await response.json();
  assert.match(payload.error, /decisionMakers must be an array/);
});
