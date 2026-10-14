#!/usr/bin/env bash
# Add-on entrypoint: reads user config from /data/options.json and the MQTT broker
# credentials from the Supervisor (mqtt:need service), then starts the bridge.
set -e

OPTS=/data/options.json

# --- standalone (no Supervisor): synthesize /data/options.json from the environment ---
# Lets the same image run as a plain Docker container (HA Container/Core users): pass
# EBO_EMAIL/EBO_PASSWORD/EBO_PAYLOAD_KEY/EBO_SIGN_KEY (+ optional EBO_EXPOSE_MQTT) as env and
# skip the options file. When Supervisor provides the file, this block is a no-op.
if [ ! -f "$OPTS" ]; then
  mkdir -p "$(dirname "$OPTS")"
  case "${EBO_EXPOSE_MQTT:-false}" in 1|true|on|yes) _MQ=true;; *) _MQ=false;; esac
  jq -n --arg e "${EBO_EMAIL:-}" --arg p "${EBO_PASSWORD:-}" \
        --arg pk "${EBO_PAYLOAD_KEY:-}" --arg sk "${EBO_SIGN_KEY:-}" --argjson mq "$_MQ" \
        '{email:$e,password:$p,payload_key:$pk,sign_key:$sk,expose_mqtt:$mq}' > "$OPTS"
  echo "[standalone] synthesized $OPTS from the environment"
fi

# --- account login + the app crypto keys (supplied by you, NOT shipped in the public code) ---
export EBO_EMAIL="$(jq -r '.email // empty' "$OPTS")"
export EBO_PASSWORD="$(jq -r '.password // empty' "$OPTS")"
export EBO_PAYLOAD_KEY="$(jq -r '.payload_key // empty' "$OPTS")"
export EBO_SIGN_KEY="$(jq -r '.sign_key // empty' "$OPTS")"

# --- account/connection + audio/video processing options all live in the add-on Configuration tab
# (/data/options.json). The old in-panel Settings (/data/panel.json) is kept only as a one-time
# migration fallback for users who had set these there. Precedence: options.json -> panel.json ->
# built-in default. NB: use `has($k)` (not `// empty`) so a `false` boolean isn't dropped. ---
PANEL_CFG=/data/panel.json
pget() {  # pget <key> <default>
  local v=""
  v="$(jq -r --arg k "$1" 'if has($k) then .[$k] else empty end' "$OPTS" 2>/dev/null || true)"
  [ -z "$v" ] && [ -f "$PANEL_CFG" ] && v="$(jq -r --arg k "$1" 'if has($k) then .[$k] else empty end' "$PANEL_CFG" 2>/dev/null || true)"
  [ -z "$v" ] && v="$2"
  printf '%s' "$v"
}
pbool() { [ "$(pget "$1" "$2")" = "true" ] && echo 1 || echo 0; }

export EBO_REGION="$(pget region GB)"
export EBO_HOST="$(pget host ebox-eu.enabotserverintl.com)"
export EBO_VIDEO="$(pbool video true)"
export EBO_AUDIO="$(pbool audio true)"
export EBO_TALK="$(pbool talk false)"
export EBO_AUDIO_PT="$(pget audio_codec 8)"
# auto-standby: option is in MINUTES (0 = never), the bridge wants SECONDS
_STANDBY_MIN="$(pget standby_after_minutes 5)"
export EBO_STANDBY_TIMEOUT="$(( _STANDBY_MIN * 60 ))"
# log level: the Configuration-tab option wins; fall back to the panel setting, then 'info'.
EBO_LOG_LEVEL="$(jq -r '.log_level // empty' "$OPTS" 2>/dev/null)"
[ -z "$EBO_LOG_LEVEL" ] && EBO_LOG_LEVEL="$(pget log_level info)"
export EBO_LOG_LEVEL
export EBO_VIDEO_MAX_HEIGHT="$(pget video_max_height 720)"
export EBO_VIDEO_FPS="$(pget video_fps 20)"
export EBO_VIDEO_BITRATE="$(pget video_bitrate 2500)"
export EBO_VIDEO_PRESET="$(pget video_preset ultrafast)"
export EBO_VIDEO_WALLCLOCK="$(pbool video_wallclock false)"
# Native-only: never publish HA entity discovery. The internal MQTT bus (localhost) is just glue
# between the bridges and the panel; Home Assistant gets everything from the companion integration.
export EBO_EXPOSE_MQTT=0
ROBOT_ID="$(pget robot_id 0)"
[ "$ROBOT_ID" != "0" ] && export EBO_ROBOT_ID="$ROBOT_ID"

