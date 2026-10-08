"""Claude Code MCP bridge for a Laya SystemOne choice endpoint."""
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv(Path(__file__).with_name(".env"))
mcp = FastMCP("laya")
lock = Lock()

DB = Path(os.getenv("LAYA_QUOTA_DB") or str(Path.home() / ".laya_mcp_quota.sqlite3"))
CONTEXT_LIMIT = int(os.getenv("JEV_CONTEXT_MAX_TOKENS", "1024"))
INPUT_LIMIT = int(os.getenv("JEV_MAX_INPUT_TOKENS", "700"))
OUTPUT_RESERVE = int(os.getenv("JEV_MAX_OUTPUT_TOKENS", "128"))
RPM = int(os.getenv("JEV_RPM", "6"))
TPM = int(os.getenv("JEV_TPM", "8192"))
RPD = int(os.getenv("JEV_RPD", "1000"))
MIN_INTERVAL = float(os.getenv("JEV_MIN_INTERVAL_SECONDS", "11"))
MAX_CANDIDATES = int(os.getenv("LAYA_MAX_CANDIDATES", "15"))


def token_estimate(data):
    # A safety estimate, NOT an exact model tokenizer. Use plenty of headroom.
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return len(raw.encode("utf-8")) + 48


def reserve(request):
    estimated_input = token_estimate(request)
    if estimated_input > INPUT_LIMIT or estimated_input + OUTPUT_RESERVE > CONTEXT_LIMIT:
        raise ValueError("Request exceeds conservative Laya context budget; shorten candidates")
    now = time.time()
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    DB.parent.mkdir(parents=True, exist_ok=True)
    with lock:
        with sqlite3.connect(DB, timeout=10) as db:
            db.execute("CREATE TABLE IF NOT EXISTS calls (ts REAL NOT NULL, day TEXT NOT NULL, tokens INTEGER NOT NULL)")
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM calls WHERE ts < ?", (now - 172800,))
            row = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(tokens),0), MAX(ts) FROM calls WHERE ts > ?",
                (now - 60,)
            ).fetchone()
            daily = db.execute("SELECT COUNT(*) FROM calls WHERE day=?", (day,)).fetchone()[0]
            if daily >= RPD:
                raise RuntimeError("Laya daily request budget exhausted (UTC day)")
            if row[0] >= RPM:
                raise RuntimeError("Laya RPM budget exhausted; retry later")
            if row[2] is not None and now - row[2] < MIN_INTERVAL:
                raise RuntimeError("Laya minimum call interval not elapsed")
            if row[1] + estimated_input + OUTPUT_RESERVE > TPM:
                raise RuntimeError("Laya TPM budget exhausted; retry later")
            db.execute("INSERT INTO calls VALUES (?,?,?)", (now, day, estimated_input + OUTPUT_RESERVE))
            db.commit()


@mcp.tool()
def select_ui_element(goal: str, candidates: dict[str, str], page_context: str = "") -> dict:
    """Choose among accessible UI elements. Provide stable IDs mapped to locators in Claude's browser tool.
    Only call when candidate selection is ambiguous. Never pass passwords or private page data.
    """
    if not isinstance(candidates, dict) or not 2 <= len(candidates) <= MAX_CANDIDATES:
        return {"error": f"Pass 2-{MAX_CANDIDATES} candidate IDs and short descriptions"}
    if any(not isinstance(k, str) or not k or len(k) > 50
           or not isinstance(v, str) or not v or len(v) > 160 for k, v in candidates.items()):
        return {"error": "Candidate IDs must be <=50 chars; descriptions must be 1-160 chars"}
    if len(goal) > 300 or len(page_context) > 200:
        return {"error": "Goal/page context too long"}
    payload = {
        "state": {"goal": goal, "page": page_context},
        "questions": {"target": {
            "type": "choice",
            "instructions": "Choose the best target element for the goal; select none if uncertain.",
            "criteria": dict(candidates, **{"none": "No candidate safely matches the goal"})
        }}
    }
    try:
        if "none" in candidates:
            return {"error": "Candidate ID 'none' is reserved"}
        url = os.environ["LAYA_BASE_URL"].rstrip("/") + "/v1/systemone"
        reserve(payload)
        headers = {"Content-Type": "application/json"}
        key = os.getenv("LAYA_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        response = requests.post(url, json=payload, headers=headers, timeout=25)
        response.raise_for_status()
        answer = response.json()["answers"]["target"]
        choice = answer["choice"]
        if choice not in candidates and choice != "none":
            return {"error": "Unexpected choice returned by Laya", "selected": None}
        return {
            "selected": choice if choice != "none" else None,
            "probabilities": answer.get("probabilities"),
            "confidence": answer.get("confidence"),
            "guidance": "Verify locator still matches before acting. Scores may be uncalibrated."
        }
    except (KeyError, ValueError, RuntimeError, requests.RequestException, json.JSONDecodeError) as exc:
        return {"error": str(exc), "selected": None, "guidance": "Fallback to Claude reasoning; do not retry immediately."}


if __name__ == "__main__":
    mcp.run(transport="stdio")
