#!/usr/bin/env bash
# mos_token_probe.sh — inspect a My Oracle Support download token WITHOUT printing it.
#
# Usage: bash mos_token_probe.sh [/home/oracle/.mos/token.txt]
#
# Prints only: token shape, iat/exp timestamps (if JWT), hours remaining,
# and HTTP status codes for the endpoints an unattended job would need.
set -euo pipefail

TOKEN_FILE="${1:-${MOS_DIR:-/home/oracle/.mos}/token.txt}"
[[ -r "$TOKEN_FILE" ]] || { echo "Token file not readable: $TOKEN_FILE" >&2; exit 1; }

# first non-empty, non-comment line; strip whitespace
TOKEN="$(grep -v '^\s*#' "$TOKEN_FILE" | grep -v '^\s*$' | head -n1 | tr -d '[:space:]')"
[[ -n "$TOKEN" ]] || { echo "Token file is empty." >&2; exit 1; }

echo "Token length: ${#TOKEN} chars"
dots="$(tr -cd '.' <<<"$TOKEN" | wc -c | tr -d ' ')"
echo "Dot-separated segments: $((dots + 1))"

# ---- JWT inspection (payload only, selected non-sensitive claims) ----
if (( dots == 2 )); then
  payload="$(cut -d. -f2 <<<"$TOKEN")"
  # base64url -> base64 with padding
  payload="$(tr '_-' '/+' <<<"$payload")"
  pad=$(( (4 - ${#payload} % 4) % 4 )); payload="${payload}$(printf '=%.0s' $(seq 1 $pad 2>/dev/null))"
  decoded="$(base64 -d <<<"$payload" 2>/dev/null || base64 -D <<<"$payload" 2>/dev/null || true)"
  if [[ -n "$decoded" ]]; then
    python3 -I - "$decoded" <<'PY'
import json, sys, datetime
try:
    d = json.loads(sys.argv[1])
except Exception as e:
    print("JWT payload not JSON:", e); sys.exit(0)
print("JWT claim names:", ", ".join(sorted(d.keys())))
now = datetime.datetime.now(datetime.timezone.utc)
for k in ("iat", "nbf", "exp"):
    v = d.get(k)
    if isinstance(v, (int, float)):
        t = datetime.datetime.fromtimestamp(v, datetime.timezone.utc)
        print(f"{k}: {t.isoformat()}  (local {t.astimezone().strftime('%Y-%m-%d %H:%M %Z')})")
if isinstance(d.get("exp"), (int, float)):
    rem = datetime.datetime.fromtimestamp(d["exp"], datetime.timezone.utc) - now
    print(f"Time remaining: {rem.total_seconds()/3600:.1f} hours ({rem.days} days)")
if isinstance(d.get("iat"), (int, float)) and isinstance(d.get("exp"), (int, float)):
    print(f"Total lifetime: {(d['exp']-d['iat'])/3600:.1f} hours ({(d['exp']-d['iat'])/86400:.1f} days)")
PY
  else
    echo "Could not base64-decode JWT payload."
  fi
else
  echo "Not a JWT (opaque token); lifetime cannot be read locally."
fi

# ---- endpoint probes: status codes only ----
# Pass the header through a curl config on stdin so the token never appears in `ps`.
probe() {  # probe <label> <url> [extra curl args...]
  local label="$1" url="$2"; shift 2
  local tmp; tmp="$(mktemp)"
  local code
  code="$(printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" | \
          curl -s -K - -o "$tmp" -w '%{http_code}' -L --max-time 60 "$@" "$url" || echo "000")"
  local size; size="$(wc -c <"$tmp" | tr -d ' ')"
  local ctype; ctype="$(file -b --mime-type "$tmp" 2>/dev/null || echo '?')"
  printf '%-34s HTTP %s  bytes=%s  type=%s\n' "$label" "$code" "$size" "$ctype" >&2
  echo "$tmp"
}

echo
echo "== Endpoint probes (Bearer token) =="

# 1) Can a token mint a new token?  (compare hashes only)
t1="$(probe "GenToken/get_token" "https://updates.oracle.com/Orion/GenToken/get_token")"
h_old="$(printf '%s' "$TOKEN" | sha256sum | cut -c1-16)"
h_new="$(tr -d '[:space:]' <"$t1" | sha256sum | cut -c1-16)"
if [[ "$h_old" == "$h_new" ]]; then echo "   -> response equals current token (no rotation)";
else
  b="$(tr -d '[:space:]' <"$t1")"; nd="$(tr -cd '.' <<<"$b" | wc -c | tr -d ' ')"
  if [[ "$(wc -c <"$t1" | tr -d ' ')" -lt 4096 && "$nd" -eq 2 ]]; then echo "   -> response looks like a DIFFERENT JWT (token refresh may be possible)";
  else echo "   -> response is not a token (likely HTML login/redirect page)"; fi
fi
rm -f "$t1"

# 2) File download with HEAD only (no body)
t2="$(probe "download (HEAD, 1of4)" \
  "https://updates.oracle.com/Orion/Services/download/PEOPLETOOLS-WIN-8.61.24_1of4.zip?aru=29519651&patch_file=PEOPLETOOLS-WIN-8.61.24_1of4.zip" -I)"
rm -f "$t2"

# 3) Legacy patch search page
t3="$(probe "Orion SimpleSearch (patch 40014041)" \
  "https://updates.oracle.com/Orion/SimpleSearch/process_form?search_type=patch&patch_number=40014041&plat_lang=233P")"
if grep -q "PEOPLETOOLS-WIN-8.61.24" "$t3" 2>/dev/null; then echo "   -> search results contain the patch file names (search is scriptable with the token)";
elif grep -qi "sign in\|signon\|login" "$t3" 2>/dev/null; then echo "   -> got a sign-in page (token not accepted for search)";
else echo "   -> no patch file names found in response"; fi
rm -f "$t3"

# 4) ARU XML search service
t4="$(probe "Orion/Services/search (XML API)" \
  "https://updates.oracle.com/Orion/Services/search?bug=40014041")"
if grep -q "<results\|<patch" "$t4" 2>/dev/null; then echo "   -> XML patch metadata returned (best option for finding the latest patch)";
else echo "   -> no XML patch metadata in response"; fi
rm -f "$t4"

echo
echo "Done. Nothing above contains the token."