# API token for the native integration to read the panel's data API (persisted in /data)
# Prefer a token pinned via env (standalone) or the add-on option; else reuse the persisted one;
# else generate. When on Supervisor and the option is empty, persist it back to the add-on options
# so the companion integration can read it (via the Supervisor) and reach the data API — no MQTT.
OPT_TOKEN="$(jq -r '.api_token // empty' "$OPTS" 2>/dev/null)"
if [ -n "${EBO_API_TOKEN:-}" ]; then
  echo "$EBO_API_TOKEN" > /data/api_token 2>/dev/null || true
elif [ -n "$OPT_TOKEN" ]; then
  echo "$OPT_TOKEN" > /data/api_token 2>/dev/null || true
elif [ ! -f /data/api_token ]; then
  head -c 24 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' > /data/api_token 2>/dev/null || true
fi
export EBO_API_TOKEN="$(cat /data/api_token 2>/dev/null || echo "${EBO_API_TOKEN:-}")"
if [ -z "$OPT_TOKEN" ] && [ -n "${SUPERVISOR_TOKEN:-}" ] && [ -n "$EBO_API_TOKEN" ]; then
  # Merge: the Supervisor REPLACES the whole options block, so we must send every existing option
  # back — otherwise persisting the token would silently wipe the user's other settings (video,
  # audio, mcp, log_level…). Start from the current options file and only add/override api_token.
  MERGED="$(jq --arg t "$EBO_API_TOKEN" '{options: (. + {api_token: $t})}' "$OPTS" 2>/dev/null)"
  # Fallback if the options file can't be read: at least keep the login fields.
  [ -z "$MERGED" ] && MERGED="$(jq -n --arg e "$EBO_EMAIL" --arg p "$EBO_PASSWORD" \
                  --arg pk "$EBO_PAYLOAD_KEY" --arg sk "$EBO_SIGN_KEY" --arg t "$EBO_API_TOKEN" \
                  '{options:{email:$e,password:$p,payload_key:$pk,sign_key:$sk,api_token:$t}}')"
  curl -sf -X POST -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" -H "Content-Type: application/json" \
    -d "$MERGED" http://supervisor/addons/self/options >/dev/null 2>&1 \
    && echo "[add-on] persisted api_token to options (readable by the integration)" || true
fi
export EBO_API_PORT="${EBO_API_PORT:-8098}"
# Home Assistant core reaches the add-on's API over the internal Supervisor network by hostname
# (works regardless of LAN/VLAN firewalls, unlike the host IP). Fall back to the container IP.
export EBO_API_HOST="$(hostname 2>/dev/null || hostname -i 2>/dev/null || echo '')"

# seed the panel store once from the resolved (migrated) values, so the panel has a file to edit
if [ ! -f "$PANEL_CFG" ]; then
  printf '{"region":"%s","host":"%s","robot_id":%s,"expose_mqtt":true,"video":%s,"audio":%s,"talk":%s,"audio_codec":%s,"log_level":"%s","video_max_height":%s,"video_fps":%s,"video_bitrate":%s,"video_preset":"%s"}\n' \
    "$EBO_REGION" "$EBO_HOST" "$ROBOT_ID" \
    "$([ "$EBO_VIDEO" = 1 ] && echo true || echo false)" \
    "$([ "$EBO_AUDIO" = 1 ] && echo true || echo false)" \
    "$([ "$EBO_TALK" = 1 ] && echo true || echo false)" \
    "$EBO_AUDIO_PT" "$EBO_LOG_LEVEL" "$EBO_VIDEO_MAX_HEIGHT" "$EBO_VIDEO_FPS" "$EBO_VIDEO_BITRATE" \
    "$EBO_VIDEO_PRESET" > "$PANEL_CFG" 2>/dev/null || true
fi

if [ -z "$EBO_EMAIL" ] || [ -z "$EBO_PASSWORD" ]; then
  echo "[add-on] ERROR: set email and password in the add-on configuration."
  exit 1
