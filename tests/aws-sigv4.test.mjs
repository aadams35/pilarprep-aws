import assert from "node:assert/strict";
import test from "node:test";

import { cognitoIdentityCredentialsProvider } from "../frontend/src/lib/aws-sigv4.ts";

class MemoryStorage {
  values = new Map();

  getItem(key) {
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    this.values.set(key, String(value));
  }

  removeItem(key) {
    this.values.delete(key);
  }
}

const originalFetch = globalThis.fetch;
const originalWindow = globalThis.window;

test.after(() => {
  globalThis.fetch = originalFetch;
  if (originalWindow === undefined) delete globalThis.window;
  else globalThis.window = originalWindow;
});

test("Cognito guest identity persists without storing temporary credentials", async () => {
  const localStorage = new MemoryStorage();
  globalThis.window = { localStorage };
  const targets = [];
  globalThis.fetch = async (_url, options) => {
    const target = options.headers["x-amz-target"];
    targets.push(target);
    const body = target.endsWith("GetId")
      ? { IdentityId: "us-east-1:persisted-test-identity" }
      : {
          Credentials: {
            AccessKeyId: "ASIATEST",
            SecretKey: "browser-test-secret",
            SessionToken: "browser-test-token",
            Expiration: Math.floor(Date.now() / 1000) + 3600,
          },
        };
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/x-amz-json-1.1" },
    });
  };

  const provider = cognitoIdentityCredentialsProvider({
    identityPoolId: "us-east-1:test-pool-one",
    region: "us-east-1",
  });
  const credentials = await provider();

  assert.equal(credentials.accessKeyId, "ASIATEST");
  assert.deepEqual(targets, [
    "AWSCognitoIdentityService.GetId",
    "AWSCognitoIdentityService.GetCredentialsForIdentity",
  ]);
  const storedValues = [...localStorage.values.values()];
  assert.deepEqual(storedValues, ["us-east-1:persisted-test-identity"]);
  assert.equal(JSON.stringify(storedValues).includes("ASIATEST"), false);
  assert.equal(JSON.stringify(storedValues).includes("browser-test-secret"), false);
});

test("Cognito provider reuses a persisted guest identity after reload", async () => {
  const localStorage = new MemoryStorage();
  localStorage.setItem(
    "pillarprep.cognito-identity.v1:us-east-1:us-east-1:test-pool-two",
    "us-east-1:reloaded-test-identity"
  );
  globalThis.window = { localStorage };
  const targets = [];
  const requestBodies = [];
  globalThis.fetch = async (_url, options) => {
    targets.push(options.headers["x-amz-target"]);
    requestBodies.push(JSON.parse(options.body));
    return new Response(
      JSON.stringify({
        Credentials: {
          AccessKeyId: "ASIARELOAD",
          SecretKey: "reload-test-secret",
          SessionToken: "reload-test-token",
          Expiration: Math.floor(Date.now() / 1000) + 3600,
        },
      }),
      { status: 200, headers: { "content-type": "application/x-amz-json-1.1" } }
    );
  };

  const provider = cognitoIdentityCredentialsProvider({
    identityPoolId: "us-east-1:test-pool-two",
    region: "us-east-1",
  });
  await provider();

  assert.deepEqual(targets, ["AWSCognitoIdentityService.GetCredentialsForIdentity"]);
  assert.equal(requestBodies[0].IdentityId, "us-east-1:reloaded-test-identity");
});
