// Checks the files that GitHub Pages serves, before they are published.
//
// - Every inline <script> in index.html must parse. One syntax error stops the whole
//   app from running and leaves an empty page, so this is the check that matters most.
// - recipes.json must be valid JSON and contain an array, and every entry must have the
//   same shape as the Recipe model in recipebot/models.py (field types and patterns).
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
// Same rules as recipebot/models.py (Recipe). Keep the two in step.
const URL_RE = /^https:\/\/www\.instagram\.com\/p\/[A-Za-z0-9_-]{5,}\/$/;
const AUTHOR_RE = /^([A-Za-z0-9._]{1,30})?$/;
const SOURCE_RE = /^(https?:\/\/\S+)?$/;
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const isStr = (v) => typeof v === "string";
const filled = (v) => isStr(v) && v.trim().length > 0;
const strList = (v) => Array.isArray(v) && v.length > 0 && v.every(filled);

// Returns a list of problems for one entry (empty when it is fine).
function entryProblems(r) {
  if (!r || typeof r !== "object" || Array.isArray(r)) return ["not an object"];
  const out = [];
  ["name", "cuisine", "type"].forEach((k) => { if (!filled(r[k])) out.push(k + " must be a non-empty string"); });
  if (!isStr(r.time)) out.push("time must be a string");
  ["ingredients", "steps"].forEach((k) => { if (!strList(r[k])) out.push(k + " must be a non-empty list of non-empty strings"); });
  if (!isStr(r.url) || !URL_RE.test(r.url)) out.push("url must look like https://www.instagram.com/p/<code>/");
  if (r.author !== undefined && !(isStr(r.author) && AUTHOR_RE.test(r.author))) out.push("author must be an Instagram username (or empty)");
  if (r.source !== undefined && !(isStr(r.source) && r.source.length <= 500 && SOURCE_RE.test(r.source))) out.push("source must be an http(s) link (or empty)");
  if (typeof r.needs_review !== "boolean") out.push("needs_review must be true or false");
  if (!isStr(r.added) || !DATE_RE.test(r.added)) out.push("added must be a date like 2026-01-31");
  return out;
}

try {
  const data = JSON.parse(readFileSync(jsonPath, "utf8"));
  if (!Array.isArray(data)) fail(jsonPath + ": expected a JSON array of recipes");
  else {
    let bad = 0;
    const seen = new Map();
    data.forEach((r, i) => {
      const label = jsonPath + ": entry " + (i + 1) + (r && filled(r.name) ? ' ("' + r.name.trim() + '")' : "");
      const probs = entryProblems(r);
      if (r && isStr(r.url) && URL_RE.test(r.url)) {
        if (seen.has(r.url)) probs.push("same url as entry " + seen.get(r.url));
        else seen.set(r.url, i + 1);
      }
      if (probs.length) { bad++; fail(label + ": " + probs.join("; ")); }
    });
    if (!bad) console.log("OK    " + jsonPath + ": valid JSON, " + data.length + " recipes, all entries well formed");
  }
} catch (e) {
  fail(jsonPath + ": " + e.message);
}

process.exit(failed ? 1 : 0);
