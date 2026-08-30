export type CognitoAuthConfig = {
  domain: string;
  clientId: string;
  redirectUri: string;
  logoutUri: string;
};

export type CognitoAuthSession = {
  idToken: string;
  accessToken: string;
  refreshToken?: string;
  expiresAt: number;
  subject: string;
  email: string;
  name: string;
};

const SESSION_KEY = "pillarprep.auth.session.v1";
const PKCE_KEY = "pillarprep.auth.pkce.v1";

function browserAvailable() {
  return typeof window !== "undefined" && typeof window.sessionStorage !== "undefined";
}

function normalizedDomain(value: string) {
  return value.trim().replace(/\/$/, "");
}

export function cognitoAuthConfigured(config: CognitoAuthConfig | null) {
  let redirectIsAllowed = false;
  if (config?.redirectUri) {
    try {
      const redirect = new URL(config.redirectUri);
      redirectIsAllowed =
        redirect.protocol === "https:" ||
        (redirect.protocol === "http:" &&
          ["localhost", "127.0.0.1", "::1"].includes(redirect.hostname));
    } catch {
      redirectIsAllowed = false;
    }
  }
  return Boolean(
    config?.clientId &&
      config.domain.startsWith("https://") &&
      redirectIsAllowed
  );
}

function base64Url(bytes: Uint8Array) {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function randomValue(length = 32) {
  const bytes = new Uint8Array(length);
  window.crypto.getRandomValues(bytes);
  return base64Url(bytes);
}

async function codeChallenge(verifier: string) {
  const digest = await window.crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(verifier)
  );
  return base64Url(new Uint8Array(digest));
}

function decodeJwtPayload(token: string): Record<string, unknown> {
  const payload = token.split(".")[1];
  if (!payload) {
    throw new Error("Cognito returned an invalid identity token.");
  }
  const normalized = payload.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  const decoded = JSON.parse(atob(padded));
  if (!decoded || typeof decoded !== "object" || Array.isArray(decoded)) {
    throw new Error("Cognito returned invalid identity claims.");
  }
  return decoded as Record<string, unknown>;
}

function sessionFromTokens(tokens: Record<string, unknown>, priorRefreshToken?: string) {
  const idToken = typeof tokens.id_token === "string" ? tokens.id_token : "";
  const accessToken =
    typeof tokens.access_token === "string" ? tokens.access_token : "";
  if (!idToken || !accessToken) {
    throw new Error("Cognito did not return a complete authenticated session.");
  }
  const claims = decodeJwtPayload(idToken);
  const emailVerified =
    claims.email_verified === true || claims.email_verified === "true";
  if (!emailVerified) {
    throw new Error("Verify your email before opening the PilarPrep workspace.");
  }
  const expiresIn =
    typeof tokens.expires_in === "number" ? Math.max(60, tokens.expires_in) : 3600;
  return {
    idToken,
    accessToken,
    refreshToken:
      typeof tokens.refresh_token === "string"
        ? tokens.refresh_token
        : priorRefreshToken,
    expiresAt: Date.now() + expiresIn * 1000,
    subject: typeof claims.sub === "string" ? claims.sub : "",
    email: typeof claims.email === "string" ? claims.email : "",
    name:
      typeof claims.name === "string"
        ? claims.name
        : typeof claims.email === "string"
          ? claims.email.split("@")[0]
          : "Workspace user",
  } satisfies CognitoAuthSession;
}

function storeSession(session: CognitoAuthSession) {
  window.sessionStorage.setItem(SESSION_KEY, JSON.stringify(session));
  return session;
}

export function loadCognitoSession() {
  if (!browserAvailable()) {
    return null;
  }
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(SESSION_KEY) || "null");
    if (
      parsed &&
      typeof parsed.idToken === "string" &&
      typeof parsed.accessToken === "string" &&
      typeof parsed.expiresAt === "number" &&
      typeof parsed.subject === "string"
    ) {
      return parsed as CognitoAuthSession;
    }
  } catch {
    window.sessionStorage.removeItem(SESSION_KEY);
  }
  return null;
}

function tokenEndpoint(config: CognitoAuthConfig) {
  return normalizedDomain(config.domain) + "/oauth2/token";
}

