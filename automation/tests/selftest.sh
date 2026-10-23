#!/usr/bin/env bash
# Offline self-checks for the automation scripts (no cluster needed): bash automation/tests/selftest.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
fail() { echo "FAIL: $*" >&2; exit 1; }

# An event-branch bootstrap must deploy the children and the guide from that branch, not main.
grep -A1 '^        gitops:' automation/bootstrap.yaml | grep -q 'revision: GIT_REVISION' ||
  fail "bootstrap.yaml does not pass gitops.revision to the children"
rendered=$(helm template lab automation/argocd --set deployer.domain=apps.example.com --set gitops.revision=event-x)
showroom_sha=$(sed -n '/^  chart:/,/path:/s/^    revision: //p' automation/argocd/values.yaml)
grep -q 'repoRef: "event-x"' <<<"$rendered" || fail "Showroom content does not follow gitops.revision"
bad=$(awk '$1 == "targetRevision:" {print $2}' <<<"$rendered" | grep -vx -e event-x -e "$showroom_sha" || true)
[[ -z $bad ]] || fail "children on another revision: $bad"

# The ownership guard must cover every namespace the lab creates, not only Application destinations.
ns=$(LIST_ONLY=1 bash automation/guard-namespaces.sh nemo-guardrails-eval-lab apps.example.com)
for want in showroom guardrails-lab-shared user1-guardrails presenter-guardrails peer-demo-guardrails; do
  grep -qx "$want" <<<"$ns" || fail "guard-namespaces does not check $want"
done

# Every namespace a component creates is also isolated by the tenant NetworkPolicy.
tenants=$(helm template lab automation/argocd/components/tenants -f automation/argocd/values.yaml --set partOf=lab)
for want in user1-guardrails presenter-guardrails peer-demo-guardrails; do
  awk '/^kind: NetworkPolicy$/ {p = 1} p && /^  namespace: / {print $2; p = 0}' <<<"$tenants" | grep -qx "$want" ||
    fail "no tenant-isolation NetworkPolicy in $want"
done

# The tenant policy admits the DevWorkspace controller pod only (namespace AND pod selector), or the
# Web Terminal never passes its health check.
grep -B1 -A1 'app.kubernetes.io/name: devworkspace-controller' <<<"$tenants" | grep -q 'podSelector:' ||
  fail "tenant policy does not admit the DevWorkspace controller by pod selector"
[[ $(grep -c 'kubernetes.io/metadata.name: openshift-operators' <<<"$tenants") -eq $(grep -c 'app.kubernetes.io/name: devworkspace-controller' <<<"$tenants") ]] ||
  fail "openshift-operators admitted without the controller pod selector"

# make users: lab.users minus lab.presenters, never a presenter even when named, and a distinct
# password per user (a shared one would let attendees log in as each other).
created=$(DRY_RUN=1 bash automation/create-users.sh | grep '^would create')
[[ $(awk '{print $3}' <<<"$created" | paste -sd' ' -) == "user1 user2 user3 user4 user5 presenter" ]] ||
  fail "default users: $created"
pw=$(awk '{print $4}' <<<"$created")
[[ $(sort -u <<<"$pw" | wc -l) -eq 6 ]] || fail "passwords are not unique per user"
awk 'length($0) != 16 || $0 !~ /^[A-Za-z0-9]+$/ {bad = 1} END {exit bad}' <<<"$pw" || fail "password format"
[[ $(LAB_SHARED_PASSWORD=same DRY_RUN=1 bash automation/create-users.sh user1 user2 | awk '{print $4}' | sort -u) == same ]] ||
  fail "LAB_SHARED_PASSWORD not applied"
out=$(PRESENTERS=kuro DRY_RUN=1 bash automation/create-users.sh kuro user9)
grep -q 'skip    kuro' <<<"$out" && grep -q '^would create user9 ' <<<"$out" && ! grep -q 'would create kuro' <<<"$out" ||
  fail "explicit users: $out"

# The guide and verify.sh must name the same namespaces as values.yaml, so a rename reaches them.
val() { sed -n "/^lab:/,/^[^ #]/s/^  $1: *//p" automation/argocd/values.yaml; }
attr() { sed -n "s/^    $1: '\(.*\)'/\1/p" content/antora.yml; }
[[ $(val namespace) == guardrails-lab-shared ]] || fail "val() did not read lab.namespace"
[[ $(attr lab_namespace) == "$(val namespace)" ]] || fail "antora.yml lab_namespace != values lab.namespace"
[[ $(attr tenant_suffix) == "$(val tenantSuffix)" ]] || fail "antora.yml tenant_suffix != values lab.tenantSuffix"
[[ $(attr decoy_namespace) == "$(val decoyTenant)$(val tenantSuffix)" ]] || fail "antora.yml decoy_namespace mismatch"
[[ $(attr safety_url) == "http://safety-guard.$(val namespace).svc:8080/v1" ]] || fail "antora.yml safety_url mismatch"
! grep -n -- "$(val tenantSuffix)" content/modules/ROOT/pages/*.adoc | grep -v -e '{tenant_suffix}' -e '{decoy_namespace}' -e 'nemo-guardrails' ||
  fail "a page hardcodes the tenant suffix; use {tenant_suffix}"
! grep -nE '^(LAB_NS|SUFFIX|DECOY_NS)=\$\{[A-Z_]+:-[a-z]' automation/verify.sh || fail "verify.sh hardcodes a namespace default"
names=$(bash -c 'VALUES=automation/argocd/values.yaml; source <(sed -n "/^value()/,/^DECOY_NS=/p" automation/verify.sh); echo "$LAB_NS $SUFFIX $DECOY_NS"')
[[ $names == "guardrails-lab-shared -guardrails peer-demo-guardrails" ]] || fail "verify.sh derives '$names'"

echo "selftest: OK"
