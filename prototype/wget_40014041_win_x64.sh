#!/usr/bin/env bash
# Force bash even if invoked via sh/ksh/zsh or sourced in a non-bash shell
[[ -n "${BASH_VERSION:-}" ]] || exec /usr/bin/env bash "$0" "$@"

set -euo pipefail

# -------------------- bash version check (bash >= 4 required) --------------------
# Solaris-proof: relies only on bash built-ins, no external tools.
require_bash4() {
  local major="${BASH_VERSINFO[0]:-0}"
  if (( major < 4 )); then
    {
      echo "ERROR: This script requires GNU bash >= 4."
      echo "Detected: ${BASH_VERSION:-unknown}"
      echo
      echo "Why: the script uses bash-4+ features (e.g., associative arrays: declare -A)."
      echo
      echo "Fix options:"
      echo "  - Linux: install/update bash (bash 4+ is standard on most modern distros)"
      echo "  - macOS: install newer bash and run explicitly, e.g.:"
      echo "      brew install bash"
      echo "      /opt/homebrew/bin/bash ./wget.sh ..."
      echo "  - Solaris/AIX/HP-UX: install GNU bash 4+ and ensure it is first in PATH, then run:"
      echo "      /path/to/bash ./wget.sh ..."
      echo
    } >&2
    exit 2
  fi
}
require_bash4

# --- Template placeholders (kept for templating systems; script itself is non-interactive) ---
is_placeholder() { [[ "${1:-}" =~ ^__[A-Z0-9_]+__$ ]]; }

TPL_START_URL="${TPL_START_URL:-__START_URL__}"
TPL_DOWNLOAD_URL="${TPL_DOWNLOAD_URL:-__DOWNLOAD_URL__}"
TPL_OUTFILE="${TPL_OUTFILE:-__OUTFILE__}"

# Compute default START_URL from template (if filled) or original fallback
_default_start_url="https://updates.oracle.com/Orion/GenToken/get_token"
if [[ -n "${TPL_START_URL:-}" ]] && ! is_placeholder "$TPL_START_URL"; then
  _default_start_url="$TPL_START_URL"
fi

# --- Config ---
START_URL="${START_URL:-$_default_start_url}"
CA_CERT_PATH="${CA_CERT_PATH:-}"
WGET_OPTS="${WGET_OPTS:-}"

usage() {
  cat <<'EOF'
Usage: wget.sh [options]

MODES (choose exactly one):
  Single download:
    -d <url>         Protected download URL
    -S <sha256>      Expected SHA-256 digest
    [-O <file>]      Output filename (optional; derived from patch_file or URL basename)
    [-W <password>]  Patch password (optional; appends &patch_password=<pwd>)

  Batch download:
    -f <file>        Batch file lines: URL,,SHA256 or URL,PATCH_PASSWORD,SHA256
                    (password optional; leave empty when not required)
                    Output filename derived from patch_file=... (preferred) else URL basename.
                    Lines starting with '#' are ignored.

  Auto-batch (no -d and no -f):
    Script auto-creates a batch file in output dir and uses it.
    Intended for Download UI placeholder replacement.

MANDATORY:
  -t <token>         Access token string (choose one of -t or -T)
  -T <file>          Read access token from file (first non-empty, non-# line)
  SHA-256 digest     Required for every download. Use -S in single mode.
                     In batch mode, pass URL,,SHA256 or URL,PATCH_PASSWORD,SHA256.

OPTIONAL:
  -p <dir>           Output directory (default: current directory)
  -P <n>             Parallel downloads (default: 10 for batch, else 1)
  -L <file>          Log file path (if set, output is written only to this file)
  -h                 Help
  WGET_OPTS          Safe extra wget options via env: timeout, retry,
                     rate-limit, and IPv4/IPv6 options only

INTERNAL / HIDDEN:
  -C <file>          Custom CA cert bundle path

AUTH BEHAVIOR:
- Preflight: If the token is invalid/expired (HTTP 401/403) before starting, exit immediately.
- Mid-run: If a download hits 401/403, stop scheduling NEW downloads, but allow already-running ones to finish.
- End summary prints which patches succeeded/failed/skipped.

ZIP VALIDATION (DEFERRED):
- ZIP validation runs AFTER all downloads complete.
- Before validating each file, a debug line is printed.
- Uses: zip -T <file> if zip is available.
- If zip is missing, validation is skipped and user must validate manually.

SHA-256 VALIDATION:
- SHA-256 validation runs after downloads complete.
- If SHA-256 is missing, malformed, or mismatched, the script fails.
- Uses sha256sum, shasum, openssl, or digest depending on what is available.

NOTES:
- Non-interactive: missing required params => exits before downloading.
- Token is never echoed.
EOF
}

# -------------------- args --------------------

OUTDIR=""
DOWNLOAD_URL=""
OUTFILE=""
BATCH_FILE=""
AUTO_BATCH_FILE=""
PARALLEL=""
LOG_FILE=""

ACCESS_TOKEN=""
TOKEN_FILE=""

PATCH_PASSWORD=""   # single mode -W
SINGLE_SHA256=""    # single mode -S

while (( "$#" )); do
  case "${1:-}" in
    -d) DOWNLOAD_URL="${2:-}"; shift 2 ;;
    -O) OUTFILE="${2:-}"; shift 2 ;;
    -W) PATCH_PASSWORD="${2:-}"; shift 2 ;;
    -S) SINGLE_SHA256="${2:-}"; shift 2 ;;
    -f) BATCH_FILE="${2:-}"; shift 2 ;;
    -p) OUTDIR="${2:-}"; shift 2 ;;
    -P) PARALLEL="${2:-}"; shift 2 ;;
    -L) LOG_FILE="${2:-}"; shift 2 ;;
    -t) ACCESS_TOKEN="${2:-}"; shift 2 ;;
    -T) TOKEN_FILE="${2:-}"; shift 2 ;;
    -C) CA_CERT_PATH="${2:-}"; shift 2 ;;   # hidden/internal
    -h) usage; exit 0 ;;
    --) shift; break ;;
    -*) echo "ERROR: Unknown option: $1" >&2; usage >&2; exit 1 ;;
    *)  break ;;
  esac