fi
if [ -z "$EBO_PAYLOAD_KEY" ] || [ -z "$EBO_SIGN_KEY" ]; then
  echo "[add-on] ERROR: the app crypto keys are not shipped with this add-on. Set 'payload_key'"
  echo "[add-on] and 'sign_key' in the configuration (the values for the EBO HOME app)."
  exit 1
fi

# --- internal MQTT bus: a mosquitto bound to localhost, private to this container ---
# It's just the glue between the bridge processes and the panel. Home Assistant never sees it
# (no external broker, no MQTT integration needed). Overridable by env for standalone/testing.
if [ -z "${EBO_MQTT_HOST:-}" ] && command -v mosquitto >/dev/null 2>&1; then
  printf 'listener 1883 127.0.0.1\nallow_anonymous true\npersistence false\n' > /data/mosquitto.conf
  mosquitto -c /data/mosquitto.conf -d 2>/dev/null || mosquitto -d 2>/dev/null || true
  for _ in $(seq 1 20); do
    (exec 3<>/dev/tcp/127.0.0.1/1883) 2>/dev/null && { exec 3>&- 3<&-; break; }
    sleep 0.2
  done
  echo "[add-on] internal MQTT bus on 127.0.0.1:1883 (private to the add-on)"
fi
: "${EBO_MQTT_HOST:=127.0.0.1}"
: "${EBO_MQTT_PORT:=1883}"
export EBO_MQTT_HOST EBO_MQTT_PORT

# Home Assistant host IP for the RTSP camera URL: use the manual option if set, else ask
# the Supervisor for the primary interface address.
EBO_HOST_IP="$(pget host_ip "")"
if [ -z "$EBO_HOST_IP" ] && [ -n "$SUPERVISOR_TOKEN" ]; then
  NET_JSON="$(curl -sf -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" http://supervisor/network/info 2>/dev/null || true)"
  EBO_HOST_IP="$(echo "$NET_JSON" | jq -r 'first((.data.interfaces[]? | select(.primary==true) | .ipv4.address[0]) // empty) // (.data.interfaces[]? | select(.enabled==true) | .ipv4.address[0])' 2>/dev/null | sed 's#/.*##' | head -1)"
fi
export EBO_HOST_IP
if [ -n "$EBO_HOST_IP" ]; then
  echo "[add-on] host IP for camera URL: ${EBO_HOST_IP}"
else
  echo "[add-on] could not detect host IP — set 'host_ip' in the add-on config for the camera URL"
fi

# ALL the host's LAN IPv4s (comma-separated): mediamtx advertises these as WebRTC ICE candidates so
# the browser (which may reach HA on a different NIC/VLAN than the robot's) can connect to the
# panel's fluid WebRTC drive video. We don't know in advance which IP the browser uses, so we offer
# them all and ICE picks the reachable one.
EBO_HOST_IPS="$(pget host_ip "")"
if [ -n "${NET_JSON:-}" ]; then
  EBO_HOST_IPS="$(echo "$NET_JSON" | jq -r '[.data.interfaces[]? | select(.enabled==true) | .ipv4.address[]?] | join(",")' 2>/dev/null | sed 's#/[0-9]*##g')"
fi
[ -z "$EBO_HOST_IPS" ] && EBO_HOST_IPS="$EBO_HOST_IP"
export EBO_HOST_IPS
echo "[add-on] host IPs advertised for WebRTC ICE: ${EBO_HOST_IPS:-<none>}"

# Log the version actually running (baked into the image) vs what the Supervisor thinks is
# installed. If they differ, the image wasn't rebuilt on update (stale) — that's the real bug.
CODE_VER="$(cat /app/VERSION.txt 2>/dev/null || echo '?')"
INST_VER="?"
if [ -n "$SUPERVISOR_TOKEN" ]; then
  INST_VER="$(curl -sf -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" http://supervisor/addons/self/info 2>/dev/null | jq -r '.data.version // "?"')"
fi
if [ "$CODE_VER" = "$INST_VER" ]; then
  echo "[add-on] version ${CODE_VER} (running code matches installed)"
