#!/usr/bin/env bash
# Refuse to deploy if any namespace this lab would deploy into or create already exists without
# this lab's ownership label: Argo CD would adopt it, and teardown would delete it.
# Usage: guard-namespaces.sh <lab app name> <ingress domain> [extra helm args, e.g. --set lab.users={a,b}]
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
app=$1 domain=$2
shift 2

# Application destinations (parent chart) plus every Namespace object a component renders.
namespaces=$(
  {
    helm template "$app" "$HERE/argocd" --set deployer.domain="$domain" "$@" |
      awk '$1 == "destination:" {d = 1} d && $1 == "namespace:" {print $2; d = 0}'
    for c in "$HERE"/argocd/components/*/; do
      helm template "$app" "$c" -f "$HERE/argocd/values.yaml" --set partOf="$app" \
        --set deployer.domain="$domain" "$@" |
        awk '/^kind: Namespace$/ {n = 1} n && /^  name: / {print $2; n = 0}'
    done
  } | sort -u
)
[[ -n $namespaces ]] || { echo "guard-namespaces: rendered no namespaces" >&2; exit 1; }
if [[ -n ${LIST_ONLY:-} ]]; then echo "$namespaces"; exit 0; fi  # for automation/tests/selftest.sh

bad=0
for ns in $namespaces; do
  state=$(oc get namespace "$ns" --ignore-not-found -o jsonpath='{.metadata.name}/{.metadata.labels.app\.kubernetes\.io/part-of}')
  case "$state" in
    "" | "$ns/$app") ;;
    *) echo "namespace $ns exists and is not this lab's ($app): change lab.namespace, lab.tenantSuffix or lab.decoyTenant in automation/argocd/values.yaml and the matching attributes in content/antora.yml (selftest checks they agree)" >&2; bad=1 ;;
  esac
done
exit "$bad"
