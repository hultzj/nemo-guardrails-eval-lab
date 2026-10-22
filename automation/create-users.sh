#!/usr/bin/env bash
# Create (or reset) the lab's Keycloak users in the realm OpenShift logs in through.
# Usage: bash automation/create-users.sh [user1 user2 ...]
# Without arguments: lab.users from argocd/values.yaml. Users in lab.presenters are always skipped:
# they are existing accounts, and this script would reset their password.
#
# Every user gets their own random password, printed once below for the facilitator to hand out
# individually: with one shared password any attendee could log in as another and reach their
# tenant. LAB_SHARED_PASSWORD='<pw>' sets one password for all instead; only for a single-person
# rehearsal. Passwords are never stored in git or on disk.
set -euo pipefail

VALUES="$(cd "$(dirname "$0")" && pwd)/argocd/values.yaml"
list() { sed -n "s/^  $1: \[\(.*\)\]/\1/p" "$VALUES" | tr -d ' ' | tr ',' ' '; }

KC_NS=${KC_NS:-keycloak}
KC_POD=${KC_POD:-keycloak-0}
REALM=${REALM:-openshift}
requested=("$@")
[[ ${#requested[@]} -gt 0 ]] || read -ra requested <<<"$(list users)"
presenters=" ${PRESENTERS-$(list presenters)} "
users=()
for u in "${requested[@]}"; do
  [[ $u =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "invalid username: $u" >&2; exit 1; }
  if [[ $presenters == *" $u "* ]]; then echo "skip    $u (presenter: existing account, not reset)"; else users+=("$u"); fi
done
[[ ${#users[@]} -gt 0 ]] || { echo "no participant users to create" >&2; exit 1; }

passwords=()
for u in "${users[@]}"; do
  if [[ -n ${LAB_SHARED_PASSWORD:-} ]]; then
    passwords+=("$LAB_SHARED_PASSWORD")
  else
    passwords+=("$(openssl rand -hex 8)")  # 16 characters, 64 random bits
  fi
done
if [[ -n ${DRY_RUN:-} ]]; then  # for automation/tests/selftest.sh: nothing reaches Keycloak
  for i in "${!users[@]}"; do echo "would create ${users[$i]} ${passwords[$i]}"; done
  exit 0
fi

admin_user=$(oc -n "$KC_NS" get secret keycloak-initial-admin -o jsonpath='{.data.username}' | base64 -d)
admin_pass=$(oc -n "$KC_NS" get secret keycloak-initial-admin -o jsonpath='{.data.password}' | base64 -d)

# Credentials travel on stdin, not on the oc command line.
{
  printf 'ADMIN_USER=%q\nADMIN_PASS=%q\nREALM=%q\n' "$admin_user" "$admin_pass" "$REALM"
  printf 'USERS=(%s)\nPASSWORDS=(%s)\n' "${users[*]}" "${passwords[*]}"
  cat <<'EOS'
set -eu
kc=/opt/keycloak/bin/kcadm.sh
export HOME=/tmp
$kc config credentials --server http://localhost:8080 --realm master --user "$ADMIN_USER" --password "$ADMIN_PASS" >/dev/null 2>&1
for i in "${!USERS[@]}"; do
  u=${USERS[$i]}
  if [ -z "$($kc get users -r "$REALM" -q username="$u" -q exact=true --fields id --format csv --noquotes)" ]; then
    $kc create users -r "$REALM" -s username="$u" -s enabled=true -s emailVerified=true \
      -s email="$u@example.com" -s firstName="$u" -s lastName=lab >/dev/null 2>&1
    echo "created $u"
  else
    echo "exists  $u (password reset)"
  fi
  $kc set-password -r "$REALM" --username "$u" --new-password "${PASSWORDS[$i]}"
done
EOS
} | oc -n "$KC_NS" exec -i "$KC_POD" -- bash -s

echo
echo "Hand each attendee only their own line (not stored anywhere; re-run to reset):"
for i in "${!users[@]}"; do printf '  %-12s %s\n' "${users[$i]}" "${passwords[$i]}"; done