else
  echo "[add-on] ⚠ version MISMATCH: running code=${CODE_VER}, Supervisor installed=${INST_VER} — the image was NOT rebuilt (stale). Try: uninstall + reinstall the add-on."
fi

# --- install the companion integration into Home Assistant (no HACS) ---
# The image bundles it; copy it into <ha-config>/custom_components/ebo so HA can load it. A new
# custom component needs ONE Home Assistant restart to be picked up (logged for the user).
# The config dir is mounted at /homeassistant (homeassistant_config map) or /config on older setups.
HA_ROOT=""
for d in /homeassistant /config; do [ -d "$d" ] && { HA_ROOT="$d"; break; }; done
HA_CC="$HA_ROOT/custom_components"
# Crash diagnostics: the bridge dumps a Python stack on a native SIGSEGV, but the add-on's private
# /data isn't reachable from the SSH/terminal add-on. Write those dumps into the HA config dir
# instead (this mount IS reachable), and surface any dump left in /data by a previous build.
if [ -n "$HA_ROOT" ]; then
  export EBO_FAULT_DIR="$HA_ROOT"
  [ -f /data/ebo_faults.log ] && cp -f /data/ebo_faults.log "$HA_ROOT/ebo_faults_prev.log" 2>/dev/null || true
fi
if [ -d /app/ha_integration/custom_components/ebo ] && [ -n "$HA_ROOT" ]; then
  mkdir -p "$HA_CC"
  if cp -r /app/ha_integration/custom_components/ebo "$HA_CC/ebo.tmp" 2>/dev/null; then
    rm -rf "$HA_CC/ebo" && mv "$HA_CC/ebo.tmp" "$HA_CC/ebo"
    echo "[add-on] installed the EBO integration into ${HA_CC}/ebo"
    echo "[add-on] → FIRST TIME: restart Home Assistant once, then Settings → Devices & Services"
    echo "[add-on]   → + Add Integration → 'EBO' (it finds your robots automatically, nothing to type)."
  fi
fi

echo "[add-on] starting Enabot integration bridge (region ${EBO_REGION})"

# --- which robot(s) to run: a specific robot_id, or discover every robot on the account ---
RIDS=(); RNAMES=()
if [ -n "$EBO_ROBOT_ID" ]; then
  RIDS=("$EBO_ROBOT_ID"); RNAMES=("EBO Air 2")
else
  DISC="$(EBO_DISCOVER=1 python /app/ebo_bridge.py 2>/dev/null)"
  while IFS=$'\t' read -r tag id name; do
    [ "$tag" = "ROBOT" ] && [ -n "$id" ] && { RIDS+=("$id"); RNAMES+=("$name"); }
  done <<< "$DISC"
