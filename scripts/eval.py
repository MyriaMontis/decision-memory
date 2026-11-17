"""評価ハーネス: 抽出を「検証なし」と「検証あり」で走らせ、台本(timeline.yaml)に対する点数を比べる。

採点（check.py と同じ基準。Neo4jを使わずJSONから直接計算）:
- 台本の各決定が、正しい会話・正しいtopicで1つだけ取れているか
- 雑談の会話が決定ゼロか
- SUPERSEDES（同じtopicの隣接する新→旧）が台本の supersedes と一致するか

結果は data/eval.json に保存し、画面に出す。
"""
import json

from common import DATA, timeline
from extract import run


def score(results: list[dict]) -> dict:
    t = timeline()
    truth = json.loads((DATA / "_truth.json").read_text(encoding="utf-8"))
    tl = {d["id"]: d for d in t["decisions"]}
    items, junk = [], []
    for c in results:
        did = truth.get(c["id"])
        found = c["decisions"]
        if did is None:
            good = not found
            junk += [{"date": c["date"], "vendor": c["vendor"], "title": c["title"], "topic": d["topic"],
                      "statement": d["statement"], "statement_en": d.get("statement_en", "")} for d in found]
        else:
            good = len(found) == 1 and found[0]["topic"] == tl[did]["topic"]
        items.append(good)
    # SUPERSEDES を load_neo4j.py と同じ規則で作る
    by_topic = {}
    for c in sorted(results, key=lambda c: c["date"]):
        for d in c["decisions"]:
            by_topic.setdefault(d["topic"], []).append(truth.get(c["id"]))
    have = {(ds[i], ds[i - 1]) for ds in by_topic.values() for i in range(1, len(ds))}
    want = {(d["id"], d["supersedes"]) for d in t["decisions"] if d.get("supersedes")}
    items += [w in have for w in want] + [False] * len(have - want)
    return {"score": sum(items), "total": len(items), "junk": junk, "wrong_links": len(have - want)}


def main() -> None:
    out = {}
    for name, naive in [("without_verify", True), ("with_verify", False)]:
        r = score(run(naive))
        out[name] = r
        print(f"{name:15} {r['score']}/{r['total']}  余計な決定 {len(r['junk'])}件  間違った変更履歴 {r['wrong_links']}件")
        for j in r["junk"]:
            print(f"    ✗ {j['date']} {j['vendor']:8} 「{j['title']}」 → [{j['topic']}] {j['statement']}")
    (DATA / "eval.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
