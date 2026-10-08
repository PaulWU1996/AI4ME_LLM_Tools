# AI4ME LLM Tools

A containerized FastAPI service that processes transcripts with a local Ollama LLM. An external orchestrator sends a `job_id` and a `mode`; each mode is a distinct function with its own prompt and response schema. The service reads the transcript from a shared volume, runs it through the LLM, and returns the mode's structured result — also writing it to `output.json` on the shared volume.

## How it works

```
Orchestrator
    │  1. Writes  shared/{job_id}/transcript.txt
    │  2. POST /process  {"job_id": "...", "job_type": "script", "mode": "summary"}
    │  3. Reads   shared/{job_id}/output.json
    ▼
llm-tools container
    ├── FastAPI :8000
    └── Ollama  :11434 (localhost only, models bind-mounted from host)
```

## Quick start

```bash
# 1. Edit config/model.txt and set it to the model you want to use

# 2. Build and start — automatically detects GPU and picks the right mode
#    First run: the container will automatically pull the model from config/model.txt
#    into ./weights/ollama if it isn't already there (requires internet access, may take a few minutes).
./scripts/run.sh up --build

# 3. Poll until ready
curl http://localhost:8000/health

# 4. Create a test job and send it
mkdir -p ./shared/test123
echo "FIFA is a sports governing body that organizes association football events all over the world. FIFA outlines several objectives in its organisational statutes, including growing the game internationally, ensuring it is accessible to everyone, and advocating for integrity and fair play.[7] It is responsible for organising and promoting association football's major international tournaments, notably the World Cup which began in 1930, and the Women's World Cup which commenced in 1991. Although FIFA does not solely set the laws of the game, that being the responsibility of the International Football Association Board of which FIFA is a member, it applies and enforces the rules across all FIFA competitions.[8] All FIFA tournaments generate revenue from sponsorships; in 2022, FIFA had revenues of over US$5.8 billion, ending the 2019–2022 cycle with a net positive of $1.2 billion, and cash reserves of over $3.9 billion." > ./shared/test123/transcript.txt

curl -X POST http://localhost:8000/process \
  -H 'Content-Type: application/json' \
  -d '{"job_id": "test123", "job_type": "script"}'

# With custom requirements (overrides only the editable part of the prompt):
curl -X POST http://localhost:8000/process \
  -H 'Content-Type: application/json' \
  -d '{
    "job_id": "test123",
    "job_type": "script",
    "prompts": "You are a news editor. Write a punchy headline and a one-sentence summary."
  }'

# Run another mode (tags instead of title + summary):
curl -X POST http://localhost:8000/process \
  -H 'Content-Type: application/json' \
  -d '{"job_id": "test123", "job_type": "script", "mode": "tagging"}'

# With a callback URL (result is POSTed there after processing):
curl -X POST http://localhost:8000/process \
  -H 'Content-Type: application/json' \
  -d '{"job_id": "test123", "job_type": "script", "callback_url": "http://orchestrator-host/jobs/test123/done"}'
```

## API

### `POST /process`

