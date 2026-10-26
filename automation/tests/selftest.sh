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
# Module 5: tenants call the shared judge proxy, which alone gets the key; the URL and model follow judge.*.
grep -q 'model: openai/deepseek-r1-distill-qwen-14b' <<<"$tenants_app" || fail "parent does not pass judge to tenants"
ns=$(sed -n "/^lab:/,/^[^ #]/s/^  namespace: *//p" automation/argocd/values.yaml)
jd=$(helm template lab automation/argocd/components/tenants -f automation/argocd/values.yaml --set partOf=lab \
  --set judge.model=m-x | sed -n '/name: lab-judge$/,/^---$/p')
[[ $jd == *"baseUrl: \"http://judge-proxy.$ns.svc:8080/v1\""* && $jd == *'model: "m-x"'* ]] ||
  fail "lab-judge does not point at the judge proxy with judge.model"
grep -qiE 'kind: Secret|api-?key|secretKeyRef' <<<"$wb" && fail "the tenants chart renders a Secret or an API key reference"
jp=$(helm template lab automation/argocd/components/safety-model -f automation/argocd/values.yaml --set partOf=lab \
  --set judge.baseUrl=https://judge.example/v1 --set judge.model=m-x)
grep -q 'value: "https://judge.example/v1"' <<<"$jp" && grep -q 'value: "m-x"' <<<"$jp" ||
  fail "the judge proxy does not follow judge.*"
grep -q 'secret: {secretName: maas-judge, optional: true}' <<<"$jp" ||
  fail "the judge proxy must mount maas-judge optionally (Modules 1 to 4 run without it)"
grep -q 'name: judge-proxy-lab-only' <<<"$jp" || fail "the judge proxy has no lab-only NetworkPolicy"
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

# verify.sh runs Module 5's checks only when the judge key exists, and flags a key in a tenant (stub oc;
# every other command succeeds silently, so only the judge lines are asserted).
stub=$(mktemp -d)
cat >"$stub/oc" <<'STUB'
#!/usr/bin/env bash
case "$*" in
  *"get secret maas-judge"*)
    [[ -n ${STUB_JUDGE_NS-} && "$*" == *"-n $STUB_JUDGE_NS "* ]] && { echo secret/maas-judge; exit 0; }
    [[ -n ${STUB_FORBIDDEN-} ]] && { echo 'Error from server (Forbidden): secrets "maas-judge" is forbidden' >&2; exit 1; }
    echo 'Error from server (NotFound): secrets "maas-judge" not found' >&2; exit 1 ;;
esac
exit 0
STUB
printf '#!/usr/bin/env bash\nexit 0\n' >"$stub/curl"
chmod +x "$stub/oc" "$stub/curl"
lab_ns=$(sed -n "/^lab:/,/^[^ #]/s/^  namespace: *//p" automation/argocd/values.yaml)
out=$(PATH="$stub:$PATH" USERS="ua ub" PRESENTERS="" bash automation/verify.sh 2>&1 || true)  # other checks fail against the stub
[[ $(grep -c '^SKIP: no Secret maas-judge' <<<"$out") == 1 ]] || fail "verify.sh without the judge key must SKIP Module 5 once"
grep -E 'judge proxy|LLM judge' <<<"$out" && fail "verify.sh ran judge checks without the judge key"
grep -E '^FAIL: .*judge' <<<"$out" && fail "a missing judge key must not fail verify.sh (Module 5 is optional)"
grep -q '^PASS: ua: holds no copy of the judge key' <<<"$out" || fail "verify.sh does not check tenants hold no judge key"
out=$(PATH="$stub:$PATH" STUB_JUDGE_NS="$lab_ns" USERS="ua ub" PRESENTERS="" bash automation/verify.sh 2>&1 || true)  # other checks fail against the stub
grep -q '^SKIP: no Secret maas-judge' <<<"$out" && fail "verify.sh skipped Module 5 although the judge key exists"
grep -q '^PASS: judge proxy rolled out' <<<"$out" && grep -q '^PASS: LLM judge answers through the judge proxy' <<<"$out" ||
  fail "verify.sh did not check the judge proxy"
[[ $(grep -c 'LLM judge answers' <<<"$out") == 1 ]] || fail "the judge completion must be asked once, not per tenant"
out=$(PATH="$stub:$PATH" STUB_JUDGE_NS="ub-guardrails" USERS="ua ub" PRESENTERS="" SUFFIX=-guardrails bash automation/verify.sh 2>&1 || true)  # other checks fail against the stub
grep -q '^FAIL: ub: holds no copy of the judge key' <<<"$out" || fail "verify.sh misses a judge key in a tenant"
out=$(PATH="$stub:$PATH" STUB_FORBIDDEN=1 USERS="ua" PRESENTERS="" bash automation/verify.sh 2>&1 || true)  # other checks fail against the stub
grep -q '^FAIL: cannot tell whether Secret maas-judge exists' <<<"$out" && ! grep -q '^SKIP: no Secret maas-judge' <<<"$out" ||
  fail "verify.sh must fail, not skip, when it cannot look up the judge key"
rm -rf "$stub"

echo "selftest: OK"
