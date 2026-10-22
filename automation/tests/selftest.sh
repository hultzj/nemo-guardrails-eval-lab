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

# make users: lab.users minus lab.presenters, and never a presenter even when named.
users=$(DRY_RUN=1 bash automation/create-users.sh | grep '^would create')
[[ $users == $'would create user1\nwould create user2\nwould create user3\nwould create user4\nwould create user5\nwould create presenter' ]] ||
  fail "default users: $users"
out=$(PRESENTERS=kuro DRY_RUN=1 bash automation/create-users.sh kuro user9)
grep -q 'skip    kuro' <<<"$out" && grep -qx 'would create user9' <<<"$out" && ! grep -q 'would create kuro' <<<"$out" ||
  fail "explicit users: $out"

echo "selftest: OK"
