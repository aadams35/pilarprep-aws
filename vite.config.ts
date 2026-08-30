import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import { localBriefApi } from "./frontend/dev/vite-brief-api";

const projectRoot = fileURLToPath(new URL(".", import.meta.url));
const contentSecurityPolicy = [
  "default-src 'self'", "base-uri 'self'", "object-src 'none'",
  "script-src 'self'", "style-src 'self'", "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self' https://*.amazonaws.com https://*.amazoncognito.com",
  "form-action 'self'", "upgrade-insecure-requests",
].join("; ");

export default defineConfig({
  root: "frontend",
  publicDir: "public",
  envDir: projectRoot,
  envPrefix: ["VITE_"],
  plugins: [
    react(),
    localBriefApi(),
    {
      name: "production-security-policy",
      apply: "build",
      transformIndexHtml: () => [{
        tag: "meta",
        attrs: { "http-equiv": "Content-Security-Policy", content: contentSecurityPolicy },
        injectTo: "head-prepend",
      }],
    },
  ],
  resolve: { alias: { "@": fileURLToPath(new URL("frontend/src", import.meta.url)) } },
  build: { outDir: "../dist/aws-frontend", emptyOutDir: true },
});
