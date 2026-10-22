SHELL := /usr/bin/env bash

AUTOMATION_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
GITOPS_NS ?= openshift-gitops
# The parent Application name, from bootstrap.yaml; every namespace this lab creates carries part-of=<LAB_APP>.
LAB_APP := $(shell awk '/^metadata:/ {m = 1} m && $$1 == "name:" {print $$2; exit}' "$(AUTOMATION_DIR)/bootstrap.yaml" 2>/dev/null)
# Defaults to the revision values.yaml names, so an event branch deploys itself.
REVISION ?= $(shell awk '/^gitops:/ {g = 1} g && $$1 == "revision:" {print $$2; exit}' "$(AUTOMATION_DIR)/argocd/values.yaml" 2>/dev/null)

.PHONY: help users bootstrap status verify teardown

help:
	@printf '%s\n' \
	  '  make users       Create/reset the Keycloak users (needs LAB_USER_PASSWORD)' \
	  '  make bootstrap   Deploy the lab through OpenShift GitOps (clusters without an RHDP order)' \
	  '  make status      Show sync and health of the lab Applications' \
	  '  make verify      PASS/FAIL readiness checks for class' \
	  '  make teardown    Delete the lab Applications and everything they created'

# bootstrap refuses any namespace the lab would deploy into or create (Application destinations and
# every Namespace a component renders) that exists without this lab's label: Argo CD would adopt it,
# and teardown would delete it. Then it creates and labels the Showroom namespace, which the upstream
# chart creates unlabeled, so Argo CD may deploy there. No comments inside the recipe: after
# backslash-newline joining, a shell comment would swallow the rest of it.
bootstrap:
	@set -euo pipefail; \
	[[ "$(REVISION)" =~ ^[A-Za-z0-9._/-]+$$ ]] || { echo "REVISION '$(REVISION)' has unsafe characters" >&2; exit 1; }; \
	domain=$$(oc get ingresses.config.openshift.io cluster -o jsonpath='{.spec.domain}'); test -n "$$domain"; \
	bash "$(AUTOMATION_DIR)/guard-namespaces.sh" "$(LAB_APP)" "$$domain"; \
	apps=$$(helm template "$(LAB_APP)" "$(AUTOMATION_DIR)/argocd" --set deployer.domain="$$domain"); \
	sns=$$(awk -v app="$(LAB_APP)-showroom" '$$1 == "name:" && $$2 == app {f = 1} f && $$1 == "destination:" {d = 1} f && d && $$1 == "namespace:" {print $$2; exit}' <<<"$$apps"); \
	if [[ -n "$$sns" ]]; then \
	  oc create namespace "$$sns" --dry-run=client -o yaml | oc apply -f -; \
	  oc label namespace "$$sns" "argocd.argoproj.io/managed-by=$(GITOPS_NS)" "app.kubernetes.io/part-of=$(LAB_APP)" --overwrite; \
	fi; \
	sed "s|DEPLOYER_DOMAIN|$$domain|; s|GIT_REVISION|$(REVISION)|" "$(AUTOMATION_DIR)/bootstrap.yaml" | oc apply -f -

# Participants from lab.users, minus lab.presenters (existing accounts that must not be reset).
users:
	bash "$(AUTOMATION_DIR)/create-users.sh" $(USERS)

status:
	oc -n $(GITOPS_NS) get applications.argoproj.io -l demo.redhat.com/application=field-content

verify:
	GITOPS_NS=$(GITOPS_NS) bash "$(AUTOMATION_DIR)/verify.sh"

teardown:
	oc delete -f "$(AUTOMATION_DIR)/bootstrap.yaml" --ignore-not-found --wait=true
