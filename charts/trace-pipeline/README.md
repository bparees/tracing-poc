# trace-pipeline Helm Chart

Deploys the Lightspeed Stack trace pipeline on OpenShift:

- **OpenTelemetry Collector** (operator sidecar) — OTLP in, file exporter out
- **Trace pusher** — incremental push to Langfuse on a configurable interval
- **Langfuse** (optional subchart) — in-cluster observability UI
- **OpenShift Route** — external access to Langfuse UI

LCS and other OTLP emitters connect via the `upload-collector` Service in the same namespace.

## Prerequisites

### 1. OpenTelemetry Operator (cluster admin, one-time)

Install the Red Hat build of OpenTelemetry operator on OpenShift. Example using OLM:

```bash
# Verify the operator is available in your cluster catalog, then subscribe.
# Exact package/channel depends on your OpenShift version — see:
# https://docs.redhat.com/en/documentation/red_hat_build_of_opentelemetry/

oc get csv -A | grep -i opentelemetry
oc get crd opentelemetrycollectors.opentelemetry.io
```

The operator controller must be running before installing this chart.

### 2. Build and push the trace pusher image

```bash
podman build -f pusher/Containerfile -t quay.io/bparees/trace-pusher:latest pusher/
podman push quay.io/bparees/trace-pusher:latest
```

## Install

Generate secrets and export variables:

```bash
export LANGFUSE_SALT=$(openssl rand -base64 32)
export LANGFUSE_ENCRYPTION_KEY=$(openssl rand -hex 32)
export LANGFUSE_NEXTAUTH_SECRET=$(openssl rand -base64 32)
export POSTGRES_PASSWORD=$(openssl rand -hex 24)
export REDIS_PASSWORD=$(openssl rand -hex 24)
export CLICKHOUSE_PASSWORD=$(openssl rand -hex 24)
export MINIO_PASSWORD=$(openssl rand -hex 24)

# Placeholder until Route exists; update after first install (see below).
export LANGFUSE_NEXTAUTH_URL=http://localhost:3000

# API keys for trace pusher (also used for headless Langfuse project init).
export LANGFUSE_PUSH_PUBLIC_KEY="pk-lf-$(openssl rand -hex 24)"
export LANGFUSE_PUSH_SECRET_KEY="sk-lf-$(openssl rand -hex 24)"

# Admin login for Langfuse UI (created automatically via headless init).
export LANGFUSE_INIT_USER_EMAIL=admin@example.com
export LANGFUSE_INIT_USER_PASSWORD=$(openssl rand -base64 16)
```

Install:

```bash
chmod +x install-openshift.sh
./install-openshift.sh install
```

Upgrade after changing env vars:

```bash
./install-openshift.sh upgrade
```

Dry-run render:

```bash
./install-openshift.sh template
```

Langfuse may take several minutes to start (PostgreSQL, ClickHouse, Redis, MinIO).

**Datastore volumes:** Langfuse PostgreSQL, ClickHouse, Redis, and MinIO use
`emptyDir` (no PVCs). Data is lost when those pods restart — fine for dev/PoC,
and reinstalls no longer hit stale-PVC password mismatches.

**S3/MinIO is still required:** Langfuse stores ingested trace events in MinIO
(S3-compatible blob storage) before processing. Disabling PVCs only makes MinIO
ephemeral; it does not remove the S3 dependency. You must set
`MINIO_PASSWORD` so Langfuse web/worker credentials match the MinIO pod.

**Important:** `langfuse.fullnameOverride` must match your Helm release name (the
install script sets this via `RELEASE_NAME`). Without it, Langfuse looks for
`{release}-langfuse-postgresql` while the database Service is `{release}-postgresql`.

## Langfuse login and API keys

