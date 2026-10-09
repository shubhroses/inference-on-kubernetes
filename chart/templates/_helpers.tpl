{{/*
The labels that identify this release's pods. The Deployment's selector, its
pod template and the Service's selector all use them, and Kubernetes does not
allow a Deployment's selector to change, so nothing that varies between
upgrades (a version, say) belongs here.
*/}}
{{- define "inference.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
The labels on every object the chart creates.
*/}}
{{- define "inference.labels" -}}
{{ include "inference.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}
