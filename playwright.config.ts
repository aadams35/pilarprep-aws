import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  expect: {
    timeout: 10_000,
  },
  use: {
    baseURL: "http://127.0.0.1:4173",
    browserName: "chromium",
    headless: true,
    trace: "retain-on-failure",
    viewport: { width: 1440, height: 900 },
  },
  webServer: {
    command: "node scripts/serve-e2e.mjs",
    url: "http://127.0.0.1:4173",
    reuseExistingServer: false,
    timeout: 120_000,
    env: {
      ...process.env,
      VITE_PILLARPREP_JOBS_API_URL:
        "https://test.execute-api.us-east-1.amazonaws.com",
      VITE_PILLARPREP_WORKSPACE_API_URL:
        "https://test.execute-api.us-east-1.amazonaws.com",
      VITE_PILLARPREP_BACKEND_REGION: "us-east-1",
      VITE_PILLARPREP_COGNITO_IDENTITY_POOL_ID:
        "us-east-1:11111111-1111-4111-8111-111111111111",
      VITE_PILLARPREP_COGNITO_USER_POOL_CLIENT_ID: "test-workspace-client",
      VITE_PILLARPREP_COGNITO_LOGIN_DOMAIN:
        "https://auth.pilarprep.test",
    },
  },
});
