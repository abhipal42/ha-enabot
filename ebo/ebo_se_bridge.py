#!/usr/bin/env python3
"""
ebo_se_bridge.py — EBO SE (LAN / TUTK-Kalay) ⇆ Home Assistant bridge for this add-on.

Unlike the cloud EBO models (ebo_bridge.py, which speaks Enabot's cloud + Agora), the EBO SE
is a LAN-only robot that speaks ThroughTek's TUTK/Kalay P2P stack. This module makes an SE
behave like any other robot on the add-on's INTERNAL MQTT bus, so the same panel, the companion
HA integration and the MCP server work unchanged:

  publishes   ebo/discovery/<node>  {node,name,sn,mac,model,rtsp}   (retained)
              <node>/status         online|offline
              <node>/state          JSON {battery,charging,docked,night_vision,avoid_obstacle,...}
              <node>/camera/state   on|off
              <node>/camera/url     rtsp://…  (the add-on's shared mediamtx)
  subscribes  <node>/move/vector <node>/joystick <node>/dock <node>/wake <node>/sleep/set
              <node>/camera/set <node>/eyes/set <node>/night_vision/set <node>/avoid_obstacle/set

Pipeline (the robot link itself):
  EBO SE ──Kalay P2P/DTLS──► native TUTK bridge (stdout frames, fd3 control)
                               │ H.265/H.264 video            ▲ MAVLink control (RDT) / ioctl
                               ▼                              │
                          ffmpeg ──► mediamtx RTSP (shared with the rest of the add-on)

The native bridge binary and the TUTK/bionic runtime are NOT shipped in this repo (proprietary,
and specific to your device). They are provided by you and loaded at runtime from the add-on's
config dir — see docs/EBO-SE.md. This module ships only original glue code.

The TUTK frame wire-format, the MAVLink control builders and the battery-status parse below are
ported from lilium360's MIT-licensed ebo-se-lan-bridge (https://github.com/lilium360/ebo-se-lan-bridge)
— credit to lilium360 for reverse-engineering the SE's local protocol.

Config via env (set per-robot by run.sh):
  EBO_SE_NODE            internal node id, e.g. "ebo_se_1"
  EBO_SE_NAME            display name
  EBO_SE_UID             TUTK UID (20 chars)                     ] your own robot's
  EBO_SE_AUTHKEY         St_IOTCConnectInput.authKey             ] credentials, supplied
  EBO_SE_IDENTITY        avClientStartEx account/identity        ] by you — never shipped,
  EBO_SE_TOKEN           avClientStartEx password/token          ] never committed
  EBO_SE_LICENSE         TUTK_SDK_Set_License_Key value          ]
  EBO_SE_DIR=/share/ebo-se    dir holding ebo_bridge, lib/, bionic/, ioctl9930.bin
  EBO_RTSP_PORT=8554     shared mediamtx RTSP port
  EBO_SE_RTSP_PATH       mediamtx path for this robot (default = node)
  EBO_HOST_IP            host IP the camera URL should use
  EBO_MQTT_HOST/PORT/USER/PASS   internal broker
  EBO_SE_VIDEO_COPY=0    1 = passthrough H.265 (no re-encode); 0 = transcode to H.264/720p
  EBO_STANDBY_TIMEOUT    seconds of no commands before releasing the session (0 = never)
"""
from ebo_log import log

import json
import os
import struct
import subprocess
import tempfile
import threading
import time

import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------------------------
NODE = os.environ.get("EBO_SE_NODE", "ebo_se")
NAME = os.environ.get("EBO_SE_NAME", "EBO SE")
SE_DIR = os.environ.get("EBO_SE_DIR", "/share/ebo-se")

UID = os.environ.get("EBO_SE_UID", "")
AUTHKEY = os.environ.get("EBO_SE_AUTHKEY", "")
IDENTITY = os.environ.get("EBO_SE_IDENTITY", "")
TOKEN = os.environ.get("EBO_SE_TOKEN", "")
LICENSE = os.environ.get("EBO_SE_LICENSE", "")

