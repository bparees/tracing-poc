# tracing-poc

A proof-of-concept for collecting and visualising LLM agent traces using
OpenTelemetry and Langfuse on OpenShift.

```
┌─────────────────────────────────────────────────────────┐
│  telemetry-poc namespace                                │
│                                                         │
│  ┌──────────────┐   OTLP/HTTP   ┌────────────────────┐  │
│  │  agent Job   │ ──────────▶   │  upload-collector  │  │
│  │ (pydantic-ai)│               │  ┌──────────────┐  │  │
│  └──────────────┘               │  │  OTel sidecar│  │  │
│                                 │  │ (file export)│  │  │
│                                 │  └──────┬───────┘  │  │
│                                 │         │spans.json│  │
│                                 │  ┌──────▼───────┐  │  │
│                                 │  │ trace pusher │  │  │
│                                 │  │ (→ Langfuse) │  │  │
│                                 │  └──────────────┘  │  │
│                                 └────────────────────┘  │
│                                                         │
│  ┌─────────────────────────────────────────────────┐    │
│  │ Langfuse (PostgreSQL  ClickHouse  Redis  MinIO) │    │
│  └─────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

- **Agent** — a [pydantic-ai](https://ai.pydantic.dev/) agent with stub tools
  (weather lookup, web search) that emits OTEL traces for every run.
- **OTel Collector sidecar** — receives OTLP and writes spans to a shared volume
  as JSON files (Red Hat build of OpenTelemetry operator).
- **Trace pusher** — a long-running loop that reads new span files and POSTs them
  to Langfuse via the OTLP HTTP ingest endpoint.
- **Langfuse** — in-cluster LLM observability UI (deployed as a Helm subchart).

## Prerequisites

| Tool | Purpose |
|------|---------|
| `podman` | Build and push container images |
| `helm` (v3) | Install the trace-pipeline chart |
| `oc` / `kubectl` | Interact with the cluster |
| OpenShift cluster | Target environment |
| OpenAI API key | Required by the agent |
| Image registry access | `quay.io/bparees` or your own registry |

## Step 1 — Build and push the images

Run from the repo root.

Note: The images referenced below are available and public so you can skip this step and use them as is, but if you build+push your own versions you'll need to replace the image references in the various chart parameters and deployment resources.

### Agent

```bash
podman build -f agent/Containerfile -t quay.io/bparees/tracing-poc-agent:latest agent/
podman push quay.io/bparees/tracing-poc-agent:latest
```

### Trace pusher

```bash
podman build -f pusher/Containerfile -t quay.io/bparees/trace-pusher:latest pusher/
podman push quay.io/bparees/trace-pusher:latest
```

## Step 2 — Deploy the trace pipeline on OpenShift

### 2a. Install the OpenTelemetry Operator (cluster-admin, one-time)

The operator must be running before the Helm chart is installed. Install it via
OLM (exact package/channel depends on your OpenShift version):

```bash
# Confirm the operator is available and ready
oc get csv -A | grep -i opentelemetry
oc get crd opentelemetrycollectors.opentelemetry.io
```

See the [Red Hat OpenTelemetry docs](https://docs.redhat.com/en/documentation/red_hat_build_of_opentelemetry/) for installation details.

### 2b. Generate secrets and install the chart

The chart deploys the OTel Collector CR, trace pusher Deployment, and an
in-cluster Langfuse instance. `install-openshift.sh` handles `envsubst` and
`helm dependency build` automatically.

Export required variables:

```bash
export LANGFUSE_SALT=$(openssl rand -base64 32)
export LANGFUSE_ENCRYPTION_KEY=$(openssl rand -hex 32)
export LANGFUSE_NEXTAUTH_SECRET=$(openssl rand -base64 32)
export POSTGRES_PASSWORD=$(openssl rand -hex 24)
export REDIS_PASSWORD=$(openssl rand -hex 24)
export CLICKHOUSE_PASSWORD=$(openssl rand -hex 24)
export MINIO_PASSWORD=$(openssl rand -hex 24)

# Placeholder URL — updated after the Route is created (see step 2c).
export LANGFUSE_NEXTAUTH_URL=http://localhost:3000

# API keys used by the trace pusher and pre-seeded into Langfuse via headless init.
export LANGFUSE_PUSH_PUBLIC_KEY="pk-lf-$(openssl rand -hex 24)"
export LANGFUSE_PUSH_SECRET_KEY="sk-lf-$(openssl rand -hex 24)"

# Admin credentials for the Langfuse UI.
export LANGFUSE_INIT_USER_EMAIL=admin@example.com
export LANGFUSE_INIT_USER_PASSWORD=$(openssl rand -base64 16)
```

Install:

```bash
cd charts/trace-pipeline
./install-openshift.sh install
```

Langfuse takes several minutes to start while PostgreSQL, ClickHouse, Redis, and
MinIO initialise. Watch progress with:

```bash
oc get pods -n telemetry-poc --watch
```

## Step 3 — Run the agent

The agent runs as a Kubernetes Job. Each invocation creates a new uniquely-named
Job (via `generateName`) so runs can be triggered repeatedly without cleanup.

### 3a. Create the credentials Secret

```bash
cd agent
cp deploy/secret.yaml.template deploy/secret.yaml
# Edit deploy/secret.yaml — replace sk-replace-me with your OpenAI API key.
# Do NOT commit secret.yaml (it is already in .gitignore).
kubectl apply -f deploy/secret.yaml
```

### 3b. Run the agent

```bash
# Use `create`, not `apply` — generateName requires it.
kubectl create -f deploy/job.yaml
```

Stream the logs and note the conversation ID printed at the end:

```bash
kubectl logs -n telemetry-poc -l app=telemetry-poc --tail=-1 -f
```

Example output:

```
============================================================
Collector:       http://upload-collector.telemetry-poc.svc.cluster.local:4318
Conversation ID: a3f1c2d4e5b6a3f1c2d4e5b6a3f1c2d4
User ID:         anonymous
============================================================

