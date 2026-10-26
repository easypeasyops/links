{{/* The app's pod template, shared by the Deployment and (canary.enabled) the Rollout, so
     the two can never drift. The Rollout adds rollouts-pod-template-hash itself. */}}
{{- define "service.podTemplate" -}}
metadata:
  labels:
    {{- include "service.selectorLabels" . | nindent 4 }}
    app.kubernetes.io/version: {{ include "service.version" . | quote }}
    app.kubernetes.io/part-of: {{ .Values.app.partOf }}
  annotations:
    prometheus.io/scrape: "true"
    prometheus.io/port: {{ .Values.config.metricsPort | quote }}
    prometheus.io/path: /metrics
spec:
  {{- with .Values.imagePullSecrets }}
  imagePullSecrets:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  automountServiceAccountToken: false # the app never talks to the Kubernetes API
  terminationGracePeriodSeconds: {{ .Values.terminationGracePeriodSeconds }}
  securityContext:
    {{- include "service.podSecurityContext" . | nindent 4 }}
  # Spread replicas across nodes so one node failure isn't an outage.
  topologySpreadConstraints:
    - maxSkew: 1
      topologyKey: kubernetes.io/hostname
      whenUnsatisfiable: ScheduleAnyway
      labelSelector:
        matchLabels:
          {{- include "service.selectorLabels" . | nindent 10 }}
  containers:
    - name: app
      image: {{ include "service.image" . }}
      imagePullPolicy: {{ .Values.image.pullPolicy }}
      ports:
        - name: http
          containerPort: 8000
        - name: metrics
          containerPort: {{ .Values.config.metricsPort }}
      env:
        {{- include "service.env" . | nindent 8 }}
      securityContext:
        {{- include "service.containerSecurityContext" . | nindent 8 }}
      # startupProbe gates the other two, so a slow start isn't killed by liveness.
      startupProbe:
        httpGet: { path: /healthz, port: http }
        periodSeconds: 2
        failureThreshold: 30
      # Both probes are answered on the event loop, outside the request threadpool, so
      # an overloaded pod is still "alive" and still reports its DB reachability. 2 s
      # of timeout (default 1 s) absorbs CPU contention without hiding a wedged process.
      livenessProbe: # "restart me if wedged" — never checks the DB (see api.py)
        httpGet: { path: /healthz, port: http }
        periodSeconds: 10
        timeoutSeconds: 2
        failureThreshold: 3
      readinessProbe: # "send me traffic?" — DB down ⇒ out of rotation, not restarted
        httpGet: { path: /readyz, port: http }
        periodSeconds: 5
        timeoutSeconds: 2
        failureThreshold: 2
      lifecycle:
        preStop:
          # Kubernetes sends SIGTERM and removes the pod from endpoints *concurrently*.
          # Sleeping first lets routers stop sending traffic before uvicorn drains.
          sleep:
            seconds: {{ .Values.preStopSleepSeconds }}
      resources:
        {{- toYaml .Values.resources | nindent 8 }}
      volumeMounts:
        - name: tmp
          mountPath: /tmp
  volumes:
    - name: tmp # read-only root FS; give Python a writable /tmp
      emptyDir:
        sizeLimit: 16Mi
  {{- with .Values.nodeSelector }}
  nodeSelector:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  {{- with .Values.tolerations }}
  tolerations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  {{- with .Values.affinity }}
  affinity:
    {{- toYaml . | nindent 4 }}
  {{- end }}
{{- end -}}
