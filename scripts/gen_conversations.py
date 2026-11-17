"""台本(timeline.yaml) → 会社ごとの会話ログ(data/chipgpt.json など)。

決定1つにつき会話1本、雑談(noise)も1本ずつ。並列で生成する。
答え合わせ用に data/_truth.json に「会話ID → 決定ID」を残す（抽出側は見ない）。
"""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from common import DATA, VENDOR_LABEL, chat_json, timeline

PROMPT = """架空の個人開発者 Bob と、AIアシスタント「{label}」の自然な日本語チャットを1本書いてください。
Bobは読書記録アプリを作っている学生です。{label}は{role}として使っています。

この会話で起きること: {what}

条件:
- 4〜6メッセージ（user と assistant が交互）。Bobはくだけた口調、少し迷ったり脱線したりする。
- {must}
- 会話のタイトルも付ける（15字以内）。
JSONだけを返す: {{"title": "...", "messages": [{{"role": "user", "text": "..."}}, {{"role": "assistant", "text": "..."}}]}}"""


def make_job(item: dict, roles: dict, is_noise: bool) -> dict:
    v = item["vendor"]
    if is_noise:
        what = item["about"]
        must = "アプリについて新しく何かを決めることはしない（迷ってもよいが結論は出さない）。"
    else:
        what = f"「{item['topic']}」について、Bobが最終的に「{item['statement']}」と決める。理由は「{item['reason']}」。"
        if item.get("supersedes"):
            what += "（以前の決定を変更する場面。前は別の方針だったことに軽く触れる）"
        must = "最後のほうでBobが自分の言葉ではっきり決めたと言う。"
    return {"item": item, "vendor": v, "noise": is_noise,
            "prompt": PROMPT.format(label=VENDOR_LABEL[v], role=roles[v], what=what, must=must)}


def run(job: dict) -> dict:
    for attempt in range(3):
        try:
            out = chat_json(job["prompt"])
            assert out["messages"] and out["title"]
            return {**job, "out": out}
        except Exception as e:  # noqa: BLE001
            print(f"  retry {attempt + 1}: {e}")
    raise RuntimeError("generation failed")


def to_vendor_format(vendor: str, cid: str, date: str, title: str, msgs: list) -> dict:
    ts = datetime.fromisoformat(date).replace(hour=21, tzinfo=timezone.utc)
    if vendor == "chipgpt":
        return {"id": cid, "create_time": ts.timestamp(), "title": title,
                "messages": [{"role": m["role"], "text": m["text"]} for m in msgs]}
    if vendor == "crowd":
        return {"uuid": cid, "created_at": ts.isoformat(), "name": title,
                "chat_messages": [{"sender": "human" if m["role"] == "user" else "assistant", "text": m["text"]} for m in msgs]}
    return {"id": cid, "date": date, "title": title,
            "turns": [{"who": "user" if m["role"] == "user" else "model", "text": m["text"]} for m in msgs]}


def main() -> None:
    t = timeline()
    roles = t["vendors"]
    jobs = [make_job(d, roles, False) for d in t["decisions"]] + [make_job(n, roles, True) for n in t["noise"]]
    print(f"generating {len(jobs)} conversations ...")
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(run, jobs))

    files = {v: [] for v in VENDOR_LABEL}
    truth = {}
    for r in sorted(results, key=lambda r: str(r["item"]["date"])):
        cid = f"{r['vendor']}-{uuid.uuid4().hex[:8]}"
        date = str(r["item"]["date"])
        files[r["vendor"]].append(to_vendor_format(r["vendor"], cid, date, r["out"]["title"], r["out"]["messages"]))
        truth[cid] = None if r["noise"] else r["item"]["id"]
        print(f"  {date} {r['vendor']:8} {r['out']['title']}")

    for v, convs in files.items():
        (DATA / f"{v}.json").write_text(json.dumps(convs, ensure_ascii=False, indent=2), encoding="utf-8")
    (DATA / "_truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
    print("done:", {v: len(c) for v, c in files.items()})


if __name__ == "__main__":
    main()
