# Decision Memory

**A memory harness that remembers what you decided — across multiple AI assistants.**

Built in one evening at +81 Builders "Build the harness, not the model" (Tokyo, 2026-11-17).

People switch between AIs all the time — one for ideas, one for code, one for images. Each AI's built-in memory only knows its own chats, so decisions get scattered and old ones silently come back. Decision Memory merges chat logs from three (fictional) AIs — **Chip GPT, Crowd and Gemmy** — into one Neo4j graph of *decisions with history*, and answers questions with the evidence path highlighted.

All chat data in this repo is **synthetic**: a made-up developer, Bob, building a reading-log app.

## What the harness does

| Part | How |
|---|---|
| **Memory as a graph** | `Conversation → Decision → Topic`, plus `Decision -[:SUPERSEDES]-> Decision` (new → old). Old decisions are marked `superseded` instead of being retrieved as if still true. |
| **Verification** (extraction) | Each extracted decision must quote the user verbatim (checked by string match), then a second LLM call asks "is this really a decision about the project?". Chit-chat ("let's do 2 nights in Kyoto") is rejected. |
| **Guardrail** (Q&A) | The model writes Cypher, but write clauses (`CREATE/MERGE/SET/DELETE/…`) are blocked and queries run in a read-only transaction. Falls back to template queries if generation fails. |
| **Evidence check** | Node IDs in the answer's query result must exist in the graph; they are highlighted in the UI. |
| **Eval** | `timeline.yaml` is the ground truth. `scripts/eval.py` scores extraction with and without verification (19/30 → 24/24 in our run). |
| **Offline fallback** | Demo answers are cached; if the API hangs (>10 s) the cached answer is served. |

Baseline for comparison: "without harness" = one AI that only has its own chat history (like a vendor's built-in memory).

## Pipeline

```
data/timeline.yaml  (ground truth: 10 decisions + 10 chit-chat topics)
   │ scripts/gen_conversations.py   LLM writes chats in 3 vendor-like export formats
   ▼
data/chipgpt.json, crowd.json, gemmy.json
   │ scripts/extract.py             decisions + quote check + LLM verify
   ▼
data/extracted.json
   │ scripts/load_neo4j.py          graph + SUPERSEDES (adjacent, same topic)
   ▼
Neo4j ── app.py (FastAPI)  GET /graph · POST /ask · POST /baseline · GET /eval
          web/index.html  force-graph visualization
```

## Run

```bash
docker run -d --name harness-neo4j -p 7474:7474 -p 7687:7687 -e NEO4J_AUTH=neo4j/<password> neo4j:5
uv venv && uv pip install neo4j fastapi "uvicorn[standard]" pyyaml openai python-dotenv
cp .env.example .env   # fill in Neo4j password and an OpenAI-compatible API (we used ai& with openai/gpt-oss-120b)
.venv/bin/python scripts/load_neo4j.py      # load the included extracted data
.venv/bin/uvicorn app:app --port 8000       # open http://localhost:8000
```

Regenerate everything: `gen_conversations.py` → `extract.py` → `load_neo4j.py` → `check.py` / `eval.py`, then `python app.py --warm` to refresh the answer cache.

## Stack

Python · FastAPI · Neo4j 5 · ai& Inference (OpenAI-compatible, `openai/gpt-oss-120b`) · force-graph

## License

MIT
