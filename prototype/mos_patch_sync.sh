#!/usr/bin/env bash
# mos_patch_sync.sh — find the newest PeopleTools product patch for a release/platform via
# Oracle's ARU XML services and download it (resumable), verifying SHA-256 digests.
#
# Requires a valid MOS download token (Bearer) in a file. Tokens live ~1 hour; mint one with
# mos_mint_token.sh (unattended, if the password grant is allowed for your account) or from
# https://updates.oracle.com/Orion/GenToken/get_token in a signed-in browser.
#
# Usage:
#   bash mos_patch_sync.sh [-t ~/.mos/token.txt] [-o ~/Downloads/peopletools] [-r 8.61] [-p 233]
#                          [-m 'PEOPLETOOLS-WIN-' ] [--list] [--dry-run]
#
#   -p platform id   233 = Microsoft Windows x64 (64-bit), 226 = Linux x86-64, 212 = AIX 64-bit
#   -m file prefix   selects "product patch" patchsets by their file names (default PEOPLETOOLS-WIN-)
#   --list           show the candidate patches and exit
#   --dry-run        resolve the newest patch and print its files, but do not download
#
# Exit codes: 0 ok, 1 usage/config, 2 token rejected (401/403), 3 download/verify failure
set -euo pipefail

TOKEN_FILE="$HOME/.mos/token.txt"
OUTDIR="$HOME/Downloads/peopletools"
RELEASE="8.61"
PLATFORM="233"
FILE_PREFIX="PEOPLETOOLS-WIN-"
PRODUCT_ID="21917"     # PeopleSoft PeopleTools (family); 21918 = PeopleSoft Enterprise PT PeopleTools
LIST_ONLY=0; DRY_RUN=0
ARU="https://updates.oracle.com"

while (( $# )); do
  case "$1" in
    -t) TOKEN_FILE="$2"; shift 2;;
    -o) OUTDIR="$2"; shift 2;;
    -r) RELEASE="$2"; shift 2;;
    -p) PLATFORM="$2"; shift 2;;
    -m) FILE_PREFIX="$2"; shift 2;;
    --list) LIST_ONLY=1; shift;;
    --dry-run) DRY_RUN=1; shift;;
    -h|--help) sed -n '2,20p' "$0"; exit 0;;
    *) echo "Unknown arg: $1" >&2; exit 1;;
  esac
done

[[ -r "$TOKEN_FILE" ]] || { echo "Token file not readable: $TOKEN_FILE" >&2; exit 1; }
TOKEN="$(grep -v '^\s*#' "$TOKEN_FILE" | grep -v '^\s*$' | head -n1 | tr -d '[:space:]')"
[[ -n "$TOKEN" ]] || { echo "Empty token" >&2; exit 1; }

# curl wrapper: token passed through a config on stdin so it never appears in `ps`.
aru_curl() {  # aru_curl <outfile> <url> [extra args]
  local out="$1" url="$2"; shift 2
  printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" | \
    curl -s -K - -L --max-time 600 -o "$out" -w '%{http_code}' "$@" "$url"
}

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT
mkdir -p "$OUTDIR"

# ---- 1. resolve release id ----
code="$(aru_curl "$work/releases.xml" "$ARU/Orion/Services/metadata?table=aru_releases")"
[[ "$code" == "200" ]] || { echo "aru_releases HTTP $code (token expired?)" >&2; exit 2; }
REL_ID="$(python3 -I - "$work/releases.xml" "$RELEASE" <<'PY'
import sys, xml.etree.ElementTree as ET
root=ET.parse(sys.argv[1]).getroot(); want=sys.argv[2]
for r in root.iter('release'):
    if (r.text or '').strip()==f"PeopleSoft PeopleTools {want}": print(r.get('id')); break
PY
)"
[[ -n "$REL_ID" ]] || { echo "Release 'PeopleSoft PeopleTools $RELEASE' not found" >&2; exit 1; }

# ---- 2. search patches for product/release/platform ----
code="$(aru_curl "$work/search.xml" "$ARU/Orion/Services/search?product=$PRODUCT_ID&release=$REL_ID&platform=$PLATFORM")"
[[ "$code" == "200" ]] || { echo "search HTTP $code (token expired?)" >&2; exit 2; }

# ---- 3. pick newest patch whose files match the prefix; emit TSV of files ----
python3 -I - "$work/search.xml" "$FILE_PREFIX" "$LIST_ONLY" > "$work/plan.tsv" <<'PY'
import sys, xml.etree.ElementTree as ET
root=ET.parse(sys.argv[1]).getroot(); prefix=sys.argv[2]; list_only=sys.argv[3]=="1"
cands=[]
for p in root.iter('patch'):
    files=[f for f in p.iter('file')]
    names=[f.findtext('name') or '' for f in files]
    if not any(n.startswith(prefix) for n in names): continue
    rel=p.findtext('released_date') or ''; upd=p.findtext('updated_date') or ''
    desc=''
    b=p.find('bug')
    if b is not None: desc=(b.findtext('abstract') or '').strip()
    cands.append((rel or upd, p.findtext('name'), desc, p.findtext('size') or '', files))
cands.sort(key=lambda c: c[0], reverse=True)
if list_only:
    for c in cands: print(f"LIST\t{c[0]}\t{c[1]}\t{c[2]}\t{c[3]}", file=sys.stderr)
    sys.exit(0)
if not cands: print("NONE", file=sys.stderr); sys.exit(4)
top=cands[0]
print(f"PATCH\t{top[1]}\t{top[0]}\t{top[2]}\t{top[3]}", file=sys.stderr)
for f in top[4]:
    url=f.find('download_url'); host=url.get('host') if url is not None else ''
    path=(url.text or '').strip() if url is not None else ''
    sha=''
    for d in f.iter('digest'):
        if d.get('type')=='SHA-256': sha=(d.text or '').strip().lower()
    print(f"{f.findtext('name')}\t{f.findtext('size')}\t{host}{path}\t{sha}")
PY
rc=$?
(( LIST_ONLY )) && exit 0
(( rc == 0 )) || { echo "No matching patch found" >&2; exit 1; }

# ---- 4. download with resume + verify ----
fail=0
while IFS=$'\t' read -r name size url sha; do
  [[ -n "$name" ]] || continue
  dest="$OUTDIR/$name"
  if [[ -f "$dest" && "$(stat -f%z "$dest" 2>/dev/null || stat -c%s "$dest")" == "$size" ]]; then
    echo "exists: $name ($size bytes) — verifying"
  else
    (( DRY_RUN )) && { echo "would download: $name ($size bytes)"; continue; }
    echo "downloading: $name ($size bytes)"
    code="$(aru_curl "$dest" "$url" -C - --retry 5 --retry-delay 15)"
    if [[ "$code" == "401" || "$code" == "403" ]]; then echo "AUTH FAILED ($code) on $name — token expired?" >&2; exit 2; fi
    if [[ "$code" != "200" && "$code" != "206" ]]; then echo "HTTP $code on $name" >&2; fail=1; continue; fi
  fi
  (( DRY_RUN )) && continue
  got="$(shasum -a 256 "$dest" | cut -d' ' -f1)"
  if [[ -n "$sha" && "$got" != "$sha" ]]; then echo "SHA-256 MISMATCH: $name" >&2; fail=1; else echo "verified: $name"; fi
done < "$work/plan.tsv"

(( fail == 0 )) || exit 3
echo "done: files in $OUTDIR"
