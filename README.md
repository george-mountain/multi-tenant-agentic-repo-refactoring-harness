# Refactor Harness

Multi-tenant web application that runs autonomous AI refactoring agents (Planner → Executor → Verifier ⇄ Supervisor) against GitHub repositories. Three microservices — React/TS frontend, FastAPI backend control plane, LangGraph agent workers — plus a privileged sandbox orchestrator, two Postgres instances, Redis, MinIO, and a Langfuse/Prometheus/Grafana observability stack. Docker Compose only; no Kubernetes.

## Prerequisites

- Docker
- An OpenAI or Gemini API key
- Optionally a GitHub token — without it (or if it lacks push rights), runs still finish with a downloadable patch instead of a pull request

### GitHub token requirements (for automatic PRs)

The token must be able to **push** to the target repository:

- **Classic personal access token**: the `repo` scope.
- Organization repos may additionally require the token to be SSO-authorized for that org.

The harness verifies this at ingest and warns in the run's activity feed if the token can't push; a push failure never discards a finished refactor — the run completes with the final patch artifact.

## Quick start

```bash
cp .env.example .env        # then set OPENAI_API_KEY (or GOOGLE_API_KEY) and optionally GITHUB_TOKEN

make up                     # or: docker compose up -d --build

open http://localhost:8080
```

One command brings up everything: the three microservices, the sandbox orchestrator, the per-run sandbox image build, both Postgres instances, Redis, MinIO, and the observability stack (Langfuse, Prometheus, Grafana). Run `make help` for all targets.

Create an organization, paste a repository URL, and watch the run live: the agent audits the codebase, plans the work, refactors it step by step with a self-correcting verification loop, and opens a pull request — all streamed over SSE.

## Scaling workers

One worker handles exactly one concurrent run:

```bash
make scale WORKERS=8        # or: docker compose up -d --scale agent-worker=8
```

## Observability

Started automatically with `make up`:

- Langfuse: http://localhost:3001 — create a project, put its keys in `.env` (`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`), then re-run `make up` to enable LLM tracing. Leave `LANGFUSE_HOST=http://langfuse-web:3000` (the internal container port — not the `3001` you browse to).
- Prometheus: http://localhost:9092 · Grafana: http://localhost:3002 (admin/admin)

## Run history

The dashboard keeps the repo-URL launcher pinned at the top while the history scrolls beneath it. History supports full-text search (repository or objective), status filtering (all / in progress / completed / failed), infinite-scroll lazy loading, and per-run deletion (completed and failed runs; in-progress runs must finish first). Deleting a run also removes its stored artifacts.

## Service map

| URL | Service |
|---|---|
| http://localhost:8080 | web app (nginx → frontend + `/api` → backend) |
| http://localhost:9001 | MinIO console |
| http://localhost:3001 | Langfuse |
| http://localhost:9092 / :3002 | Prometheus / Grafana |

## Architecture

### High level

Three microservices behind nginx. The backend is a stateless control plane; the agent layer scales horizontally (one active run per worker); only the sandbox orchestrator is privileged. State lives in Postgres/Redis/MinIO, so scaling is just adding workers.

