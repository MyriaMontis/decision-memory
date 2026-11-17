"""FastAPI: GET /graph, POST /ask, POST /baseline, 静的ファイル web/。

起動: .venv/bin/uvicorn app:app --port 8000
オフライン保険: LLM呼び出しが失敗したら data/cache.json の答えを返す（固定3問）。
  OFFLINE=1 で起動すると最初からキャッシュだけで返す。
キャッシュ作成: .venv/bin/python app.py --warm
"""
import json
import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import harness

ROOT = Path(__file__).parent
CACHE = ROOT / "data" / "cache.json"
DEMO_QS = [
    "What did I end up naming the app?",
    "Did my monetization plan change? Why?",
    "Did switching AIs ever make me reverse an earlier decision?",
]

app = FastAPI()


class Q(BaseModel):
    q: str


def cached(kind: str, q: str, fn):
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    hit = cache.get(kind, {}).get(q)
    if os.environ.get("OFFLINE") == "1" and hit:
        return {**hit, "cached": True}
    try:
        return fn(q)
    except Exception as e:  # noqa: BLE001  ネットやAPIが落ちたとき
        if hit:
            return {**hit, "cached": True}
        raise e


@app.get("/graph")
def get_graph():
    return harness.graph()


@app.post("/ask")
def post_ask(body: Q):
    return cached("ask", body.q, harness.ask)


@app.post("/baseline")
def post_baseline(body: Q):
    return cached("baseline", body.q, harness.ask_baseline)


@app.get("/eval")
def get_eval():
    p = ROOT / "data" / "eval.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


@app.post("/eval/run")
def run_eval():
    import subprocess
    subprocess.run([sys.executable, str(ROOT / "scripts" / "eval.py")], check=True, timeout=240)
    return get_eval()


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")


if __name__ == "__main__" and "--warm" in sys.argv:
    out = {"ask": {}, "baseline": {}}
    for q in DEMO_QS:
        out["ask"][q] = harness.ask(q)
        out["baseline"][q] = harness.ask_baseline(q)
        print("✓", q, "|", out["ask"][q]["answer"]["en"][:80])
    CACHE.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("saved", CACHE)
