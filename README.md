# Traceable Research Assistant

A question-answering service over your own PDFs. Every sentence in an answer cites the passage it came from, so readers can check it themselves. It runs on AWS with OpenAI, or fully on-premises with a fine-tuned open model, so confidential documents never leave the company.

> **中文摘要**　把碩士論文（RAG 失敗分析）做成可以實際使用的知識問答服務：上傳 PDF 後提問，回答逐句附上出處。先用 OpenAI 在 AWS 上做出可靠的後端（背景處理、自動重試、故障演練、成本控制、CI/CD、告警），再換成資料不出公司的地端版本：本機的轉向量模型，加上自己用 LoRA 訓練的 Qwen3.5-9B。用兩套考卷跟 gpt-4o 比：在沒看過的考試題上，地端版每一項都不輸 gpt-4o。所有設計決策照主線整理在 [`docs/decisions/`](docs/decisions/)（中文）。

## What it does

- **Upload PDFs** into a private workspace. Files are parsed, chunked, embedded and indexed in the background.
- **Ask questions** about those documents. Answers mark each sentence with `[1]`, `[2]`… and return only the passages that were actually cited.
- **Say "not found"** instead of guessing when the documents do not contain the answer.
- **Reject duplicate uploads** by content hash, and let users **retry** any document that failed.

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

| Folder | What it holds |
|---|---|
| `app/` | The service. `api/` (HTTP) → `services/` (business rules, including the answer prompt) → `repositories/` (SQL) → `models/`. `core/` holds config, logging, PDF parsing, chunking and the OpenAI / S3 / SQS clients |
| `tests/` | Automated tests, with OpenAI, S3 and SQS replaced by fakes |
| `eval/` | The two exam sets and the scoring script |
| `train/` | Training data pipeline and LoRA training for the on-premises model |
| `deploy/` | AWS configuration files and the alarm setup script |
| `onprem/` | docker-compose for the on-premises version: database, S3-compatible storage (SeaweedFS), API, web server and the two models |
| `frontend/` | React web app |
| `docs/` | [Architecture](docs/architecture.md), [decision records](docs/decisions/README.md), [results](docs/eval-results.md), [failure modes](docs/failure-modes.md), deployment commands |

## Engineering highlights

### Background processing that recovers on its own
- Uploads return immediately. Processing happens in a worker fed by SQS.
- Calls to OpenAI and S3 use the SDKs' built-in retries with explicit timeouts. If a document still fails, it is marked *failed* with a clear reason (temporary outage or configuration problem) and the user can retry it.
- A worker **claims** a document with a lease: a claim token checked by conditional updates. A duplicate SQS message or a double-clicked retry can never process the same document twice, and a worker that lost its claim cannot save its results.
- The worker reports in after each embedding batch. If it goes silent for 10 minutes, the document is marked *interrupted* and can be retried, instead of staying "processing" forever.
- Chunks and the "processed" status are saved in one transaction at the end, so a failure never leaves half its chunks behind. No database transaction is held open while calling S3 or OpenAI.

### Failures tested on purpose
About 30 failure modes are listed in [`docs/failure-modes.md`](docs/failure-modes.md), each with its evidence. Several were reproduced in the cloud. For example, pointing the worker at an unreachable OpenAI endpoint, or killing it mid-run. One drill showed that a worker crashing at the wrong moment left a document stuck forever, which led to the lease and heartbeat design above.

### Measured, not guessed
Structured logs carry a request ID from the API through SQS to the worker. Per-step timings showed that a 170 ms round trip between the Sydney Lambdas and the Singapore database dominated processing time. Moving everything to Singapore cut upload-to-ready time from about 23 s to about 11.5 s.

### Cost control
Token usage and cost are recorded for every call. Daily limits apply per user (USD 0.50) and across the whole system (USD 5). Limits are checked before work starts, and work that has started is allowed to finish.

### CI/CD
GitHub Actions ([`ci-cd.yml`](.github/workflows/ci-cd.yml)) runs on every push:
- Lint (ruff) and 156 tests against a real PostgreSQL + pgvector service container, plus a frontend type check and build.
- On `main`, it deploys only what changed. AWS access uses short-lived OIDC credentials, so no keys are stored in GitHub.
- Database migrations run before the Lambdas are updated, and must stay compatible with the previous version.
- After deploying, it warms up the new image and checks that the API responds, failing the run if it does not.

### Alerts
Email alarms cover only the problems that need a person: error logs, an invalid or exhausted OpenAI key, the system-wide limit being reached, a sweeper that has not run, and messages in the dead-letter queue. Alarms fire on state changes, so a week-long issue sends one email, not hundreds.

### Answer quality, evaluated
Two exam sets, 140 questions in total:
- **A fictional company** (78 documents). Besides the current rules, it contains outdated versions, a subsidiary's rules, drafts and announcements that take effect next year, so many questions test whether a rule actually *applies to the person asking*.
- **22 real Taiwanese labor laws**, long documents where the answer is one article among hundreds.

Questions are split into a practice set (94) used while improving the system and a held-out test set (46) run only at the end.