```mermaid
flowchart TB
    subgraph Clients["Clients (multiple concurrent tenants)"]
        B1["Tenant A users"]
        B2["Tenant B users"]
    end

    subgraph Edge["Edge"]
        NGINX["nginx reverse proxy<br/>routing: / → frontend, /api → backend,<br/>SSE passthrough (buffering off)"]
    end

    subgraph FE["Frontend Microservice"]
        WEB["React + TypeScript (Vite) SPA, served by nginx<br/>repo-URL-only launcher · run history + stats ·<br/>live pipeline stepper · plan & diff viewer ·<br/>supervisor decisions in activity feed"]
    end

    subgraph BE["Backend Microservice (Control Plane) — FastAPI, scale ×N"]
        API["REST API<br/>auth (JWT + tenant_id claim),<br/>runs CRUD, plan approval,<br/>artifact endpoints (diffs/patch/report)"]
        SSE["SSE endpoint<br/>replays persisted history,<br/>then streams live events"]
        ADM["Admission control + enqueue<br/>per-tenant concurrency cap,<br/>GitHub-URL validation"]
        CONS["Event consumer<br/>persists events, projects<br/>run status/plan/PR into postgres-app"]
    end

    subgraph MQ["Redis"]
        STREAM["Streams: run jobs<br/>consumer groups · XAUTOCLAIM reclaim ·<br/>claim heartbeats while a run is active"]
        PUBSUB["Pub/Sub: run event fan-out<br/>+ per-run event seq counters"]
    end

    subgraph AGENTS["AI Agent Layer (Data Plane) — LangGraph workers ×N, one active run each"]
        W1["agent-worker 1<br/>Planner → Executor →<br/>Verifier ⇄ Supervisor"]
        WN["agent-worker N<br/>(same graph, own run)"]
    end

    subgraph SBX["Sandbox Orchestrator (only service with docker.sock)"]
        SBAPI["Sandbox API — stage allowlist enforced<br/>create / exec / read / search_replace /<br/>ast_edit / write / network / destroy"]
        C1["ephemeral container sbx-{run}<br/>own volume · non-root · cap_drop ALL<br/>egress bridge (ICC off) attached only<br/>during clone / install / push"]
        C2["ephemeral container sbx-{run'}<br/>(another tenant's run,<br/>physically unreachable from C1)"]
    end

    subgraph DATA["State & Storage"]
        PGAPP[("postgres-app<br/>tenants, users, runs,<br/>plans, run_events")]
        PGAGENT[("postgres-agent<br/>LangGraph checkpoints,<br/>LLM call ledger")]
        MINIO[("MinIO (S3)<br/>repo snapshots, per-step git bundles,<br/>step diffs, final patch, failure reports")]
    end

    subgraph OBS["Observability (part of the default stack)"]
        LF["Langfuse (+ ClickHouse)<br/>LLM traces, tokens per node,<br/>verify-loop + supervisor spans"]
        PROM["Prometheus + Grafana<br/>backend /metrics,<br/>worker run counters"]
    end

    EXT["GitHub<br/>token-based clone, branch push, PR —<br/>degrades to patch artifact without token"]
    LLM["LLM providers (pluggable factory)<br/>openai · gemini — per-tenant/run setting"]

    B1 & B2 --> NGINX
    NGINX --> WEB
    NGINX --> API
    NGINX --> SSE
    WEB -.->|"REST + SSE"| API
    API --> PGAPP
    API --> ADM
    ADM --> STREAM
    STREAM --> W1 & WN
    W1 & WN -->|"tool calls over internal HTTP<br/>(X-Agent-Stage header)"| SBAPI
    SBAPI --> C1 & C2
    W1 & WN -->|"checkpoints + ledger"| PGAGENT
    W1 & WN --> MINIO
    W1 & WN --> LLM
    W1 & WN -->|"events"| PUBSUB
    PUBSUB --> SSE
    PUBSUB --> CONS
    CONS --> PGAPP
    W1 & WN -->|"traces"| LF
    C1 -->|"git clone / push<br/>(egress window only)"| EXT
    API -->|"artifact reads"| MINIO
    PROM -.-> BE & AGENTS
```

### Low level — LangGraph orchestration

One graph instance per run, executed inside a single agent-worker, checkpointed to `postgres-agent` after every node. The Planner audits and plans, the Executor makes surgical cross-cutting edits, the Verifier gates them (tests + critic), and the Supervisor adaptively drives the loop (retry / revise / accept / skip / abort).

