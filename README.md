# Paint + Laya RL Agent + Cua Driver (Windows 10)

Proof of concept for drawing line art in Microsoft Paint using an OpenAI-compatible internal LLM planner, optional Laya SystemOne decision API, and Cua Driver.

## Safety and limitations

- Only straight-line strokes; select the Pencil tool and color manually first.
- Screen/canvas coordinates are **window-local** and require calibration.
- No API keys or internal hostnames are included. Keep `.env` local.
- Laya is **optional**: the default deterministic mode works without Laya.
- `laya-rl-agent` API compatibility with `/v1/systemone` must be confirmed on your server.
- The Cua Driver CLI and Paint integration have **not been end-to-end verified on Windows 10**.

## Setup

1. Install Python 3.11 or later and Cua Driver per https://cua.ai/driver.
2. Open PowerShell:

```powershell
cua-driver doctor
cua-driver autostart kick
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
Start-Process mspaint.exe
python app.py --inspect
```

3. Edit `.env`: internal LLM URL, model, key, Paint PID/window ID, and canvas coordinates.
4. Select Pencil in Paint and test:

```powershell
python app.py --prompt "Draw a simple house and sun"
```

5. After reviewing the planned strokes, set `DRY_RUN=false` in `.env` and run:

```powershell
python app.py --prompt "Draw a simple house and sun" --approve
```

## Laya API (optional)

Set `SELECTOR_MODE=laya`, `LAYA_BASE_URL`, and (if necessary) `LAYA_API_KEY`. The adapter currently expects `POST /v1/systemone` with a `questions.next_action` choice and `answers.next_action.choice` response. This is an **integration assumption** pending confirmation of your internal deployment.

Laya is invoked at milestones only (default every 10 strokes), not for every mouse movement. The budget limiter uses a conservative sliding 60-second window, an input/output token estimate, an 11-second minimum interval, and an in-memory daily counter. These limits apply to this single process; for **shared** API usage, move quotas into a central server-side limiter. The 1,024-token context limit is interpreted conservatively as a **total per-question budget** until verified.

Laya exceptions or quota exhaustion fall back to deterministic stroke order. Rate-limit retries are not automatically repeated; a failure consumes a budget reservation to avoid hammering the endpoint. No automatic visual correctness check is implemented.

## Environment

See `.env.example`. Avoid committing `.env`, screenshots of internal apps, or server secrets.

## Architecture

`user prompt -> internal OpenAI-compatible LLM -> validated normalized strokes -> optional Laya milestone choice -> Cua Driver CLI -> Windows Paint`
