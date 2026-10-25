{{/*
Everything a platform child Application shares. `wave` orders the root's sync: Argo CD
applies a wave, waits until its Applications are Healthy (argocd-cm's Application health
check, deploy/kind/argocd-values.yaml), then starts the next.
  wave 0: the charts and the CRDs they ship; also what needs only a fast chart's CRDs
          (the Gateway, the tool routes: the sync retry covers the few seconds' race)
  wave 1: what needs a slow chart running (the Kyverno policies: Kyverno's webhook; the
          alerts on Argo CD: kube-prometheus-stack's CRDs)
*/}}
{{- define "platform.meta" -}}
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {{ .name }}
  namespace: argocd
  labels:
    app.kubernetes.io/part-of: platform
  annotations:
    argocd.argoproj.io/sync-wave: {{ .wave | quote }}
    # Diff against what the API server would store, not against the raw manifest: API
    # defaults and fields other controllers own (webhook caBundles, aggregated ClusterRole
    # rules) are not drift.
    argocd.argoproj.io/compare-options: ServerSideDiff=true
  # No resources-finalizer, on purpose: deleting a child Application (or the root) orphans
  # its resources instead of deleting the monitoring stack or the admission webhook. Remove
  # a component by deleting its resources, then its Application.
{{- end }}

{{/*
Automated sync with pruning and self-heal, like the app's environments. Server-side apply:
several of these charts ship CRDs too large for client-side apply's last-applied
annotation (kube-prometheus-stack, Kyverno, Envoy Gateway). `labels` become the
namespace's labels (Pod Security levels), set by Argo CD when it creates the namespace.
*/}}
{{- define "platform.syncPolicy" -}}
syncPolicy:
  automated: { prune: true, selfHeal: true }
  syncOptions:
    - CreateNamespace=true
    - ServerSideApply=true
  {{- with .labels }}
  managedNamespaceMetadata:
    labels:
      {{- toYaml . | nindent 6 }}
  {{- end }}
  # A webhook that isn't serving yet (Kyverno's, right after it starts) refuses the first
  # apply: retry instead of failing the sync.
  retry:
    limit: 10
    backoff: { duration: 10s, factor: 2, maxDuration: 2m }
{{- end }}

{{/* A chart from an OCI registry, pinned by digest, with its values file from git. */}}
{{- define "platform.chartSources" -}}
sources:
  - repoURL: {{ .chart.repo }}
    targetRevision: {{ .chart.digest }} # {{ .chart.version }}
    path: .
    helm:
      releaseName: {{ .release }} # the name scripts/cluster.sh's helm install used
      valueFiles:
        - $values/{{ .values }}
      {{- with .valuesObject }}
      valuesObject:
        {{- toYaml . | nindent 8 }}
      {{- end }}
  - repoURL: {{ .root.Values.repoURL }}
    targetRevision: {{ .root.Values.revision }}
    ref: values
{{- end }}