RTSP_PORT = int(os.environ.get("EBO_RTSP_PORT", "8554"))
RTSP_PATH = os.environ.get("EBO_SE_RTSP_PATH", NODE)
HOST_IP = os.environ.get("EBO_HOST_IP", "127.0.0.1")

MQTT_HOST = os.environ.get("EBO_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("EBO_MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("EBO_MQTT_USER") or None
MQTT_PASS = os.environ.get("EBO_MQTT_PASS") or None

VIDEO_COPY = os.environ.get("EBO_SE_VIDEO_COPY", "0") == "1"
VIDEO_MAX_H = int(os.environ.get("EBO_VIDEO_MAX_HEIGHT", "720"))
VIDEO_FPS = int(os.environ.get("EBO_VIDEO_FPS", "20"))
VIDEO_BITRATE = int(os.environ.get("EBO_VIDEO_BITRATE", "2500"))
VIDEO_PRESET = os.environ.get("EBO_VIDEO_PRESET", "ultrafast")
STANDBY_TIMEOUT = int(os.environ.get("EBO_STANDBY_TIMEOUT", "0"))

RTSP_URL_INTERNAL = "rtsp://127.0.0.1:%d/%s" % (RTSP_PORT, RTSP_PATH)
RTSP_URL_CAMERA = "rtsp://%s:%d/%s" % (HOST_IP, RTSP_PORT, RTSP_PATH)

DISCOVERY_PREFIX = "homeassistant"

# ---------------------------------------------------------------------------------------------
# MAVLink control builders (ported from lilium360's ebo-se-lan-bridge, MIT)
#   control is sent to the native bridge's fd3 as:  [u32 len LE][u8 kind][payload]
#     kind 0 = MAVLink over RDT (motors/params/commands)
#     kind 1 = avSendIOCtrl : payload = [io_type u16 LE][data]
# ---------------------------------------------------------------------------------------------
def _mav_crc(data, crc_extra):
    crc = 0xFFFF
    for b in data:
        t = b ^ (crc & 0xFF)
        t = (t ^ (t << 4)) & 0xFF
        crc = ((crc >> 8) ^ (t << 8) ^ (t << 3) ^ (t >> 4)) & 0xFFFF
    t = crc_extra ^ (crc & 0xFF)
    t = (t ^ (t << 4)) & 0xFF
    crc = ((crc >> 8) ^ (t << 8) ^ (t << 3) ^ (t >> 4)) & 0xFFFF
    return crc


def _mav_frame(msgid, payload, crc_extra):
    hdr = bytes([len(payload), 0, 0, 0, msgid])
    return bytes([0xFE]) + hdr + payload + struct.pack("<H", _mav_crc(hdr + payload, crc_extra))


def motor_frame(ly=0.0, rx=0.0, lx=0.0, ry=0.0, buttons=0):
    # ly>0 = forward (robot convention is inverted, so negate). Axes are floats in -1..1.
    return _mav_frame(202, struct.pack("<ffff", lx, -ly, rx, ry) + bytes([buttons & 0xFF]), 211)


def param_set_frame(group, key, value, ptype=11):
    pid = ("%s-%s" % (group, key)).encode()[:32].ljust(32, b"\x00")
    return _mav_frame(229, struct.pack("<f", value) + bytes([255, 3]) + pid + bytes([ptype]), 208)


def command_frame(command):
    return _mav_frame(200, struct.pack("<H", command) + bytes([255, 1]), 196)


CMD_DOCK = 40154

# param group/key mapping for the SE's toggles (lilium360)
PARAM = {
    "eyes":          ("display", "enable"),
    "night_vision":  ("video", "night_vision"),
    "avoid_obstacle": ("control", "auto_avoidance"),
    "fall":          ("control", "fallarrest"),
    "patrol":        ("security_patrol", "enable"),
    "sleep":         ("power", "sleep"),
}


def _truthy(payload):
    return str(payload).strip().lower() in ("1", "on", "true", "yes", "open")


# ---------------------------------------------------------------------------------------------
# the SE link: native bridge + ffmpeg + frame demux
# ---------------------------------------------------------------------------------------------
def _has_param_set(data, codec):
    """True once a frame carries the codec parameter sets (HEVC VPS/SPS, H.264 SPS). ffmpeg must
    start at such a frame or it can't learn the stream dimensions."""
    i, n = 0, len(data)
    while i + 4 < n:
        if data[i] == 0 and data[i + 1] == 0 and data[i + 2] == 0 and data[i + 3] == 1:
            nal = data[i + 4]
            if codec == 80 and ((nal >> 1) & 0x3F) in (32, 33, 34):   # HEVC VPS/SPS/PPS
                return True
            if codec == 78 and (nal & 0x1F) == 7:                      # H.264 SPS
                return True
            i += 4
        else:
            i += 1
    return False


class SEBridge:
    def __init__(self):
        self.proc = None          # native TUTK bridge
        self.ff = None            # ffmpeg (video → RTSP)
        self.mediamtx = None      # RTSP/HLS/WebRTC server for this robot
        self._ctrl_r, self._ctrl_w = os.pipe()
        self._running = True
        self._ff_primed = False
        self.connected = False
        self.last_cmd = time.time()
        self.paused = False
        # published state
        self.state = {"battery": None, "charging": "false", "docked": "false",
                      "night_vision": "false", "avoid_obstacle": "false",
                      "eyes": "false", "move_mode": "joystick"}
        self.mqtt = None
        self.frames = 0

    # ---- mediamtx: this robot's RTSP/HLS/WebRTC server (same config as ebo_video.py) ----
    def _start_mediamtx(self):
        if self.mediamtx is not None:
            return
        cfg = os.path.join(tempfile.gettempdir(), "ebo_se_mediamtx_%d.yml" % RTSP_PORT)
        # match the cloud bridge's derivation so the panel's HLS/WebRTC drive view works unchanged
        hls_port = 8888 + (RTSP_PORT - 8554)
        webrtc_port = 8189 + (RTSP_PORT - 8554)
        seen, hosts = set(), []
        for ip in (x.strip() for x in os.environ.get("EBO_HOST_IPS", "").split(",") if x.strip()):
            if ip not in seen:
                seen.add(ip)
                hosts.append(ip)
        additional_hosts = ("[" + ", ".join(hosts) + "]") if hosts else "[]"
        with open(cfg, "w") as f:
            f.write("logLevel: error\n"
                    f"rtspAddress: :{RTSP_PORT}\n"
                    "hls: yes\n"
                    f"hlsAddress: :{hls_port}\n"
                    "hlsVariant: lowLatency\n"
                    "hlsAlwaysRemux: no\n"
                    "hlsSegmentCount: 7\n"
                    "hlsSegmentDuration: 1s\n"
                    "hlsPartDuration: 200ms\n"
                    "hlsAllowOrigin: '*'\n"
                    "webrtc: yes\n"
                    f"webrtcAddress: :{webrtc_port}\n"
                    f"webrtcLocalUDPAddress: :{webrtc_port}\n"
                    f"webrtcAdditionalHosts: {additional_hosts}\n"
                    "webrtcAllowOrigin: '*'\n"
                    "webrtcICEServers2: []\n"
                    "rtmp: no\n"
                    "paths:\n  all_others:\n")
        try:
            self.mediamtx = subprocess.Popen(["/usr/local/bin/mediamtx", cfg],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1)
            log("[se] mediamtx RTSP :%d HLS :%d WebRTC :%d" % (RTSP_PORT, hls_port, webrtc_port))
        except FileNotFoundError:
            log("[se] mediamtx not found — video disabled")
            self.mediamtx = None

    # ---- ffmpeg: robot video (stdin, Annex-B) → shared mediamtx RTSP ----
    def _start_ffmpeg(self, codec_name):
        fmt = "hevc" if codec_name == "hevc" else "h264"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning",
               "-fflags", "nobuffer", "-flags", "low_delay",
               "-analyzeduration", "500000", "-probesize", "1000000",
               "-thread_queue_size", "256", "-f", fmt, "-i", "pipe:0"]
        if VIDEO_COPY:
            cmd += ["-c:v", "copy", "-an"]
        else:
            cmd += ["-an", "-vf", "scale=-2:'min(%d,ih)'" % VIDEO_MAX_H,
                    "-c:v", "libx264", "-preset", VIDEO_PRESET, "-tune", "zerolatency",
                    "-b:v", "%dk" % VIDEO_BITRATE, "-r", str(VIDEO_FPS), "-g", str(VIDEO_FPS * 2),
                    "-pix_fmt", "yuv420p"]
        cmd += ["-f", "rtsp", "-rtsp_transport", "tcp", RTSP_URL_INTERNAL]
        self.ff = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log("[se] ffmpeg pid", self.ff.pid, "copy=%s" % VIDEO_COPY)

    def _restart_ffmpeg(self, codec_name):
        for fn in ((self.ff.stdin.close if self.ff else None), (self.ff.kill if self.ff else None)):
            try:
                fn and fn()
            except Exception:
                pass
        self._ff_primed = False
        self._start_ffmpeg(codec_name)

    # ---- native bridge: linker + binary, fd3 = control pipe (lilium360 spawn) ----
    def _start_native(self):
        # 64-bit (arm64) bionic uses 'linker64'; 32-bit uses 'linker'. Prefer whichever is present.
        linker = ""
        for cand in ("linker64", "linker"):
            p = os.path.join(SE_DIR, "bionic", cand)
            if os.path.exists(p):
                linker = p
                break
        binary = os.path.abspath(os.path.join(SE_DIR, "ebo_bridge"))
        env = dict(os.environ)
        env["EBO_LIB_DIR"] = os.path.join(SE_DIR, "lib")
        # stream-start blob: prefer a freshly generated one (cloud discovery passes it base64 in
        # EBO_SE_IOCTL9930_B64), else fall back to a captured ioctl9930.bin in se_dir.
        ioctl_path = os.path.join(SE_DIR, "ioctl9930.bin")
        b64 = os.environ.get("EBO_SE_IOCTL9930_B64", "")
        if b64:
            try:
                import base64 as _b64
                ioctl_path = os.path.join(tempfile.gettempdir(), "ebo_se_ioctl_%s.bin" % NODE)
                with open(ioctl_path, "wb") as _f:
                    _f.write(_b64.b64decode(b64))
            except Exception as e:
                log("[se] could not write generated ioctl9930:", e)
        env["EBO_IOCTL9930"] = ioctl_path
        env["LD_LIBRARY_PATH"] = os.path.join(SE_DIR, "bionic") + ":" + os.path.join(SE_DIR, "lib")
        # the native bridge reads credentials from these (its own env names)
        env["EBO_UID"], env["EBO_AUTHKEY"] = UID, AUTHKEY
        env["EBO_IDENTITY"], env["EBO_TOKEN"], env["EBO_LICENSE"] = IDENTITY, TOKEN, LICENSE
        argv = [linker, binary] if linker else [binary]
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env=env, pass_fds=(self._ctrl_r,),
                                     preexec_fn=lambda: os.dup2(self._ctrl_r, 3))
        log("[se] native bridge pid", self.proc.pid)
        threading.Thread(target=self._log_stderr, daemon=True).start()

    def _log_stderr(self):
        for line in iter(self.proc.stderr.readline, b""):
            s = line.decode(errors="replace").rstrip()
            log("[se:native]", s)
            if "connected" in s:
                self.connected = True
                self._publish_status(True)

    # ---- control helpers ----
    def send_rdt(self, mavlink: bytes):
        os.write(self._ctrl_w, struct.pack("<I", 1 + len(mavlink)) + b"\x00" + mavlink)

    def send_ioctl(self, io_type: int, data: bytes = b""):
        payload = b"\x01" + struct.pack("<H", io_type) + data
        os.write(self._ctrl_w, struct.pack("<I", len(payload)) + payload)

    # ---- frame demux: stdout = [u32 len][u8 codec][payload] ----
    def _read_frames(self):
        f = self.proc.stdout
        codec_name = "hevc"
        while self._running:
            hdr = f.read(5)
            if len(hdr) < 5:
                break
            n = struct.unpack("<I", hdr[:4])[0]
            codec = hdr[4]
            data = f.read(n)
            if len(data) < n:
                break
            if codec == 0xFF:                 # inbound MAVLink status (battery, etc.)
                self._parse_status(data)
                continue
            if codec == 0xA0:                 # audio (listen-only) — not wired into RTSP here
                continue
            # video
            self.frames += 1
            new_name = "hevc" if codec == 80 else ("h264" if codec == 78 else codec_name)
            if self.ff is None or new_name != codec_name:
                codec_name = new_name
                self._restart_ffmpeg(codec_name)
            if not self._ff_primed:
                if not _has_param_set(data, codec):
                    continue
                self._ff_primed = True
            try:
                self.ff.stdin.write(data)
                self.ff.stdin.flush()
            except Exception:
                self._restart_ffmpeg(codec_name)
            if self.frames == 1 and self.mqtt:
                self.mqtt.publish("%s/camera/state" % NODE, "on", retain=True)

    def _parse_status(self, u):
        i = 0
        while i + 8 <= len(u):
            if u[i] == 0xFE:
                plen = u[i + 1]
                msgid = u[i + 5] if i + 5 < len(u) else -1
                if msgid == 207 and i + 24 <= len(u):      # BATTERY_STATUS
                    bp, cs = u[i + 22], u[i + 23]
                    changed = (str(bp) != str(self.state.get("battery")) or
                               ("true" if cs else "false") != self.state.get("charging"))
                    self.state["battery"] = bp
                    self.state["charging"] = "true" if cs else "false"
                    if changed:
                        self._publish_state()
                i += 8 + plen + 2 if plen else i + 1
            else:
                i += 1

    # ---- MQTT: discovery / state / commands ----
    def _publish_discovery(self):
        payload = {"node": NODE, "name": NAME, "model": "EBO SE",
                   "sn": os.environ.get("EBO_SE_SN", ""), "mac": os.environ.get("EBO_SE_MAC", ""),
                   "rtsp": RTSP_URL_CAMERA}
        self.mqtt.publish("ebo/discovery/%s" % NODE, json.dumps(payload), retain=True)

    def _publish_status(self, online):
        if self.mqtt:
            self.mqtt.publish("%s/status" % NODE, "online" if online else "offline", retain=True)

    def _publish_state(self):
        if self.mqtt:
            self.mqtt.publish("%s/state" % NODE, json.dumps(self.state), retain=True)

    def _on_connect(self, c, u, flags, rc, properties=None):
        log("[se] MQTT connected rc=%s" % rc)
        self._publish_discovery()
        self.mqtt.publish("%s/camera/url" % NODE, RTSP_URL_CAMERA, retain=True)
        self.mqtt.publish("%s/camera/state" % NODE, "on" if self.frames else "off", retain=True)
        for t in ("move/vector", "joystick", "dock", "wake", "sleep/set", "camera/set",
                  "eyes/set", "night_vision/set", "avoid_obstacle/set"):
            c.subscribe("%s/%s" % (NODE, t))
        self._publish_state()

    def _on_message(self, c, u, msg):
        try:
            leaf = msg.topic[len(NODE) + 1:]
            payload = msg.payload.decode("utf-8", "replace")
            self.last_cmd = time.time()
            if self.paused:
                self._resume()
            self._handle(leaf, payload)
        except Exception as e:
            log("[se] cmd error:", e)

    def _handle(self, leaf, payload):
        if leaf == "move/vector":
            d = json.loads(payload) if payload else {}
            ly = max(-1.0, min(1.0, float(d.get("ly", 0)) / 100.0))
            rx = max(-1.0, min(1.0, float(d.get("rx", 0)) / 100.0))
            hold = float(d.get("hold", 0) or 0)
            buttons = int(d.get("buttons", 0) or 0)
            self._drive(ly, rx, buttons, hold)
        elif leaf == "joystick":
            d = json.loads(payload) if payload else {}
            ly = max(-1.0, min(1.0, float(d.get("y", 0))))
            rx = max(-1.0, min(1.0, float(d.get("x", 0))))
            self.send_rdt(motor_frame(ly=ly, rx=rx, buttons=1 if (ly or rx) else 0))
        elif leaf == "dock":
            self.send_rdt(command_frame(CMD_DOCK))
        elif leaf in ("wake", "sleep/set"):
            sleep_on = (leaf == "sleep/set" and _truthy(payload))
            g, k = PARAM["sleep"]
            self.send_rdt(param_set_frame(g, k, 1.0 if sleep_on else 0.0))
        elif leaf == "camera/set":
            # for the SE, "camera on" = wake so video flows; "off" = let it sleep
            g, k = PARAM["sleep"]
            self.send_rdt(param_set_frame(g, k, 0.0 if _truthy(payload) else 1.0))
            self.mqtt.publish("%s/camera/state" % NODE, "on" if _truthy(payload) else "off", retain=True)
        elif leaf in ("eyes/set", "night_vision/set", "avoid_obstacle/set"):
            name = leaf.split("/", 1)[0]
            g, k = PARAM[name]
            on = _truthy(payload)
            self.send_rdt(param_set_frame(g, k, 1.0 if on else 0.0))
            self.state[name] = "true" if on else "false"
            self._publish_state()

    def _drive(self, ly, rx, buttons, hold):
        """Send a motor vector, then keep it alive at ~10 Hz for `hold` seconds, then stop.
        A dead-man's stop guards against a dropped 'stop'."""
        def run():
            end = time.time() + max(0.0, hold)
            self.send_rdt(motor_frame(ly=ly, rx=rx, buttons=buttons or (1 if (ly or rx) else 0)))
            while hold > 0 and time.time() < end and self._running:
                self.send_rdt(motor_frame(ly=ly, rx=rx, buttons=buttons or 1))
                time.sleep(0.1)
            if hold > 0 or (ly == 0 and rx == 0):
                self.send_rdt(motor_frame(0, 0, buttons=0))
        threading.Thread(target=run, daemon=True).start()

    # ---- session pause/resume for standby ----
    def _pause(self):
        if self.paused:
            return
        self.paused = True
        self.connected = False
        self._publish_status(False)
        self.mqtt and self.mqtt.publish("%s/camera/state" % NODE, "off", retain=True)
        for fn in ((self.proc.terminate if self.proc else None),):
            try:
                fn and fn()
            except Exception:
                pass
        log("[se] paused (standby) — session released")

    def _resume(self):
        if not self.paused:
            return
        self.paused = False
        self._ff_primed = False
        self.frames = 0
        log("[se] resuming session")
        self._start_native()
        threading.Thread(target=self._read_frames, daemon=True).start()

    def _standby_loop(self):
        while self._running and STANDBY_TIMEOUT > 0:
            time.sleep(5)
            if not self.paused and (time.time() - self.last_cmd) > STANDBY_TIMEOUT:
                self._pause()

    # ---- run ----
    def run(self):
        missing = [k for k, v in (("UID", UID), ("AUTHKEY", AUTHKEY), ("IDENTITY", IDENTITY),
                                   ("TOKEN", TOKEN), ("LICENSE", LICENSE)) if not v]
        if missing:
            log("[se] missing credentials:", ", ".join(missing), "— not starting", NODE)
            return
        if not os.path.exists(os.path.join(SE_DIR, "ebo_bridge")):
            log("[se] native bridge not found in", SE_DIR, "— see docs/EBO-SE.md; not starting", NODE)
            return
        c = mqtt.Client(client_id="ebo_se_%s" % NODE)
        if MQTT_USER:
            c.username_pw_set(MQTT_USER, MQTT_PASS)
        c.on_connect = self._on_connect
        c.on_message = self._on_message
        c.will_set("%s/status" % NODE, "offline", retain=True)
        c.connect(MQTT_HOST, MQTT_PORT, 30)
        self.mqtt = c
        c.loop_start()

        self._start_mediamtx()
        threading.Thread(target=self._standby_loop, daemon=True).start()
        self._start_native()
        while self._running:
            self._read_frames()                   # returns when the native bridge exits
            if not self._running:
                break
            self.connected = False
            self._publish_status(False)
            if self.paused:
                time.sleep(1)
                continue
            log("[se] native bridge exited — restarting in 5s")
            time.sleep(5)
            self._ff_primed = False
            self._start_native()


if __name__ == "__main__":
    SEBridge().run()
