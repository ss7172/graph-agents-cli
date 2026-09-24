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
image.tag as a string. A number written unquoted in a values file reaches the
chart as a float that may already have lost digits (0123456 is read as octal
42798, long digit strings are rounded), so only a string is accepted, plus the
exact integer `--set image.tag=1234567` produces (`--set-string` is preferred).
*/}}
{{- define "agent.imageTag" -}}
{{- $tag := .Values.image.tag -}}
{{- if or (kindIs "string" $tag) (kindIs "int64" $tag) -}}
{{- toString $tag -}}
{{- else if not (kindIs "invalid" $tag) -}}
{{- fail (printf "image.tag must be a quoted string, got the %s %v: quote it in the values file (tag: \"<tag>\"); an unquoted number loses digits (0123456 reads as 42798)." (kindOf $tag) $tag) -}}
{{- end -}}
{{- end -}}

{{/*
What the HTTPRoute / Ingress publish, as a YAML list of {path, type}:
route.publicPaths, plus route.devPaths while env.APP_ENV is exactly dev (the app's
own test: `DEV` or ` dev` is a deployed environment). Read it back
with `include "agent.publicPaths" . | fromYamlArray`. Every entry is checked
here, so each template that publishes paths fails with the same message. An
empty list is refused while a Gateway or Ingress is enabled: it would publish
nothing, and a Gateway API rule without matches would publish every path.
*/}}
{{- define "agent.publicPaths" -}}
{{- $paths := .Values.route.publicPaths | default list -}}
{{- if eq (dig "APP_ENV" "" (.Values.env | default dict) | toString) "dev" -}}
{{- $paths = concat $paths (.Values.route.devPaths | default list) -}}
{{- end -}}
{{- if and (or .Values.gateway.enabled .Values.ingress.enabled) (not $paths) -}}
{{- fail "route.publicPaths is empty: the HTTPRoute / Ingress would publish nothing (and a Gateway API rule without matches publishes every path). List the agent's API paths." -}}
{{- end -}}
{{- if gt (len $paths) 64 -}}
{{- fail (printf "route.publicPaths lists %d paths; a Gateway API rule takes at most 64." (len $paths)) -}}
{{- end -}}
{{- range $paths -}}
{{- if not (kindIs "map" .) -}}
{{- fail (printf "route.publicPaths: each entry is {path, type}, got %v." .) -}}
{{- end -}}
{{- $path := .path | default "" | toString -}}
{{- if not (regexMatch "^/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*$" $path) -}}
{{- fail (printf "route.publicPaths: path %q must be an absolute URL path (start with /, no spaces, query or fragment)." $path) -}}
{{- end -}}
{{- if or (contains "//" $path) (contains "/./" $path) (contains "/../" $path) (hasSuffix "/." $path) (hasSuffix "/.." $path) (contains "%2f" (lower $path)) -}}
{{- fail (printf "route.publicPaths: path %q holds //, /./, /../ or an encoded slash, which the Gateway API and Ingress refuse." $path) -}}
{{- end -}}
{{- if not (has (.type | default "" | toString) (list "PathPrefix" "Exact")) -}}
{{- fail (printf "route.publicPaths: %s has type %q; use PathPrefix or Exact." $path (.type | default "" | toString)) -}}
{{- end -}}
{{- end -}}
{{- toYaml $paths -}}
{{- end -}}

{{/*
Render-time checks: a misconfiguration fails `helm template` / `helm upgrade`
(and shows as a comparison error in Argo CD) instead of producing pods that
pull a missing image or an autoscaler that cannot compute utilisation.
*/}}
{{- define "agent.validate" -}}
{{- if not (include "agent.imageTag" .) -}}
{{- fail "image.tag is empty: deploy a built image (`graph-agents-cli deploy --env <env>` passes the tag it deploys; in argocd mode CI or `deploy` writes image.tag into values-<env>.yaml). The chart never defaults to latest." -}}
{{- end -}}
{{- if .Values.hpa.enabled -}}
{{- if not (dig "requests" "cpu" "" (.Values.resources | default dict)) -}}
{{- fail "hpa.enabled needs resources.requests.cpu: the HPA targets a percentage of the CPU request, so without one it can never scale." -}}
{{- end -}}
{{- if gt (int .Values.hpa.minReplicas) (int .Values.hpa.maxReplicas) -}}
{{- fail (printf "hpa.minReplicas (%v) is greater than hpa.maxReplicas (%v)." .Values.hpa.minReplicas .Values.hpa.maxReplicas) -}}
{{- end -}}
{{- end -}}
{{- if or .Values.gateway.enabled .Values.ingress.enabled -}}
{{- $_ := include "agent.publicPaths" . -}}
{{- end -}}
{{- $shutdown := .Values.shutdown | default dict -}}
{{- $pause := int ($shutdown.preStopSleepSeconds | default 0) -}}
{{- $drain := int ($shutdown.drainSeconds | default 0) -}}
{{- $grace := int .Values.terminationGracePeriodSeconds -}}
{{- if or (lt $pause 0) (lt $drain 0) -}}
{{- fail "shutdown.preStopSleepSeconds and shutdown.drainSeconds must be 0 or more." -}}
{{- end -}}
{{- if le $grace (add $pause $drain) -}}
{{- fail (printf "terminationGracePeriodSeconds (%d) must be greater than shutdown.preStopSleepSeconds + shutdown.drainSeconds (%d + %d): Kubernetes kills the pod at the grace period, counted from the start of the preStop pause." $grace $pause $drain) -}}
{{- end -}}
{{- if not (or (kindIs "bool" .Values.secretOptional) (kindIs "invalid" .Values.secretOptional)) -}}
{{- fail (printf "secretOptional must be true or false, got %v." .Values.secretOptional) -}}
{{- end -}}
{{- if or .Values.metrics.scrapeAnnotations .Values.metrics.serviceMonitor.enabled -}}
{{- if has (dig "METRICS_ENABLED" "" (.Values.env | default dict) | toString | trim | lower) (list "0" "false" "no" "off") -}}
{{- fail "metrics.scrapeAnnotations / metrics.serviceMonitor.enabled scrape /metrics, which env.METRICS_ENABLED turns off: turn scraping off, or set env.METRICS_ENABLED to true." -}}
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
