# tracing-poc agent

A pydantic-ai agent that emits OpenTelemetry traces to a configured OTLP collector.
Runs as a Kubernetes Job — each execution sends one prompt (or a canned batch) and
records the response as OTEL spans, with optional user feedback.

## Prerequisites

- A running OTLP-compatible collector in-cluster (e.g. from the `trace-pipeline` Helm chart)
- An OpenAI API key
- `kubectl` or `oc` access to the target namespace

## 1. Build and push the image

From the repo root:

```bash
podman build -f agent/Containerfile -t quay.io/bparees/tracing-poc-agent:latest agent/
podman push quay.io/bparees/tracing-poc-agent:latest
```

## 2. Create the namespace

```bash
kubectl create namespace telemetry-poc
```

## 3. Create the credentials Secret

Copy the template and fill in your OpenAI API key:

```bash
cp deploy/secret.yaml.template deploy/secret.yaml
# Edit deploy/secret.yaml and replace sk-replace-me with your real key.
# Do NOT commit secret.yaml — add it to .gitignore.
kubectl apply -f deploy/secret.yaml
```

## 4. Run the agent

`job.yaml` uses `generateName` so each invocation creates a uniquely named Job
without needing to delete the previous one first. **Use `create`, not `apply`.**

```bash
kubectl create -f deploy/job.yaml
```

Watch it complete and stream the logs:

```bash
kubectl get jobs -n telemetry-poc -l app=telemetry-poc --watch
kubectl logs -n telemetry-poc -l app=telemetry-poc --tail=-1
```

The conversation ID is printed in the logs — save it to continue the conversation
or submit feedback later.

## 5. Customise the run

Edit `deploy/job.yaml` before running, or patch env vars on the fly:

| Env var | Default | Description |
|---------|---------|-------------|
| `OTEL_COLLECTOR_ENDPOINT` | `http://upload-collector.telemetry-poc.svc.cluster.local:4318` | OTLP HTTP endpoint |
| `OTEL_SERVICE_NAME` | `telemetry-poc` | Service name in traces |
| `TELEMETRY_PROMPT` | _(run canned batch)_ | Single prompt to run |
| `TELEMETRY_USER_ID` | `anonymous` | User identity on every span |
| `TELEMETRY_CONVERSATION_ID` | _(new conversation)_ | 32-char hex ID to continue an existing conversation |
| `TELEMETRY_FEEDBACK_SCORE` | _(none)_ | `1` (positive) or `-1` (negative) |
| `TELEMETRY_FEEDBACK_COMMENT` | _(none)_ | Freeform feedback text |
| `TELEMETRY_TURN_TRACE_ID` | _(none)_ | Specific turn trace ID to link feedback to |

### Run the canned batch of prompts

Remove (or comment out) the `TELEMETRY_PROMPT` env var in `job.yaml`. The agent
runs all five built-in prompts with their pre-canned feedback scores.

### Continue an existing conversation

Uncomment `TELEMETRY_CONVERSATION_ID` in `job.yaml` and set it to the hex ID
printed from a prior run:

```yaml
- name: TELEMETRY_CONVERSATION_ID
  value: "a3f1c2d4e5b6..."
- name: TELEMETRY_PROMPT
  value: "How does that compare to Tokyo?"
```

All turns with the same conversation ID appear as one trace in the backend.

### Submit feedback without running a new prompt

Set `TELEMETRY_CONVERSATION_ID` and feedback vars, and remove `TELEMETRY_PROMPT`:

```yaml
- name: TELEMETRY_CONVERSATION_ID
  value: "a3f1c2d4e5b6..."
- name: TELEMETRY_FEEDBACK_SCORE
  value: "1"
- name: TELEMETRY_FEEDBACK_COMMENT
  value: "Really helpful response."
# Optionally link to a specific turn:
# - name: TELEMETRY_TURN_TRACE_ID
#   value: "a3f1c2d4e5b6..."
```

## 6. Check traces in Langfuse

If the `trace-pipeline` chart is installed, get the Langfuse route:

```bash
oc get route langfuse-web -n telemetry-poc -o jsonpath='https://{.spec.host}{"\n"}'
```

Log in with the credentials from your Helm install and browse to the **Traces** view.
