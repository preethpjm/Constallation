#!/usr/bin/env node
// bom-compare: compares a purchasing BOM (CSV) with parts extracted from CAD (JSON).
// written by the ops team, no docs beyond this comment.
//   node index.js <bom.csv> <parts.json> <outDir>
const fs = require("fs");
const path = require("path");

const [bomPath, partsPath, outDir] = process.argv.slice(2);
if (!bomPath || !partsPath || !outDir) {
  console.error("usage: node index.js <bom.csv> <parts.json> <outDir>");
  process.exit(2);
}

const lines = fs.readFileSync(bomPath, "utf8").trim().split(/\r?\n/);
const header = lines.shift().split(",").map((h) => h.trim().toLowerCase());
const bom = lines.map((l) => {
  const c = l.split(",");
  const r = {};
  header.forEach((h, i) => (r[h] = (c[i] || "").trim()));
  return r;
});
const cad = JSON.parse(fs.readFileSync(partsPath, "utf8")).parts || [];
const cadMap = Object.fromEntries(cad.map((p) => [p.part_number, p]));

const rows = [];
const seen = new Set();
for (const b of bom) {
  const pn = b.part_number;
  seen.add(pn);
  const c = cadMap[pn];
  const bq = Number(b.quantity || 0);
  const cq = c ? c.quantity : 0;
  rows.push({
    part_number: pn,
    name: c ? c.name : b.description || "",
    bom_qty: bq,
    cad_qty: cq,
    unit_price: Number(b.unit_price || 0),
    supplier: b.supplier || "",
    status: !c ? "missing_in_cad" : bq === cq ? "match" : "qty_mismatch",
  });
}
for (const c of cad) {
  if (!seen.has(c.part_number))
    rows.push({ part_number: c.part_number, name: c.name, bom_qty: 0, cad_qty: c.quantity,
      unit_price: 0, supplier: "", status: "missing_in_bom" });
}
const total = rows.reduce((s, r) => s + r.unit_price * Math.max(r.bom_qty, r.cad_qty), 0);
fs.mkdirSync(outDir, { recursive: true });
fs.writeFileSync(path.join(outDir, "comparison.json"),
  JSON.stringify({ total_cost: Number(total.toFixed(2)), rows }, null, 2));
const csv = ["part_number,name,bom_qty,cad_qty,unit_price,supplier,status"]
  .concat(rows.map((r) => [r.part_number, r.name, r.bom_qty, r.cad_qty, r.unit_price, r.supplier, r.status].join(",")));
fs.writeFileSync(path.join(outDir, "report.csv"), csv.join("\n") + "\n");
console.log(`compared ${rows.length} rows, total ${total.toFixed(2)}`);
