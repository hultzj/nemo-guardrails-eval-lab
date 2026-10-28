# Evaluating NeMo Guardrails on OpenShift AI

A hands-on lab (about 2 hours; Modules 1 to 4 stand alone as 90 minutes) that builds NeMo
Guardrails on OpenShift AI, measures what it blocks and what it lets through, and shows every
decision live. Each participant gets their own project with a JupyterLab workbench and a live
monitor; the same pages work as a presenter-led demo.

| Module | What you do |
|---|---|
| 1. Stand up a guarded endpoint | Regex and Presidio rails behind kube-rbac-proxy; prove anonymous and cross-tenant callers are refused |
| 2. Add a content-safety model | A second configuration backed by NVIDIA Nemotron Safety Guard 8B; compare verdicts and latency |
| 3. Evaluate | 50 labelled prompts per configuration: precision, recall, FPR, the misclassified prompts |
| 4. Tune and break | What a policy edit really changes (labels versus verdicts); fail open versus fail closed in an outage |
| 5. An LLM as the judge | NeMo's `self check input` rail with your own API key, model and URL; fix a silent parsing failure; measure a policy edit against the judge's noise |

The guide is an Antora site in `content/`, served by Showroom on the cluster and published to
[GitHub Pages](https://hultzj.github.io/nemo-guardrails-eval-lab). Every exercise has a short
video recorded on a live cluster, and the *Architecture and Control Boundaries* page shows how a
request flows, for the deterministic rails and for the LLM judge.

## Prerequisites

### On the cluster (cluster-admin)

| Prerequisite | Why | Check |
|---|---|---|
| OpenShift 5.0 (built and verified on 5.0.0-rc.2) | the platform | `oc get clusterversion` |
| OpenShift GitOps operator | the lab deploys itself through Argo CD | `oc get csv -n openshift-gitops-operator` |
| OpenShift AI 3.5 with the **TrustyAI** and **Workbenches** components `Managed` | the `NemoGuardrails` resource and operator; the participants' JupyterLab | `oc get crd nemoguardrails.trustyai.opendatahub.io notebooks.kubeflow.org` |
| The TrustyAI workbench image (ships with OpenShift AI) | the workbench image | `oc -n redhat-ods-applications get is odh-trustyai-notebook` |
| A worker with 32 free CPU cores and 24 GiB free memory | the safety model runs on CPU: 2 replicas x 16 cores (tested on an m5.metal) | `oc describe nodes \| grep -A5 'Allocated resources'` |
| A default StorageClass | workbench volumes, Showroom | `oc get storageclass` |
| Egress to `github.com`, `huggingface.co`, `ghcr.io`, `registry.access.redhat.com` and `quay.io` (plus the registries OpenShift itself uses) | GitHub: Argo CD pulls this repository and the Showroom chart, Showroom its UI theme, each workbench the lab files; Hugging Face: the 4.9 GB model file (sha256-verified); ghcr.io: the llama.cpp image; registry.access.redhat.com: the UBI Python image (the monitor, the model download); quay.io: the oauth-proxy and Showroom images | the Applications sync and every lab pod reaches `Running` |
| Keycloak as the cluster's identity provider (or create the users yourself) | participants log in; `make users` creates their accounts in Keycloak | `oc -n keycloak get pod/keycloak-0 secret/keycloak-initial-admin` |
| For Module 5: an API key per participant for an OpenAI-compatible endpoint (the RHDP Model-as-a-Service portal issues them), and egress to it | the LLM judge | the participant's *Hello, world!* check prints `"accepted":true` |

Per participant: about 2 cores and 4 GiB for the workbench, a 5 GiB volume, about 100 MiB for the
monitor and about 1 GiB for their guardrails server.

### On your workstation

`oc` (logged in as cluster-admin), `helm` (`make bootstrap` renders the chart), `make`, `bash`,
`git`, `curl`, `python3` and `openssl`. Building the guide or running the tests locally also needs
Node.js and npm; re-recording the videos needs Node.js and ffmpeg.

## What the lab deploys

You do not install the live monitor or the safety model yourself: `make bootstrap` creates an Argo
CD Application that deploys, and keeps in sync:

| Piece | Namespace | What it is |
|---|---|---|
| Safety model | `guardrails-lab-shared` | llama.cpp serving Nemotron Safety Guard 8B (Q4_K_M GGUF) on CPU, 2 replicas, reachable only from the lab's namespaces |
| Tenant projects | `<user>-guardrails` | an OpenShift AI Data Science Project per participant: RBAC, an isolation NetworkPolicy, the `lab-source` ConfigMap and an empty `judge-api-key` Secret |
| Live monitor (`rails-monitor`) | each project | a small proxy behind oauth-proxy that records every verdict and streams it, with the guardrails pod log, to the browser |
| Workbench (`guardrails-lab`) | each project | JupyterLab whose ServiceAccount holds exactly the participant's permissions |
| Decoy tenant | `peer-demo-guardrails` | the "other tenant" Module 1 proves you cannot reach |
| Showroom | `showroom` | the lab guide |

Participants create their own guardrails servers and rail configurations during the modules.

## How the live monitor works

The live monitor is this lab's own code, not an OpenShift or NeMo component: about 240 lines of
Python (standard library only) in
[`monitor.py`](automation/argocd/components/tenants/files/monitor.py) and a single page,
[`index.html`](automation/argocd/components/tenants/files/index.html), behind OpenShift's stock
oauth-proxy.

![Live monitor request flow: the notebook calls oauth-proxy, which checks the caller's token; monitor.py forwards the request with its own ServiceAccount token through kube-rbac-proxy to NeMo Guardrails, records the verdict, returns the answer unchanged, and streams decisions and the guardrails pod log to the browser](docs/images/live-monitor-flow.png)

([Interactive version](docs/live-monitor-flow.html): download it and open it in a browser.)

**When it is built.** `make bootstrap` creates the parent Argo CD Application, which renders the
tenants chart ([`tenants.yaml`](automation/argocd/components/tenants/templates/tenants.yaml)) once
per user in `lab.users`, after the safety model. Every `<user>-guardrails` project therefore has
its monitor before the participant logs in. The pod template carries a checksum of `monitor.py` and
`index.html`, so pushing a change to either rolls the monitor in every project.

**What each project gets.**

| Resource | Purpose |
|---|---|
| ConfigMap `rails-monitor-app` | `monitor.py` and `index.html` from the chart's `files/`, mounted at `/app` |
| Deployment `rails-monitor` | one pod: an init container that generates a random session-cookie key (no secret in git); the `monitor` container (`python3 /app/monitor.py`, UBI Python image, listening only on `127.0.0.1:8080`); and the `oauth-proxy` sidecar on `:8443` |
| ServiceAccount `rails-monitor` | the pod's identity, also registered as oauth-proxy's OAuth client for the Route |
| Role and RoleBinding | read pods and pod logs and `get services` in its own project only |
| ClusterRoleBinding (`system:auth-delegator`) | lets oauth-proxy ask the API server whether a caller's token is allowed |
| Service `rails-monitor` (8443) | with an OpenShift serving certificate, so in-cluster callers use verified TLS |
| Route `rails-monitor` | `https://rails-monitor-<user>-guardrails.<apps domain>`, re-encrypted |

**How a request flows.**

1. A notebook (`labeval.py`) or the guide's `ask` helper sends `POST /v1/chat/completions` to the
   monitor instead of the guardrails server.
2. oauth-proxy admits the request only if the caller's token may `get services` in that project;
   another participant's token is refused. In a browser it runs the OpenShift login instead.
3. `monitor.py` forwards the request to the guardrails Service with its own ServiceAccount token
   (asking, for chat requests, for the verdict details); kube-rbac-proxy in front of the guardrails
   server checks that token the same way.
4. The answer goes back to the caller unchanged. The monitor classifies it as allowed, blocked
   (with the rail and categories) or error (no verdict), keeps it in memory and pushes it to every
   open browser over Server-Sent Events (`/events`).
5. If the request carries `X-Eval-Run`, `X-Eval-Label` and `X-Eval-Total` headers (the notebooks
   send them), the monitor also tallies TP/FP/FN/TN for that run; the notebooks read the tallies
   back from `/api/runs` to cross-check their own numbers.
6. A background thread finds the guardrails pod through the Kubernetes API and streams its log to
   the page, picking up the new pod after every rollout.

**Limits.** State is in memory: the last 300 decisions, the last 60 log lines and every run. A
restart or rollout clears it. The monitor sees only requests sent through it; calling the
guardrails Route directly (as Module 1, Exercise 3 and Module 2, Exercise 4 do on purpose)
bypasses it.

**Code and tests.** [`automation/tests/test_monitor.py`](automation/tests/test_monitor.py) runs the
real handler against a stub guardrails server and a stub Kubernetes API. To look at a running
monitor: `oc -n <user>-guardrails get deploy,svc,route rails-monitor` and
`oc -n <user>-guardrails logs deploy/rails-monitor -c monitor`.

## Deploy

From a checkout of this repository, logged in to the cluster as cluster-admin:

1. Edit `automation/argocd/values.yaml` and push it: Argo CD deploys from the remote, not your
   checkout.
   * `lab.users`: the participant accounts (default `user1`..`user5` and `presenter`).
   * `gitops.repoUrl`: your fork, if you deploy from one. `make bootstrap` deploys from this
     repository (override with `REPO_URL=...`) at `gitops.revision` (override with `REVISION=...`).
   * `images.oauthProxy`: this cluster's oauth-proxy image,
     `oc get is oauth-proxy -n openshift -o jsonpath='{.status.tags[0].items[0].dockerImageReference}'`.
   * `workbench.imageCommit`: the build commit of this cluster's TrustyAI workbench image (the
     `opendatahub.io/notebook-build-commit` annotation on the `odh-trustyai-notebook` image stream
     tag). `make verify` reports a mismatch in either image value.
2. Create the Keycloak accounts. Each participant gets a random password, printed once and stored
   nowhere; hand each attendee only their own line.
   ```bash
   make -C automation users
   ```
   `make users` runs Keycloak's `kcadm.sh` inside the Keycloak pod, as the admin in the
   `keycloak-initial-admin` Secret. It expects namespace `keycloak`, pod `keycloak-0` and the realm
   OpenShift logs in through, `openshift`; override them with `KC_NS`, `KC_POD` and `REALM`. With
   another identity provider, create accounts named as in `lab.users` there and skip this step.
3. Deploy, and wait until every Application is `Synced` and `Healthy` (the safety model takes a few
   minutes to download and verify its model file):
   ```bash
   make -C automation bootstrap
   make -C automation status
   ```
4. Check the cluster is ready for class; every line must print `PASS`:
   ```bash
   make -C automation verify
   ```

The Showroom URL is `oc -n showroom get route showroom`. The *Facilitator Guide* page covers the
day of class; `make -C automation teardown` removes the lab and every namespace it created.

## Repository layout

| Path | Contents |
|---|---|
| `content/` | the Antora guide: pages, step videos (`assets/images/*.mp4`), diagrams |
| `notebooks/` | the evaluation notebooks, `labeval.py` and the 50-prompt `dataset.jsonl` |
| `automation/` | the Argo CD chart (`argocd/`), `make` targets, `create-users.sh`, `verify.sh`, tests |
| `diagrams/` | sources of the architecture diagrams ([how to regenerate](diagrams/README.md)) |
| `docs/` | the live monitor flow diagram for this README (interactive HTML and PNG) |
| `scripts/check-lab.sh` | the guide's contract: every runnable step has a verified expected result |
| `scripts/videos/` | the recorder for the step videos ([how to re-record](scripts/videos/README.md)) |

## Development

```bash
npm ci && npm run build                        # the guide, into www/
bash scripts/check-lab.sh                      # the guide's contract
python3 -m unittest discover -s automation/tests
bash automation/tests/selftest.sh              # chart, scripts and guide checks (needs helm)
node --test scripts/videos/lib.test.mjs        # recorder checks (browser and ffmpeg ones skip without them)
```

The GitHub Actions workflow runs these checks on every push to `main` and every pull request
against it, and publishes the guide to GitHub Pages from `main`.

## Security notes

* No credentials are stored in this repository. Participant passwords are generated per user and
  printed once; Module 5's API key is each participant's own, typed without echo and kept in the
  `judge-api-key` Secret in their project.
* Participants cannot read Secrets, other than their own `judge-api-key`, and cannot create any.
  They cannot reach another tenant's guardrails server.
* The step videos are recorded with an off-camera login and a scan of every on-screen text for
  known credentials and token-shaped strings; a match discards the video.
