"""記憶ハーネス本体: 質問 → Cypher生成 → ガードレール → 実行 → 検証 → 答え。

比較用に「ハーネスなし」も持つ: 1社（Chip GPT）の会話だけを記憶として渡す＝各社の公式メモリ相当。
"""
import json
import os
import re
import sys
import time

import openai
from pathlib import Path

from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).parent / "scripts"))
from common import DATA, VENDOR_LABEL, chat_json, llm, model  # noqa: E402

driver = GraphDatabase.driver(os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]))

SCHEMA = """ノード:
(:Vendor {name})            name は 'chipgpt' | 'crowd' | 'gemmy'
(:Conversation {id, title, date})
(:Topic {name})             name は 'アプリ名' | '収益化' | '対応端末' | '技術スタック' | '公開時期' | 'デザイン'
(:Decision {id, statement, date, status})   status は 'active'（現役）| 'superseded'（新しい決定で置き換え済み）
(:Reason {text})
関係:
(Conversation)-[:ON]->(Vendor)
(Conversation)-[:CONTAINS]->(Decision)
(Decision)-[:ABOUT]->(Topic)
(Decision)-[:BECAUSE]->(Reason)
(Decision)-[:SUPERSEDES]->(Decision)   新しい決定 → 置き換えられた古い決定"""

TEMPLATES = {
    "history": """MATCH (t:Topic {name:'%s'})<-[:ABOUT]-(d:Decision)<-[:CONTAINS]-(c:Conversation)-[:ON]->(v:Vendor)
OPTIONAL MATCH (d)-[:SUPERSEDES]->(old:Decision)
OPTIONAL MATCH (d)-[:BECAUSE]->(r:Reason)
RETURN d.id AS id, d.statement AS statement, d.date AS date, d.status AS status,
       v.name AS vendor, r.text AS reason, old.id AS old_id, c.id AS conv_id
ORDER BY d.date""",
    "changes": """MATCH (t:Topic {name:'%s'})<-[:ABOUT]-(new:Decision)-[:SUPERSEDES]->(old:Decision)
MATCH (cn:Conversation)-[:CONTAINS]->(new), (co:Conversation)-[:CONTAINS]->(old)
MATCH (cn)-[:ON]->(vn:Vendor), (co)-[:ON]->(vo:Vendor)
OPTIONAL MATCH (new)-[:BECAUSE]->(r:Reason)
RETURN new.id AS id, new.statement AS statement, new.date AS date, vn.name AS vendor,
       old.id AS old_id, old.statement AS old_statement, old.date AS old_date, vo.name AS old_vendor,
       r.text AS reason, cn.id AS conv_id, co.id AS old_conv_id""",
    "cross": """MATCH (cn:Conversation)-[:CONTAINS]->(new:Decision)-[:SUPERSEDES]->(old:Decision)<-[:CONTAINS]-(co:Conversation)
MATCH (cn)-[:ON]->(vn:Vendor), (co)-[:ON]->(vo:Vendor)
WHERE vn.name <> vo.name
OPTIONAL MATCH (new)-[:BECAUSE]->(r:Reason)
RETURN new.id AS id, new.statement AS statement, new.date AS date, vn.name AS vendor,
       old.id AS old_id, old.statement AS old_statement, old.date AS old_date, vo.name AS old_vendor,
       r.text AS reason, cn.id AS conv_id, co.id AS old_conv_id
ORDER BY new.date""",
}

CYPHER_PROMPT = f"""Neo4jのグラフに対する読み取り専用のCypherを1本だけ書く。
スキーマ:
{SCHEMA}

ルール:
- 決定(Decision)・会話(Conversation)の id を必ず RETURN に含める（id, old_id, conv_id などの列名で）。
- vendor 名・日付・理由(Reason)も取れるなら返す。
- 書き込み（CREATE/MERGE/SET/DELETE など）は禁止。
- 質問が英語でも、Topic名・vendor名などの値はスキーマどおり（日本語・小文字）で書く。
- 「結局どうなった」「何にした」系の質問でも、置き換えられた古い決定を含むトピックの履歴全体を返す（例1の形）。

例1 質問: アプリの名前、結局どうなった？
{{"cypher": {json.dumps(TEMPLATES['history'] % 'アプリ名', ensure_ascii=False)}}}

例2 質問: 収益化の方針は変わった？なぜ？
{{"cypher": {json.dumps(TEMPLATES['changes'] % '収益化', ensure_ascii=False)}}}

例3 質問: AIを乗り換えたせいで、前の決定をひっくり返したことはある？
{{"cypher": {json.dumps(TEMPLATES['cross'], ensure_ascii=False)}}}

JSONだけを返す: {{"cypher": "..."}}

質問: """