fi
NR=${#RIDS[@]}
if [ "$NR" -eq 0 ]; then
  # discovery failed (network/creds): fall back to a single default bridge (picks 1st robot)
  RIDS=(""); RNAMES=("EBO Air 2"); NR=1
fi
[ "$NR" -gt 1 ] && echo "[add-on] ${NR} robots on the account — running one bridge each"

# --- EBO SE robots (LAN / TUTK) ---
# The EBO SE is a LOCAL robot (not on the Enabot cloud for streaming); its native bridge + TUTK/bionic
# runtime live in se_dir (default /share/ebo) and are NOT shipped in the image (see docs/EBO-SE.md).
# Two ways to get each robot's TUTK credentials:
#   (a) se_cloud: true  — log into the ROLA cloud with your email+password and DISCOVER them (plus a
#       fresh stream-start token), so you hand-extract nothing per robot. Needs the app keys below.
#   (b) se_robots: [...] — list each robot's credentials manually.
export EBO_SE_DIR="$(pget se_dir /share/ebo)"

# Auto-provision the native stack: the proprietary TUTK libs can't ship in the image, so you drop the
# bundle (ebo-se-share.tgz: ebo_bridge + lib/ + bionic/) into /share once. On boot we unpack it into
# se_dir and set perms — no SSH/tar/chmod needed. Re-extracts only when the bridge is missing or the
# tarball is newer, so it's cheap on every start.
for _TGZ in "$EBO_SE_DIR/ebo-se-share.tgz" /share/ebo-se-share.tgz /share/ebo.tgz; do
  [ -f "$_TGZ" ] || continue
  if [ ! -x "$EBO_SE_DIR/ebo_bridge" ] || [ "$_TGZ" -nt "$EBO_SE_DIR/ebo_bridge" ]; then
    mkdir -p "$EBO_SE_DIR"
    echo "[add-on] EBO SE: unpacking native stack from ${_TGZ} -> ${EBO_SE_DIR}"
    if tar xzf "$_TGZ" -C "$EBO_SE_DIR" 2>/dev/null; then
      chmod 755 "$EBO_SE_DIR/ebo_bridge" "$EBO_SE_DIR/bionic/linker64" "$EBO_SE_DIR/bionic/linker" 2>/dev/null || true
      # tar restores the archived files' (older) mtimes, so bump the bridge's mtime past the tarball's
      # — otherwise it looks stale every boot and we'd re-extract needlessly. A newer dropped tarball
      # still wins (its mtime > this touch), so replacing the bundle re-provisions as expected.
      touch "$EBO_SE_DIR/ebo_bridge" 2>/dev/null || true
    else
      echo "[add-on] EBO SE: failed to unpack ${_TGZ}"
    fi
  fi
  break
done
# app keys + constants you supply (same posture as the cloud models; may equal your EBO HOME keys).
export EBO_SE_BODY_KEY="$(jq -r '.se_body_key // empty' "$OPTS" 2>/dev/null)"
export EBO_SE_SIGN_KEY="$(jq -r '.se_sign_key // empty' "$OPTS" 2>/dev/null)"
export EBO_SE_UAT_KEY="$(jq -r '.se_uat_key // empty' "$OPTS" 2>/dev/null)"
export EBO_SE_LICENSE_CONST="$(jq -r '.se_license // empty' "$OPTS" 2>/dev/null)"
export EBO_SE_AUTHKEY_CONST="$(jq -r '.se_authkey // empty' "$OPTS" 2>/dev/null)"
export EBO_SE_EMAIL="$(jq -r '.se_email // empty' "$OPTS" 2>/dev/null)"
export EBO_SE_PASSWORD="$(jq -r '.se_password // empty' "$OPTS" 2>/dev/null)"
export EBO_SE_CLOUD_HOST="$(pget se_cloud_host ebo.enabotserverintl.com)"
export EBO_SE_LOGIN_REGION="$(pget se_login_region GB)"

# manual list count
NSE_MANUAL="$(jq -r '(.se_robots // []) | length' "$OPTS" 2>/dev/null || echo 0)"
case "$NSE_MANUAL" in ''|*[!0-9]*) NSE_MANUAL=0;; esac

# cloud discovery (optional): emit one TSV line per SE into a temp file for the launch loop below.
SE_DISCO=/data/se_discovered.tsv
: > "$SE_DISCO"
NSE_CLOUD=0
if [ "$(pget se_cloud false)" = "true" ] && [ -n "$EBO_SE_EMAIL" ] && [ -n "$EBO_SE_PASSWORD" ]; then
  echo "[add-on] EBO SE: discovering robots via the ROLA cloud…"
  if python /app/ebo_se_cloud.py --emit > "$SE_DISCO" 2>/data/se_discover.log; then
    NSE_CLOUD="$(grep -c . "$SE_DISCO" 2>/dev/null || echo 0)"
    echo "[add-on] EBO SE: discovered ${NSE_CLOUD} robot(s) from the cloud"
  else
    echo "[add-on] EBO SE: cloud discovery failed (see /data/se_discover.log) — check email/password + app keys"
    : > "$SE_DISCO"
  fi
fi

NSE=$(( NSE_MANUAL + NSE_CLOUD ))
# total robot count decides whether EVERY robot needs a distinct node/RTSP port (even one cloud
# robot must move off the default path once an SE shares the broker).
TOTAL=$(( NR + NSE ))
[ "$NSE" -gt 0 ] && echo "[add-on] ${NSE} EBO SE robot(s) total (${EBO_SE_DIR})"

