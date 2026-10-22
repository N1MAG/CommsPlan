import fs from "node:fs";
import path from "node:path";
import process from "node:process";

const siteRoot = path.resolve(".vitepress/dist");
const expectedHtmlPages = [
  "404.html",
  "index.html",
  "start-here.html",
  "install/index.html",
  "guide/first-radio.html",
  "guide/operating-picture.html",
  "guide/whats-new.html",
  "integrations/mesh.html",
  "support.html",
];
const forbiddenText = [
  "docs/internal",
  "internal-testing",
  "private-testing",
  "production-release",
];
const legacyProductName = /freq(?:in)?out/i;
const htmlFiles = [];
const allFiles = [];
const failures = [];

function walk(directory) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const fullPath = path.join(directory, entry.name);
    const relativePath = path.relative(siteRoot, fullPath);
    const stat = fs.lstatSync(fullPath);

    if (stat.isSymbolicLink()) {
      failures.push(`Generated artifact contains a symbolic link: ${relativePath}`);
      continue;
    }
    if (entry.isDirectory()) {
      walk(fullPath);
      continue;
    }
    allFiles.push(fullPath);
    if (entry.name.endsWith(".html")) htmlFiles.push(fullPath);
  }
}

function htmlCandidates(currentFile, rawTarget) {
  const [target, fragment] = rawTarget.split("#", 2);
  const cleanTarget = (target || "").split("?", 1)[0];
  const base = cleanTarget.startsWith("/CommsPlan/")
    ? path.join(siteRoot, cleanTarget.slice("/CommsPlan/".length))
    : cleanTarget.startsWith("/")
      ? path.join(siteRoot, cleanTarget.slice(1))
      : cleanTarget
        ? path.resolve(path.dirname(currentFile), cleanTarget)
        : currentFile;
  const candidates = new Set([base]);
  if (!path.extname(base)) {
    candidates.add(`${base}.html`);
    candidates.add(path.join(base, "index.html"));
  }
  return { candidates: [...candidates], fragment: fragment || "" };
}

function hasFragment(file, fragment) {
  if (!fragment) return true;
  const source = fs.readFileSync(file, "utf8");
  return [...source.matchAll(/\b(?:id|name)="([^"]+)"/g)].some(
    ([, value]) => value === fragment,
  );
}

if (!fs.existsSync(siteRoot)) {
  console.error(`Generated site does not exist: ${siteRoot}`);
  process.exit(1);
}

walk(siteRoot);

const actualHtmlPages = htmlFiles.map((file) => path.relative(siteRoot, file)).sort();
const expectedHtmlSet = new Set(expectedHtmlPages);
const actualHtmlSet = new Set(actualHtmlPages);

for (const expected of expectedHtmlPages) {
  if (!actualHtmlSet.has(expected)) failures.push(`Expected generated page is missing: ${expected}`);
}
for (const actual of actualHtmlPages) {
  if (!expectedHtmlSet.has(actual)) failures.push(`Unexpected generated HTML page: ${actual}`);
}

for (const file of allFiles) {
  if (!/\.(?:html|js|css|json|txt|xml)$/i.test(file)) continue;
  const source = fs.readFileSync(file, "utf8");
  if (legacyProductName.test(source)) {
    failures.push(`${path.relative(siteRoot, file)} contains a retired product name`);
  }
  for (const forbidden of forbiddenText) {
    if (source.includes(forbidden)) {
      failures.push(
        `${path.relative(siteRoot, file)} contains forbidden internal marker: ${forbidden}`,
      );
    }
  }
}

for (const file of htmlFiles) {
  const source = fs.readFileSync(file, "utf8");
  for (const match of source.matchAll(/(href|src)="([^"]+)"/g)) {
    const attribute = match[1];
    const value = match[2];
    if (/^(?:https?:|mailto:|data:)/.test(value)) continue;
    if (value.startsWith("/") && !value.startsWith("/CommsPlan/")) {
      failures.push(`${path.relative(siteRoot, file)} escapes the /CommsPlan/ site base: ${value}`);
      continue;
    }

    const { candidates, fragment } = htmlCandidates(file, value);
    const existing = candidates.find((candidate) => fs.existsSync(candidate));
    if (!existing) {
      failures.push(`${path.relative(siteRoot, file)} has unresolved target: ${value}`);
      continue;
    }
    if (attribute === "href" && fragment && !hasFragment(existing, fragment)) {
      failures.push(
        `${path.relative(siteRoot, file)} has unresolved fragment: ${value}`,
      );
    }
  }
}

if (failures.length) {
  console.error(failures.join("\n"));
  process.exit(1);
}

console.log(
  `Verified curated Pages artifact: ${htmlFiles.length} HTML pages, ${allFiles.length} total files.`,
);