async function tokenRequest(
  config: CognitoAuthConfig,
  values: Record<string, string>
) {
  const response = await fetch(tokenEndpoint(config), {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(values),
  });
  const body = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok) {
    throw new Error("Cognito could not complete authentication. Please try again.");
  }
  return body;
}

export async function beginCognitoLogin(config: CognitoAuthConfig) {
  if (!browserAvailable() || !cognitoAuthConfigured(config)) {
    throw new Error("Authenticated workspace login is not configured.");
  }
  const verifier = randomValue(48);
  const state = randomValue(24);
  window.sessionStorage.setItem(PKCE_KEY, JSON.stringify({ verifier, state }));
  const query = new URLSearchParams({
    response_type: "code",
    client_id: config.clientId,
    redirect_uri: config.redirectUri,
    scope: "openid email profile",
    state,
    code_challenge_method: "S256",
    code_challenge: await codeChallenge(verifier),
  });
  window.location.assign(
    normalizedDomain(config.domain) + "/oauth2/authorize?" + query.toString()
  );
}

function clearAuthQuery() {
  const url = new URL(window.location.href);
  for (const name of ["code", "state", "error", "error_description"]) {
    url.searchParams.delete(name);
  }
  window.history.replaceState({}, document.title, url.pathname + url.search + url.hash);
}

export async function completeCognitoLogin(config: CognitoAuthConfig) {
  if (!browserAvailable() || !cognitoAuthConfigured(config)) {
    return null;
  }
  const query = new URLSearchParams(window.location.search);
  const authError = query.get("error");
  if (authError) {
    const detail = query.get("error_description") || "Cognito authentication failed.";
    clearAuthQuery();
    throw new Error(detail);
  }
  const code = query.get("code");
  if (!code) {
    return loadCognitoSession();
  }
  const returnedState = query.get("state");
  const pending = JSON.parse(window.sessionStorage.getItem(PKCE_KEY) || "null") as {
    verifier?: string;
    state?: string;
  } | null;
  window.sessionStorage.removeItem(PKCE_KEY);
  if (!pending?.verifier || !pending.state || pending.state !== returnedState) {
    clearAuthQuery();
    throw new Error("The login response could not be verified. Start sign-in again.");
  }
  const tokens = await tokenRequest(config, {
    grant_type: "authorization_code",
    client_id: config.clientId,
    redirect_uri: config.redirectUri,
    code,
    code_verifier: pending.verifier,
  });
  clearAuthQuery();
  return storeSession(sessionFromTokens(tokens));
}

export async function validCognitoIdToken(
  config: CognitoAuthConfig,
  session: CognitoAuthSession
) {
  if (session.expiresAt - Date.now() > 60_000) {
    return { token: session.idToken, session };
  }
  if (!session.refreshToken) {
    window.sessionStorage.removeItem(SESSION_KEY);
    throw new Error("Your workspace session expired. Sign in again.");
  }
  const tokens = await tokenRequest(config, {
    grant_type: "refresh_token",
    client_id: config.clientId,
    refresh_token: session.refreshToken,
  });
  const refreshed = storeSession(sessionFromTokens(tokens, session.refreshToken));
  return { token: refreshed.idToken, session: refreshed };
}

export function signOutCognito(config: CognitoAuthConfig) {
  if (!browserAvailable()) {
    return;
  }
  window.sessionStorage.removeItem(SESSION_KEY);
  window.sessionStorage.removeItem(PKCE_KEY);
  const query = new URLSearchParams({
    client_id: config.clientId,
    logout_uri: config.logoutUri,
  });
  window.location.assign(
    normalizedDomain(config.domain) + "/logout?" + query.toString()
  );
}

export async function bearerApiFetch(
  url: string,
  token: string,
  options: { method?: "GET" | "POST"; payload?: unknown; signal?: AbortSignal } = {}
) {
  const method = options.method ?? "GET";
  const body = options.payload === undefined ? undefined : JSON.stringify(options.payload);
  return fetch(url, {
    method,
    signal: options.signal,
    headers: {
      accept: "application/json",
      authorization: "Bearer " + token,
      ...(body === undefined ? {} : { "content-type": "application/json" }),
    },
    ...(body === undefined ? {} : { body }),
  });
}
