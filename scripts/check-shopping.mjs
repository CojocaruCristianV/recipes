// Checks the combined shopping list (the SHOP block in index.html).
//
// - Runs the ingredient matching on fixed examples (same ingredient written in different ways,
//   amounts added up, unit conversions, vague amounts) and fails when a result changes.
// - Reads every ingredient in recipes.json and lists the lines it could not read, so new
//   recipe formats can be handled. Those lines are still shown on the list, unchanged.
//
// Usage: node scripts/check-shopping.mjs [index.html] [recipes.json] [--all]
//   --all  also prints how every ingredient in recipes.json was read.
// Exits with 1 when an example fails. No dependencies.

import { readFileSync } from "node:fs";
import vm from "node:vm";

const args = process.argv.slice(2);
const showAll = args.includes("--all");
const files = args.filter((a) => !a.startsWith("--"));
const htmlPath = files[0] || "index.html";
const jsonPath = files[1] || "recipes.json";

// ----- load the SHOP block -----
const html = readFileSync(htmlPath, "utf8");
const START = "/* ---- shopping list: combined view (start) ---- */";
const END = "/* ---- shopping list: combined view (end) ---- */";
const a = html.indexOf(START), b = html.indexOf(END);
if (a < 0 || b < a) {
  console.log("ERROR " + htmlPath + ": the combined shopping list block was not found (its start / end comments are missing)");
  process.exit(1);
}
const fold = (s) => String(s || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
const ctx = vm.createContext({ fold });
const SHOP = vm.runInContext(html.slice(a, b) + "\nSHOP;", ctx);

// ----- examples -----
let failed = 0, passed = 0;
const group = (name, items) => ({ id: name, name, items: items.map((t) => (typeof t === "string" ? { t, c: false } : t)) });
// Each case: recipes as [name, [lines]], then the expected combined lines ("Name - amount", with an em dash).
function expectList(title, recipes, want) {
  const got = SHOP.combine(recipes.map(([n, l]) => group(n, l))).map((e) => e.text);
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (ok) { passed++; return; }
  failed++;
  console.log("FAIL " + title + "\n  want: " + JSON.stringify(want) + "\n  got:  " + JSON.stringify(got));
}

expectList("eggs from two recipes are added up",
  [["A", ["2 eggs"]], ["B", ["3 eggs"]]],
  ["Eggs \u2014 5"]);
expectList("parsley in tablespoons, cups and a bunch",
  [["A", ["1 tablespoon of parsley"]], ["B", ["1 cup chopped parsley"]], ["C", ["a bunch of parsley"]]],
  ["Parsley \u2014 1 cup + 1 tbsp + 1 bunch"]);
expectList("teaspoons and tablespoons are converted",
  [["A", ["1 tbsp olive oil", "~1 tsp vanilla extract"]], ["B", ["2 tsp olive oil", "1 Tsp Vanilla Extract"]]],
  ["Olive oil \u2014 1 tbsp + 2 tsp", "Vanilla extract \u2014 ~2 tsp"]);
expectList("grams, kilograms and estimates",
  [["A", ["~170 g butter"]], ["B", ["150g Unsalted Butter, softened", "2 tbsp butter"]], ["C", ["1 kg flour", "250 g plain flour"]]],
  ["All purpose flour \u2014 1.25 kg", "Butter \u2014 2 tbsp + ~170 g", "Unsalted butter \u2014 150 g"]);
expectList("salt and pepper, to taste, pinches",
  [["A", ["Salt, to taste", "salt and pepper"]], ["B", ["1 pinch salt", "Large pinch of Salt", "Black pepper (to taste)"]]],
  ["Black pepper \u2014 to taste", "Salt \u2014 2 pinches + to taste"]);
expectList("garlic cloves written three ways",
  [["A", ["3 cloves garlic, minced (~10 g)"]], ["B", ["2 garlic cloves, minced"]], ["C", ["~3 cloves garlic, sliced", "1 whole head garlic, halved"]]],
  ["Garlic \u2014 ~8 cloves + 1 head"]);
expectList("spring onions, green onions and scallions",
  [["A", ["4 spring onions, finely chopped"]], ["B", ["2 green onions"]], ["C", ["green onions, generous amount", "1 bunch scallions"]]],
  ["Spring onions \u2014 6 + 1 bunch + generous amount"]);
expectList("ranges count the larger number, fractions and lemon juice",
  [["A", ["4-5 medium carrots, sliced (270 g)", "1/2 cup water", "juice of 1 lemon"]],
   ["B", ["1 1/4 cups water", "\u00bd cup water", "3 tbsp lemon juice", "Juice of 2 lemons"]]],
  ["Carrots \u2014 5", "Lemon juice \u2014 3 tbsp + 3 lemons", "Water \u2014 2 1/4 cups"]);
expectList("egg yolks stay separate from eggs; singular when one",
  [["A", ["3 Egg Yolks", "1 fried egg"]], ["B", ["7 egg yolks"]]],
  ["Egg \u2014 1", "Egg yolks \u2014 10"]);
expectList("lines that cannot be read are kept, and merged when they are the same",
  [["A", ["TBD: spices", "Salt for boiling"]], ["B", ["TBD: spices"]]],
  ["Salt \u2014 for boiling", "TBD: spices"]);
expectList("ticked lines are not counted until every line is ticked",
  [["A", [{ t: "2 eggs", c: true }, "1 cup milk"]], ["B", ["3 eggs", { t: "1 cup milk", c: true }]]],
  ["Eggs \u2014 3", "Milk \u2014 1 cup"]);

// Ticking state and sources.
{
  const list = SHOP.combine([group("A", [{ t: "2 eggs", c: true }]), group("B", [{ t: "3 eggs", c: true }, "1 onion"])]);
  const eggs = list.find((e) => e.key === "egg");
  const onion = list.find((e) => e.key === "onion");
  const ok = eggs && eggs.done && eggs.text === "Eggs \u2014 5" && JSON.stringify(eggs.sources) === '["A","B"]' &&
    JSON.stringify(eggs.refs) === "[[0,0],[1,0]]" && onion && !onion.done;
  if (ok) passed++; else { failed++; console.log("FAIL done / sources / refs: " + JSON.stringify(list)); }
}

// Amount formatting.
[[1.25, "1 1/4"], [0.333, "1/3"], [2, "2"], [0.5, "1/2"], [2.99, "3"], [1.4, "1.4"]].forEach(([x, want]) => {
  const got = SHOP.fmtNum(x);
  if (got === want) passed++; else { failed++; console.log("FAIL fmtNum(" + x + ") = " + got + ", want " + want); }
});

console.log(passed + " passed, " + failed + " failed");

// ----- every ingredient in recipes.json -----
let recipes = [];
try { recipes = JSON.parse(readFileSync(jsonPath, "utf8")); } catch (e) { console.log("Skipping " + jsonPath + ": " + e.message); }
if (Array.isArray(recipes) && recipes.length) {
  const lines = recipes.flatMap((r) => (Array.isArray(r.ingredients) ? r.ingredients : []));
  const unread = lines.filter((l) => SHOP.parseLine(l).some((p) => p.raw));
  if (showAll) {
    lines.forEach((l) => {
      const parts = SHOP.parseLine(l).map((p) => p.raw ? "(kept as is)" :
        p.key + " | " + (p.qty == null ? "-" : p.qty) + " " + (p.u ? p.u.unit : "") + (p.notes.length ? " | " + p.notes.join(", ") : ""));
      console.log(JSON.stringify(l) + "\n    " + parts.join("\n    "));
    });
  }
  const merged = SHOP.combine(recipes.map((r, i) => group(r.name || "Recipe " + i, Array.isArray(r.ingredients) ? r.ingredients : [])));
  const shared = merged.filter((e) => e.sources.length > 1);
  console.log(lines.length + " ingredient lines in " + jsonPath + ": " + merged.length + " combined entries, " +
    shared.length + " used by more than one recipe, " + unread.length + " kept as is");
  shared.forEach((e) => console.log("  " + e.text + "   (" + e.sources.length + " recipes)"));
  unread.forEach((l) => console.log("  kept as is: " + l));
}

process.exit(failed ? 1 : 0);
