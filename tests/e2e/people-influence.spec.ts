import { expect, test } from "@playwright/test";

const appUrl = process.env.PILARPREP_E2E_URL ?? "/";

test("people and influence separates authority and stakeholder profiles", async ({ page }) => {
  await page.goto(appUrl);
  await page.getByRole("button", { name: /PeakCart Retail/ }).click();

  const decisionTab = page.getByRole("tab", { name: /Decision-makers/ });
  const stakeholderTab = page.getByRole("tab", { name: /Stakeholders/ });
  await expect(decisionTab).toContainText("1");
  await expect(stakeholderTab).toContainText("1");

  await stakeholderTab.click();
  await expect(stakeholderTab).toHaveAttribute("aria-selected", "true");
  const luisCard = page.locator(".decision-maker-card").first();
  await expect(luisCard.getByLabel("Name")).toHaveValue("Luis Ramirez");
  await luisCard.locator(".person-profile-details > summary").click();
  await expect(luisCard.getByLabel("Organizational role")).toHaveValue("Technical evaluator and implementation champion");
  await expect(luisCard.getByLabel("Influence")).toHaveValue("high");
  await expect(luisCard.getByLabel("Current stance")).toHaveValue("champion");

  await luisCard.getByLabel("Profile type").selectOption("decision-maker");
  await expect(decisionTab).toHaveAttribute("aria-selected", "true");
  await expect(decisionTab).toContainText("2");
  await expect.poll(() =>
    page.locator(".decision-maker-card input").evaluateAll((inputs) =>
      inputs.some((input) => (input as HTMLInputElement).value === "Luis Ramirez")
    )
  ).toBe(true);
  await expect(stakeholderTab).toContainText("0");
});
test("Blue Mesa exposes decision-makers and non-decision stakeholders", async ({ page }) => {
  await page.goto(appUrl);
  await page.getByRole("button", { name: /BlueMesa Payments/ }).click();

  const decisionTab = page.getByRole("tab", { name: /Decision-makers/ });
  const stakeholderTab = page.getByRole("tab", { name: /Stakeholders/ });
  await expect(decisionTab).toContainText("4");
  await expect(stakeholderTab).toContainText("4");
  await expect(page.getByLabel("Known context")).toHaveValue(/Amazon EKS/);

  const layout = await page.locator(".brief-input-grid").evaluate(() => {
    const main = document.querySelector(".brief-input-panel-main")?.getBoundingClientRect();
    const people = document.querySelector(".brief-input-panel-stakeholders")?.getBoundingClientRect();
    const priorities = document.querySelector(".brief-input-priorities")?.getBoundingClientRect();
    const pillar = document.querySelector(".pillar-rank-card")?.getBoundingClientRect();
    return {
      mainRight: main?.right ?? 0,
      peopleLeft: people?.left ?? 0,
      prioritiesRight: priorities?.right ?? 0,
      pillarRight: pillar?.right ?? 0,
    };
  });
  expect(layout.mainRight).toBeLessThanOrEqual(layout.peopleLeft);
  expect(layout.pillarRight).toBeLessThanOrEqual(layout.prioritiesRight + 1);

  await stakeholderTab.click();
  await expect(page.locator(".person-profile-details")).toHaveCount(4);
  await expect(page.locator(".person-profile-details[open]")).toHaveCount(0);
  const stakeholderNames = await page.locator(".decision-maker-card input").evaluateAll((inputs) =>
    inputs.map((input) => (input as HTMLInputElement).value)
  );
  expect(stakeholderNames).toEqual(
    expect.arrayContaining(["Priya Shah", "Elena Torres", "Noah Grant", "Omar Fields"])
  );
});
test("legacy Blue Mesa workspace is replaced by the current scenario fixture", async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "pillarprep.workspace.v1",
      JSON.stringify({
        scenarioId: "bluemesa",
        company: "BlueMesa Payments",
        context: "BlueMesa is migrating an on-premises platform.",
        decisionMakers: [
          { name: "Old One", title: "CIO", context: "Legacy", roleType: "decision-maker" },
          { name: "Old Two", title: "CTO", context: "Legacy", roleType: "decision-maker" },
          { name: "Old Three", title: "CISO", context: "Legacy", roleType: "decision-maker" },
        ],
      })
    );
  });

  await page.goto(appUrl);
  await expect(page.getByRole("button", { name: /Apex Mutual/ })).toHaveClass(/scenario-button-active/);
  await expect.poll(() => page.evaluate(() => window.localStorage.getItem("pillarprep.workspace.v1"))).toBeNull();

  await page.getByRole("button", { name: /BlueMesa Payments/ }).click();
  await expect(page.getByRole("tab", { name: /Decision-makers/ })).toContainText("4");
  await expect(page.getByRole("tab", { name: /Stakeholders/ })).toContainText("4");
  await expect(page.getByLabel("Known context")).toHaveValue(/Amazon EKS/);
});