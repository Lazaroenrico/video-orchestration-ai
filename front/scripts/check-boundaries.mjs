import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const root = new URL("..", import.meta.url).pathname;
const src = join(root, "src");
const blocked = [
  /src\/orchestrator/,
  /\borchestrator\./,
  /from\s+["'][^"']*orchestrator/,
  /src\/orchestrator\/web\/server\.py/,
];

function files(dir) {
  return readdirSync(dir).flatMap((entry) => {
    const path = join(dir, entry);
    const stat = statSync(path);
    if (stat.isDirectory()) return files(path);
    return /\.(ts|tsx|js|jsx)$/.test(entry) ? [path] : [];
  });
}

const failures = [];
for (const path of files(src)) {
  const text = readFileSync(path, "utf8");
  for (const pattern of blocked) {
    if (pattern.test(text)) {
      failures.push(`${relative(root, path)} matches ${pattern}`);
    }
  }
}

if (failures.length) {
  console.error("Frontend boundary violation:");
  for (const failure of failures) console.error(`- ${failure}`);
  process.exit(1);
}

console.log("Frontend boundaries OK");
