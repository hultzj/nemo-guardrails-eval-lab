#!/usr/bin/env bash
# Create (or replace) Secret maas-judge (key api-key) in the lab namespace, for Module 5's judge proxy.
# Usage: read -rs MAAS_API_KEY && export MAAS_API_KEY && bash automation/judge-secret.sh
# The key comes from the environment only: never from git, never printed, never in a tenant namespace
# (participants write the rail configs their server follows, so a key there could be sent anywhere).
set -euo pipefail

: "${MAAS_API_KEY:?set MAAS_API_KEY to the API key of the judge endpoint}"
HERE=$(cd "$(dirname "$0")" && pwd)
VALUES="$HERE/argocd/values.yaml"
ns=${LAB_NS:-$(sed -n "/^lab:/,/^[^ #]/s/^  namespace: *//p" "$VALUES")}
[[ -n $ns ]] || { echo "no lab.namespace in $VALUES" >&2; exit 1; }
oc get namespace "$ns" -o name >/dev/null || { echo "$ns is not deployed yet: run make bootstrap first" >&2; exit 1; }
# The key travels on stdin, not on the command line.
printf '%s' "$MAAS_API_KEY" | oc -n "$ns" create secret generic maas-judge --from-file=api-key=/dev/stdin \
  --dry-run=client -o yaml | oc -n "$ns" apply -f - >/dev/null
echo "secret  $ns/maas-judge (the judge proxy reads it within a minute or two)"
