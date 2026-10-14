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

## 1. Create the SE config directory

Default is `/share/ebo-se` (override with the `se_dir` option). Create it and populate it with the
files below (e.g. over Samba, or the SSH/Terminal add-on):

```
/share/ebo-se/
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
│   └── libstdc++.so
└── ioctl9930.bin       # captured "start streaming" payload (see step 3)
```

The `.so` files, the bionic runtime and `ioctl9930.bin` are proprietary / device-specific and are
**not** distributed here. You extract them once from software and hardware you own.

## 2. Build the native bridge (64-bit)

The upstream project ships only a 32-bit binary. For the Pi 5 (64-bit kernel) build an **arm64**
binary from the vendored source, using the Android NDK:

```bash
NDK=/path/to/android-ndk ARCH=arm64 ./se/build_bridge.sh
# -> ebo/se/ebo_bridge  (ELF 64-bit aarch64)
```

Copy the result to `/share/ebo-se/ebo_bridge`. The binary's bitness must match the `lib/` and
`bionic/` files — 64-bit binary ⇒ 64-bit (`arm64-v8a`) `.so` files and the 64-bit `linker64`.

## 3. Get the TUTK libraries, runtime, blob and credentials

Follow lilium360's guide — it covers all of these in detail:
<https://github.com/lilium360/ebo-se-lan-bridge/blob/main/docs/SETUP.md>

In short:
- **TUTK `.so` (`lib/`)** — from the ROLA app's `lib/arm64-v8a/` (use the **arm64** ones for a 64-bit build).
- **bionic runtime (`bionic/`)** — pulled from a 64-bit Android system (`linker64` + the listed libs).
- **`ioctl9930.bin`** — the captured stream-start payload.
- **Credentials** — the five per-robot values below, extracted from the ROLA app while it connects
  to *your* robot:

| Config field | TUTK source |
|---|---|
| `uid` | the 20-char TUTK UID |
| `authkey` | `St_IOTCConnectInput.authKey` |
| `identity` | `avClientStartEx` account / identity |
| `token` | `avClientStartEx` password / token |
| `license` | `TUTK_SDK_Set_License_Key` argument |

These belong to your device/app; keep them private. They never leave your Home Assistant.

## 4. Add the robot(s) to the add-on config

In the add-on **Configuration** tab:

```yaml
se_dir: /share/ebo-se
se_robots:
  - name: "Living room EBO SE"
    uid: "XXXXXXXXXXXXXXXXXXXX"
    authkey: "xxxxxxxx"
    identity: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    token: "xxxxxxxx"
    license: "xxxxxxxx"
```

Start (or restart) the add-on. The SE appears in the panel and, after one Home Assistant restart,
as an **EBO** device with a live camera — same as a cloud robot.

## Notes & limits

- **Video:** by default the SE's H.265 is re-encoded to H.264 720p (so the WebRTC drive view and
  all browsers work). Set `EBO_SE_VIDEO_COPY=1` in the add-on only if you want raw H.265 passthrough.
- **Ports:** each robot (cloud or SE) gets RTSP `8554+n` and WebRTC `8189+n`. The add-on maps
  WebRTC host ports `8189–8192`, so the **first four** robots get the fluid drive view; a fifth+
  robot still has camera + HLS preview + snapshots, just not WebRTC.
- **Sleep:** the `standby_after_minutes` option also applies to the SE — after that idle time the
  add-on releases the P2P session (so the robot can sleep); any command wakes it again.
- **One stream at a time:** keep the ROLA app closed while the add-on holds the robot.