- Answers are scored by a gpt-4o judge against per-question checklists of required facts and forbidden claims. The judge itself was audited and its rules corrected.
- Citations are checked by code: every number in a sentence must appear in the passage it cites.
- The same settings run twice can differ by up to 9 points, so comparisons use at least 2 runs.
- Wrong answers are split into *not retrieved* versus *retrieved but misused*, the same two-layer analysis as my thesis.
- Ideas that did not measurably help were dropped, with the measurements kept in [`docs/decisions/archive/`](docs/decisions/archive/).

### On-premises version with a fine-tuned model
The same code switches by configuration: embeddings and answers can point at any OpenAI-compatible server. On one RTX 3090 with vLLM, it runs Qwen3-Embedding-0.6B and Qwen3.5-9B with a LoRA adapter I trained.

- **Training data stays on-premises too.** Questions come from Taipei City Government internal regulations, which do not overlap with the exams. Qwen in thinking mode writes the questions, which I reviewed one by one, and the example answers. Each example must pass a local judge, the citation check and a consistency check, and a sample was checked by hand.
- **Final evaluation on the held-out test set** (46 questions never used while improving the on-premises version; both editions run on the same day with the same code):

| | Correct | Key facts | Citations complete | Citations match | Misuse | Consistent | Avg. length | Seconds |
|---|---|---|---|---|---|---|---|---|
| gpt-4o | 70% | 76% | 65% | 93% | 9% | 98% | 95 | 1.7 |
| Qwen3.5-9B + LoRA, on-premises | 83% | 85% | 72% | 97% | 2% | 98% | 93 | 4.2 |

On unseen questions the on-premises model is not worse than gpt-4o on any metric. Only 12 questions differ between the two (9 to 3), which could still be chance, so the claim is "not worse", not "better". On the practice set (2 runs) it falls short of the release bar I set beforehand on three metrics, by 1 to 6 points: citations complete 61% vs 65%, citations match 89% vs 95%, misuse 6% vs 5%. The bar was not changed after seeing the results.

Training cut answers from 265 to about 93 characters without losing accuracy. A rule split across two adjacent chunks was the main cause of wrong citations; merging adjacent passages fixed most of them. Details: [`docs/eval-results.md`](docs/eval-results.md), [decision 044](docs/decisions/044-最後驗收.md).

## Design decisions

Every non-trivial choice has a short decision record in [`docs/decisions/`](docs/decisions/) (in Chinese), covering the context, the options considered, the measurements and why one was chosen. The [index](docs/decisions/README.md) follows the project's main line. A few examples:

| # | Decision |
|---|---|
| 001 | Go deeper on reliability, cost and quality instead of adding features |
| 003 | Background processing with SQS + Lambda |
| 008 | Heartbeats and leases to detect dead workers |
| 011 | Move all AWS resources to Singapore, next to the database |
| 013 | Fixed HTTPS URL: API on Lambda behind CloudFront |
| 014 | CI/CD with GitHub Actions and OIDC |
| 017 | Evaluation method and how to avoid overfitting prompts to the test set |
| 034 | On-premises version: keep documents inside the company, train our own answer model |
| 039 | How training data is generated and filtered |
| 042 | Temperature 0, and the release bar: no worse than gpt-4o on any metric |
| 046 | Cloud Core: keep the cloud architecture, simplify the reliability mechanisms (SDK retries, no automatic re-queueing, no worker takeover) |
| 043 | Merge adjacent passages of the same document |
| 044 | Final evaluation on the held-out test set |

## Tech stack

**Backend:** Python 3.11, FastAPI, SQLAlchemy 2.0, Alembic, PostgreSQL + pgvector, pytest, ruff
**AI:** OpenAI `text-embedding-3-small` and gpt-4o; Qwen3-Embedding-0.6B and Qwen3.5-9B served with vLLM; LoRA training with PyTorch, Transformers and PEFT, tracked in MLflow; pypdf, tiktoken
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

234 tests cover ownership checks across users, the processing pipeline, retries, leases, concurrent workers, usage limits, Q&A, and that the on-premises start script runs the models with the same settings as docker-compose. OpenAI, S3 and SQS are replaced with fakes, so the suite costs nothing and needs no API key. Locally it uses a separate database configured in `.env.test` (see `.env.test.example`). In CI it uses a throwaway PostgreSQL container.

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
| POST | `/documents/{id}/retry` | Retry a failed document |
| POST | `/workspaces/{id}/ask` | Ask a question, get an answer with citations |
| GET | `/usage/today` | Today's usage against the daily limit |

## Known limitations

- Cross-lingual retrieval is weak: a Chinese question about an English document matches noticeably worse than the same question in English.
- Summary questions ("what is this paper about?") are a poor fit for top-5 passage retrieval.
- The free-tier database sleeps after 5 minutes idle, so the first request afterwards takes about 2 s longer.
- Evaluation is run manually when Q&A code changes. It is not in CI because it costs money and its results vary between runs.
- The comparison with gpt-4o covers this task only (Chinese regulation Q&A with this system's retrieval and prompt), and the test set was run once. When a question does not say which company the asker works for, the on-premises model sometimes gives only the subsidiary's rule instead of listing both.
- The on-premises version answers in about 4 s, gpt-4o in under 2 s. Even at temperature 0, vLLM does not always return the same answer twice.

## What's next

- Start the two models from docker-compose as well. The package (`onprem/`) already runs on the lab GPU machine with the models started by hand; the NVIDIA Container Toolkit is installed, but the shared machine needs about 20 GB more disk space for the vLLM image.
