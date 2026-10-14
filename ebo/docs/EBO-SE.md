# EBO SE (LAN) support

The **EBO SE** is not a cloud robot. Unlike the EBO Air 2 / X / Max (which this add-on drives over
Enabot's cloud + Agora), the SE speaks **ThroughTek TUTK/Kalay P2P on your LAN**. This add-on can
run an SE *alongside* your cloud robots, so it shows up in the same panel, as the same kind of Home
Assistant device, and is drivable by the MCP server.

> This reuses the local protocol work from **lilium360's
> [ebo-se-lan-bridge](https://github.com/lilium360/ebo-se-lan-bridge)** (MIT). The SE's native TUTK
> bridge source lives here in [`se/ebo_bridge.c`](../se/ebo_bridge.c); the proprietary TUTK
> libraries, the Android runtime and your robot credentials are **not** shipped — you provide them.

## What works

- Live camera (RTSP → the add-on's shared mediamtx → HA camera, panel preview, WebRTC drive view)
- Driving: analog joystick / dual sticks and the move buttons
- Dock / wake / sleep, eye lights, night vision, collision avoidance
- Battery % and charging state
- MCP: `ebo_look`, `ebo_move`, `ebo_stop`, `ebo_dock`, `ebo_wake`, `ebo_night_vision`

**Not available on the SE:** laser, talk/say (two-way audio), per-robot volume. Sending those
commands is simply ignored by the SE bridge.

## Requirements

- Home Assistant on a **Raspberry Pi 5 / 4 (aarch64)** — the SE's TUTK libraries are ARM Android.
- The robot and HA on the **same LAN**.
- Only one client may stream from an SE at a time: **close the ROLA app** while the add-on is connected.

## 1. Provide the native stack

The SE's native bridge + TUTK/bionic runtime aren't shipped in the image (the TUTK libraries are
proprietary). You provide them once. By default the add-on uses `<config>/ebo` — i.e. an `ebo`
folder in your Home Assistant config dir, shown as **`/config/ebo`** in the File editor add-on.
(Set the `se_dir` option to override, e.g. `/share/ebo`.)

**Easiest — drop the bundle and let the add-on unpack it.** Put a single `ebo-se-share.tgz`
(containing `ebo_bridge`, `lib/`, `bionic/`) into the File editor's **`/config`** root — no settings
change needed, since File editor shows `/config` by default. On the next start the add-on extracts
it into `/config/ebo` and sets permissions — no SSH, `tar` or `chmod`. It re-extracts only when the
bridge is missing or you drop a newer bundle. (Accepted drop locations: `/config/ebo-se-share.tgz`,
`/config/ebo.tgz`, or `/share/ebo-se-share.tgz` if you prefer `/share`.)

**Or place the files yourself** — the expected layout under `se_dir` (`/config/ebo` by default):

```
<config>/ebo/      (a.k.a. /config/ebo in the File editor)
├── ebo_bridge          # the native bridge you build in step 2 (must match the .so bitness)
├── lib/                # the 4 TUTK .so files (see step 3)
│   ├── libTUTKGlobalAPIs.so
│   ├── libIOTCAPIs.so
│   ├── libRDTAPIs.so
│   └── libAVAPIs.so
├── bionic/             # the Android runtime (see step 3) — use the 64-bit linker64 for arm64
│   ├── linker64
│   ├── libc.so
│   ├── libm.so
│   ├── libdl.so
│   ├── libc++.so
│   ├── liblog.so
│   ├── libstdc++.so
│   └── libnetd_client.so   # needed for DNS from the Android libc (TUTK master-server lookup)
└── ioctl9930.bin       # ONLY for manual mode (Option B) — cloud discovery generates it for you
```

The `.so` files and the bionic runtime are proprietary / device-specific and are **not** distributed
here. You extract them once from software and hardware you own. (`ioctl9930.bin` is needed only for
manual mode — cloud discovery generates a fresh one per connect.)

## 2. Build the native bridge (64-bit)

The upstream project ships only a 32-bit binary. For the Pi 5 (64-bit kernel) build an **arm64**
binary from the vendored source, using the Android NDK:

```bash
NDK=/path/to/android-ndk ARCH=arm64 ./se/build_bridge.sh
# -> ebo/se/ebo_bridge  (ELF 64-bit aarch64)
```

Copy the result into your bundle (or to `<config>/ebo/ebo_bridge`). The binary's bitness must match the `lib/` and
`bionic/` files — 64-bit binary ⇒ 64-bit (`arm64-v8a`) `.so` files and the 64-bit `linker64`.

## 3. Get the TUTK libraries + runtime

Follow lilium360's guide for the `lib/` and `bionic/` files:
<https://github.com/lilium360/ebo-se-lan-bridge/blob/main/docs/SETUP.md>

- **TUTK `.so` (`lib/`)** — from the ROLA app's `lib/arm64-v8a/` (use the **arm64** ones for a 64-bit build).
- **bionic runtime (`bionic/`)** — pulled from a 64-bit Android system (`linker64` + the listed libs).

These are proprietary / device-specific and are **not** distributed here.

## 4. Choose how the robot credentials are supplied

Each SE needs five TUTK values (UID, identity, token, authKey, license) and a per-connect
stream-start token. You can let the add-on **discover them from the ROLA cloud** (recommended), or
enter them **manually**.

### Option A — cloud discovery (recommended, least setup)

Set `se_cloud: true` and give the add-on your **ROLA email + password** plus the app's keys. The
add-on then logs in like the ROLA app, pulls your robot list, and derives every TUTK value **and a
fresh stream-start token** automatically — so you hand-extract nothing per robot, and you **don't
need `ioctl9930.bin`** at all.

The keys are the app's symmetric secrets — the **same kind** the cloud (EBO HOME) models already
need in this add-on, and quite possibly the *same values* you already configured there. They are
**not shipped**; you extract them once from your own app (jadx for the Java-side key, Frida for the
native-side keys — see [GET-APP-KEYS.md](GET-APP-KEYS.md) and [`tools/extract_keys.js`](../tools/extract_keys.js)).
ROLA field names:

| Config field | ROLA app field (`SignatureHelper`) | used for |
|---|---|---|
| `se_body_key` | `bodyEncryptKeyS2` | encrypt the login body (AES-GCM) |
| `se_sign_key` | `accessKeySecretS2` | sign the login (HMAC-SHA1) |
| `se_sign_key_v1` | `accessKeySecret` | sign the other requests, e.g. the stream token (HMAC-SHA1) |
| `se_uat_key`  | `userAuthTokenKey` | the stream-start token (AES-CFB) |
| `se_license`  | `TUTK_SDK_Set_License_Key` arg | TUTK license (app constant) |
| `se_authkey`  | `St_IOTCConnectInput.authKey` | TUTK connect authKey (app constant) |

```yaml
se_cloud: true
se_email: "you@example.com"
se_password: "your-rola-password"
se_body_key: "................"
se_sign_key: "................"
se_sign_key_v1: "................"
se_uat_key:  "................"
se_license:  "................"
se_authkey:  "........"
# se_login_region: GB          # match your account region
# se_cloud_host: ebo.enabotserverintl.com   # CN accounts: ebo.enabotserver.com
```

Keys and password never leave your Home Assistant. If discovery fails, the add-on log points to
`/data/se_discover.log`.

### Option B — manual per-robot credentials

If you'd rather not supply the cloud keys, extract the five TUTK values per robot yourself (per
lilium360's guide) and drop an `ioctl9930.bin` into `se_dir`:

```yaml
se_robots:
  - name: "Living room EBO SE"
    uid: "XXXXXXXXXXXXXXXXXXXX"
    authkey: "xxxxxxxx"
    identity: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    token: "xxxxxxxx"
    license: "xxxxxxxx"
```

(With Option B you also need `se_dir/ioctl9930.bin`, the captured stream-start payload.)

## 5. Start

Start (or restart) the add-on. Each SE appears in the panel and, after one Home Assistant restart,
as an **EBO** device with a live camera — same as a cloud robot. You can mix cloud-discovered and
manual robots, and run them alongside your cloud Air 2.

## Notes & limits

- **Video:** by default the SE's H.265 is re-encoded to H.264 720p (so the WebRTC drive view and
  all browsers work). Set `EBO_SE_VIDEO_COPY=1` in the add-on only if you want raw H.265 passthrough.
- **Ports:** each robot (cloud or SE) gets RTSP `8554+n` and WebRTC `8189+n`. The add-on maps
  WebRTC host ports `8189–8192`, so the **first four** robots get the fluid drive view; a fifth+
  robot still has camera + HLS preview + snapshots, just not WebRTC.
- **Sleep:** the `standby_after_minutes` option also applies to the SE — after that idle time the
  add-on releases the P2P session (so the robot can sleep); any command wakes it again.
- **One stream at a time:** keep the ROLA app closed while the add-on holds the robot.
