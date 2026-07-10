#!/usr/bin/env bash
# Render values-openshift.example.yaml from env vars and run helm.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VALUES_TEMPLATE="${SCRIPT_DIR}/values-openshift.example.yaml"
RELEASE_NAME="${RELEASE_NAME:-trace-pipeline}"
NAMESPACE="${NAMESPACE:-telemetry-poc}"

REQUIRED_VARS=(
    LANGFUSE_SALT
    LANGFUSE_ENCRYPTION_KEY
    LANGFUSE_NEXTAUTH_SECRET
    LANGFUSE_NEXTAUTH_URL
    POSTGRES_PASSWORD
    REDIS_PASSWORD
    CLICKHOUSE_PASSWORD
    MINIO_PASSWORD
    LANGFUSE_PUSH_PUBLIC_KEY
    LANGFUSE_PUSH_SECRET_KEY
    LANGFUSE_INIT_USER_EMAIL
    LANGFUSE_INIT_USER_PASSWORD
)

SUBST_VARS=(
    LANGFUSE_SALT
    LANGFUSE_ENCRYPTION_KEY
    LANGFUSE_NEXTAUTH_SECRET
    LANGFUSE_NEXTAUTH_URL
    POSTGRES_PASSWORD
    REDIS_PASSWORD
    CLICKHOUSE_PASSWORD
    MINIO_PASSWORD
    LANGFUSE_PUSH_PUBLIC_KEY
    LANGFUSE_PUSH_SECRET_KEY
    LANGFUSE_ROUTE_HOST
    LANGFUSE_ROUTE_PUBLIC_URL
    RELEASE_NAME
    LANGFUSE_INIT_ORG_ID
    LANGFUSE_INIT_ORG_NAME
    LANGFUSE_INIT_PROJECT_ID
    LANGFUSE_INIT_PROJECT_NAME
    LANGFUSE_INIT_USER_EMAIL
    LANGFUSE_INIT_USER_NAME
    LANGFUSE_INIT_USER_PASSWORD
)

usage() {
    cat <<EOF
Usage: $(basename "$0") install|upgrade|template|render-values

Export required environment variables, then run one of:

  install        helm install (creates namespace if needed)
  upgrade        helm upgrade --install
  template       helm template (dry-run render to stdout)
  render-values  print substituted values YAML to stdout

Environment:
  RELEASE_NAME   Helm release name (default: trace-pipeline)
  NAMESPACE      Target namespace (default: telemetry-poc)

Required variables:
  LANGFUSE_SALT LANGFUSE_ENCRYPTION_KEY LANGFUSE_NEXTAUTH_SECRET
  LANGFUSE_NEXTAUTH_URL POSTGRES_PASSWORD REDIS_PASSWORD
  CLICKHOUSE_PASSWORD MINIO_PASSWORD LANGFUSE_PUSH_PUBLIC_KEY LANGFUSE_PUSH_SECRET_KEY
  LANGFUSE_INIT_USER_EMAIL LANGFUSE_INIT_USER_PASSWORD

Optional variables (defaults applied by install-openshift.sh when unset):
  LANGFUSE_INIT_ORG_ID (lightspeed)
  LANGFUSE_INIT_ORG_NAME (Lightspeed)
  LANGFUSE_INIT_PROJECT_ID (telemetry)
  LANGFUSE_INIT_PROJECT_NAME (Telemetry)
  LANGFUSE_INIT_USER_NAME (admin)
  LANGFUSE_ROUTE_HOST LANGFUSE_ROUTE_PUBLIC_URL
  RELEASE_NAME (default: trace-pipeline; must match langfuse.fullnameOverride)
EOF
}