Turn 1/5: What's the weather like in Paris today?
Response:  The weather in Paris today is partly cloudy, 18°C ...
Trace ID:  a3f1c2d4e5b6a3f1c2d4e5b6a3f1c2d4

Session complete.
Conversation ID: a3f1c2d4e5b6a3f1c2d4e5b6a3f1c2d4
  (pass --conversation-id a3f1c2d4... to continue this conversation)
```

### 3c. Continue a conversation or submit feedback

Edit `deploy/job.yaml` to set the relevant env vars and re-run `kubectl create`:

**Continue an existing conversation:**
```yaml
- name: TELEMETRY_CONVERSATION_ID
  value: "a3f1c2d4e5b6a3f1c2d4e5b6a3f1c2d4"
- name: TELEMETRY_PROMPT
  value: "How does that compare to Tokyo?"
```

**Submit feedback on a prior run (no new prompt):**
```yaml
- name: TELEMETRY_CONVERSATION_ID
  value: "a3f1c2d4e5b6a3f1c2d4e5b6a3f1c2d4"
- name: TELEMETRY_FEEDBACK_SCORE
  value: "1"          # 1 = positive, -1 = negative
- name: TELEMETRY_FEEDBACK_COMMENT
  value: "Really helpful — clear comparison across cities."
```

See [`agent/README.md`](agent/README.md) for the full list of env vars.

## Step 4 — View traces in Langfuse

Open the Langfuse UI:

```bash
oc get route langfuse-web -n telemetry-poc -o jsonpath='https://{.spec.host}{"\n"}'
```

Log in with `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD` from step 2b.

The trace pusher runs on a 60-second interval by default.

```bash
oc logs -f deployment/upload-collector -c uploader -n telemetry-poc
```

Once pushed, navigate to **Traces** in the Langfuse sidebar. Each agent run
appears as a trace containing:

- An `agent_turn` root span with the prompt and response as attributes
- pydantic-ai model request/response spans (GenAI semantic conventions)
- Tool call spans for each `get_weather` or `search_web` invocation
- A `user_feedback` span if feedback was submitted for that run

Runs sharing the same `TELEMETRY_CONVERSATION_ID` are grouped as a single trace,
so multi-turn conversations appear as one entry with multiple turns nested inside.

## Uninstall

### Remove the agent jobs

Completed Jobs are garbage-collected automatically after 1 hour (`ttlSecondsAfterFinished: 3600`). To remove them immediately:

```bash
kubectl delete jobs -n telemetry-poc -l app=telemetry-poc
```

### Remove the agent credentials Secret

```bash
kubectl delete -f agent/deploy/secret.yaml
```

### Uninstall the trace pipeline chart

```bash
helm uninstall trace-pipeline -n telemetry-poc
```

Langfuse datastores use `emptyDir` (no PVCs), so this is a clean removal with no
leftover volumes. If you want to remove the namespace entirely:

```bash
kubectl delete namespace telemetry-poc
```

## Repository layout

```
├── agent/               # pydantic-ai agent
│   ├── agent.py         # Agent definition and stub tools
│   ├── prompts.py       # Canned prompts with feedback
│   ├── run_agent.py     # Entry point (OTEL setup, arg parsing)
│   ├── Containerfile    # Agent container image
│   ├── requirements.txt
│   ├── deploy/
│   │   ├── job.yaml              # Kubernetes Job manifest
│   │   └── secret.yaml.template  # Secret template (copy and fill in)
│   └── README.md        # Agent-specific docs
│
├── pusher/              # Trace pusher
│   ├── push_to_langfuse.py
│   ├── otlp_utils.py
│   ├── run_push_loop.sh
│   ├── Containerfile
│   └── requirements.txt
│
└── charts/
    └── trace-pipeline/  # Helm chart (collector + pusher + Langfuse)
        ├── Chart.yaml
        ├── values.yaml
        ├── values-openshift.example.yaml
        ├── install-openshift.sh
        └── README.md    # Chart-specific docs and troubleshooting
```

## Further reading

- [`agent/README.md`](agent/README.md) — agent env vars, job customisation, feedback
- [`charts/trace-pipeline/README.md`](charts/trace-pipeline/README.md) — chart configuration, troubleshooting
- [pydantic-ai OpenTelemetry docs](https://ai.pydantic.dev/logfire/)
- [Langfuse Helm chart](https://langfuse.com/self-hosting/deployment/kubernetes-helm)
- [Red Hat build of OpenTelemetry](https://docs.redhat.com/en/documentation/red_hat_build_of_opentelemetry/)
