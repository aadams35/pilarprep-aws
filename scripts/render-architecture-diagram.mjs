import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { chromium } from "@playwright/test";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");
const svgPath = path.join(
  root,
  "docs",
  "architecture",
  "pilarprep-aws-architecture.svg",
);
const pngPath = path.join(
  root,
  "docs",
  "architecture",
  "pilarprep-aws-architecture.png",
);
const svg = await readFile(svgPath, "utf8");
const viewBox = svg.match(/viewBox="\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*"/);

if (!viewBox) {
  throw new Error("The architecture SVG requires a numeric viewBox.");
}

const scale = 2;
const width = Math.round(Number(viewBox[3]) * scale);
const height = Math.round(Number(viewBox[4]) * scale);
const browser = await chromium.launch({ headless: true });

try {
  const page = await browser.newPage({
    viewport: { width, height },
    deviceScaleFactor: 1,
  });
  await page.goto(pathToFileURL(svgPath).href, { waitUntil: "load" });
  await page.evaluate(
    ({ renderWidth, renderHeight }) => {
      const rootSvg = document.documentElement;
      rootSvg.setAttribute("width", String(renderWidth));
      rootSvg.setAttribute("height", String(renderHeight));
    },
    { renderWidth: width, renderHeight: height },
  );
  await page.evaluate(() => document.fonts.ready);
  await page.screenshot({
    path: pngPath,
    clip: { x: 0, y: 0, width, height },
    animations: "disabled",
  });
} finally {
  await browser.close();
}

console.log(
  `Rendered docs/architecture/pilarprep-aws-architecture.png at ${width}x${height}.`,
);
