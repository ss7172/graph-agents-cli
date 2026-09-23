{{/*
Helpers for the graph-agents-cli agent chart. Copied verbatim into projects.
*/}}

{{- define "agent.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Release name = project name; resources are named after the release. */}}
{{- define "agent.fullname" -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "agent.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
image.tag as a string. A tag written unquoted in a values file that looks like
a number (a short SHA made of digits) reaches the chart as a float; print it
back without an exponent.
*/}}
{{- define "agent.imageTag" -}}
{{- $tag := .Values.image.tag -}}
{{- if kindIs "float64" $tag -}}
{{- $tag = printf "%.0f" $tag -}}
{{- end -}}
{{- if $tag -}}{{- toString $tag -}}{{- end -}}
{{- end -}}

{{/*
Render-time checks: a misconfiguration fails `helm template` / `helm upgrade`
(and shows as a comparison error in Argo CD) instead of producing pods that
pull a missing image or an autoscaler that cannot compute utilisation.
*/}}
{{- define "agent.validate" -}}
{{- if not (include "agent.imageTag" .) -}}
{{- fail "image.tag is empty: deploy a built image (`graph-agents-cli deploy --env <env>` passes --set image.tag=<tag>; in argocd mode CI or `deploy` writes image.tag into values-<env>.yaml). The chart never defaults to latest." -}}
{{- end -}}
{{- if .Values.hpa.enabled -}}
{{- if not (dig "requests" "cpu" "" (.Values.resources | default dict)) -}}
{{- fail "hpa.enabled needs resources.requests.cpu: the HPA targets a percentage of the CPU request, so without one it can never scale." -}}
{{- end -}}
{{- if gt (int .Values.hpa.minReplicas) (int .Values.hpa.maxReplicas) -}}
{{- fail (printf "hpa.minReplicas (%v) is greater than hpa.maxReplicas (%v)." .Values.hpa.minReplicas .Values.hpa.maxReplicas) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "agent.labels" -}}
helm.sh/chart: {{ include "agent.chart" . }}
{{ include "agent.selectorLabels" . }}
app.kubernetes.io/version: {{ include "agent.imageTag" . | default .Chart.AppVersion | trunc 63 | trimSuffix "-" | trimSuffix "." | quote }}
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

{{/* The app Secret: existingSecret, default "<release>-app". */}}
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

{{/* The subchart renders auth.existingSecret through tpl; so does this chart. */}}
{{- define "agent.postgresql.secretName" -}}
{{- if .Values.postgresql.auth.existingSecret -}}
{{- tpl .Values.postgresql.auth.existingSecret . -}}
{{- else -}}
{{- printf "%s-postgresql" .Release.Name -}}
{{- end -}}
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