The install uses [Langfuse headless initialization](https://langfuse.com/self-hosting/headless-initialization).
On startup, Langfuse automatically creates:

- An admin user (`LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_PASSWORD`)
- An org and project
- Project API keys matching `LANGFUSE_PUSH_PUBLIC_KEY` / `LANGFUSE_PUSH_SECRET_KEY`

The trace pusher is pre-configured — no UI steps required. Log in to the UI with
your init email/password to browse traces.

## Langfuse external access (Route)

The chart creates an OpenShift `Route` when `langfuseRoute.enabled: true` (default).
The Route is named `langfuse-web` (overridable via `langfuseRoute.name`). It targets
the Langfuse web Service `<release>-web`, not the upload-collector OTLP Service.

```bash
oc get route langfuse-web -n telemetry-poc -o jsonpath='https://{.spec.host}{"\n"}'
```

If you do not see that Route, list all routes in the namespace:

```bash
oc get route -n telemetry-poc
helm get manifest trace-pipeline -n telemetry-poc | grep -A15 'kind: Route'
```

Re-run `./install-openshift.sh upgrade` after pulling chart changes to create or
rename the Route.

Set `LANGFUSE_NEXTAUTH_URL` (and optionally `LANGFUSE_ROUTE_HOST` /
`LANGFUSE_ROUTE_PUBLIC_URL`) to that HTTPS URL, then upgrade:

```bash
export LANGFUSE_NEXTAUTH_URL=https://<route-host>
./install-openshift.sh upgrade
```

Optional: pin a hostname with `langfuseRoute.host` and set `langfuseRoute.publicUrl` to match.

## OTLP emitters (e.g. Lightspeed Stack)

Point tracing at the in-cluster Service (plaintext OTLP):

```yaml
tracing:
  enabled: true
  otlp_endpoint: http://upload-collector.<namespace>.svc:4318
```

## Configuration reference

| Value | Default | Description |
|-------|---------|-------------|
| `pusher.intervalSeconds` | `60` | Push loop interval (1 min) |
| `pusher.langfuse.host` | in-cluster Service | Langfuse URL for pusher; override for external |
| `collector.rotation.maxMegabytes` | `100` | File exporter rotation size |
| `langfuseRoute.enabled` | `true` | Create OpenShift Route for Langfuse UI |
| `langfuseRoute.name` | `langfuse-web` | Route object name |
| `langfuse.enabled` | `true` | Deploy Langfuse subchart |
| `nameOverride` / `fullnameOverride` | `upload-collector` | Collector+uploader Deployment and OTLP Service name |
| `collector.crName` | `upload-collector` | OpenTelemetryCollector CR and sidecar injection name |
| `langfuse.fullnameOverride` | Helm release name | Must match `RELEASE_NAME` for Langfuse DB DNS |
| Langfuse datastores | `emptyDir` | No PVCs; data lost on pod restart (dev/PoC) |

After upgrade, find the collector+uploader pod with:

```bash
oc get pods -l app.kubernetes.io/component=upload-collector -n telemetry-poc
oc logs deploy/upload-collector -c uploader -n telemetry-poc
```

### Sidecar not injected on first install

On fresh install or upgrade, the first `upload-collector` pod may start without
the OTel collector sidecar. This is a timing issue: Helm creates the
`OpenTelemetryCollector` CR and the `Deployment` simultaneously, but the operator
needs a moment to reconcile the CR before its sidecar-injection webhook is active.

The chart includes a `post-install,post-upgrade` hook Job
(`upload-collector-sidecar-restart`) that waits 30 seconds then triggers a
`kubectl rollout restart` automatically. The replacement pod starts after the
webhook is ready and gets the sidecar injected correctly.

If you need to trigger it manually:

```bash
kubectl rollout restart deployment/upload-collector -n telemetry-poc
```

## Collector sidecar vs init container

On Kubernetes 1.29+ (OpenShift 4.16+), the OpenTelemetry Operator injects
collector sidecars as **native sidecar containers**: they appear under
`spec.initContainers` with `restartPolicy: Always`, not under `spec.containers`.

```bash
oc get pod -l app.kubernetes.io/component=upload-collector -o jsonpath='{range .items[0].spec.initContainers[*]}{.name}{" restartPolicy="}{.restartPolicy}{"\n"}{end}'
# otc-container restartPolicy=Always
```

This is different from:

- **Short-lived init containers** (run once at startup, then exit) — used by
  auto-instrumentation to copy agent files
- **Legacy sidecars** — a second entry in `spec.containers`

The native sidecar starts before `uploader`, keeps running for the pod's
lifetime, and shares the pod network namespace (localhost OTLP still works). See
[Kubernetes native sidecars](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/)
and the [OTel operator sidecar injection docs](https://github.com/open-telemetry/opentelemetry-operator#sidecar-injection).

## Troubleshooting

### Langfuse web: `P1013` / invalid port in database URL

Langfuse builds PostgreSQL `DATABASE_URL` from the password. **Base64 passwords**
(`openssl rand -base64`) often contain `/`, `+`, or `=` which break URL parsing
and produce misleading "invalid port number" errors.

**Check:**

```bash
oc get secret trace-pipeline-postgresql -n telemetry-poc -o jsonpath='{.data.password}' | base64 -d; echo
oc exec deploy/trace-pipeline-web -n telemetry-poc -- printenv DATABASE_URL
```

If the password contains `/`, `+`, `=`, or `@`, that's the problem.

**Fix:** use hex passwords (URL-safe), uninstall, and reinstall:

```bash
export POSTGRES_PASSWORD=$(openssl rand -hex 24)
export REDIS_PASSWORD=$(openssl rand -hex 24)
export CLICKHOUSE_PASSWORD=$(openssl rand -hex 24)
export MINIO_PASSWORD=$(openssl rand -hex 24)
# re-export other LANGFUSE_* vars, then:
helm uninstall trace-pipeline -n telemetry-poc
./install-openshift.sh install
```

### Langfuse web: `Can't reach database server at ...-langfuse-postgresql`

The Langfuse subchart names data stores `{release}-postgresql`, but Langfuse app
env defaults to `{release}-langfuse-postgresql` unless `langfuse.fullnameOverride`
matches the release name. Fix:

```bash
./install-openshift.sh upgrade
```

Also confirm PostgreSQL is running:

```bash
oc get pods -l app.kubernetes.io/name=postgresql -n telemetry-poc
oc get svc trace-pipeline-postgresql -n telemetry-poc
```

### Trace push: `Failed to upload JSON to S3` (HTTP 500)

Langfuse always uploads OTLP trace payloads to MinIO before ingestion. This is
**not** caused by switching Postgres/Redis to `emptyDir` — it means Langfuse
web cannot authenticate to MinIO.

**Diagnose:**

```bash
# MinIO pod should be Running
oc get pods -n telemetry-poc -l app.kubernetes.io/name=s3

# Langfuse web must have a non-empty S3 secret (common bug: empty string)
oc set env deployment/trace-pipeline-web -n telemetry-poc --list | grep LANGFUSE_S3_EVENT_UPLOAD
```

If `LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY` is empty, MinIO has a password
but Langfuse web does not.

**Fix:** set `MINIO_PASSWORD` and upgrade (use hex, same as other datastore
passwords):

```bash
export MINIO_PASSWORD=$(openssl rand -hex 24)
# re-export other LANGFUSE_* / POSTGRES_* / REDIS_* / CLICKHOUSE_* vars, then:
./install-openshift.sh upgrade
```

After upgrade, confirm the secret is populated and retry a push:

```bash
oc rollout status deployment/trace-pipeline-web -n telemetry-poc
oc logs -f deployment/upload-collector -c uploader -n telemetry-poc
```

### Trace push / Langfuse web: `SignatureDoesNotMatch`

This means Langfuse web **is** sending credentials, but they do not match what
the MinIO pod was started with. Typical after a `helm upgrade` that changed
`MINIO_PASSWORD`: Langfuse web got the new password inline, while MinIO still
uses the old password from its Secret or from its first boot.

**Compare credentials:**

```bash
oc get secret trace-pipeline-s3 -n telemetry-poc -o jsonpath='{.data.root-user}' | base64 -d; echo
oc set env deployment/trace-pipeline-web -n telemetry-poc --list | grep LANGFUSE_S3_EVENT_UPLOAD
```

**Fix (PoC / emptyDir):** recycle MinIO with a single shared Secret. The chart
creates `trace-pipeline-s3` and wires Langfuse web to read it via
`secretKeyRef` (not inline env vars):

```bash
# re-export MINIO_PASSWORD and other required vars
./install-openshift.sh upgrade
oc delete pod -n telemetry-poc -l app.kubernetes.io/name=s3
oc rollout restart deployment/trace-pipeline-web -n telemetry-poc
oc rollout restart deployment/trace-pipeline-worker -n telemetry-poc
```

If the Secret still has a stale password from an earlier install, delete it
before upgrade so Helm recreates it from `MINIO_PASSWORD`:

```bash
oc delete secret trace-pipeline-s3 -n telemetry-poc
./install-openshift.sh upgrade
oc delete pod -n telemetry-poc -l app.kubernetes.io/name=s3
```

## Uninstall

```bash
helm uninstall trace-pipeline -n telemetry-poc
```

Langfuse datastores use `emptyDir` (no PVCs), so a plain uninstall is enough for
a clean reinstall.

## See also

- [RHBOT file exporter](https://docs.redhat.com/en/documentation/red_hat_build_of_opentelemetry/3.10/html-single/configuring_the_collector/index#otel-exporters-file-exporter_otel-collector-exporters)
- [Langfuse Helm chart](https://langfuse.com/self-hosting/deployment/kubernetes-helm)
