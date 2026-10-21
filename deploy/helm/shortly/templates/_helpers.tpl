{{- define "shortly.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "shortly.labels" -}}
app.kubernetes.io/name: shortly
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ include "shortly.version" . | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "shortly.selectorLabels" -}}
app.kubernetes.io/name: shortly
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Image reference: digest wins over tag. Fails the render if neither is set. */}}
{{- define "shortly.image" -}}
{{- if .Values.image.digest -}}
{{ .Values.image.repository }}@{{ .Values.image.digest }}
{{- else if .Values.image.tag -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- else -}}
{{- fail "set image.digest (preferred) or image.tag" -}}
{{- end -}}
{{- end -}}

{{/*
Version for the app.kubernetes.io/version label. Label values allow only [A-Za-z0-9_.-],
max 63 chars — so a digest must lose its "sha256:" prefix (":" is illegal).
*/}}
{{- define "shortly.version" -}}
{{- if .Values.image.tag -}}
{{- .Values.image.tag | trunc 63 | trimSuffix "-" | trimSuffix "." -}}
{{- else -}}
{{- .Values.image.digest | trimPrefix "sha256:" | trunc 12 -}}
{{- end -}}
{{- end -}}

{{/* Env shared by the app and the migration Job — one source of truth. */}}
{{- define "shortly.env" -}}
- name: SHORTLY_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.secretKey }}
- name: SHORTLY_BASE_URL
  value: {{ .Values.config.baseUrl | quote }}
- name: SHORTLY_LOG_LEVEL
  value: {{ .Values.config.logLevel | quote }}
- name: SHORTLY_DB_POOL_SIZE
  value: {{ .Values.config.dbPoolSize | quote }}
- name: SHORTLY_DB_POOL_TIMEOUT_SECONDS
  value: {{ .Values.config.dbPoolTimeoutSeconds | quote }}
{{- if .Values.otel.endpoint }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .Values.otel.endpoint | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ printf "deployment.environment.name=%s" .Release.Namespace | quote }}
{{- end }}
{{- end -}}

{{/* Restricted Pod Security Standard: what every container in this chart runs with. */}}
{{- define "shortly.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "shortly.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/* The fewest replicas that can be running: the HPA floor, or the fixed count. */}}
{{- define "shortly.minReplicas" -}}
{{- if .Values.autoscaling.enabled }}{{ .Values.autoscaling.minReplicas }}{{ else }}{{ .Values.replicaCount }}{{ end }}
{{- end -}}
