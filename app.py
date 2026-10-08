"""Minimal Windows Paint agent: OpenAI planner, optional Laya milestone selector, Cua CLI."""
import argparse
import collections
import json
import math
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv(Path(__file__).with_name(".env"))


def env_int(key, default):
    value = int(os.getenv(key, str(default)))
    if value <= 0:
        raise ValueError(f"{key} must be positive")
    return value


def cua(tool, args):
    result = subprocess.run(
        ["cua-driver", "call", tool, json.dumps(args, ensure_ascii=False)],
        text=True, capture_output=True, encoding="utf-8", errors="replace", timeout=45,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"cua-driver {tool}: {result.stderr or result.stdout}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid cua-driver response: {result.stdout[:500]}") from exc
    if isinstance(payload, dict) and (
        payload.get("isError") or payload.get("is_error") or payload.get("ok") is False
    ):
        raise RuntimeError(f"cua-driver {tool}: {payload}")
    return payload


def make_plan(prompt, maximum):
    client = OpenAI(
        base_url=os.environ["PLANNER_BASE_URL"],
        api_key=os.getenv("PLANNER_API_KEY") or "unused",
        timeout=60,
    )
    system = (
        'Return only JSON: {"strokes":[[x1,y1,x2,y2],...]}. '
        "Integer coordinates on a normalized 1000x1000 canvas. "
        f"At most {maximum} straight lines. Simple recognizable line art; no text."
    )
    response = client.chat.completions.create(
        model=os.environ["PLANNER_MODEL"], temperature=0,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": prompt}],
    )
    raw = (response.choices[0].message.content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
    data = json.loads(raw)
    if not isinstance(data, dict) or not isinstance(data.get("strokes"), list):
        raise ValueError('Expected {"strokes": [[x1,y1,x2,y2], ...]}')
    strokes = data["strokes"]
    if not 1 <= len(strokes) <= maximum:
        raise ValueError("Unexpected number of strokes")
    for stroke in strokes:
        if not isinstance(stroke, list) or len(stroke) != 4:
            raise ValueError(f"Invalid stroke: {stroke}")
        if any(type(v) not in (int, float) or not math.isfinite(v)
               or not 0 <= v <= 1000 for v in stroke):
            raise ValueError(f"Invalid coordinate in {stroke}")
    return strokes


class LayaBudget:
    """Single-process rolling RPM/TPM budget and daily cap; not a distributed limiter."""

    def __init__(self):
        self.rpm = env_int("JEV_RPM", 6)
        self.tpm = env_int("JEV_TPM", 8192)
        self.rpd = env_int("JEV_RPD", 1000)
        self.context = env_int("JEV_CONTEXT_MAX_TOKENS", 1024)
        self.max_input = env_int("JEV_MAX_INPUT_TOKENS", 700)
        self.max_output = env_int("JEV_MAX_OUTPUT_TOKENS", 128)
        self.interval = float(os.getenv("JEV_MIN_INTERVAL_SECONDS", "11"))
        self.history = collections.deque()
        self.daily_count = 0
        self.day = date.today()
        self.last = -1e20

    @staticmethod
    def estimate_tokens(payload):
        # Conservative heuristic, NOT tokenizer-accurate; leave substantial headroom.
        return (len(json.dumps(payload, ensure_ascii=False)) + 2) // 3 + 32

    def reserve(self, payload):
        now = time.monotonic()
        today = date.today()
        if today != self.day:
            self.day, self.daily_count = today, 0
        while self.history and now - self.history[0][0] >= 60:
            self.history.popleft()
        estimate = self.estimate_tokens(payload)
        cost = estimate + self.max_output
        if estimate > self.max_input or cost > self.context:
            raise RuntimeError("Laya question exceeds conservative context budget")
        if self.daily_count >= self.rpd:
            raise RuntimeError("Laya daily limit reached")
        if now - self.last < self.interval:
            raise RuntimeError("Laya minimum interval not elapsed")
        if len(self.history) >= self.rpm:
            raise RuntimeError("Laya RPM limit reached")
        if sum(t for _, t in self.history) + cost > self.tpm:
            raise RuntimeError("Laya TPM limit reached")
        self.history.append((now, cost))
        self.daily_count += 1
        self.last = now


def laya_choose(budget, completed, remaining):
    # Milestone decision only: ask whether to continue or stop for manual inspection.
    request = {
        "state": {"task": "Draw line art in Paint",
                  "done": len(completed), "pending": len(remaining)},
        "questions": {"next_action": {
            "type": "choice",
            "instructions": "Choose continue unless manual review is needed.",
            "criteria": {
                "continue": "Proceed with the next planned strokes",
                "observe": "Pause for a human to inspect Paint",
            },
        }},
    }
    budget.reserve(request)
    url = os.environ["LAYA_BASE_URL"].rstrip("/") + "/v1/systemone"
    headers = {}
    if os.getenv("LAYA_API_KEY"):
        headers["Authorization"] = "Bearer " + os.environ["LAYA_API_KEY"]
    response = requests.post(url, json=request, headers=headers, timeout=30)
    response.raise_for_status()
    answer = response.json()["answers"]["next_action"]["choice"]
    if answer not in ("continue", "observe"):
        raise ValueError(f"Unexpected Laya choice: {answer}")
    return answer


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--prompt", default="Draw a simple house, tree and sun")
    parser.add_argument("--approve", action="store_true")
    args = parser.parse_args()
    if args.inspect:
        print(json.dumps(cua("list_windows", {}), ensure_ascii=False, indent=2))
        return
    maximum = env_int("MAX_STROKES", 60)
    strokes = make_plan(args.prompt, maximum)
    print("Planned strokes:\n" + json.dumps(strokes, indent=2))
    if os.getenv("DRY_RUN", "true").lower() != "false" or not args.approve:
        print("DRY RUN: set DRY_RUN=false and add --approve to draw.")
        return
    x, y, w, h = (int(os.environ[k]) for k in
                  ("CANVAS_X", "CANVAS_Y", "CANVAS_WIDTH", "CANVAS_HEIGHT"))
    if x < 0 or y < 0 or min(w, h) < 100:
        raise ValueError("Calibrate canvas position/size in .env")
    pid, window_id = int(os.environ["PAINT_PID"]), int(os.environ["PAINT_WINDOW_ID"])
    if min(pid, window_id) <= 0:
        raise ValueError("Set PAINT_PID and PAINT_WINDOW_ID after --inspect")
    mode = os.getenv("SELECTOR_MODE", "disabled").lower()
    if mode not in ("disabled", "laya"):
        raise ValueError("SELECTOR_MODE must be disabled or laya")
    target = {"kind": "window", "pid": pid, "window_id": window_id}
    session = "paint-drawing-session"
    # Pixel actions require a screenshot from the same session and target.
    cua("get_window_state", {"pid": pid, "window_id": window_id,
                             "session": session, "include_screenshot": True})
    budget = LayaBudget()
    milestone = env_int("JEV_MILESTONE_STROKES", 10)
    completed = []
    for i, stroke in enumerate(strokes):
        if mode == "laya" and i and i % milestone == 0:
            try:
                choice = laya_choose(budget, completed, strokes[i:])
                if choice == "observe":
                    input("Review the Paint window, then press Enter to continue...")
            except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
                print(f"Laya unavailable ({exc}); deterministic fallback.")
        coords = [
            round(x + stroke[0] * w / 1000), round(y + stroke[1] * h / 1000),
            round(x + stroke[2] * w / 1000), round(y + stroke[3] * h / 1000),
        ]
        print(f"Drawing {i + 1}/{len(strokes)}: {coords}", flush=True)
        cua("drag", {
            "target": target, "session": session, "from_x": coords[0], "from_y": coords[1],
            "to_x": coords[2], "to_y": coords[3],
            "duration_ms": 350, "steps": 20, "delivery_mode": "foreground",
        })
        completed.append(stroke)
        time.sleep(0.15)
    print(f"Sent {len(completed)} strokes. Verify the result in Paint.")


if __name__ == "__main__":
    try:
        run()
    except (OSError, RuntimeError, ValueError, KeyError, requests.RequestException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
