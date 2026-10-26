{{- define "service.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "service.labels" -}}
app.kubernetes.io/name: {{ .Values.app.name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ include "service.version" . | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "service.selectorLabels" -}}
app.kubernetes.io/name: {{ .Values.app.name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Labels of the cleanup Job's pods. Deliberately NOT the selector labels: a pod matching
     those is counted by the HPA (its CPU skews the average) and the PodDisruptionBudget.
     part-of ties every pod of the chart together for the Postgres network policy. */}}
{{- define "service.cleanupSelectorLabels" -}}
app.kubernetes.io/name: {{ .Values.app.name }}-cleanup
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: {{ .Values.app.partOf }}
{{- end -}}

{{/* Egress every pod of the chart needs: DNS, and the database. Nothing else. */}}
{{- define "service.egress" -}}
- to:
    - namespaceSelector:
        matchLabels: { kubernetes.io/metadata.name: kube-system }
      podSelector:
        matchLabels: { k8s-app: kube-dns }
  ports:
    - { port: 53, protocol: UDP }
    - { port: 53, protocol: TCP }
- to:
    {{- toYaml .Values.networkPolicy.databaseTo | nindent 4 }}
  ports:
    - { port: {{ .Values.networkPolicy.databasePort }}, protocol: TCP }
{{- with .Values.networkPolicy.extraEgress }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/* Image reference: digest wins over tag. Fails the render if neither is set. */}}
{{- define "service.image" -}}
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
{{- define "service.version" -}}
{{- if .Values.image.tag -}}
{{- .Values.image.tag | trunc 63 | trimSuffix "-" | trimSuffix "." -}}
{{- else -}}
{{- .Values.image.digest | trimPrefix "sha256:" | trunc 12 -}}
{{- end -}}
{{- end -}}

{{/* Env shared by the app and the migration Job — one source of truth. */}}
{{- define "service.env" -}}
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
- name: SHORTLY_METRICS_PORT
  value: {{ .Values.config.metricsPort | quote }}
- name: SHORTLY_DB_POOL_TIMEOUT_SECONDS
  value: {{ .Values.config.dbPoolTimeoutSeconds | quote }}
- name: SHORTLY_DB_STATEMENT_TIMEOUT_MS
  value: {{ .Values.config.dbStatementTimeoutMs | quote }}
- name: SHORTLY_DB_LOCK_TIMEOUT_MS
  value: {{ .Values.config.dbLockTimeoutMs | quote }}
- name: SHORTLY_MAX_CONCURRENT_REQUESTS
  value: {{ .Values.config.maxConcurrentRequests | quote }}
{{- if .Values.config.faultErrorRatio }}
- name: SHORTLY_FAULT_ERROR_RATIO # DRILL: requests fail on purpose (see values.yaml)
  value: {{ .Values.config.faultErrorRatio | quote }}
{{- end }}
- name: SHORTLY_RATE_LIMIT_CREATES_PER_MINUTE
  value: {{ .Values.config.rateLimit.createsPerMinute | quote }}
- name: SHORTLY_RATE_LIMIT_BURST
  value: {{ .Values.config.rateLimit.burst | quote }}
- name: FORWARDED_ALLOW_IPS # read by uvicorn itself
  value: {{ .Values.config.forwardedAllowIps | quote }}
{{- if .Values.otel.endpoint }}
- name: OTEL_EXPORTER_OTLP_ENDPOINT
  value: {{ .Values.otel.endpoint | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ printf "deployment.environment.name=%s" .Release.Namespace | quote }}
{{- end }}
{{- end -}}

{{/* Restricted Pod Security Standard: what every container in this chart runs with. */}}
{{- define "service.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "service.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end -}}

{{/* The fewest replicas that can be running: the HPA floor, or the fixed count. */}}
{{- define "service.minReplicas" -}}
{{- if .Values.autoscaling.enabled }}{{ .Values.autoscaling.minReplicas }}{{ else }}{{ .Values.replicaCount }}{{ end }}
{{- end -}}

{{/* "true" when a canary is judged by Prometheus analysis: only where Prometheus scrapes
     the app (the ServiceMonitor's condition). Empty otherwise: no metrics, no analysis. */}}
{{- define "service.canaryAnalysis" -}}
{{- if and .Values.canary.enabled .Values.monitoring.enabled (.Capabilities.APIVersions.Has "monitoring.coreos.com/v1/ServiceMonitor") }}true{{ end -}}
{{- end -}}
