"""台本(timeline.yaml)とNeo4jの中身を突き合わせる（eval）。

- 決定: 台本の各決定が、正しい会話・正しいtopicで1つだけ取れているか
- 雑談: 決定ゼロになっているか
- 変更: SUPERSEDES が台本の supersedes と一致するか
"""
import json
import os

from neo4j import GraphDatabase

from common import DATA, timeline


def main() -> None:
    t = timeline()
    truth = json.loads((DATA / "_truth.json").read_text(encoding="utf-8"))  # 会話ID → 決定ID
    tl = {d["id"]: d for d in t["decisions"]}
    drv = GraphDatabase.driver(os.environ["NEO4J_URI"], auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]))
    with drv.session() as s:
        got = s.run("""MATCH (c:Conversation)-[:CONTAINS]->(d:Decision)-[:ABOUT]->(t:Topic)
                       RETURN c.id AS conv, d.id AS id, t.name AS topic""").data()
        sup = s.run("""MATCH (n:Decision)-[:SUPERSEDES]->(o:Decision)
                       MATCH (cn:Conversation)-[:CONTAINS]->(n), (co:Conversation)-[:CONTAINS]->(o)
                       RETURN cn.id AS new, co.id AS old""").data()
    drv.close()

    ok = 0
    score = []
    by_conv = {}
    for g in got:
        by_conv.setdefault(g["conv"], []).append(g)
    for conv, did in truth.items():
        found = by_conv.get(conv, [])
        if did is None:
            good = not found
            label = "雑談 → 決定なし"
        else:
            good = len(found) == 1 and found[0]["topic"] == tl[did]["topic"]
            label = f"{did} [{tl[did]['topic']}] {tl[did]['statement']}"
        score.append(good)
        print(("✓ " if good else "✗ ") + label + ("" if good else f"   取れたもの: {[f['topic'] for f in found]}"))

    want = {(d["id"], d["supersedes"]) for d in t["decisions"] if d.get("supersedes")}
    have = {(truth.get(r["new"]), truth.get(r["old"])) for r in sup}
    for w in sorted(want):
        good = w in have
        score.append(good)
        print(("✓ " if good else "✗ ") + f"変更 {w[1]} → {w[0]}")
    for extra in sorted(have - want, key=str):
        score.append(False)
        print(f"✗ 余計な変更 {extra[1]} → {extra[0]}")

    print(f"\nスコア: {sum(score)}/{len(score)}")


if __name__ == "__main__":
    main()