stopping=0
term() {
  stopping=1
  echo "[add-on] stopping…"
  pkill -TERM -f '/app/panel.py' 2>/dev/null || true
  pkill -TERM -f '/app/ebo_mcp.py' 2>/dev/null || true
  pkill -TERM -f '/app/ebo_bridge.py' 2>/dev/null || true
  pkill -TERM -f '/app/ebo_se_bridge.py' 2>/dev/null || true
  for _ in $(seq 1 16); do
    pgrep -f '/app/ebo_bridge.py' >/dev/null 2>&1 || pgrep -f '/app/ebo_se_bridge.py' >/dev/null 2>&1 || break
    sleep 0.5
  done
  exit 0
}
trap term SIGTERM SIGINT

# Supervise ONE robot: restart on exit; after repeated quick crashes with A/V on, fall back
# to control-only for that robot (control and video share one Agora connection).
run_robot() {
  local id="$1" idx="$2" name="$3" crashes=0 v="$EBO_VIDEO" a="$EBO_AUDIO"
  while [ "$stopping" -eq 0 ]; do
    local start; start=$(date +%s)
    (
      export EBO_VIDEO="$v" EBO_AUDIO="$a"
      [ -n "$id" ] && export EBO_ROBOT_ID="$id"
      export EBO_DEVICE_NAME="$name"    # always use the robot's real (account) name
      if [ "$TOTAL" -gt 1 ]; then       # distinct node/port/path once more than one robot shares the bus
        export EBO_NODE="ebo_${id}" EBO_RTSP_PATH="ebo_${id}" \
               EBO_RTSP_PORT="$((8554 + idx))"
      fi
      exec python /app/ebo_bridge.py
    ) &
    # NOTE: the script runs with `set -e`. A crashing bridge makes `wait` return non-zero, which
    # would kill THIS supervising subshell — leaving the robot permanently offline while the panel
    # kept running (the container stays "started", so nothing looks broken). `|| rc=$?` keeps the
    # supervisor alive so the bridge is actually restarted, which is the whole point of this loop.
    local rc=0
    wait $! || rc=$?
    [ "$stopping" -eq 1 ] && break
    local ran=$(( $(date +%s) - start ))
    if [ "$ran" -lt 60 ] && { [ "$rc" -ge 128 ] || [ "$rc" -ne 0 ]; }; then
      crashes=$(( crashes + 1 ))
    else
      crashes=0
    fi
    if [ "$crashes" -ge 2 ] && { [ "$v" != "0" ] || [ "$a" = "1" ]; }; then
      echo "[add-on] robot ${id:-single} crashed ${crashes}× with A/V — control only."
      v=0; a=0; crashes=0
    fi
    if [ "$rc" -ge 128 ]; then
      echo "[add-on] bridge (${id:-single}) exited (rc=${rc} = signal $((rc-128)) — likely a native SDK crash; see /data/ebo_faults.log for the Python stack), restarting in 15s…"
    else
      echo "[add-on] bridge (${id:-single}) exited (rc=${rc}), restarting in 15s…"
    fi
    sleep 15 & wait $! || true
  done
}

# Supervise ONE EBO SE robot (native TUTK bridge via ebo_se_bridge.py). The caller has already
# exported this robot's EBO_SE_* credentials; here we just pin its node/RTSP path+port and restart it
# on exit. `n` is a 1-based ordinal (node name), `gidx` the global robot index: RTSP 8554+gidx →
# WebRTC 8189+gidx. Only the first 4 global robots get a host-mapped WebRTC port; a 5th+ still has
# panel preview via HLS/snapshot, just not the fluid drive view.
run_se_robot() {
  local n="$1" gidx="$2"
  export EBO_SE_NODE="ebo_se_${n}" EBO_SE_RTSP_PATH="ebo_se_${n}" EBO_RTSP_PORT="$((8554 + gidx))"
  # license + authkey are app constants (same for every SE); the per-robot vars are set by the caller.
  export EBO_SE_LICENSE="${EBO_SE_LICENSE:-$EBO_SE_LICENSE_CONST}"
  export EBO_SE_AUTHKEY="${EBO_SE_AUTHKEY:-$EBO_SE_AUTHKEY_CONST}"
  while [ "$stopping" -eq 0 ]; do
    ( exec python /app/ebo_se_bridge.py ) &
    local rc=0
    wait $! || rc=$?
    [ "$stopping" -eq 1 ] && break
    echo "[add-on] EBO SE (${EBO_SE_NODE}) exited (rc=${rc}), restarting in 15s…"
    sleep 15 & wait $! || true
  done
}

