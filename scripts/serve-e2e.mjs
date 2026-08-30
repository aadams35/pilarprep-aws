import { spawn, spawnSync } from "node:child_process";

const isWindows = process.platform === "win32";
const commandOptions = {
  env: process.env,
  stdio: "inherit",
};

const command = (windowsCommand, unixCommand, args) =>
  isWindows
    ? {
        executable: process.env.ComSpec ?? "cmd.exe",
        args: ["/d", "/s", "/c", `${windowsCommand} ${args.join(" ")}`],
      }
    : { executable: unixCommand, args };

const buildCommand = command("npm.cmd", "npm", ["run", "build"]);

const build = spawnSync(
  buildCommand.executable,
  buildCommand.args,
  commandOptions
);

if (build.status !== 0) {
  process.exit(build.status ?? 1);
}

const previewArgs = [
    "vite",
    "preview",
    "--config",
    "vite.config.ts",
    "--host",
    "127.0.0.1",
    "--port",
    "4173",
  ];
const previewCommand = command("npx.cmd", "npx", previewArgs);
const server = spawn(
  previewCommand.executable,
  previewCommand.args,
  commandOptions
);

const stop = (signal) => {
  if (!server.killed) server.kill(signal);
};

process.on("SIGINT", () => stop("SIGINT"));
process.on("SIGTERM", () => stop("SIGTERM"));
server.on("exit", (code) => process.exit(code ?? 0));