| Field | Type | Required | Description |
|---|---|---|---|
| `job_id` | string | yes | Orchestrator-assigned job identity |
| `job_type` | string | yes | Must be `"script"` |
| `mode` | string | no | Which function to run — `"summary"` (default) or `"tagging"`. See [Modes](#modes) |
| `language` | string | no | Response language, e.g. `"en"`, `"zh"` (default `"en"`) |
| `callback_url` | string | no | If set, result is POSTed here after `output.json` is written |
| `prompts` | string | no | Overrides the requirements section of the mode's prompt (see [Prompt structure](#prompt-structure)); must contain a `{language}` slot |
| `transcript` | string | no | Transcript text to process directly. If omitted, falls back to reading `shared/{job_id}/transcript.txt` |
| `record_key` | string | no | Key to upsert the result under in MongoDB, if configured. Defaults to `job_id` when omitted. See [Result persistence](#result-persistence) |
| `metadata` | object | no | Arbitrary caller-supplied context (e.g. `vpid`, `scene_id`) stored as-is alongside the result in MongoDB. Not interpreted by this service |

**Response (HTTP 200):** the shape depends on `mode`. For `summary`:
```json
{
  "job_id": "test123",
  "title": "Why Morning Routines Are Secretly Rewriting Your Brain",
  "summary": "Researchers found that habits formed before 9 AM have an outsized impact on daily productivity, driven by peak prefrontal cortex plasticity immediately after waking.",
  "model": "llama3.2:3b",
  "processing_time_ms": 4217
}
```

For `tagging`:
```json
{
  "job_id": "test123",
  "tags": ["science", "habits", "productivity"],
  "model": "llama3.2:3b",
  "processing_time_ms": 3100
}
```

The same payload is written to `shared/{job_id}/output.json`, and (if MongoDB is configured) upserted into MongoDB — see [Result persistence](#result-persistence).

**Error responses:**

| Status | Condition |
|---|---|
| 404 | No `transcript` field in the request and `transcript.txt` not found for the given `job_id` |
| 413 | Transcript exceeds `MAX_TRANSCRIPT_CHARS` limit |
| 422 | `job_type` is not `"script"`, or transcript is empty |
| 503 | Ollama is not ready |

### `GET /health`

```json
{ "status": "ok", "ollama_ready": true, "model": "llama3.2:3b", "mongo_ready": true }
```

Returns HTTP 503 if Ollama is not ready. Poll this before sending the first job.

`mongo_ready` is `true` both when MongoDB is reachable and when it isn't configured at all (the feature is off, not degraded) — it's only `false` when `MONGO_HOST` is set but the connection fails. It never causes a 503; Ollama is the only hard dependency.

## Modes

A mode is one function this service can perform on a transcript. Each mode has a name, a response schema, and its own prompt files. The mode is selected with the `mode` field on every request.

| Mode | Response fields | Purpose |
|---|---|---|
| `summary` (default) | `title`, `summary` | Catchy headline + bullet-point summary |
| `tagging` | `tags` | Descriptive topic/keyword tags in order of discussion |

**Adding a new mode** (e.g. `sentiment`) requires two prompt files:

```
prompts/sentiment/transcript.txt         # requirements — must contain a {language} slot
prompts/sentiment/output_structure.txt   # the exact JSON shape the model must return
```

Then register the mode in the `MODES` registry in `app/routers/process.py`:

```python
MODES: dict[str, tuple[Path, type[SummaryResponse] | type[TaggingResponse] | ...]] = {
    "summary": (Path("summary"), SummaryResponse),
    "tagging": (Path("tagging"), TaggingResponse),
    "sentiment": (Path("sentiment"), SentimentResponse),   # new
}
```

The response model declares the mode's output fields, and a matching branch in `_validate_result` checks the LLM's JSON before it is returned or written to `output.json`. No other code changes are needed — the mode is discovered at request time, so any new `mode` value starts working immediately after a rebuild.

## Composing with the orchestrator

Build the image once from this repo, then reference it by name in the orchestrator's `docker-compose.yml` — no source code needed on the orchestrator side.

**Step 1 — Build the image:**
```bash
docker compose build
```

**Step 2 — Add this snippet to the orchestrator's `docker-compose.yml`:**
```yaml
  transcript-processor:
    image: ai4me-transcript:latest
    container_name: transcript-processor
    ports:
      - "8000:8000"
    volumes:
      - ./weights/ollama:/root/.ollama/models
      - ./shared:/shared
      - ./prompts:/app/prompts
      - ./config:/app/config
    environment:
      - SHARED_VOLUME_PATH=/shared
      - UVICORN_WORKERS=1
      - UVICORN_LOG_LEVEL=info
      - MAX_TRANSCRIPT_CHARS=0
      - MONGO_HOST=${MONGO_HOST:-}
      - MONGO_PORT=${MONGO_PORT:-27017}
      - MONGO_MACHINE_USER=${MONGO_MACHINE_USER:-}
      - MONGO_MACHINE_PASSWORD=${MONGO_MACHINE_PASSWORD:-}
      - MONGO_DATABASE=${MONGO_DATABASE:-ai4me_llm_tools}
      - MONGO_COLLECTION=${MONGO_COLLECTION:-transcript_results}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 60s
    restart: unless-stopped
    # Remove the deploy block below if running CPU-only
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

If distributing across machines, push to a registry first. For AWS ECR, use the bundled script instead of plain `docker tag`/`docker push`:

```bash
cp .env.example .env   # fill in AWS_ACCOUNT_ID, ECR_REPO, AWS_REGION below (Mongo vars are unrelated to this step)
./scripts/push-to-ecr.sh
```

Prerequisites: [AWS CLI](https://aws.amazon.com/cli/) installed and `aws configure`'d with credentials that can push to ECR.

`.env` (git-ignored — copy from `.env.example` and never commit it):

| Variable | Required for | Notes |
|---|---|---|
| `AWS_ACCOUNT_ID` | ECR push | Your AWS account id; used to build the registry hostname |
| `ECR_REPO` | ECR push | Target ECR repository name; created automatically if it doesn't exist |
| `AWS_REGION` | ECR push | AWS region the repository lives in |
| `MONGO_*` | Result persistence (runtime, not build) | See [Result persistence](#result-persistence) — unrelated to pushing the image |

`./scripts/push-to-ecr.sh` (see `scripts/lib-ecr.sh`) loads `.env`, logs into ECR, creates the repository if missing, builds the image for `linux/amd64`, and pushes it as `<account>.dkr.ecr.<region>.amazonaws.com/<ECR_REPO>:summarise`.

If you'd rather push to a different registry (e.g. Docker Hub, GHCR) instead of ECR, skip the script and use plain `docker tag`/`docker push`:
```bash
docker tag ai4me-transcript:latest your-registry/ai4me-transcript:latest
docker push your-registry/ai4me-transcript:latest
```
Then update `image:` in the snippet above to match the registry path.

The orchestrator must provide its own `./config/model.txt` and `./prompts/` directories alongside its `docker-compose.yml` — see [Model switching](#model-switching) and [Prompt structure](#prompt-structure).

## Prompt structure

Each mode's prompt is assembled from two files inside `prompts/{mode}/`:

| File | Editable | Purpose |
|---|---|---|
| `prompts/{mode}/transcript.txt` | Yes — overridable via the `prompts` field | Requirements: what the model should produce and in what style |
| `prompts/{mode}/output_structure.txt` | No — always fixed | Output schema: the exact JSON format the model must return |

The final prompt assembled at runtime looks like:

```
<system>
{requirements}          ← from {mode}/transcript.txt, or the prompts field if provided
{output_structure}      ← always from {mode}/output_structure.txt, never overridden
</system>

<user>
Transcript:
---
{transcript text}       ← injected by the service, not part of either template
---
Produce the JSON output now.
</user>
```

Keeping the output structure fixed means the JSON parser always gets a predictable response regardless of what custom requirements are passed in. When providing a custom `prompts` value, only include a `{language}` slot — the transcript and output format are handled automatically.

## Configuration

Most values are hardcoded in `docker-compose.yml`. Two things are instead mounted from the host so they can be changed without rebuilding the image — see below.

| Variable | Default | Notes |
|---|---|---|
| `SHARED_VOLUME_PATH` | `/shared` | Container-side path — matches `./shared` mount |
| `MAX_TRANSCRIPT_CHARS` | `0` | Character limit per request; `0` = no limit |

The model is not an environment variable — see [Model switching](#model-switching) below.

### Prompts mount

`./prompts` (at the repo root, alongside `docker-compose.yml`) is bind-mounted to `/app/prompts` inside the container. An external party can edit an existing mode's `transcript.txt` / `output_structure.txt`, or add a whole new mode folder, directly on the host — changes take effect on the next request, no rebuild needed. Registering a brand-new `mode` value still requires adding it to `JOB_TYPES` in `app/routers/process.py` (and a rebuild), since that's a code change, not a prompt change.

### Model switching

`./config` is bind-mounted to `/app/config`. `config/model.txt` holds the active model tag as a single line, e.g.:

```
llama3.2:3b
```

It is re-read on every `/process` request, so editing this file and sending the next request switches the model immediately — no restart required. This is meant for occasional manual switches, not per-request routing; Ollama itself keeps the current model loaded in memory and only swaps it when the tag actually changes.

The model must already be pulled into `./weights/ollama` before switching to it — the container only auto-pulls the model named in `config/model.txt` at startup, not on every switch.

`config/model.txt` is required — the container refuses to start if it's missing or empty.

### Result persistence

By default, results only go to `shared/{job_id}/output.json`. Setting `MONGO_HOST` (and the other `MONGO_*` env vars — see `.env.example`) also upserts every successful result into MongoDB. The feature is entirely optional: with `MONGO_HOST` unset, no connection is ever attempted and nothing else changes.

Documents are upserted by a `key` field, derived as `record_key` if the request provided one, otherwise `job_id`:

```json
{
  "key": "<record_key or job_id>",
  "job_id": "test123",
  "record_key": null,
  "job_type": "summary",
  "language": "en",
  "title": "...",
  "summary": "...",
  "model": "llama3.2:3b",
  "processing_time_ms": 4217,
  "metadata": null,
  "created_at": "2026-10-08T12:00:00Z",
  "updated_at": "2026-10-08T12:00:00Z"
}
```

`job_id` and `record_key` serve different purposes and are both kept on the document:
- **`job_id`** is the per-invocation id used for the shared-volume path, logs, and response correlation. It doesn't need to repeat across calls.
- **`record_key`** (via the `record_key` request field) lets a caller supply its own stable, deterministic key — e.g. a hash of `programme_id + scene_id + sorted avoid-types` — so that re-processing "the same logical thing" under a different `job_id` still upserts the same document instead of creating a new one. This is what makes a cache-check pattern work: a caller can query MongoDB directly by that same key *before* ever calling `/process`, and only call it on a miss.

`metadata` is stored exactly as given, under its own key — use it to carry whatever context your orchestrator needs to look records up later (`vpid`, `scene_id`, etc.); this service never reads or validates it.

A Mongo write failure (unreachable, auth error, etc.) is logged but never fails the `/process` request — `output.json` remains the durable, required artifact, and MongoDB is an additive sink on top of it.

**Env vars** (see `.env.example`):

| Variable | Default | Notes |
|---|---|---|
| `MONGO_HOST` | *(unset)* | Leave unset to disable the feature entirely |
| `MONGO_PORT` | `27017` | |
| `MONGO_MACHINE_USER` | *(unset)* | Omit for an unauthenticated connection (e.g. local testing) |
| `MONGO_MACHINE_PASSWORD` | *(unset)* | |
| `MONGO_DATABASE` | `ai4me_llm_tools` | |
| `MONGO_COLLECTION` | `transcript_results` | |

In `docker-compose.yml`, all `MONGO_*` values are pulled from `${VAR}` (with sensible defaults, e.g. `MONGO_HOST` defaults to empty = feature off) — set them in a local `.env` file (git-ignored) rather than editing the compose file, so credentials are never committed.

**Operational note:** once a real MongoDB instance is in use, add a unique index on `key` to guarantee upsert correctness under concurrent writes:
```
db.transcript_results.createIndex({"key": 1}, {"unique": true})
```
This service doesn't manage index lifecycle, so this is a one-time manual/deploy-time step.

## GPU support

Use `scripts/run.sh` instead of `docker compose` directly — it detects GPU availability and picks the right compose configuration automatically:

```bash
./scripts/run.sh up --build   # GPU mode if nvidia-smi found, CPU mode otherwise
./scripts/run.sh down         # any other docker compose subcommand works too
```

The script prints which mode was chosen at startup:
```
GPU detected: NVIDIA GeForce RTX 4090 — starting in GPU mode
# or
No GPU detected — starting in CPU mode
```

Internally it merges `docker-compose.yml` (base) with `docker-compose.gpu.yml` (GPU override) when a GPU is found. For CPU-only, the base file is used alone — no manual edits needed.

**GPU prerequisite**: [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) must be installed on the host.

| Mode | Inference time (3B model) |
|---|---|
| CPU | 15–90 s |
| GPU (e.g. RTX 4090) | 1–5 s |

## Logging

Logs go to stdout and are captured by Docker:

```bash
docker logs -f transcript-processor          # follow live
docker logs --tail 50 transcript-processor   # last 50 lines
docker logs transcript-processor 2>&1 | grep "job_id=test123"  # filter by job
docker logs transcript-processor 2>&1 | grep "ERROR"           # errors only
```

A happy-path request produces:
```
INFO  | job received      | job_id=abc123 job_type=script language=en
INFO  | transcript read   | job_id=abc123 chars=4821
INFO  | ollama call start | job_id=abc123
INFO  | ollama call done  | job_id=abc123 ms=18432
INFO  | output written    | job_id=abc123 path=/shared/abc123/output.json
```

## Notes

- **Serial processing**: one request at a time. Scale by running multiple container replicas behind a load balancer.
- **Models are not bundled in the image**: stored in `./weights/ollama`, pulled automatically on first run.
- **Orchestrator timeout**: set above 120 s for CPU, 30 s is sufficient for GPU.
