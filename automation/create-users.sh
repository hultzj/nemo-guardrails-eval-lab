#!/usr/bin/env bash
# Create (or reset) the lab's Keycloak users in the realm OpenShift logs in through.
# Usage: LAB_USER_PASSWORD='<choose one>' bash automation/create-users.sh [user1 user2 ...]
# The password is never stored in git; hand it to attendees on the day.
set -euo pipefail

: "${LAB_USER_PASSWORD:?set LAB_USER_PASSWORD to the password attendees will use}"
KC_NS=${KC_NS:-keycloak}
KC_POD=${KC_POD:-keycloak-0}
REALM=${REALM:-openshift}
users=("$@")
[[ ${#users[@]} -gt 0 ]] || users=(user1 user2 user3 user4 user5)
for u in "${users[@]}"; do
  [[ $u =~ ^[a-z0-9][a-z0-9-]*$ ]] || { echo "invalid username: $u" >&2; exit 1; }
done

admin_user=$(oc -n "$KC_NS" get secret keycloak-initial-admin -o jsonpath='{.data.username}' | base64 -d)
admin_pass=$(oc -n "$KC_NS" get secret keycloak-initial-admin -o jsonpath='{.data.password}' | base64 -d)

# Credentials travel on stdin, not on the oc command line.
{
  printf 'ADMIN_USER=%q\nADMIN_PASS=%q\nUSER_PASS=%q\nREALM=%q\n' \
    "$admin_user" "$admin_pass" "$LAB_USER_PASSWORD" "$REALM"
  printf 'USERS=(%s)\n' "${users[*]}"
  cat <<'EOS'
set -eu
kc=/opt/keycloak/bin/kcadm.sh
export HOME=/tmp
$kc config credentials --server http://localhost:8080 --realm master --user "$ADMIN_USER" --password "$ADMIN_PASS" >/dev/null 2>&1
for u in "${USERS[@]}"; do
  if [ -z "$($kc get users -r "$REALM" -q username="$u" -q exact=true --fields id --format csv --noquotes)" ]; then
    $kc create users -r "$REALM" -s username="$u" -s enabled=true -s emailVerified=true \
      -s email="$u@example.com" -s firstName="$u" -s lastName=lab >/dev/null 2>&1
    echo "created $u"
  else
    echo "exists  $u (password reset)"
  fi
  $kc set-password -r "$REALM" --username "$u" --new-password "$USER_PASS"
done
EOS
} | oc -n "$KC_NS" exec -i "$KC_POD" -- bash -s
