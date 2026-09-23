{{/*
Helpers for the graph-agents-cli agent chart. Copied verbatim into projects.
*/}}

{{- define "agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Release name = project name (D9); resources are named after the release. */}}
{{- define "agent.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "agent.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "agent.labels" -}}
helm.sh/chart: {{ include "agent.chart" . }}
{{ include "agent.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
graph-agents-cli/runtime: {{ .Values.runtime | quote }}
{{- end -}}

{{- define "agent.selectorLabels" -}}
app.kubernetes.io/name: {{ include "agent.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "agent.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "agent.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* The app Secret: existingSecret, default "<release>-app" (D14). */}}
{{- define "agent.secretName" -}}
{{- default (printf "%s-app" (include "agent.fullname" .)) .Values.existingSecret -}}
{{- end -}}

{{/* The TLS Secret: tls.existingSecret, else "<release>-tls" (cert-manager writes it). */}}
{{- define "agent.tlsSecretName" -}}
{{- default (printf "%s-tls" (include "agent.fullname" .)) .Values.tls.existingSecret -}}
{{- end -}}

{{/* Bitnami postgresql subchart service and secret names. */}}
{{- define "agent.postgresql.host" -}}
{{- printf "%s-postgresql" .Release.Name -}}
{{- end -}}

{{- define "agent.postgresql.secretName" -}}
{{- default (printf "%s-postgresql" .Release.Name) .Values.postgresql.auth.existingSecret -}}
{{- end -}}

{{/* Connection string for the bundled Postgres; $(POSTGRES_PASSWORD) expands in the container. */}}
{{- define "agent.postgresql.dsn" -}}
{{- printf "postgresql://%s:$(POSTGRES_PASSWORD)@%s:5432/%s" .Values.postgresql.auth.username (include "agent.postgresql.host" .) .Values.postgresql.auth.database -}}
{{- end -}}

{{/* Bitnami redis subchart (standalone) master service. */}}
{{- define "agent.redis.uri" -}}
{{- printf "redis://%s-redis-master:6379" .Release.Name -}}
{{- end -}}

{{- define "agent.hostname" -}}
{{- if .Values.gateway.enabled -}}{{ .Values.gateway.hostname }}{{- else -}}{{ .Values.ingress.hostname }}{{- end -}}
{{- end -}}

{{/*
Public base URL for the A2A agent card (APP_URL): appUrl, else env.APP_URL,
else derived from the traffic entry's hostname (https on a Gateway listener or
a TLS Ingress, http on a plain Ingress). Empty when nothing is known.
*/}}
{{- define "agent.appUrl" -}}
{{- if .Values.appUrl -}}
{{- .Values.appUrl -}}
{{- else if (index .Values.env "APP_URL") -}}
{{- index .Values.env "APP_URL" -}}
{{- else if and .Values.gateway.enabled .Values.gateway.hostname -}}
{{- printf "https://%s" .Values.gateway.hostname -}}
{{- else if and .Values.ingress.enabled .Values.ingress.hostname -}}
{{- if or .Values.tls.existingSecret .Values.tls.certManager.enabled -}}
{{- printf "https://%s" .Values.ingress.hostname -}}
{{- else -}}
{{- printf "http://%s" .Values.ingress.hostname -}}
{{- end -}}
{{- end -}}
{{- end -}}
