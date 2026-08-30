import type { Plugin } from "vite";
import { GET, POST } from "./brief-api";

// This deterministic endpoint exists only in the local Vite server, never in the AWS build.
export function localBriefApi(): Plugin {
  return {
    name: "local-brief-api",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use(async (request, response, next) => {
        if (request.url?.split("?")[0] !== "/api/brief") return next();
        if (request.method !== "GET" && request.method !== "POST") {
          response.writeHead(405, { Allow: "GET, POST" }).end();
          return;
        }
        try {
          const chunks: Buffer[] = [];
          let size = 0;
          for await (const chunk of request) {
            size += Buffer.byteLength(chunk);
            if (size > 1024 * 1024) {
              response.writeHead(413).end("Request too large");
              return;
            }
            chunks.push(Buffer.from(chunk));
          }
          const headers = new Headers();
          for (const [name, value] of Object.entries(request.headers)) {
            if (value !== undefined) headers.set(name, Array.isArray(value) ? value.join(", ") : value);
          }
          const result = request.method === "GET" ? await GET() : await POST(
            new Request("http://127.0.0.1/api/brief", {
              method: "POST", headers, body: Buffer.concat(chunks).toString("utf8"),
            }),
          );
          response.writeHead(result.status, Object.fromEntries(result.headers));
          response.end(Buffer.from(await result.arrayBuffer()));
        } catch {
          response.writeHead(500, { "content-type": "application/json" });
          response.end(JSON.stringify({ error: "Local brief generation failed." }));
        }
      });
    },
  };
}
