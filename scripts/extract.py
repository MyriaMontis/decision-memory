"""会社ごとの会話ログ → 決定JSON(data/extracted.json)。

3社の形式を共通形に揃え、会話ごとにLLMで「決定」だけを抜き出す。
topic は固定リストから選ばせる（表記ゆれ防止）。
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from datetime import datetime, timezone

from common import DATA, VENDOR_LABEL, chat_json, timeline

PROMPT = """あなたは会話ログから「決定」だけを抽出する。
決定 = Bobが自分の読書記録アプリの「方針・仕様」として明確に選んだ・変更したこと。
含めないもの: 迷い・検討だけで結論がないもの／AIの提案だけのもの／雑談・趣味の話／
エラー対応などその場限りの作業（コマンドを実行する等）／トピックのどれにも本当は当てはまらないもの。
topic は次のリストから1つ選ぶ: {topics}
statement は「〜にする」「〜に変更する」の形で短く。reason はBobが挙げた理由を短く。
evidence には、決めたことが分かるBobの発言を会話から一字一句そのまま抜き出す（20〜60字）。
statement_en / reason_en には statement / reason の短い英訳を入れる（例: "Rename the app to Margin"）。
JSONだけを返す: {{"decisions":[{{"topic":"...","statement":"...","reason":"...","evidence":"...","statement_en":"...","reason_en":"..."}}]}}
決定がなければ {{"decisions":[]}}

--- 会話（{vendor}, {date}）---
{text}"""


def normalize() -> list[dict]:
    """3社の形式 → {id, vendor, date, title, messages:[{role,text}]}"""
    convs = []
    for c in json.loads((DATA / "chipgpt.json").read_text(encoding="utf-8")):
        convs.append({"id": c["id"], "vendor": "chipgpt", "title": c["title"],
                      "date": datetime.fromtimestamp(c["create_time"], timezone.utc).date().isoformat(),
                      "messages": c["messages"]})
    for c in json.loads((DATA / "crowd.json").read_text(encoding="utf-8")):
        convs.append({"id": c["uuid"], "vendor": "crowd", "title": c["name"], "date": c["created_at"][:10],
                      "messages": [{"role": "user" if m["sender"] == "human" else "assistant", "text": m["text"]}
                                   for m in c["chat_messages"]]})
    for c in json.loads((DATA / "gemmy.json").read_text(encoding="utf-8")):
        convs.append({"id": c["id"], "vendor": "gemmy", "title": c["title"], "date": c["date"],
                      "messages": [{"role": "user" if m["who"] == "user" else "assistant", "text": m["text"]}
                                   for m in c["turns"]]})
    return sorted(convs, key=lambda c: c["date"])


def _norm(s: str) -> str:
    return re.sub(r"[\s、。，．,.!！?？「」『』（）()\"'…ー〜~]", "", s)


def quoted(evidence: str, source: str) -> bool:
    """引用がBobの発言に本当にあるか（句読点を無視して10字以上連続一致）。"""
    ev, src = _norm(evidence), _norm(source)
    if len(ev) < 6:
        return False
    m = SequenceMatcher(None, ev, src, autojunk=False).find_longest_match(0, len(ev), 0, len(src))
    return m.size >= min(10, len(ev))


VERIFY = """次の会話で、Bobは読書記録アプリの「{topic}」について「{statement}」と最終的に決めましたか？
yes の条件: Bob自身がアプリの方針として結論を出し、採用している（「仮に」「ひとまず」でも採用したなら yes）。
no の条件: 迷っているだけ／遊び・雑談／一時的な作業／AIが提案しただけ／アプリの方針ではない。
JSONだけを返す: {{"verdict": "yes" | "no", "why": "短く"}}

--- 会話 ---
{text}"""


NAIVE_PROMPT = """あなたは会話ログから「決定」だけを抽出する。
決定 = ユーザーが明確に選んだ・変更した・やめたこと。迷い・検討・雑談・AIの提案だけのものは含めない。
topic は次のリストから必ず1つ選ぶ: {topics}
statement_en には statement の短い英訳を入れる。
JSONだけを返す: {{"decisions":[{{"topic":"...","statement":"...","reason":"...","statement_en":"..."}}]}}
決定がなければ {{"decisions":[]}}

--- 会話（{vendor}, {date}）---
{text}"""


def verified(d: dict, text: str) -> tuple[bool, str]:
    """ハーネスの検証2段目: 別のLLM呼び出しで、候補が本当に最終決定かを判定させる。"""
    out = chat_json(VERIFY.format(topic=d["topic"], statement=d["statement"], text=text), temperature=0)
    return out.get("verdict") == "yes", out.get("why", "")


def extract_one(conv: dict, topics: list[str], naive: bool = False) -> dict:
    text = "\n".join(f"{'Bob' if m['role'] == 'user' else VENDOR_LABEL[conv['vendor']]}: {m['text']}"
                     for m in conv["messages"])
    if naive:  # ハーネスなし: 素朴なプロンプト1回だけ、検証なし
        out = chat_json(NAIVE_PROMPT.format(topics="、".join(topics), vendor=VENDOR_LABEL[conv["vendor"]],
                                            date=conv["date"], text=text), temperature=0)
        return {**conv, "decisions": [d for d in out.get("decisions", []) if d.get("topic") in topics], "rejected": []}
    out = chat_json(PROMPT.format(topics="、".join(topics), vendor=VENDOR_LABEL[conv["vendor"]],
                                  date=conv["date"], text=text), temperature=0)
    bob = "".join(m["text"] for m in conv["messages"] if m["role"] == "user")
    ds, rejected = [], []
    for d in out.get("decisions", []):
        # ハーネスの検証: topic が固定リストにあり、根拠の引用が本当にBobの発言にあるか
        if d.get("topic") not in topics or not quoted(d.get("evidence") or "", bob):
            rejected.append({**d, "why": "引用が会話にない／トピック外"})
            continue
        ok, why = verified(d, text)
        (ds if ok else rejected).append({**d, "why": why})
    return {**conv, "decisions": ds, "rejected": rejected}


def run(naive: bool = False) -> list[dict]:
    topics = timeline()["topics"]
    with ThreadPoolExecutor(8) as ex:
        return list(ex.map(lambda c: extract_one(c, topics, naive), normalize()))


def main() -> None:
    results = run()
    for r in results:
        print(f"{r['date']} {r['vendor']:8} {r['title']}")
        for d in r["decisions"]:
            print(f"    → [{d['topic']}] {d['statement']}  (理由: {d.get('reason', '')})")
        for d in r["rejected"]:
            print(f"    ✗ 検証で除外 [{d.get('topic')}] {d.get('statement')}  ({d.get('why')})")
    (DATA / "extracted.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