ANSWER_PROMPT = """あなたはBob（個人開発者）の「決定の記憶」です。下のグラフ検索結果だけを根拠に、質問に2〜3文で答える。
英語(en)と、その日本語訳(ja)の両方を書く。AI名は Chip GPT / Crowd / Gemmy と書く。
- 現在の決定（status=active）と、置き換えられた古い決定を区別する。
- いつ・どのAI（{labels}）との会話で決めたか、理由があれば添える。
- 結果にないことは言わない。結果が空なら「記録が見つからない」と答える。

質問: {q}
検索結果(JSON): {rows}

JSONだけを返す: {{"en": "...", "ja": "..."}}"""

WRITE_OPS = re.compile(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|CALL|LOAD|FOREACH)\b", re.I)


# ── ガードレール ─────────────────────────────────
def guard(cypher: str) -> str | None:
    """書き込み系のCypherを止める。止めたら理由を返す。"""
    m = WRITE_OPS.search(cypher)
    return f"Blocked: contains write op {m.group(1).upper()}" if m else None


# ── ツール: グラフを読む（読み取り専用トランザクション） ─────
def run_cypher(cypher: str) -> list[dict]:
    with driver.session() as s:
        return s.execute_read(lambda tx: tx.run(cypher).data())


def fallback_template(q: str) -> str:
    if re.search(r"乗り換|会社|AIを|ひっくり返|switch|another AI|overturn|revers|across", q, re.I):
        return TEMPLATES["cross"]
    topic = next((t for t in ["アプリ名", "収益化", "対応端末", "技術スタック", "公開時期", "デザイン"]
                  if t[:2] in q or re.search(EN_TOPIC[t], q, re.I)), "アプリ名")
    return (TEMPLATES["changes"] if re.search(r"変わ|なぜ|理由|chang|why", q, re.I) else TEMPLATES["history"]) % topic


EN_TOPIC = {"アプリ名": r"name|名前", "収益化": r"monet|money|price|pric", "対応端末": r"device|ipad|iphone",
            "技術スタック": r"stack|tech|swift", "公開時期": r"launch|release", "デザイン": r"design|color|colour"}


# ── 検証: 答えの根拠IDが実在するか ────────────────────
def collect_ids(rows: list[dict]) -> list[str]:
    ids = {str(v) for r in rows for v in r.values() if isinstance(v, str) and ("#" in v or v[:4] in ("chip", "crow", "gemm", "tl-d") or re.fullmatch(r"d\d+", v))}
    if not ids:
        return []
    with driver.session() as s:
        found = s.run("""MATCH (n) WHERE (n:Decision OR n:Conversation) AND n.id IN $ids
                         OPTIONAL MATCH (n)<-[:CONTAINS]-(c:Conversation)
                         OPTIONAL MATCH (n)-[:ABOUT]->(t:Topic)
                         RETURN collect(DISTINCT n.id) + collect(DISTINCT c.id) + collect(DISTINCT 'topic:' + t.name) AS ids""",
                      ids=list(ids)).single()["ids"]
    return [i for i in found if i]


def for_llm(rows: list[dict]) -> list[dict]:
    """答えを書かせる前に、IDを外してAI名を表示名にする（答えにIDが漏れないように）。"""
    out = []
    for r in rows:
        out.append({k: (VENDOR_LABEL.get(v, v) if "vendor" in k else v) for k, v in r.items()
                    if not (k == "id" or k.endswith("_id"))})
    return out


