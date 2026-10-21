# Traceable Research Assistant

A question-answering service over your own PDFs. Every sentence in an answer cites the passage it came from, and the system is built to keep running on AWS when things go wrong.

> **中文摘要**　把碩士論文（多跳問答的 RAG 失敗分析）做成可以實際使用的知識問答服務：上傳 PDF 後提問，回答逐句附上出處。重點不在功能數量，而在可靠性與可驗證性：背景處理與自動重試、雲端故障演練、延遲與成本量測、CI/CD、告警，以及用評估資料集決定設計取捨。所有設計決策記錄在 [`docs/decisions/`](docs/decisions/)（中文）。

## What it does

- **Upload PDFs** into a private workspace. Files are parsed, chunked, embedded and indexed in the background.
- **Ask questions** about those documents. Answers mark each sentence with `[1]`, `[2]`… and return only the passages that were actually cited.
- **Say "not found"** instead of guessing when the documents do not contain the answer.
- **Reject duplicate uploads** by content hash, and let users **retry or cancel** anything they are waiting on.

## Why this project

My master's thesis studied why RAG systems answer multi-hop questions incorrectly, and split failures into *evidence not retrieved* versus *evidence retrieved but misused*. This project takes that research out of the notebook: it is a deployed service where reliability, cost and answer quality are measured rather than assumed.

## Architecture

```
                         CloudFront (fixed HTTPS URL)
                   ┌───────────────┴───────────────┐
            static frontend                    API requests
            (S3, React)                 Lambda: FastAPI via Mangum
                                                   │
   browser ── presigned upload ──▶ S3 (PDFs)       │
                                                   ▼
                                   SQS ──▶ Lambda: worker ──▶ OpenAI
                                    │                         (embeddings, answers)
                                   DLQ
                 hourly schedule ──▶ Lambda: sweeper
                                                   │
                                   PostgreSQL + pgvector (Neon)
```

| Component | Role |
|---|---|
| API Lambda | FastAPI app (auth, workspaces, documents, Q&A) behind CloudFront |
| S3 | PDFs are uploaded directly from the browser with a presigned POST, so large files never pass through the API |
| SQS + worker Lambda | Background processing: parse → chunk → embed → store |
| Sweeper Lambda | Hourly: marks stuck documents as failed and removes orphaned S3 files |
| PostgreSQL + pgvector | Data and vector search, always scoped to one workspace |
| Secrets Manager / CloudWatch | Secrets loaded at cold start; structured JSON logs and alarms |

All three Lambdas run from one Docker image. Nothing runs when nobody is using it, so idle cost is close to zero.

The backend uses four layers, `api → services → repositories → models`, so ownership rules live in one place and only repositories touch the database.

## Engineering highlights

### Background processing that recovers on its own
- Uploads return immediately. Processing happens in a worker fed by SQS.
- OpenAI errors are classified as temporary or permanent. Temporary failures are retried in seconds, then deferred for 2 minutes, for up to 3 rounds.
- A worker holds a **lease with heartbeats**. If it dies mid-document, the document is marked *interrupted* within 5 minutes and can be retried, instead of staying "processing" forever.
- Each document is processed in one transaction, so a failure never leaves half its chunks behind.

### Failures tested on purpose
About 30 failure modes are listed in [`docs/failure-modes.md`](docs/failure-modes.md), each with its evidence. Several were reproduced in the cloud. For example, pointing the worker at an unreachable OpenAI endpoint, or killing it mid-run. One drill showed that a worker crashing at the wrong moment left a document stuck forever, which led to the lease and heartbeat design above.

### Measured, not guessed
Structured logs carry a request ID from the API through SQS to the worker. Per-step timings showed that a 170 ms round trip between the Sydney Lambdas and the Singapore database dominated processing time. Moving everything to Singapore cut upload-to-ready time from about 23 s to about 11.5 s.

### Cost control
Token usage and cost are recorded for every call. Daily limits apply per user (USD 0.50) and across the whole system (USD 5). Limits are checked before work starts, and work that has started is allowed to finish.

### CI/CD
GitHub Actions ([`ci-cd.yml`](.github/workflows/ci-cd.yml)) runs on every push:
- Lint (ruff) and 135 tests against a real PostgreSQL + pgvector service container, plus a frontend type check and build.
- On `main`, it deploys only what changed. AWS access uses short-lived OIDC credentials, so no keys are stored in GitHub.
- Database migrations run before the Lambdas are updated, and must stay compatible with the previous version.
- After deploying, it warms up the new image and checks that the API responds, failing the run if it does not.

### Alerts
Email alarms cover only the problems that need a person: error logs, an invalid or exhausted OpenAI key, the system-wide limit being reached, a sweeper that has not run, and messages in the dead-letter queue. Alarms fire on state changes, so a week-long issue sends one email, not hundreds.