done

# -------------------- helpers --------------------
trim_ws() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

die() { echo "ERROR: $*" >&2; exit 1; }

validate_wget_opts() {
  [[ -n "${WGET_OPTS:-}" ]] || return 0
  local opt
  for opt in $WGET_OPTS; do
    case "$opt" in
      --timeout=*|--dns-timeout=*|--connect-timeout=*|--read-timeout=*|--tries=*|--waitretry=*|--limit-rate=*|--retry-connrefused|--inet4-only|--inet6-only)
        ;;
      *)
        die "Unsupported WGET_OPTS value: $opt. Allowed values are timeout, retry, rate-limit, and IPv4/IPv6 options only. TLS, config, debug, header, output-routing, and output-suppression options are not allowed."
        ;;
    esac
  done
}

read_token_from_file() {
  local f="$1"
  [[ -f "$f" ]] || die "Token file not found: $f"
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="$(trim_ws "$line")"
    [[ -z "$line" ]] && continue
    [[ "$line" == \#* ]] && continue
    printf '%s' "$line"
    return 0
  done < "$f"
  die "Token file has no usable token line: $f"
}

urlencode() {
  local s="$1" out="" i c
  LC_ALL=C
  for (( i=0; i<${#s}; i++ )); do
    c="${s:i:1}"
    case "$c" in
      [a-zA-Z0-9.~_-]) out+="$c" ;;
      *) printf -v out '%s%%%02X' "$out" "'$c" ;;
    esac
  done
  printf '%s' "$out"
}

append_query_param() {
  local url="$1" key="$2" val="$3"
  if [[ "$url" == *\?* ]]; then
    printf '%s&%s=%s' "$url" "$key" "$val"
  else
    printf '%s?%s=%s' "$url" "$key" "$val"
  fi
}

derive_name_from_url() {
  local url="$1"
  local base="${url##*/}"
  base="${base%%\?*}"
  [[ -n "$base" ]] || base="download.bin"
  printf '%s' "$base"
}

# Prefer patch_file query param if present
extract_patch_file() {
  local url="$1"
  [[ "$url" != *\?* ]] && { printf ''; return 0; }
  local q="${url#*\?}"
  local part key val
  local IFS='&'
  # shellcheck disable=SC2206
  set -- $q
  for part in "$@"; do
    key="${part%%=*}"
    val="${part#*=}"
    if [[ "$key" == "patch_file" && "$val" != "$part" ]]; then
      val="${val%%\#*}"
      printf '%s' "$val"
      return 0
    fi
  done
  printf ''
}

validate_outfile_plain() {
  local of="$1"
  [[ -n "$of" ]] || die "Derived output filename is empty. Provide -O or ensure patch_file is present."
  if [[ "$of" == /* || "$of" == *".."* || "$of" == *"/"* ]]; then
    die "Invalid output filename (must be a plain filename, no path): $of"
  fi
}

# -------------------- tool detection (Solaris-safe) --------------------
pick_first() { for c in "$@"; do command -v "$c" >/dev/null 2>&1 && { printf '%s' "$c"; return 0; }; done; return 1; }

# AWK: /usr/bin/awk can be old on Solaris; prefer xpg4 awk or nawk.
AWK_BIN="$(pick_first gawk /usr/xpg4/bin/awk nawk awk || true)"
[[ -n "${AWK_BIN:-}" ]] || die "No awk found on PATH."

# GREP: need ERE support. Solaris /usr/bin/grep may not support -E.
GREP_BIN="$(pick_first ggrep /usr/xpg4/bin/grep grep || true)"
EGREP_BIN="$(pick_first egrep /usr/xpg4/bin/egrep || true)"
[[ -n "${GREP_BIN:-}" || -n "${EGREP_BIN:-}" ]] || die "No grep/egrep found on PATH."

grep_ere_quiet() {
  # Usage: grep_ere_quiet 'pattern' file
  local pat="$1" file="$2"
  if [[ -n "${GREP_BIN:-}" ]] && "$GREP_BIN" -E 'a' </dev/null >/dev/null 2>&1; then
    "$GREP_BIN" -Eq "$pat" "$file" 2>/dev/null
    return $?
  fi
  if [[ -n "${EGREP_BIN:-}" ]]; then
    "$EGREP_BIN" "$pat" "$file" >/dev/null 2>&1
    return $?
  fi
  "$GREP_BIN" "$pat" "$file" >/dev/null 2>&1
}

normalize_sha256() {
  local value="$1"
  value="$(trim_ws "$value")"
  printf '%s' "$value" | tr '[:upper:]' '[:lower:]'
}

is_sha256_hex() {
  local value
  value="$(normalize_sha256 "$1")"
  [[ "$value" =~ ^[0-9a-f]{64}$ ]]
}

compute_sha256() {
  local file="$1"
  local result=""

  if command -v sha256sum >/dev/null 2>&1; then
    result="$(sha256sum "$file" | "$AWK_BIN" '{print $1}')"
  elif command -v shasum >/dev/null 2>&1; then
    result="$(shasum -a 256 "$file" | "$AWK_BIN" '{print $1}')"
  elif command -v openssl >/dev/null 2>&1; then
    result="$(openssl dgst -sha256 "$file" | "$AWK_BIN" '{print $NF}')"
  elif command -v digest >/dev/null 2>&1; then
    result="$(digest -a sha256 "$file" | "$AWK_BIN" '{print $NF}')"
  else
    return 127
  fi

  result="$(normalize_sha256 "$result")"
  is_sha256_hex "$result" || return 1
  printf '%s' "$result"
}

# Detect auth failure (401/403) from wget output
is_auth_failure_log() {
  local f="$1"
  grep_ere_quiet 'HTTP/[0-9.]+[[:space:]]+(401|403)\b' "$f"
}

# -------------------- wget capability check (Solaris-safe) --------------------
require_gnu_wget() {
  local help_out
  help_out="$(wget --help 2>&1 || true)"

  # BusyBox/minimal wget won't have these flags. Use AWK for Solaris portability.
  if ! printf '%s\n' "$help_out" | "$AWK_BIN" '
        /--server-response/ {f1=1}
        /--content-disposition/ {f2=1}
        /--progress=/ {f3=1}
        /--no-config/ {f4=1}
        END { exit !(f1 && f2 && f3 && f4) }
     '; then
    cat >&2 <<'EOF'
ERROR: GNU wget is required to run this script.

The wget found on this system does not support required options such as:
  --server-response
  --content-disposition
  --progress=dot:giga
  --no-config

This usually means you are using BusyBox wget or a minimal wget build.

Please install GNU wget and ensure it is first in PATH.
EOF
    exit 2
  fi
}
require_gnu_wget

# -------------------- output dir default --------------------
if [[ -z "${OUTDIR:-}" ]]; then
  echo "INFO: Output dir not specified, defaulting to current directory." >&2
  OUTDIR="."
fi

mkdir -p "$OUTDIR" || die "Cannot create output directory: $OUTDIR"
OUTDIR="${OUTDIR%/}"

# -------------------- token validation --------------------
if [[ -n "${ACCESS_TOKEN:-}" && -n "${TOKEN_FILE:-}" ]]; then
  die "Provide token using exactly one of -t <token> OR -T <token_file>, not both."
fi
if [[ -z "${ACCESS_TOKEN:-}" && -z "${TOKEN_FILE:-}" ]]; then
  die "Missing token. Provide -t <token> OR -T <token_file>."
fi
if [[ -n "${TOKEN_FILE:-}" ]]; then
  ACCESS_TOKEN="$(read_token_from_file "$TOKEN_FILE")"
fi
ACCESS_TOKEN="$(printf "%s" "${ACCESS_TOKEN:-}" | tr -d '\r\n')"
[[ -n "$ACCESS_TOKEN" ]] || die "Token is empty after processing."

# Validate internal cert path if provided
if [[ -n "${CA_CERT_PATH:-}" && ! -f "$CA_CERT_PATH" ]]; then
  die "CA certificate file not found: $CA_CERT_PATH"
fi
validate_wget_opts

# -------------------- Optional logging --------------------
if [[ -n "${LOG_FILE:-}" ]]; then
  mkdir -p "$(dirname "$LOG_FILE")" || die "Cannot create log directory"
  : > "$LOG_FILE" || die "Cannot write log file"
  exec >"$LOG_FILE" 2>&1
fi

# -------------------- Mode selection --------------------
if [[ -n "${DOWNLOAD_URL:-}" && -n "${BATCH_FILE:-}" ]]; then
  die "Choose exactly one mode: either -d (single) OR -f (batch), not both."
fi

# Auto-batch if neither -d nor -f
if [[ -z "${DOWNLOAD_URL:-}" && -z "${BATCH_FILE:-}" ]]; then
  AUTO_BATCH_FILE="$OUTDIR/updates_patch_list.txt"
  cat > "$AUTO_BATCH_FILE" <<'EOF'
#start of patch list
https://updates.oracle.com/Orion/Services/download/PEOPLETOOLS-WIN-8.61.24_1of4.zip?aru=29519651&patch_file=PEOPLETOOLS-WIN-8.61.24_1of4.zip,,700f9e028c75a86e9195bf05c3fc21fe2b3a67979966685ed7ddf429921ea32e
https://updates.oracle.com/Orion/Services/download/PEOPLETOOLS-WIN-8.61.24_3of4.zip?aru=29519651&patch_file=PEOPLETOOLS-WIN-8.61.24_3of4.zip,,eec2b9014b83ad2491c0c8ab32d40f9b7effb076718e218558715a409ea6bec3
https://updates.oracle.com/Orion/Services/download/PEOPLETOOLS-WIN-8.61.24_2of4.zip?aru=29519651&patch_file=PEOPLETOOLS-WIN-8.61.24_2of4.zip,,c21f2d6ba83155e622ef62595706f4223c619e4e0e22d15f6574ef970fa6456d
https://updates.oracle.com/Orion/Services/download/PEOPLETOOLS-WIN-8.61.24_4of4.zip?aru=29519651&patch_file=PEOPLETOOLS-WIN-8.61.24_4of4.zip,,1296afbcf13b4bd94798a23ee087cff68a577e1dab23572a719fd2342a44af9c
#END of patch list
EOF
  BATCH_FILE="$AUTO_BATCH_FILE"
fi

if [[ -n "${BATCH_FILE:-}" && ! -f "$BATCH_FILE" ]]; then
  die "Batch file not found: $BATCH_FILE"
fi

# Parallel default
if [[ -z "${PARALLEL:-}" ]]; then
  if [[ -n "${BATCH_FILE:-}" ]]; then PARALLEL=10; else PARALLEL=1; fi
fi
if ! [[ "$PARALLEL" =~ ^[0-9]+$ ]] || [[ "$PARALLEL" -lt 1 ]]; then
  die "Invalid -P parallel value: $PARALLEL"
fi

# -------------------- OAuth info (display only) --------------------
echo
echo "=== OAuth start URL (manual login/token generation) ==="
echo "$START_URL"

# -------------------- Build tasks --------------------
declare -a TASKS=()  # each: URL<TAB>OUTFILE
declare -A EXPECTED_SHA256=()
TASK_COUNT=0
is_batch=0

if [[ -n "${BATCH_FILE:-}" ]]; then
  is_batch=1
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="$(trim_ws "$line")"
    [[ -z "$line" ]] && continue
    [[ "$line" == \#* ]] && continue

    local_url=""
    local_pw=""
    local_sha256=""
    IFS=',' read -r local_url local_pw local_sha256 _rest <<< "$line"

    local_url="$(trim_ws "${local_url:-}")"
    local_pw="$(trim_ws "${local_pw:-}")"
    local_sha256="$(normalize_sha256 "${local_sha256:-}")"
    [[ -z "$local_url" ]] && continue

    local_of="$(extract_patch_file "$local_url")"
    [[ -n "$local_of" ]] || local_of="$(derive_name_from_url "$local_url")"
    validate_outfile_plain "$local_of"
    if [[ -z "$local_sha256" ]]; then
      die "Missing SHA-256 for batch entry: $local_of"
    fi
    if ! is_sha256_hex "$local_sha256"; then
      die "Invalid SHA-256 for batch entry: $local_of"
    fi

    if [[ -n "$local_pw" ]]; then
      local_url="$(append_query_param "$local_url" "patch_password" "$(urlencode "$local_pw")")"
    fi

    TASKS+=( "${local_url}"$'\t'"${local_of}" )
    TASK_COUNT=$((TASK_COUNT+1))
    EXPECTED_SHA256["$local_of"]="$local_sha256"
  done < "$BATCH_FILE"
else
  if [[ -z "${OUTFILE:-}" ]]; then
    OUTFILE="$(extract_patch_file "$DOWNLOAD_URL")"
    [[ -n "$OUTFILE" ]] || OUTFILE="$(derive_name_from_url "$DOWNLOAD_URL")"
  fi
  validate_outfile_plain "$OUTFILE"
  SINGLE_SHA256="$(normalize_sha256 "${SINGLE_SHA256:-}")"
  if [[ -z "$SINGLE_SHA256" ]]; then
    die "Missing SHA-256 for single download: $OUTFILE"
  fi
  if ! is_sha256_hex "$SINGLE_SHA256"; then
    die "Invalid SHA-256 for single download: $OUTFILE"
  fi
  if [[ -n "${PATCH_PASSWORD:-}" ]]; then
    DOWNLOAD_URL="$(append_query_param "$DOWNLOAD_URL" "patch_password" "$(urlencode "$PATCH_PASSWORD")")"
  fi
  TASKS+=( "${DOWNLOAD_URL}"$'\t'"${OUTFILE}" )
  TASK_COUNT=$((TASK_COUNT+1))
  EXPECTED_SHA256["$OUTFILE"]="$SINGLE_SHA256"
fi

[[ "$TASK_COUNT" -gt 0 ]] || die "No downloads to process."

# Duplicate outfile detection
declare -A SEEN_OUTFILES=()
for task in "${TASKS[@]}"; do
  of="${task#*$'\t'}"
  if [[ -n "${SEEN_OUTFILES[$of]:-}" ]]; then
    die "Duplicate output filename detected: $of"
  fi
  SEEN_OUTFILES["$of"]=1
done

# -------------------- auth + status dir --------------------
AUTH_FAIL_FLAG="$OUTDIR/.auth_failed.flag"
rm -f "$AUTH_FAIL_FLAG" 2>/dev/null || true

STATUS_DIR="$OUTDIR/.download_status"
mkdir -p "$STATUS_DIR" || die "Cannot create status dir"
rm -f "$STATUS_DIR"/*.ok "$STATUS_DIR"/*.fail "$STATUS_DIR"/*.skip \
      "$STATUS_DIR"/*.zipok "$STATUS_DIR"/*.zipfail "$STATUS_DIR"/*.zipskip 2>/dev/null || true

mark_ok(){ : > "$STATUS_DIR/$1.ok"; }
mark_fail(){ printf '%s\n' "$2" > "$STATUS_DIR/$1.fail"; }
mark_skip(){ printf '%s\n' "$2" > "$STATUS_DIR/$1.skip"; }

mark_zip_ok(){ : > "$STATUS_DIR/$1.zipok"; }
mark_zip_fail(){ printf '%s\n' "$2" > "$STATUS_DIR/$1.zipfail"; }
mark_zip_skip(){ printf '%s\n' "$2" > "$STATUS_DIR/$1.zipskip"; }

# -------------------- auth preflight --------------------
preflight_auth_check() {
  local url="$1"
  local tmp
  tmp="$(mktemp 2>/dev/null || true)"
  [[ -n "$tmp" ]] || tmp="$OUTDIR/.preflight.$$"
  : > "$tmp" || true

  local -a args=( --no-config --spider --server-response --header "Authorization: Bearer $ACCESS_TOKEN" )
  if [[ -n "${CA_CERT_PATH:-}" ]]; then
    args+=( --ca-certificate "$CA_CERT_PATH" )
  fi
  if [[ -n "${WGET_OPTS:-}" ]]; then
    local opt
    for opt in $WGET_OPTS; do
      args+=( "$opt" )
    done
  fi
  args+=( -- "$url" )

  wget "${args[@]}" 2>&1 | "$AWK_BIN" '
    {
      line = $0
      gsub(/patch_password=[^&[:space:]"]+/, "patch_password=REDACTED", line)
      gsub(/Authorization: Bearer [^[:space:]"]+/, "Authorization: Bearer REDACTED", line)
      print line
    }
  ' >"$tmp" || true
  if is_auth_failure_log "$tmp"; then
    rm -f "$tmp" 2>/dev/null || true
    die "Authorization failed during preflight (HTTP 401/403). Token invalid or expired."
  fi
  rm -f "$tmp" 2>/dev/null || true
}

preflight_task="${TASKS[0]}"
preflight_auth_check "${preflight_task%%$'\t'*}"

# -------------------- portable parallel limiter --------------------
# Keep PIDs in a scalar list so Bash 4.0-4.3 with set -u never expands an empty array.
PIDS=""

pid_count() {
  local pid count=0
  for pid in $PIDS; do
    count=$((count+1))
  done
  printf '%s' "$count"
}

reap_finished() {
  local pid live=""
  for pid in $PIDS; do
    if kill -0 "$pid" 2>/dev/null; then
      live="${live:+$live }$pid"
    fi
  done
  PIDS="$live"
}

wait_for_slot() {
  local limit="$1"
  while :; do
    reap_finished
    if [[ "$(pid_count)" -lt "$limit" ]]; then
      return 0
    fi
    sleep 1
  done
}

wait_for_all() {
  local pid failures=0
  for pid in $PIDS; do
    if wait "$pid"; then :; else failures=$((failures+1)); fi
  done
  PIDS=""
  return "$failures"
}

# -------------------- signal handling --------------------
# Ctrl+C should stop the parent script and all active background downloads.
terminate_tracked_jobs() {
  local sig="${1:-TERM}" pid
  reap_finished
  for pid in $PIDS; do
    kill "-$sig" "$pid" 2>/dev/null || true
  done
}

handle_signal() {
  local sig="$1" code="$2"
  trap - INT TERM HUP
  echo >&2
  echo "INFO: Received $sig; terminating active downloads..." >&2
  terminate_tracked_jobs TERM
  sleep 1
  terminate_tracked_jobs KILL
  wait_for_all || true
  exit "$code"
}

trap 'handle_signal INT 130' INT
trap 'handle_signal TERM 143' TERM
trap 'handle_signal HUP 129' HUP

# -------------------- wget args builder --------------------
build_wget_args() {
  local url="$1"
  local dest="$2"
  WGET_ARGS=( --no-config
              --header "Authorization: Bearer $ACCESS_TOKEN"
              --content-disposition
              --server-response
              -O "$dest"
              --progress=dot:giga )

  if [[ -n "${CA_CERT_PATH:-}" ]]; then
    WGET_ARGS+=( --ca-certificate "$CA_CERT_PATH" )
  fi

  if [[ -n "${WGET_OPTS:-}" ]]; then
    local opt
    for opt in $WGET_OPTS; do
      WGET_ARGS+=( "$opt" )
    done
  fi

  WGET_ARGS+=( -- "$url" )
}

# -------------------- downloads --------------------
echo
echo "=== Starting downloads ==="
echo "Mode          : $([[ $is_batch -eq 1 ]] && echo "batch (-f or auto)" || echo "single (-d)")"
echo "Output dir    : $OUTDIR"
echo "Parallel      : $PARALLEL"
echo "Count         : $TASK_COUNT"
echo "TLS verify    : enabled"
echo

download_one() {
  local url="$1"
  local of="$2"
  local dest="$OUTDIR/$of"
  local wget_pid=""
  local stream_pid=""
  local stream_pipe=""

  echo "-> $of"

  local tmp_log
  tmp_log="$(mktemp 2>/dev/null || true)"
  [[ -n "$tmp_log" ]] || tmp_log="$STATUS_DIR/.wget.$of.$$"
  : > "$tmp_log" || true
  stream_pipe="$STATUS_DIR/.wget.pipe.$$.$RANDOM"
  mkfifo "$stream_pipe" 2>/dev/null || die "Cannot create stream pipe: $stream_pipe"

  build_wget_args "$url" "$dest"

  cleanup_wget() {
    [[ -n "${wget_pid:-}" ]] || return 0
    kill -TERM "$wget_pid" 2>/dev/null || true
    sleep 1
    kill -KILL "$wget_pid" 2>/dev/null || true
    wait "$wget_pid" 2>/dev/null || true
  }
  cleanup_stream() {
    [[ -n "${stream_pid:-}" ]] || return 0
    kill -TERM "$stream_pid" 2>/dev/null || true
    sleep 1
    kill -KILL "$stream_pid" 2>/dev/null || true
    wait "$stream_pid" 2>/dev/null || true
  }
  cleanup_files() {
    rm -f "$stream_pipe" "$tmp_log" 2>/dev/null || true
  }
  trap 'cleanup_wget; cleanup_stream; cleanup_files; exit 130' INT TERM HUP

  # Stream output live with prefix while redacting secrets from captured wget logs.
  "$AWK_BIN" -v f="$of" -v logfile="$tmp_log" '
    {
      line = $0
      gsub(/patch_password=[^&[:space:]"]+/, "patch_password=REDACTED", line)
      gsub(/Authorization: Bearer [^[:space:]"]+/, "Authorization: Bearer REDACTED", line)
      print "[" f "] " line
      print line >> logfile
    }
  ' < "$stream_pipe" &
  stream_pid="$!"

  # Keep wget as a directly tracked child PID for portable signal cleanup.
  wget "${WGET_ARGS[@]}" >"$stream_pipe" 2>&1 &
  wget_pid="$!"

  if wait "$wget_pid"; then
    wait "$stream_pid" 2>/dev/null || true
    trap - INT TERM HUP
    mark_ok "$of"
    mark_zip_skip "$of" "ZIP_VALIDATION_PENDING"
    rm -f "$stream_pipe" "$tmp_log" 2>/dev/null || true
    return 0
  fi

  wait "$stream_pid" 2>/dev/null || true
  trap - INT TERM HUP
  rm -f "$stream_pipe" 2>/dev/null || true
  if is_auth_failure_log "$tmp_log"; then
    touch "$AUTH_FAIL_FLAG" 2>/dev/null || true
    mark_fail "$of" "AUTH_FAILED (HTTP 401/403)"
    echo "[$of] AUTH failure detected (401/403). Will stop scheduling new downloads." >&2
  else
    mark_fail "$of" "DOWNLOAD_FAILED (wget non-zero)"
  fi

  mark_zip_skip "$of" "ZIP_VALIDATION_SKIPPED (download failed)"
  rm -f "$tmp_log" 2>/dev/null || true
  return 1
}

seq=0
for task in "${TASKS[@]}"; do
  url="${task%%$'\t'*}"
  of="${task#*$'\t'}"

  if [[ -f "$AUTH_FAIL_FLAG" ]]; then
    mark_skip "$of" "SKIPPED (auth failed earlier; token likely expired)"
    mark_zip_skip "$of" "ZIP_VALIDATION_SKIPPED (not started)"
    continue
  fi

  wait_for_slot "$PARALLEL"
  seq=$((seq+1))
  echo "$seq starting download for $of"
  download_one "$url" "$of" &
  PIDS="${PIDS:+$PIDS }$!"
done

wait_for_all || true

# -------------------- SHA-256 validation pass (post-download) --------------------
echo
echo "=== SHA-256 validation pass (post-download) ==="

for task in "${TASKS[@]}"; do
  of="${task#*$'\t'}"
  expected_sha256="${EXPECTED_SHA256[$of]:-}"
  [[ -f "$STATUS_DIR/$of.ok" ]] || continue

  echo "[SHA256-VALIDATE] Preparing to validate: $of"

  if [[ -z "${expected_sha256:-}" ]]; then
    rm -f "$STATUS_DIR/$of.ok" 2>/dev/null || true
    mark_fail "$of" "SHA256_NOT_PROVIDED"
    mark_zip_skip "$of" "ZIP_VALIDATION_SKIPPED (sha256 missing)"
    echo "[SHA256-VALIDATE] FAILED: $of (SHA-256 not provided)"
    continue
  fi

  actual_sha256=""
  if ! actual_sha256="$(compute_sha256 "$OUTDIR/$of")"; then
    rm -f "$STATUS_DIR/$of.ok" 2>/dev/null || true
    mark_fail "$of" "SHA256_UNAVAILABLE_OR_FAILED"
    mark_zip_skip "$of" "ZIP_VALIDATION_SKIPPED (sha256 failed)"
    echo "[SHA256-VALIDATE] FAILED: $of (unable to compute SHA-256)"
    continue
  fi

  if [[ "$actual_sha256" == "$expected_sha256" ]]; then
    echo "[SHA256-VALIDATE] OK: $of"
  else
    rm -f "$STATUS_DIR/$of.ok" 2>/dev/null || true
    echo "[SHA256-VALIDATE] FAILED: $of"
    mark_fail "$of" "SHA256_MISMATCH"
    mark_zip_skip "$of" "ZIP_VALIDATION_SKIPPED (sha256 failed)"
  fi
done

# -------------------- ZIP validation pass (post-download) --------------------
echo
echo "=== ZIP validation pass (post-download) ==="

ZIP_AVAILABLE=0
command -v zip >/dev/null 2>&1 && ZIP_AVAILABLE=1

for task in "${TASKS[@]}"; do
  of="${task#*$'\t'}"
  [[ -f "$STATUS_DIR/$of.ok" ]] || continue

  rm -f "$STATUS_DIR/$of.zipskip" 2>/dev/null || true

  echo "[ZIP-VALIDATE] Preparing to validate: $of"

  if [[ "$of" != *.zip ]]; then
    mark_zip_skip "$of" "NOT_A_ZIP"
    continue
  fi

  if [[ $ZIP_AVAILABLE -ne 1 ]]; then
    mark_zip_skip "$of" "ZIP_NOT_AVAILABLE"
    continue
  fi

  if zip -T "$OUTDIR/$of" >/dev/null 2>&1; then
    echo "[ZIP-VALIDATE] OK: $of"
    mark_zip_ok "$of"
  else
    echo "[ZIP-VALIDATE] FAILED: $of"
    mark_zip_fail "$of" "ZIP_CORRUPT (zip -T failed)"
  fi
done

# -------------------- Summary report --------------------
echo
echo "=== Summary ==="

succeeded=0; failed=0; skipped=0
zip_ok=0; zip_fail=0; zip_skip=0

echo
echo "Succeeded downloads:"
for task in "${TASKS[@]}"; do
  of="${task#*$'\t'}"
  if [[ -f "$STATUS_DIR/$of.ok" ]]; then
    echo "  ✅ $of"
    succeeded=$((succeeded+1))
  fi
done
[[ "$succeeded" -gt 0 ]] || echo "  (none)"

echo
echo "Failed downloads:"
for task in "${TASKS[@]}"; do
  of="${task#*$'\t'}"
  if [[ -f "$STATUS_DIR/$of.fail" ]]; then
    echo "  ❌ $of — $(cat "$STATUS_DIR/$of.fail" 2>/dev/null || true)"
    failed=$((failed+1))
  fi
done
[[ "$failed" -gt 0 ]] || echo "  (none)"

echo
echo "Skipped / Not started downloads:"
for task in "${TASKS[@]}"; do
  of="${task#*$'\t'}"
  if [[ -f "$STATUS_DIR/$of.skip" ]]; then
    echo "  ⏭️  $of — $(cat "$STATUS_DIR/$of.skip" 2>/dev/null || true)"
    skipped=$((skipped+1))
  fi
done
[[ "$skipped" -gt 0 ]] || echo "  (none)"

echo
echo "ZIP validation:"
echo "  OK:"
for task in "${TASKS[@]}"; do
  of="${task#*$'\t'}"
  if [[ -f "$STATUS_DIR/$of.zipok" ]]; then
    echo "    ✅ $of"
    zip_ok=$((zip_ok+1))
  fi
done
[[ "$zip_ok" -gt 0 ]] || echo "    (none)"

echo "  FAILED:"
for task in "${TASKS[@]}"; do
  of="${task#*$'\t'}"
  if [[ -f "$STATUS_DIR/$of.zipfail" ]]; then
    echo "    ❌ $of — $(cat "$STATUS_DIR/$of.zipfail" 2>/dev/null || true)"
    zip_fail=$((zip_fail+1))
  fi
done
[[ "$zip_fail" -gt 0 ]] || echo "    (none)"

echo "  SKIPPED:"
for task in "${TASKS[@]}"; do
  of="${task#*$'\t'}"
  if [[ -f "$STATUS_DIR/$of.zipskip" ]]; then
    echo "    ⏭️  $of — $(cat "$STATUS_DIR/$of.zipskip" 2>/dev/null || true)"
    zip_skip=$((zip_skip+1))
  fi
done
[[ "$zip_skip" -gt 0 ]] || echo "    (none)"

echo
echo "Totals:"
echo "  downloads: succeeded=$succeeded, failed=$failed, skipped=$skipped, total=$TASK_COUNT"
echo "  zip-test : ok=$zip_ok, failed=$zip_fail, skipped=$zip_skip"

# Exit codes:
#  0  all downloads succeeded AND SHA-256/zip validation did not fail
# 12  auth expired mid-run (caused skips/fails)
# 10  any non-auth failure (including SHA-256 or zip failures)
if [[ -f "$AUTH_FAIL_FLAG" ]]; then
  echo "Result: AUTHORIZATION FAILURE occurred during run (token expired/invalid)."
  exit 12
fi
if [[ "$failed" -gt 0 || "$zip_fail" -gt 0 ]]; then
  echo "Result: Completed with failures."
  exit 10
fi

echo "Result: All downloads completed successfully."
exit 0
