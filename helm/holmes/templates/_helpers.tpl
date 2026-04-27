{{/*
Resolve and validate the Holmes container image.
values.yaml ships with image tag "0.0.0" — a placeholder rewritten by
.github/workflows/build-docker-images.yaml at release time. Installing the
chart directly from a source checkout (e.g. `helm install ./helm/holmes`)
leaves the placeholder in place and produces an unhelpful ImagePullBackOff.
Fail fast here with a message that points the user at the right install path.
*/}}
{{- define "holmes.image" -}}
{{- if hasSuffix ":0.0.0" .Values.image -}}
{{- fail (printf "\n\nThe image tag %q is a placeholder that the release pipeline rewrites at release time.\nYou appear to be installing the chart from a source checkout, where the placeholder has not been replaced.\n\nFix this with one of:\n  1. Install the released chart (recommended):\n       helm repo add robusta https://robusta-charts.storage.googleapis.com\n       helm install holmesgpt robusta/holmes -f values.yaml\n     See https://holmesgpt.dev/installation/kubernetes-installation/\n  2. Override the image tag explicitly, e.g.:\n       helm install ... --set image=holmes:latest\n  3. Check out a release tag (e.g. `git checkout <tag>`) before running `helm install` from source.\n" .Values.image) -}}
{{- end -}}
{{- printf "%s/%s" .Values.registry .Values.image -}}
{{- end -}}

{{/*
Resolve and validate the Holmes operator container image. See holmes.image for
context — same placeholder mechanism applies to operator.image.
*/}}
{{- define "holmes.operator.image" -}}
{{- if hasSuffix ":0.0.0" .Values.operator.image -}}
{{- fail (printf "\n\nThe operator.image tag %q is a placeholder that the release pipeline rewrites at release time.\nYou appear to be installing the chart from a source checkout, where the placeholder has not been replaced.\n\nFix this with one of:\n  1. Install the released chart (recommended):\n       helm repo add robusta https://robusta-charts.storage.googleapis.com\n       helm install holmesgpt robusta/holmes -f values.yaml\n     See https://holmesgpt.dev/installation/kubernetes-installation/\n  2. Override the operator image tag explicitly, e.g.:\n       helm install ... --set operator.image=holmes-operator:latest\n  3. Check out a release tag (e.g. `git checkout <tag>`) before running `helm install` from source.\n" .Values.operator.image) -}}
{{- end -}}
{{- printf "%s/%s" .Values.operator.registry .Values.operator.image -}}
{{- end -}}

{{/*
Return the service account name to use
*/}}
{{- define "holmes.serviceAccountName" -}}
{{- if .Values.customServiceAccountName -}}
{{ .Values.customServiceAccountName }}
{{- else if .Values.createServiceAccount -}}
{{ .Release.Name }}-holmes-service-account
{{- else -}}
default
{{- end -}}
{{- end -}}

{{/*
Determine if this is a Robusta (hosted) environment.
Returns "true" if ROBUSTA_UI_DOMAIN is not set OR ends with "robusta.dev"
*/}}
{{- define "holmes.isSaasEnvironment" -}}
{{- $robustaUiDomain := "" -}}
{{- range .Values.additionalEnvVars -}}
  {{- if eq .name "ROBUSTA_UI_DOMAIN" -}}
    {{- $robustaUiDomain = .value -}}
  {{- end -}}
{{- end -}}
{{- if or (eq $robustaUiDomain "") (hasSuffix ".robusta.dev" $robustaUiDomain) -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}

{{/*
- If enableTelemetry field exists in values: use its value
- If field does not exist: true for SaaS environments, false otherwise
*/}}
{{- define "holmes.enableTelemetry" -}}
{{- if hasKey .Values "enableTelemetry" -}}
{{- .Values.enableTelemetry -}}
{{- else if eq (include "holmes.isSaasEnvironment" .) "true" -}}
true
{{- else -}}
false
{{- end -}}
{{- end -}}
