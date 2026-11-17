"""決定JSON → Neo4j。SUPERSEDES は同じ topic を日付順に並べ、隣接だけ「新→旧」で張る。

使い方:
  python scripts/load_neo4j.py              # data/extracted.json（LLM抽出）から
  python scripts/load_neo4j.py --timeline   # 逃げ道: 台本から直接（デモ用の保険）
"""
import json
import os
import sys

from neo4j import GraphDatabase

from common import DATA, timeline

SCHEMA = [
    "CREATE CONSTRAINT IF NOT EXISTS FOR (c:Conversation) REQUIRE c.id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (d:Decision) REQUIRE d.id IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (t:Topic) REQUIRE t.name IS UNIQUE",
    "CREATE CONSTRAINT IF NOT EXISTS FOR (v:Vendor) REQUIRE v.name IS UNIQUE",
]


def rows_from_extracted() -> list[dict]:
    rows = []
    for c in json.loads((DATA / "extracted.json").read_text(encoding="utf-8")):
        for i, d in enumerate(c["decisions"]):
            rows.append({"conv_id": c["id"], "title": c["title"], "vendor": c["vendor"], "date": c["date"],
                         "id": f"{c['id']}#{i}", "topic": d["topic"], "statement": d["statement"],
                         "reason": d.get("reason") or "", "evidence": d.get("evidence") or "",
                         "statement_en": d.get("statement_en") or "", "reason_en": d.get("reason_en") or ""})
    # 雑談など決定ゼロの会話もノードとして残す
    convs = [{"conv_id": c["id"], "title": c["title"], "vendor": c["vendor"], "date": c["date"]}
             for c in json.loads((DATA / "extracted.json").read_text(encoding="utf-8"))]
    return rows, convs


def rows_from_timeline() -> list[dict]:
    rows = [{"conv_id": f"tl-{d['id']}", "title": d["statement"], "vendor": d["vendor"], "date": str(d["date"]),
             "id": d["id"], "topic": d["topic"], "statement": d["statement"], "reason": d["reason"], "evidence": "",
             "statement_en": "", "reason_en": ""}
            for d in timeline()["decisions"]]
    return rows, [{k: r[k] for k in ("conv_id", "title", "vendor", "date")} for r in rows]


def main() -> None:
    rows, convs = rows_from_timeline() if "--timeline" in sys.argv else rows_from_extracted()
    drv = GraphDatabase.driver(os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]))
    with drv.session() as s:
        s.run("MATCH (n) DETACH DELETE n")
        for q in SCHEMA:
            s.run(q)
        s.run("""UNWIND $convs AS c
                 MERGE (v:Vendor {name: c.vendor})
                 MERGE (cv:Conversation {id: c.conv_id}) SET cv.title = c.title, cv.date = c.date
                 MERGE (cv)-[:ON]->(v)""", convs=convs)
        s.run("""UNWIND $rows AS r
                 MATCH (cv:Conversation {id: r.conv_id})
                 MERGE (t:Topic {name: r.topic})
                 CREATE (d:Decision {id: r.id, statement: r.statement, statement_en: r.statement_en, date: r.date, evidence: r.evidence})
                 CREATE (cv)-[:CONTAINS]->(d)
                 CREATE (d)-[:ABOUT]->(t)
                 FOREACH (_ IN CASE WHEN r.reason <> '' THEN [1] ELSE [] END |
                   MERGE (rs:Reason {text: r.reason}) SET rs.text_en = r.reason_en CREATE (d)-[:BECAUSE]->(rs))""", rows=rows)
        # 同じ topic を日付順に並べ、隣接だけ 新→旧 で SUPERSEDES
        s.run("""MATCH (t:Topic)<-[:ABOUT]-(d:Decision)
                 WITH t, d ORDER BY d.date
                 WITH t, collect(d) AS ds
                 UNWIND range(1, size(ds) - 1) AS i
                 WITH ds[i] AS new, ds[i - 1] AS old
                 CREATE (new)-[:SUPERSEDES]->(old)""")
        s.run("MATCH (:Decision)-[:SUPERSEDES]->(o:Decision) SET o.status = 'superseded'")
        s.run("MATCH (d:Decision) WHERE d.status IS NULL SET d.status = 'active'")
        counts = s.run("""MATCH (n) WITH labels(n)[0] AS l, count(*) AS c RETURN l, c ORDER BY l""").data()
        sup = s.run("MATCH ()-[r:SUPERSEDES]->() RETURN count(r) AS c").single()["c"]
    drv.close()
    print({r["l"]: r["c"] for r in counts}, "SUPERSEDES:", sup)


if __name__ == "__main__":
    main()