```mermaid
flowchart TB
    START(("START")) --> INGEST

    INGEST["ingest<br/>clone repo (auto-detect default branch<br/>unless one was given), pin base_sha,<br/>cut branch refactor/{run_id},<br/>install deps + snapshot bundle → MinIO,<br/>auto-detect test & lint commands,<br/>then DETACH from network"]

    subgraph P["STAGE 1 — PLANNER (context isolation + autonomous audit)"]
        PLAN["planner_agent<br/>READ-ONLY tools: read_file · list_dir<br/>(write calls 403'd by sandbox API).<br/>No objective given? → audits the codebase<br/>and writes the objective itself"]
        VALIDATE{"strict JSON plan<br/>(Pydantic schema,<br/>≤3 retries with<br/>errors fed back)"}
        PLAN --> VALIDATE
        VALIDATE -->|"invalid"| PLAN
    end

    INGEST --> PLAN
    VALIDATE -->|"valid → publish plan"| GATE

    GATE["plan_gate<br/>optional human approval<br/>(LangGraph interrupt; runs resume<br/>via a queued resume job)"]

    GATE --> ROUTER{"route_step<br/>steps remaining?"}

    subgraph E["STAGE 2 — EXECUTOR (surgical edits, sandbox-scoped)"]
        EXEC["executor_agent<br/>search (git grep: find real paths + all call sites)<br/>read_file · search_replace (anchored, unique match)<br/>ast_edit (ast-grep codemods) · write_file (new files)<br/>edits ALL affected files, not just the plan's list;<br/>input: step + files + error_log[] (incl. guidance)"]
    end

    subgraph V["STAGE 3 — VERIFIER (layered back-pressure)"]
        GATES["verify_gates (deterministic, unspoofable)<br/>adaptive guard: auto-adopt related files into scope;<br/>HARD-fail only on test deletion or >N-file sprawl —<br/>then tests + linter → structured ErrorRecords"]
        CRITIC["critic (semantic review)<br/>diff vs step intent: behavior preserved?<br/>conventions? hidden API changes?<br/>→ ReviewFindings"]
        GATES -->|"gates pass"| CRITIC
    end

    ROUTER -->|"next step k"| EXEC
    EXEC --> GATES

    SUP["supervisor (adaptive loop control)<br/>perceives: error trajectory across attempts,<br/>which gate failed (test vs reviewer),<br/>diff, attempts vs ceiling, tokens vs budget →<br/>retry w/ guidance · revise_step · accept_step<br/>(commit when tests pass & only nits remain) ·<br/>skip_step · abort"]

    SKIP["skip_step<br/>discard uncommitted edits,<br/>mark step skipped, continue"]

    COMMIT["commit_step<br/>git commit on run branch,<br/>export diff + git bundle → MinIO,<br/>attempt=0, error_log=[]"]

    GATES -->|"gates fail"| SUP
    CRITIC -->|"blocking_issues"| SUP
    CRITIC -->|"approved (no blockers)"| COMMIT
    COMMIT --> ROUTER
    SUP -->|"retry / revise_step<br/>(attempt++, guidance & revised<br/>plan step into state)"| EXEC
    SUP -->|"accept_step (tests green,<br/>reviewer nits only)"| COMMIT
    SUP -->|"skip_step"| SKIP
    SKIP --> ROUTER
    SUP -->|"abort decision · hard ceiling<br/>(10 attempts/step) · token budget<br/>(deterministic BACKSTOPS)"| ABORT

    ROUTER -->|"all steps done"| FINALV["final_verify<br/>full test suite + lint on completed tree,<br/>export final patch → MinIO"]
    FINALV -->|"pass"| PR["open_pr<br/>reattach network, push refactor/{run_id}<br/>via GitHub token, open PR against base branch<br/>(never commits to main; without a token the<br/>final patch artifact is the deliverable)"]
    FINALV -->|"fail"| ABORT

    ABORT["abort_rollback<br/>git reset --hard to branch point,<br/>persist failure report → MinIO,<br/>destroy sandbox, emit run_failed"]

    PR --> DONE(("END: success"))
    ABORT --> FAILED(("END: reported failure"))

    CKPT[("PostgresSaver (postgres-agent)<br/>checkpoint after EVERY node<br/>thread_id = run_id ·<br/>LLM calls replayed from ledger on resume")]
    INGEST -.-> CKPT
    PLAN -.-> CKPT
    EXEC -.-> CKPT
    GATES -.-> CKPT
    SUP -.-> CKPT
    COMMIT -.-> CKPT
```

### Highlights

- **Isolation**: only the sandbox orchestrator mounts the Docker socket. Each run gets an ephemeral container + volume (non-root, cap-dropped, egress network attached only during clone/install/push). The Planner stage is rejected server-side on any write endpoint.
- **Resilience**: LangGraph checkpoints in `postgres-agent`, jobs on Redis Streams with `XAUTOCLAIM` reclaim and claim heartbeats, sandbox reconstruction from MinIO git bundles, and an LLM call ledger so a resumed run replays completions instead of re-billing them.
- **Guardrails**: an adaptive supervisor drives the verification loop (retry with guidance, revise step scope, accept when tests pass and only reviewer nits remain, skip step, or abort based on the error trajectory), bounded by deterministic backstops — a 10-attempt-per-step hard ceiling and a per-run token budget. Because refactoring is cross-cutting, the Executor can `search` (git grep) for real paths and all call sites, and the scope guard auto-adopts the related files it edits rather than blocking them; only test deletion and runaway file-count sprawl hard-fail, with tests and the LLM critic enforcing that a change is complete and correct.
