{{/*
Expand the name of the chart.
*/}}
{{- define "trace-pipeline.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "trace-pipeline.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "trace-pipeline.labels" -}}
helm.sh/chart: {{ include "trace-pipeline.chart" . }}
{{ include "trace-pipeline.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "trace-pipeline.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "trace-pipeline.selectorLabels" -}}
app.kubernetes.io/name: {{ include "trace-pipeline.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: upload-collector
{{- end }}

{{/*
OpenShift Route name for the Langfuse web UI (independent of upload-collector naming).
*/}}
{{- define "trace-pipeline.langfuseRouteName" -}}
{{- default "langfuse-web" .Values.langfuseRoute.name }}
{{- end }}

{{/*
Langfuse subchart fullname (used for MinIO Service/Secret naming).
*/}}
{{- define "trace-pipeline.langfuseFullname" -}}
{{- default .Release.Name .Values.langfuse.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
MinIO credentials Secret shared by the MinIO pod and Langfuse web/worker.
*/}}
{{- define "trace-pipeline.minioSecretName" -}}
{{- default (printf "%s-s3" (include "trace-pipeline.langfuseFullname" .)) .Values.minio.secretName | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Langfuse web Service name created by the langfuse subchart.
*/}}
{{- define "trace-pipeline.langfuseWebServiceName" -}}
{{- default (printf "%s-web" .Release.Name) .Values.langfuseRoute.webServiceName }}
{{- end }}

{{/*
Langfuse URL used by the trace pusher (in-cluster).
*/}}
{{- define "trace-pipeline.langfusePushHost" -}}
{{- if .Values.pusher.langfuse.host }}
{{- .Values.pusher.langfuse.host }}
{{- else }}
{{- printf "http://%s:%d" (include "trace-pipeline.langfuseWebServiceName" .) 3000 }}
{{- end }}
{{- end }}

{{/*
Public Langfuse URL for NextAuth (Route or explicit override).
*/}}
{{- define "trace-pipeline.langfusePublicUrl" -}}
{{- if .Values.langfuseRoute.publicUrl }}
{{- .Values.langfuseRoute.publicUrl }}
{{- else if .Values.langfuseRoute.host }}
{{- printf "https://%s" .Values.langfuseRoute.host }}
{{- else }}
{{- printf "http://%s:%d" (include "trace-pipeline.langfuseWebServiceName" .) 3000 }}
{{- end }}
{{- end }}
