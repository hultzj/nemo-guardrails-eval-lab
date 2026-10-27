#!/usr/bin/env bash
# Offline self-checks for the automation scripts (no cluster needed): bash automation/tests/selftest.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
fail() { echo "FAIL: $*" >&2; exit 1; }

# An event-branch bootstrap must deploy the children and the guide from that branch, not main.
grep -A2 '^        gitops:' automation/bootstrap.yaml | grep -q 'revision: GIT_REVISION' ||
  fail "bootstrap.yaml does not pass gitops.revision to the children"
# ... and deploys from the repository values.yaml names, so a fork deploys its own values.
grep -q '^    repoURL: GIT_REPO_URL$' automation/bootstrap.yaml || fail "bootstrap.yaml hardcodes the repository instead of GIT_REPO_URL"
want=$(awk '/^gitops:/ {g = 1} g && $1 == "repoUrl:" {print $2; exit}' automation/argocd/values.yaml)
# a dry run prints the recipe with REPO_URL expanded (and runs nothing)
recipe=$(make -n -C automation bootstrap 2>/dev/null)
[[ -n $want && $recipe == *"s|GIT_REPO_URL|$want|"* ]] || fail "make bootstrap would not deploy from gitops.repoUrl '$want'"
recipe=$(make -n -C automation bootstrap REPO_URL=https://github.com/someone/fork.git 2>/dev/null)
[[ $recipe == *'s|GIT_REPO_URL|https://github.com/someone/fork.git|'* ]] || fail "REPO_URL does not override the repository"
# the parent hands the repository to its children and Showroom, as it does the revision
grep -A2 '^        gitops:' automation/bootstrap.yaml | grep -q 'repoUrl: GIT_REPO_URL' ||
  fail "bootstrap.yaml does not pass gitops.repoUrl to the children"
fork=$(helm template lab automation/argocd --set gitops.repoUrl=https://github.com/someone/fork.git --set deployer.domain=apps.example.com)
[[ $(grep -cE 'repo(URL|Url): "?https://github.com/someone/fork.git' <<<"$fork") -ge 3 ]] &&
  ! grep -q 'hultzj/nemo-guardrails-eval-lab' <<<"$fork" || fail "children or Showroom do not follow gitops.repoUrl"
# YAML quotes around gitops.repoUrl/revision are not part of the value (on a copy: values.yaml stays untouched)
q=$(mktemp -d); cp -R automation "$q/"
sed -i.bak -E "s|^  repoUrl: (.*)$|  repoUrl: '\\1'|; s|^  revision: (.*)$|  revision: \"\\1\"|" "$q/automation/argocd/values.yaml"
recipe=$(make -n -C "$q/automation" bootstrap 2>/dev/null)
[[ $recipe == *"s|GIT_REPO_URL|$want|"* && $recipe == *"s|GIT_REVISION|$(awk '/^gitops:/ {g = 1} g && $1 == "revision:" {print $2; exit}' automation/argocd/values.yaml)|"* ]] ||
  fail "make bootstrap keeps YAML quotes around gitops.repoUrl or revision"
rm -rf "$q"
# ... and refuses a repository URL that is not https, before touching the cluster (oc and helm stubbed to fail)
stub=$(mktemp -d); printf '#!/usr/bin/env bash\necho "stub: must not run" >&2; exit 99\n' >"$stub/oc"; cp "$stub/oc" "$stub/helm"; chmod +x "$stub/oc" "$stub/helm"
out=$(PATH="$stub:$PATH" KUBECONFIG=/dev/null make -s -C automation bootstrap REPO_URL='file:///etc' 2>&1 || true)
[[ $out == *"is not an https git URL"* && $out != *"stub: must not run"* ]] || fail "make bootstrap accepted a non-https REPO_URL"
rm -rf "$stub"
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

# Every tenant gets a workbench whose ServiceAccount holds the participant bindings, and the
# tenant policy admits only the namespace itself and openshift-ingress (router + RHOAI gateway).
[[ $(grep -c '^kind: Notebook$' <<<"$tenants") -eq 6 ]] || fail "expected one workbench Notebook per user"
[[ $(grep -c 'name: guardrails-lab, namespace: user1-guardrails' <<<"$tenants") -eq 2 ]] ||
  fail "workbench ServiceAccount is not bound to view and guardrails-participant"
! grep -qE 'pods/exec|devworkspaces|openshift-console|openshift-operators' <<<"$tenants" ||
  fail "leftover Web Terminal permissions or policy peers"
# Argo CD must leave the webhook-managed Notebook fields alone, and respect that when it syncs.
parent=$(helm template lab automation/argocd --set deployer.domain=apps.example.com --set gitops.revision=event-x)
tenants_app=$(sed -n '/name: lab-tenants$/,/^---$/p' <<<"$parent")
grep -q 'kind: Notebook' <<<"$tenants_app" ||
  fail "tenants Application has no Notebook ignoreDifferences"
grep -qF 'jsonPointers: [/spec/template/spec, /metadata/annotations, /metadata/labels, /metadata/finalizers]' <<<"$tenants_app" ||
  fail "tenants Application ignores the wrong Notebook fields"
grep -q 'RespectIgnoreDifferences=true' <<<"$tenants_app" ||
  fail "tenants Application does not respect ignoreDifferences on sync"

# The workbench clones the lab files from the deployed repo and revision.
grep -q 'revision: "event-x"' <<<"$tenants_app" || fail "parent does not pass gitops.revision to tenants"
wb=$(helm template lab automation/argocd/components/tenants -f automation/argocd/values.yaml --set partOf=lab --set gitops.revision=event-x)
src=$(sed -n '/name: lab-source$/,/^---$/p' <<<"$wb" | grep -E '^  (repoUrl|revision):')
[[ $src == *'revision: "event-x"'* ]] || fail "lab-source revision does not follow gitops.revision"
[[ $src == *"repoUrl: \"$(sed -n 's/^  repoUrl: //p' automation/argocd/values.yaml)\""* ]] || fail "lab-source repoUrl is not gitops.repoUrl"
# Module 5: each participant writes their own judge key into the empty judge-api-key Secret the lab
# creates; they may get/update/patch that one Secret by name and create none (a created Secret could be
# a service-account-token one), and Argo CD leaves the key they wrote.
key=$(sed -n '/^kind: Secret$/,/^---$/p' <<<"$wb" | sed -n '/name: judge-api-key$/,/^---$/p')
[[ $key == *'type: Opaque'* && $key == *'api-key: ""'* ]] || fail "the tenants chart does not create an empty Opaque judge-api-key Secret"
[[ $(grep -c '^kind: Secret$' <<<"$wb") == $(grep -c 'name: judge-api-key$' <<<"$wb") ]] || fail "the tenants chart renders a Secret other than judge-api-key"
role=$(sed -n '/^kind: Role$/,/^---$/p' <<<"$wb" | sed -n '/name: guardrails-participant$/,/^---$/p')
# one Secret rule per participant Role, and every one names only judge-api-key with get/update/patch
[[ $(grep -c 'resources: \[secrets\]' <<<"$role") == $(grep -c 'name: guardrails-participant$' <<<"$role") ]] &&
  [[ $(grep -A2 'resources: \[secrets\]' <<<"$role" | grep -v 'resources: \[secrets\]' | grep -v '^--$' | sort -u | tr '\n' '|') == \
     '    resourceNames: [judge-api-key]|    verbs: [get, update, patch]|' ]] ||
  fail "participants may do more with Secrets than get/update/patch judge-api-key"
grep -A2 'kind: Secret$' <<<"$tenants_app" | grep -q 'name: judge-api-key' && grep -q 'jsonPointers: \[/data\]' <<<"$tenants_app" ||
  fail "Argo CD would overwrite the participant's judge key (no ignoreDifferences on judge-api-key /data)"
grep -rqE 'judge-proxy|maas-judge|lab-judge|judge-secret' automation content/modules/ROOT/pages diagrams --exclude-dir=tests --include='*.yaml' --include='*.adoc' --include='*.json' --include='*.sh' --include=Makefile &&
  fail "something still refers to the removed shared judge proxy"
grep -q 'git fetch -q --depth 1 "$REPO" "$REV" && git checkout -q --detach FETCH_HEAD' content/modules/ROOT/pages/02-details.adoc ||
  fail "the guide does not fetch the deployed revision"
# ... and that fetch works for a branch, a tag-like ref and a bare commit, into a new or existing checkout.
tmp=$(mktemp -d); git init -q "$tmp/src"; git -C "$tmp/src" -c user.email=t@t -c user.name=t commit -q --allow-empty -m one
sha1=$(git -C "$tmp/src" rev-parse HEAD); git -C "$tmp/src" -c user.email=t@t -c user.name=t commit -q --allow-empty -m two
git -C "$tmp/src" config uploadpack.allowReachableSHA1InWant true
for rev in "$(git -C "$tmp/src" branch --show-current)" "$sha1"; do
  ( mkdir -p "$tmp/wb" && cd "$tmp/wb" && { [ -d .git ] || git init -q; } &&
    git fetch -q --depth 1 "file://$tmp/src" "$rev" && git checkout -q --detach FETCH_HEAD ) || fail "fetch of $rev failed"
done
[[ $(git -C "$tmp/wb" rev-parse HEAD) == "$sha1" ]] || fail "existing checkout did not move to the pinned commit"
rm -rf "$tmp"

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

# The built guide must not link to cluster-internal or placeholder addresses (clicking them fails).
if [[ -d www ]]; then
  ! grep -rhoE '<a href="[^"]*(\.svc[:/"]|&lt;)[^"]*"' www --include='*.html' ||
    fail "the guide links to a cluster-internal or placeholder URL; wrap it in pass:c,a,q[...]"
fi


# The architecture diagrams draw the deployment as built: the workbench, monitor, proxy, server, their
# rails and (Module 5) the participant's own judge key all run in the participant's project.
python3 - <<'PYDIAG' || fail "an architecture diagram misplaces a component's namespace"
import json, sys
want = {"guardrails-deterministic": {"Your project: <user>-guardrails": {"client", "monitor", "rbac", "nemo", "regex", "presidio", "config"}},
        "guardrails-llm-judge": {"Your project: <user>-guardrails": {"client", "monitor", "rbac", "nemo", "config", "rail", "secret"}}}
for name, bounds in want.items():
    d = json.load(open(f"diagrams/{name}.architecture.json"))
    got = {b["label"]: set(b["wraps"]) for b in d["boundaries"]}
    for label, members in bounds.items():
        if got.get(label) != members: sys.exit(f"{name}: {label} wraps {sorted(got.get(label, []))}, expected {sorted(members)}")
PYDIAG
# ... and the page says who checks which token: oauth-proxy yours, kube-rbac-proxy the monitor's.
grep -q "oauth-proxy admits only" content/modules/ROOT/pages/01-overview.adoc &&
  grep -q "its own service-account token, which kube-rbac-proxy checks" content/modules/ROOT/pages/01-overview.adoc ||
  fail "the overview no longer says which proxy checks which token"

# Every embedded step video exists, and none is longer than two minutes (when ffprobe is available).
for v in $(grep -rhoE '^video::[^[]+' content/modules/ROOT/pages | sed 's/^video:://'); do
  f=content/modules/ROOT/assets/images/$v
  [[ -f $f ]] || fail "guide embeds $v, but $f is missing"
  if command -v ffprobe >/dev/null; then
    d=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f")
    awk -v d="$d" 'BEGIN { exit !(d <= 121) }' || fail "$v is $d s long (at most two minutes)"
  fi
done

echo "selftest: OK"
