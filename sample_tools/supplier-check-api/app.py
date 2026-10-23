"""Supplier risk checker microservice (owned by procurement).
Start: uvicorn app:app --port 8077
"""
import csv
import io

from fastapi import FastAPI, File, UploadFile

app = FastAPI(title="supplier-check", description="Scores supplier risk for each row of a parts report CSV")

RISK = {"Acme Metals": 0.1, "FastenCo": 0.3, "SealPro": 0.6}


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/score")
async def score(report: UploadFile = File(..., description="parts report csv with a supplier column")):
    text = (await report.read()).decode()
    rows = list(csv.DictReader(io.StringIO(text)))
    scored = [{"part_number": r.get("part_number"), "supplier": r.get("supplier"),
               "risk": RISK.get(r.get("supplier", ""), 0.9)} for r in rows]
    return {"suppliers": scored, "max_risk": max((s["risk"] for s in scored), default=0)}
