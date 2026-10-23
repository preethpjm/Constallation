#!/usr/bin/env bash
# make_listing.sh REPORT_CSV OUTPUT_HTML [TITLE]
# Builds an HTML affiliate/parts listing page from a BOM comparison report.
set -euo pipefail
if [ "$#" -lt 2 ]; then echo "usage: $0 REPORT_CSV OUTPUT_HTML [TITLE]" >&2; exit 2; fi
IN="$1"; OUT="$2"; TITLE="${3:-Parts Listing}"
{
  echo "<!doctype html><html><head><meta charset='utf-8'><title>$TITLE</title>"
  echo "<style>body{font-family:sans-serif;margin:2rem}td,th{padding:6px 10px;border-bottom:1px solid #ddd}.bad{color:#b00}</style></head><body>"
  echo "<h1>$TITLE</h1><table><tr><th>Part</th><th>Name</th><th>Qty</th><th>Price</th><th>Supplier</th><th>Status</th><th>Buy</th></tr>"
  tail -n +2 "$IN" | while IFS=, read -r pn name bq cq price sup status; do
    qty=$(( bq > cq ? bq : cq ))
    cls=""; [ "$status" != "match" ] && cls="bad"
    link="https://shop.example.com/search?q=${pn}&ref=affiliate-001"
    echo "<tr><td>$pn</td><td>$name</td><td>$qty</td><td>$price</td><td>$sup</td><td class='$cls'>$status</td><td><a href='$link'>buy</a></td></tr>"
  done
  echo "</table></body></html>"
} > "$OUT"
echo "listing written to $OUT"
