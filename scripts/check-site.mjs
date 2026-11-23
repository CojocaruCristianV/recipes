// Checks the files that GitHub Pages serves, before they are published.
//
// - Every inline <script> in index.html must parse. One syntax error stops the whole
//   app from running and leaves an empty page, so this is the check that matters most.
// - recipes.json must be valid JSON and contain an array.
//
// Usage: node scripts/check-site.mjs [index.html] [recipes.json]
// Exits with 1 and prints the file line of the problem when something is wrong.
// No dependencies: it only uses Node's built-in parser (vm.Script compiles without running).

import { readFileSync } from "node:fs";
import vm from "node:vm";

const htmlPath = process.argv[2] || "index.html";
const jsonPath = process.argv[3] || "recipes.json";
let failed = false;

function fail(msg) {
  failed = true;
  // The ::error:: prefix shows the message as an annotation in the GitHub Actions run.
  console.log((process.env.GITHUB_ACTIONS ? "::error::" : "ERROR ") + msg);
}

// ----- index.html: inline scripts -----
const html = readFileSync(htmlPath, "utf8");
const lineAt = (offset) => html.slice(0, offset).split("\n").length;
// Inline scripts only (no src attribute). JSON or template blocks are skipped.
const re = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
let count = 0;
for (let m; (m = re.exec(html)); ) {
  const attrs = m[1];
  if (/\bsrc\s*=/i.test(attrs)) continue;
  const type = (attrs.match(/\btype\s*=\s*["']?([^"'\s>]+)/i) || [])[1];
  if (type && !/^(text|application)\/javascript$|^module$/i.test(type)) continue;
  count++;
  const code = m[2];
  const startLine = lineAt(m.index + m[0].indexOf(">") + 1); // file line where the code starts
  try {
    // Modules cannot be compiled by vm.Script; this app only uses classic scripts.
    new vm.Script(code, { filename: htmlPath, lineOffset: startLine - 1 });
    console.log("OK    " + htmlPath + ": script " + count + " parses (" + code.length + " chars, from line " + startLine + ")");
  } catch (e) {
    // The stack's first line is "file:line"; the line already includes lineOffset.
    const where = (String(e.stack).match(new RegExp(htmlPath.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ":(\\d+)")) || [])[1];
    const line = where ? +where : null;
    const src = line ? html.split("\n")[line - 1] : "";
    fail(htmlPath + (line ? ":" + line : "") + ": " + e.name + ": " + e.message + (src ? "\n      " + src.trim() : ""));
  }
}
if (!count) fail(htmlPath + ": no inline <script> found");

// ----- recipes.json -----
try {
  const data = JSON.parse(readFileSync(jsonPath, "utf8"));
  if (!Array.isArray(data)) fail(jsonPath + ": expected a JSON array of recipes");
  else console.log("OK    " + jsonPath + ": valid JSON, " + data.length + " recipes");
} catch (e) {
  fail(jsonPath + ": " + e.message);
}

process.exit(failed ? 1 : 0);