watch_stack() {
    local timeout_seconds=600
    local poll_interval=15
    local elapsed=0

    echo ""
    echo "Monitoring pod startup in namespace '${NAMESPACE}'."
    echo "Langfuse (PostgreSQL, ClickHouse, Redis, MinIO) can take several minutes."
    echo "Press Ctrl-C to stop watching — the install continues in the background."

    while [[ ${elapsed} -lt ${timeout_seconds} ]]; do
        sleep "${poll_interval}"
        elapsed=$(( elapsed + poll_interval ))

        echo ""
        echo "=== $(date '+%H:%M:%S') — ${elapsed}s elapsed ==="
        oc get pods -n "${NAMESPACE}" 2>/dev/null || kubectl get pods -n "${NAMESPACE}" 2>/dev/null || true

        # Count pods not in a terminal-good state (Running, Completed, Succeeded).
        local not_ready
        not_ready=$(
            { oc get pods -n "${NAMESPACE}" --no-headers 2>/dev/null || \
              kubectl get pods -n "${NAMESPACE}" --no-headers 2>/dev/null; } \
            | grep -cEv '\s+(Running|Completed|Succeeded)\s+' || true
        )

        if [[ "${not_ready}" -eq 0 ]]; then
            echo ""
            echo "All pods are Running/Completed. Stack is ready."
            return 0
        fi
    done

    echo ""
    echo "Warning: still waiting after ${timeout_seconds}s. Some pods may still be starting."
    echo "Check status with:  oc get pods -n ${NAMESPACE}"
}

require_vars() {
    local missing=0
    for var in "${REQUIRED_VARS[@]}"; do
        if [[ -z "${!var:-}" ]]; then
            echo "ERROR: ${var} is not set" >&2
            missing=1
        fi
    done
    if [[ "${missing}" -ne 0 ]]; then
        echo "Set the required variables and retry." >&2
        exit 1
    fi
}

render_values() {
    require_vars
    export RELEASE_NAME="${RELEASE_NAME:-trace-pipeline}"
    export LANGFUSE_ROUTE_HOST="${LANGFUSE_ROUTE_HOST:-}"
    export LANGFUSE_ROUTE_PUBLIC_URL="${LANGFUSE_ROUTE_PUBLIC_URL:-}"
    export LANGFUSE_INIT_ORG_ID="${LANGFUSE_INIT_ORG_ID:-lightspeed}"
    export LANGFUSE_INIT_ORG_NAME="${LANGFUSE_INIT_ORG_NAME:-Lightspeed}"
    export LANGFUSE_INIT_PROJECT_ID="${LANGFUSE_INIT_PROJECT_ID:-telemetry}"
    export LANGFUSE_INIT_PROJECT_NAME="${LANGFUSE_INIT_PROJECT_NAME:-Telemetry}"
    export LANGFUSE_INIT_USER_NAME="${LANGFUSE_INIT_USER_NAME:-admin}"

    local subst_list=""
    for var in "${SUBST_VARS[@]}"; do
        subst_list+="\${${var}} "
    done

    envsubst "${subst_list}" < "${VALUES_TEMPLATE}"
}

cmd="${1:-}"
case "${cmd}" in
    install | upgrade | template)
        require_vars
        cd "${SCRIPT_DIR}"
        if [[ "${cmd}" != "template" ]]; then
            helm dependency build >/dev/null 2>&1 || helm dependency update
        fi
        rendered="$(mktemp)"
        trap 'rm -f "${rendered}"' EXIT
        render_values > "${rendered}"

        case "${cmd}" in
            install)
                helm install "${RELEASE_NAME}" . \
                    --namespace "${NAMESPACE}" \
                    --create-namespace \
                    -f "${rendered}"
                watch_stack
                ;;
            upgrade)
                helm upgrade --install "${RELEASE_NAME}" . \
                    --namespace "${NAMESPACE}" \
                    --create-namespace \
                    -f "${rendered}"
                watch_stack
                ;;
            template)
                helm template "${RELEASE_NAME}" . -f "${rendered}"
                ;;
        esac
        ;;
    render-values)
        render_values
        ;;
    -h | --help | help)
        usage
        ;;
    *)
        usage >&2
        exit 1
        ;;
esac
