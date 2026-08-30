import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const skipped = new Set([".git", "node_modules", ".venv", "venv", "dist", "work", "outputs", ".artifacts", "coverage", "test-results", "playwright-report", "__pycache__"]);
function walk(directory) {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    if (skipped.has(entry.name)) return [];
    const absolute = path.join(directory, entry.name);
    return entry.isDirectory() ? walk(absolute) : [path.relative(root, absolute).replaceAll("\\", "/")];
  });
}

let files;
try {
  const gitRoot = execFileSync("git", ["rev-parse", "--show-toplevel"], { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
  if (path.resolve(gitRoot).toLowerCase() !== path.resolve(root).toLowerCase()) throw new Error("Not this repository");
  files = execFileSync("git", ["ls-files", "-z", "--cached", "--others", "--exclude-standard"], { cwd: root, encoding: "utf8" }).split("\0").filter(Boolean);
} catch {
  files = walk(root);
}
files = [...new Set(files)].filter((file) => existsSync(path.join(root, file)));

const rules = [
  ["AWS access key", /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g],
  ["GitHub token", /\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b/g],
  ["Private key", /-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----/g],
  ["Signed download URL", /X-Amz-Signature=[a-f0-9]{32,}/gi],
  ["Hard-coded AWS secret", /["'](?:SecretAccessKey|secretAccessKey)["']\s*:\s*["'][A-Za-z0-9/+]{40}["']/g],
  ["Machine-specific home path", /(?:[A-Z]:[\\/]+Users[\\/]+[A-Za-z][^\s"']*|\/Users\/[A-Za-z][^\s"']*)/g],
];
const publicExampleKeys = new Set(["AKIAIOSFODNN7EXAMPLE"]);
const findings = [];
for (const file of files) {
  if (/(^|\/)(?:\.env(?!\.example$)[^/]*|credentials[^/]*|\.aws|\.aws-sam)(\/|$)|\.(?:pem|key|p12|pfx)$/i.test(file)) {
    findings.push(`${file}: private configuration or key file`);
  }
  if (/\.(?:png|jpe?g|gif|webp|ico|mp3|wav|woff2?)$/i.test(file)) continue;
  const text = readFileSync(path.join(root, file), "utf8");
  for (const [label, expression] of rules) {
    expression.lastIndex = 0;
    for (const match of text.matchAll(expression)) {
      if (label === "AWS access key" && publicExampleKeys.has(match[0])) continue;
      const line = text.slice(0, match.index).split("\n").length;
      findings.push(`${file}:${line}: ${label}`);
    }
  }
  if (!file.endsWith(".md")) continue;
  for (const match of text.matchAll(/!?\[[^\]]*\]\(([^\n)]+)\)/g)) {
    const target = match[1].replace(/^<|>$/g, "");
    if (/^(?:https?:|mailto:|#)/i.test(target)) continue;
    const linkPath = decodeURIComponent(target.split("#")[0]);
    if (linkPath && !existsSync(path.resolve(root, path.dirname(file), linkPath))) findings.push(`${file}: broken local link to ${linkPath}`);
  }
}

if (findings.length) {
  console.error(`Publication check found ${findings.length} issue(s). Values are intentionally omitted:\n${findings.join("\n")}`);
  process.exitCode = 1;
} else {
  console.log(`Publication check passed for ${files.length} source files. Review staged changes before publishing.`);
}
