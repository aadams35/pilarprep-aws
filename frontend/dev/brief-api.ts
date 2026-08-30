import { generateDemoBrief, validateBriefRequest } from "../src/lib/generator.ts";
import { normalizeBriefResponse } from "../src/lib/response.ts";
import type { BriefRequest } from "../src/lib/types.ts";

const noStoreHeaders = {
  "cache-control": "no-store",
};

export async function GET() {
  return Response.json(
    {
      liveConfigured: false,
      authMode: "local-demo",
      provider: "demo",
      livePath: "The browser uses the unified IAM-signed Jobs API.",
    },
    { headers: noStoreHeaders }
  );
}

export async function POST(request: Request) {
  const requestedMode = request.headers.get("x-pillarprep-mode");
  if (requestedMode === "live") {
    return Response.json(
      {
        error:
          "Live requests use the browser's configured IAM-signed Jobs API.",
      },
      { status: 503, headers: noStoreHeaders }
    );
  }
  let payload: Record<string, unknown>;

  try {
    payload = (await request.json()) as Record<string, unknown>;
  } catch {
    return Response.json({ error: "Invalid JSON payload" }, { status: 400, headers: noStoreHeaders });
  }

  const validationError = validateBriefRequest(payload as Partial<BriefRequest>);

  if (validationError) {
    return Response.json({ error: validationError }, { status: 400, headers: noStoreHeaders });
  }

  const briefRequest = payload as BriefRequest;
  const brief = normalizeBriefResponse(generateDemoBrief(briefRequest), "demo");

  return Response.json(brief, {
    headers: noStoreHeaders,
  });
}