def ask(q: str) -> dict:
    t0 = time.time()
    steps = []
    cypher, source = None, "llm"
    for attempt in range(2):
        try:
            prompt = CYPHER_PROMPT + q + (f"\n前回のCypherはエラー: {err}" if attempt else "")  # noqa: F821
            cypher = chat_json(prompt, temperature=0)["cypher"]
            blocked = guard(cypher)
            if blocked:
                steps.append({"step": "guardrail", "ok": False, "detail": blocked})
                raise ValueError(blocked)
            rows = run_cypher(cypher)
            if not rows:
                raise ValueError("0 rows")
            steps.append({"step": "cypher", "ok": True, "detail": f"{len(rows)} rows"})
            break
        except (openai.APIConnectionError, openai.APITimeoutError):
            raise  # ネットが落ちている → 再試行せず、すぐキャッシュへ
        except Exception as e:  # noqa: BLE001
            err = str(e)[:300]
            steps.append({"step": "cypher", "ok": False, "detail": err})
    else:
        source = "template"
        cypher = fallback_template(q)
        rows = run_cypher(cypher)
        steps.append({"step": "fallback", "ok": True, "detail": f"{len(rows)} rows"})

    path_ids = collect_ids(rows)
    steps.append({"step": "verify", "ok": bool(path_ids), "detail": f"{len(path_ids)} evidence nodes exist"})
    answer = chat_json(ANSWER_PROMPT.format(
        labels="、".join(f"{k}={v}" for k, v in VENDOR_LABEL.items()), q=q,
        rows=json.dumps(for_llm(rows), ensure_ascii=False, default=str)), temperature=0.2)
    return {"answer": answer, "cypher": cypher, "source": source, "rows": rows,
            "path_ids": path_ids, "steps": steps, "ms": int((time.time() - t0) * 1000)}


def ask_baseline(q: str, vendor: str = "chipgpt") -> dict:
    """ハーネスなし: 1社の会話ログだけを記憶として渡す（各社の公式メモリ相当）。"""
    convs = [c for c in json.loads((DATA / "extracted.json").read_text(encoding="utf-8")) if c["vendor"] == vendor]
    memory = "\n\n".join(f"[{c['date']} {c['title']}]\n" + "\n".join(
        f"{'Bob' if m['role'] == 'user' else 'AI'}: {m['text']}" for m in c["messages"]) for c in convs)
    answer = chat_json(f"質問: {q}", temperature=0.2, system=(
        f"あなたは{VENDOR_LABEL[vendor]}。ユーザーBobとの過去の会話の記憶は以下だけ。記憶にもとづいて2〜3文で答える。"
        '英語(en)とその日本語訳(ja)を書き、JSONだけを返す: {"en": "...", "ja": "..."}\n\n' + memory))
    return {"answer": answer, "vendor": VENDOR_LABEL[vendor], "memory_convs": len(convs)}


def graph() -> dict:
    with driver.session() as s:
        nodes = s.run("""MATCH (n) RETURN
            CASE WHEN n:Topic THEN 'topic:' + n.name WHEN n:Vendor THEN 'vendor:' + n.name
                 WHEN n:Reason THEN 'reason:' + n.text ELSE n.id END AS id,
            labels(n)[0] AS kind, coalesce(n.statement, n.title, n.name, n.text) AS label, coalesce(n.statement_en, n.text_en) AS label_en,
            n.status AS status, n.date AS date""").data()
        links = s.run("""MATCH (a)-[r]->(b) RETURN
            CASE WHEN a:Topic THEN 'topic:' + a.name WHEN a:Vendor THEN 'vendor:' + a.name
                 WHEN a:Reason THEN 'reason:' + a.text ELSE a.id END AS source,
            CASE WHEN b:Topic THEN 'topic:' + b.name WHEN b:Vendor THEN 'vendor:' + b.name
                 WHEN b:Reason THEN 'reason:' + b.text ELSE b.id END AS target,
            type(r) AS type""").data()
        vend = {r["c"]: r["v"] for r in s.run("MATCH (c:Conversation)-[:ON]->(v:Vendor) RETURN c.id AS c, v.name AS v")}
        dv = {r["d"]: r["v"] for r in s.run(
            "MATCH (v:Vendor)<-[:ON]-(:Conversation)-[:CONTAINS]->(d:Decision) RETURN d.id AS d, v.name AS v")}
    for n in nodes:
        n["vendor"] = vend.get(n["id"]) or dv.get(n["id"]) or (n["id"][7:] if n["kind"] == "Vendor" else None)
    return {"nodes": nodes, "links": links}
