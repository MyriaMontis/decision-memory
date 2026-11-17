"""共通: .env の読み込み、LLMクライアント、パス。"""
import json
import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
load_dotenv(ROOT / ".env")

VENDOR_LABEL = {"chipgpt": "Chip GPT", "crowd": "Crowd", "gemmy": "Gemmy"}


def llm() -> OpenAI:
    # 会場Wi-Fiが固まっても10秒で諦めて、キャッシュに切り替えられるように
    return OpenAI(base_url=os.environ["AI_BASE_URL"], api_key=os.environ["AI_API_KEY"], timeout=10, max_retries=0)


def model() -> str:
    return os.environ["AI_MODEL"]


def timeline() -> dict:
    return yaml.safe_load((DATA / "timeline.yaml").read_text(encoding="utf-8"))


def chat_json(prompt: str, system: str = "JSONだけを返す。", temperature: float = 0.7) -> dict:
    """LLMにJSONを返させる。コードブロックで返ってきても剥がす。"""
    for attempt in range(3):  # 壊れたJSONが返ったら取り直す
        r = llm().chat.completions.create(
            model=model(),
            messages=[{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            temperature=temperature,
        )
        text = r.choices[0].message.content or ""
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(m.group(0) if m else text)
        except json.JSONDecodeError:
            if attempt == 2:
                raise
