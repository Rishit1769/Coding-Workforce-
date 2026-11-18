import { expect, test } from "@playwright/test";

test("dashboard shows live orchestration state", async ({ page, request }) => {
  await page.goto("/");
  // all eight agents with their model assignments
  for (const role of ["manager", "architect", "developer_1", "developer_2", "tester", "reviewer", "devops", "worker"]) {
    await expect(page.getByTestId(`agent-${role}`)).toBeVisible();
  }
  await expect(page.getByTestId("agent-architect")).toContainText("Claude Opus 5.5");
  // tasks reflect the real engine run (both waiting for integration approval)
  const table = page.getByTestId("tasks-table").first();
  await expect(table).toContainText("T-001");
  await expect(table).toContainText("INTEGRATING");
  // task detail shows the failed-then-passed acceptance tests from the correction loop
  await table.getByText("T-001").first().click();
  await expect(page.getByTestId("task-detail")).toContainText("acceptance");
  await expect(page.getByTestId("task-detail")).toContainText("Correction feedback");
});

test("approvals and controls act on real state", async ({ page, request }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /^Approvals/ }).click();
  const list = page.getByTestId("approvals");
  await expect(list).toContainText("Integrate T-001");
  const before = await (await request.get("/api/approvals")).json();
  const target = before.find((a: { subject: string }) => a.subject.startsWith("Integrate T-001"));
  await list.locator("li", { hasText: "Integrate T-001" }).getByRole("button", { name: "Approve" }).click();
  await expect(list).not.toContainText("Integrate T-001");
  const all = await (await request.get("/api/approvals?status=all")).json();
  expect(all.find((a: { id: number }) => a.id === target.id).status).toBe("approved");

  await page.getByRole("button", { name: "Pause" }).first().click();
  await expect(page.getByTestId("run-state")).toContainText("paused");
  const ov = await (await request.get("/api/overview")).json();
  expect(ov.run_state).toBe("paused");
});

test("traceability and git views", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Traceability" }).click();
  await expect(page.getByTestId("trace-table")).toContainText("FR-3");
  await page.getByRole("button", { name: "Git" }).click();
  await expect(page.getByText("wf/textstats/integration").first()).toBeVisible();
});