### Answer quality, evaluated
I built two evaluation sets: a fictional company knowledge base (18 documents, 58 questions) and a fictional school (21 questions, a different domain used only for held-out checks). The questions focus on whether a rule actually *applies to the person asking*, for example a policy that differs by seniority, an outdated version, or a regional exception.

- Retrieval is scored by code. Answers are scored by an LLM judge against per-question checklists of required facts and forbidden claims.
- Wrong answers are split into *not retrieved* versus *retrieved but misused*, the same two-layer analysis as my thesis.
- Each change is kept only if it measurably helps. Rejected ideas are recorded with the reason.

Comparing answer models (2 runs each, same prompt):

| Question set | gpt-4o-mini | gpt-4o |
|---|---|---|
| Company, practice (41) | 76% / 73% | 80% / 80% |
| Company, unseen exam (17) | 76% / 65% | 76% / 82% |
| School, other domain (21) | 86% / 90% | 95% / 95% |

The biggest gap was on questions where a rule does *not* apply to the asker (73% → 95% across all sets), so answers now use gpt-4o. Details: [`docs/eval-results.md`](docs/eval-results.md).

## Design decisions

Every non-trivial choice has a short decision record in [`docs/decisions/`](docs/decisions/) (24 so far, in Chinese), covering the context, the options considered and why one was chosen. A few examples:

| # | Decision |
|---|---|
| 001 | Go deeper on reliability, cost and quality instead of adding features |
| 003 | Background processing with SQS + Lambda |
| 008 | Heartbeats and leases to detect dead workers |
| 011 | Move all AWS resources to Singapore, next to the database |
| 013 | Fixed HTTPS URL: API on Lambda behind CloudFront |
| 014 | CI/CD with GitHub Actions and OIDC |
| 017 | Evaluation method and how to avoid overfitting prompts to the test set |
| 023 | Answer model comparison: gpt-4o-mini vs gpt-4o |

## Tech stack

**Backend:** Python 3.11, FastAPI, SQLAlchemy 2.0, Alembic, PostgreSQL + pgvector, pytest, ruff
**AI:** OpenAI `text-embedding-3-small` (embeddings), gpt-4o (answers), pypdf, tiktoken
**Cloud:** AWS Lambda, SQS, S3, CloudFront, ECR, Secrets Manager, CloudWatch, EventBridge, IAM; Docker
**CI/CD:** GitHub Actions
**Frontend:** React, Vite, TypeScript, Tailwind CSS

## Run locally

```bash
git clone https://github.com/gunter0128/traceable-research-assistant.git
cd traceable-research-assistant
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in a PostgreSQL connection string (with pgvector), a secret key (`python -c "import secrets; print(secrets.token_urlsafe(32))"`), an OpenAI API key and an S3 bucket. Without `SQS_QUEUE_URL`, documents are processed inline in the upload request, so no queue is needed for local development.

```bash
alembic upgrade head
uvicorn app.main:app --reload     # API docs at http://localhost:8000/docs
```

Frontend (needs Node.js LTS):

```bash
cd frontend
npm install
npm run dev                       # http://localhost:5173, proxies API calls to :8000
```

## Tests

```bash
pytest
```

135 tests cover ownership checks across users, the processing pipeline, retries, leases, cancellation, usage limits and Q&A. OpenAI, S3 and SQS are replaced with fakes, so the suite costs nothing and needs no API key. Locally it uses a separate database configured in `.env.test` (see `.env.test.example`). In CI it uses a throwaway PostgreSQL container.

## API

| Method | Path | Description |
|---|---|---|
| POST | `/auth/register` · `/auth/login` | Sign up, get a JWT |
| GET | `/auth/me` | Current user |
| POST / GET | `/workspaces` | Create / list workspaces |
| GET / PUT / DELETE | `/workspaces/{id}` | Read / rename / delete a workspace (and its files) |
| POST | `/workspaces/{id}/uploads` | Get a presigned S3 upload ticket |
| POST / GET | `/workspaces/{id}/documents` | Register an uploaded file / list documents |
| GET / DELETE | `/documents/{id}` | Read / delete a document |
| POST | `/documents/{id}/retry` · `/documents/{id}/cancel` | Retry a failed document / cancel one in progress |
| POST | `/workspaces/{id}/ask` | Ask a question, get an answer with citations |
| GET | `/usage/today` | Today's usage against the daily limit |

## Known limitations

- Cross-lingual retrieval is weak: a Chinese question about an English document matches noticeably worse than the same question in English.
- Summary questions ("what is this paper about?") are a poor fit for top-5 passage retrieval.
- The free-tier database sleeps after 5 minutes idle, so the first request afterwards takes about 2 s longer.
- Evaluation is run manually when Q&A code changes. It is not in CI because it costs money and its results vary between runs.

## What's next

- Multi-hop retrieval with a visible reasoning trace, which is the core idea of my thesis.
- A load test following the same pattern as the rest of the project: write down the expected bottleneck, measure, change, measure again.