# Feeder: a MANUAL se_robots[sidx] entry -> export its creds, then supervise.
run_se_manual() {
  local sidx="$1" gidx="$2" n=$(( sidx + 1 )) base=".se_robots[$sidx]"
  EBO_SE_NAME="$(jq -r "${base}.name // \"EBO SE ${n}\"" "$OPTS")" \
  EBO_SE_UID="$(jq -r "${base}.uid // empty" "$OPTS")" \
  EBO_SE_IDENTITY="$(jq -r "${base}.identity // empty" "$OPTS")" \
  EBO_SE_TOKEN="$(jq -r "${base}.token // empty" "$OPTS")" \
  EBO_SE_SN="$(jq -r "${base}.sn // empty" "$OPTS")" \
  EBO_SE_MAC="$(jq -r "${base}.mac // empty" "$OPTS")" \
  EBO_SE_LICENSE="$(jq -r "${base}.license // empty" "$OPTS")" \
  EBO_SE_AUTHKEY="$(jq -r "${base}.authkey // empty" "$OPTS")" \
  EBO_SE_IOCTL9930_B64="" \
    run_se_robot "$n" "$gidx"
}

# Feeder: a CLOUD-discovered TSV line -> export its creds (incl. the freshly generated ioctl blob),
# then supervise. Fields: SE id name uid identity token sn model ioctl9930_b64
run_se_cloud() {
  local n="$1" gidx="$2"; shift 2
  local id="$1" name="$2" uid="$3" identity="$4" token="$5" sn="$6" model="$7" ioctl="$8"
  EBO_SE_NAME="$name" EBO_SE_UID="$uid" EBO_SE_IDENTITY="$identity" EBO_SE_TOKEN="$token" \
  EBO_SE_SN="$sn" EBO_SE_IOCTL9930_B64="$ioctl" \
    run_se_robot "$n" "$gidx"
}

# Ingress web panel (one for the whole add-on): aggregates every robot's state over MQTT.
export EBO_PANEL_PORT="${EBO_PANEL_PORT:-8099}"
python /app/panel.py &
PANEL_PID=$!

# MCP server for AI agents (opt-in): only started when the 'mcp' option is ON, so it costs nothing
# when unused. Guarded by the add-on's api_token (Bearer). Non-fatal: if fastmcp isn't available or
# it fails to start, the add-on keeps working normally.
EBO_MCP="$(pget mcp false)"
if [ "$EBO_MCP" = "true" ]; then
  export EBO_MCP_PORT="${EBO_MCP_PORT:-8100}"
  if python -c 'import fastmcp' 2>/dev/null; then
    echo "[add-on] starting MCP server for AI agents on :${EBO_MCP_PORT} (token-guarded)"
    python /app/ebo_mcp.py &
  else
    echo "[add-on] MCP requested but 'fastmcp' isn't installed in this image — skipping"
  fi
fi

for i in "${!RIDS[@]}"; do
  run_robot "${RIDS[$i]}" "$i" "${RNAMES[$i]}" &
done
# EBO SE robots: global index continues after the cloud robots (NR) so RTSP/WebRTC ports stay unique.
# Node ordinals (ebo_se_N) are 1-based and continue across manual then cloud robots.
SE_ORD=0
# (a) manual se_robots entries
s=0
while [ "$s" -lt "$NSE_MANUAL" ]; do
  SE_ORD=$(( SE_ORD + 1 ))
  run_se_manual "$s" "$(( NR + SE_ORD - 1 ))" &
  s=$(( s + 1 ))
done
# (b) cloud-discovered robots (one TSV line each)
if [ "$NSE_CLOUD" -gt 0 ] && [ -s "$SE_DISCO" ]; then
  while IFS=$'\t' read -r tag id name uid identity token sn model ioctl; do
    [ "$tag" = "SE" ] || continue
    SE_ORD=$(( SE_ORD + 1 ))
    run_se_cloud "$SE_ORD" "$(( NR + SE_ORD - 1 ))" \
      "$id" "$name" "$uid" "$identity" "$token" "$sn" "$model" "$ioctl" &
  done < "$SE_DISCO"
fi
wait || true   # never let a crashing child kill the supervisor under `set -e`
