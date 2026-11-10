#!/usr/bin/env bash
# mos_mint_token.sh - try to mint a My Oracle Support download token WITHOUT a browser,
# using the OAuth2 "password" grant against Oracle's external identity domain.
#
# This is the mechanism Oracle's own tools (AutoUpgrade, Enterprise Manager) appear to use
# when given MOS username/password. Whether it is permitted for YOUR account is exactly
# what this script tests. It prints only HTTP status, OAuth error codes and token lifetime.
#
# Usage (interactive, prompts for password; nothing is echoed):
#   bash mos_mint_token.sh [username] [token_out_file]
#
# Unattended (password from env or macOS Keychain):
#   MOS_USER=you@example.edu MOS_PASS="$(security find-generic-password -a you@example.edu -s mos -w)" \
#     bash mos_mint_token.sh
set -euo pipefail

TOKEN_ENDPOINT="https://login-ext.identity.oraclecloud.com/oauth2/v1/token"
# Client that issued the browser token (UPDATES_PROD). Override with MOS_CLIENT_ID if needed.
CLIENT_ID="${MOS_CLIENT_ID:-d590f99d833d47789fb686701f021cba}"
CLIENT_SECRET="${MOS_CLIENT_SECRET:-}"
# Scope variants to try, in order. OCI identity domains want custom scopes prefixed with the
# resource audience (the browser token had aud "updates." and scope "search metadata ... patch_download").
if [[ -n "${MOS_SCOPE:-}" ]]; then SCOPES=("$MOS_SCOPE"); else
  SCOPES=(
    "updates.search updates.metadata updates.conflict_checks updates.get_inventory_upi updates.conflict_resolution updates.patch_download"
    "updates.patch_download updates.search updates.metadata"
    "urn:opc:idm:__myscopes__"
    "openid"
  )
fi

USER_NAME="${MOS_USER:-${1:-}}"
OUT="${2:-$HOME/.mos/token.txt}"

if [[ -z "$USER_NAME" ]]; then read -r -p "MOS username (email): " USER_NAME; fi
if [[ -z "${MOS_PASS:-}" ]]; then read -r -s -p "MOS password (not shown): " MOS_PASS; echo; fi

umask 077
mkdir -p "$(dirname "$OUT")"
resp="$(mktemp)"; trap 'rm -f "$resp"' EXIT

# Build the form body via a curl config file so credentials never appear in `ps`.
cfg="$(mktemp)"; trap 'rm -f "$resp" "$cfg"' EXIT
attempt() {  # attempt <scope> -> writes $resp, echoes http code
  local scope="$1"
  {
    echo 'url = "'"$TOKEN_ENDPOINT"'"'
    echo 'request = "POST"'
    echo 'header = "Content-Type: application/x-www-form-urlencoded"'
    echo 'data-urlencode = "grant_type=password"'
    echo 'data-urlencode = "scope='"$scope"'"'
    echo 'data-urlencode = "username='"$USER_NAME"'"'
    echo 'data-urlencode = "password='"$MOS_PASS"'"'
    if [[ -n "$CLIENT_SECRET" ]]; then
      echo 'user = "'"$CLIENT_ID:$CLIENT_SECRET"'"'
    else
      echo 'data-urlencode = "client_id='"$CLIENT_ID"'"'
    fi
  } > "$cfg"
  curl -s -K "$cfg" -o "$resp" -w '%{http_code}' --max-time 60 || echo 000
}

ok=0
for scope in "${SCOPES[@]}"; do
  code="$(attempt "$scope")"
  short="${scope:0:60}"; if [[ ${#scope} -gt 60 ]]; then short="${short}..."; fi
  if [[ "$code" == "200" ]] && grep -q '"access_token"' "$resp" 2>/dev/null; then
    echo "HTTP 200  scope=[$short]  -> token issued"; ok=1; break
  fi
  err="$(python3 -I -c 'import json,sys
try:
    d=json.load(open(sys.argv[1])); print(d.get("error"), "-", d.get("error_description"))
except Exception: print("non-JSON response")' "$resp")"
  echo "HTTP $code  scope=[$short]  -> $err"
done
unset MOS_PASS; : > "$cfg"
(( ok )) || { echo "No scope variant produced a token."; exit 3; }

python3 -I - "$resp" "$OUT" <<'PY'
import json, sys, base64, datetime, os
resp, out = sys.argv[1], sys.argv[2]
d = json.load(open(resp))
tok = d["access_token"]
with open(out, "w") as f:
    f.write(tok + "\n")
os.chmod(out, 0o600)
print("Token saved to", out)
print("token_type:", d.get("token_type"), " expires_in:", d.get("expires_in"), "s",
      " refresh_token:", "yes" if d.get("refresh_token") else "no")
try:
    p = tok.split(".")[1]; p += "=" * (-len(p) % 4)
    c = json.loads(base64.urlsafe_b64decode(p))
    exp = datetime.datetime.fromtimestamp(c["exp"], datetime.timezone.utc)
    print("exp:", exp.astimezone().strftime("%Y-%m-%d %H:%M %Z"), " amr:", c.get("amr"), " gtp:", c.get("gtp"))
except Exception:
    pass
PY
