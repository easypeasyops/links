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

{{/* Labels of a scheduled job's pods, (dict "root" $ "name" <cronJobs key>). Deliberately
     NOT the selector labels: a pod matching those is counted by the HPA (its CPU skews the
     average) and the PodDisruptionBudget. part-of ties every pod of the chart together for
     the database's network policy. */}}
{{- define "service.jobSelectorLabels" -}}
app.kubernetes.io/name: {{ .root.Values.app.name }}-{{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/part-of: {{ .root.Values.app.partOf }}
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

{{/* Renders an env list from values (`env`, or a scheduled job's own `env`), in order.
     Each string is a template (it may read other values, such as config.*, which env files
     override); an entry with `when` is rendered only if `when` renders to something other
     than "", "0" or "false". Call with (dict "root" $ "env" <list>). */}}
{{- define "service.envList" -}}
{{- $root := .root }}
{{- range .env }}
{{- if or (not (hasKey . "when")) (not (has (tpl (toString .when) $root) (list "" "0" "false"))) }}
- name: {{ .name }}
{{- if .secret }}
  valueFrom:
    secretKeyRef:
      name: {{ tpl .secret.name $root }}
      key: {{ tpl .secret.key $root }}
{{- else }}
  value: {{ tpl (toString .value) $root | quote }}
{{- end }}
{{- end }}
{{- end }}
{{- end -}}

{{/* The app's environment, shared by the app, the migration Job and the scheduled jobs. */}}
{{- define "service.env" -}}
{{- include "service.envList" (dict "root" . "env" .Values.env) }}
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
