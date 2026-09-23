{{- define "mini.fullname" -}}
{{ .Release.Name }}
{{- end -}}
{{- define "mini.labels" -}}
app.kubernetes.io/name: {{ .Release.Name }}
{{- end -}}
