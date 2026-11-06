{{/* Labels shared by every object. Kept identical to the plain-manifest output so both paths match. */}}
{{- define "svc.labels" -}}
app.kubernetes.io/name: {{ .Values.name }}
app.kubernetes.io/part-of: {{ .Values.team }}
app.kubernetes.io/managed-by: intentops
{{- end }}

{{- define "svc.selector" -}}
app.kubernetes.io/name: {{ .Values.name }}
{{- end }}

{{- define "svc.meta" -}}
namespace: {{ .Values.team }}
labels:
  {{- include "svc.labels" . | nindent 2 }}
{{- end }}

{{/* PromQL selector for this service's SLI series */}}
{{- define "svc.sel" -}}
{{- printf "namespace=\"%s\",service=\"%s\"" .Values.team .Values.name -}}
{{- end }}
