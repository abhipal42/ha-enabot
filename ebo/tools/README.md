# `extract_keys.js` — pull your own app keys at runtime (recent EBO HOME builds)

On current EBO HOME builds the two keys the add-on needs (`payload_key`, `sign_key`) are no longer
in the Java code — the native library `libeboSignature.so` deobfuscates them at runtime and writes
them into Java fields (`bodyEncryptKeyS2`, `headerAccessKeySecret`). This little [Frida](https://frida.re)
script reads those fields **after** the loader has run, so you don't have to reverse the `.so`.

It reads the keys from **your own** running app and prints them to your terminal. Nothing is shipped
and nothing leaves your device. This is for making **your own** robot work with **your own** Home
Assistant — don't share the printed values (they're Enabot's app-level constants, which is exactly
why this project never ships them).

## What you need

- The **EBO HOME** app installed on an Android device or emulator you control.
- **Frida**: `pipx install frida-tools` (or `pip install frida-tools`) on your computer.
- The **frida-server** running on the device (rooted device / emulator), or a Frida-gadget build.
  See <https://frida.re/docs/android/>. USB debugging on, device visible to `frida-ls-devices`.

## Run it

Spawn the app under Frida, then **log in in the app** when it opens (that triggers the signing code
which fills the fields):

```bash
frida -U -f com.enabot.ebox.intl -l extract_keys.js
```

Or attach to the app if it's already running:

```bash
frida -U -n "EBO HOME" -l extract_keys.js
# (if -n doesn't match, list processes: frida-ps -Ua | grep -i ebo, then use -p <pid>)
```

Leave it running and log in / open your robot. Within a moment you'll see:

```
  ========================================================
  FOUND  payload_key   (AES body key)
         bodyEncryptKeyS2 = "................" (length 16)
         source: ....SignatureHelper.loadSignatureResources() -> ...
  ========================================================
```

and the same for `sign_key` (`headerAccessKeySecret`). Both are exactly **16 characters**.

## Put them in the add-on

Home Assistant → **Settings → Add-ons → EBO → Configuration**: paste the value under
`bodyEncryptKeyS2` into **`payload_key`** and the value under `headerAccessKeySecret` into
**`sign_key`**, then fill in your account email/password and save. See
[`../docs/GET-APP-KEYS.md`](../docs/GET-APP-KEYS.md) for the full walkthrough.

## If nothing prints

- Make sure you actually **logged in** (or opened the robot) in the app while the script was
  attached — the fields are filled lazily by the native loader.
- The script auto-discovers the class by its field names and re-scans for 2 minutes. If your build
  names the class differently, run `frida-ps` to confirm the app is `com.enabot.ebox.intl`, and
  check the class shows up: the script logs `candidate class holding the key fields: <name>`.
- Still stuck? Open an issue with the app version (Settings → About) — **without** pasting any key.

---

# `extract_keys_rola.js` — EBO SE keys from the **ROLA** app

For the **EBO SE**, the add-on's `se_cloud` discovery needs five values from the **ROLA** app
(package `com.enabot.ebo.intl`). They come from two places:

**The three crypto keys — read-only, via Frida (ATTACH after login, do NOT spawn):**

| add-on field | ROLA source |
|---|---|
| `se_body_key` | `SignatureHelper.bodyEncryptKeyS2` |
| `se_sign_key` | `SignatureHelper.accessKeySecretS2` |
| `se_uat_key`  | `SignatureHelper.userAuthTokenKey` |

```bash
# 1) in the emulator: open ROLA, LOG IN, open your EBO SE (this fills the key fields)
# 2) with ROLA in the foreground, attach (frontmost):
frida -U -F -l extract_keys_rola.js
# (spawning with -f can crash the app's startup checks; attach after login instead)
```

The script only *reads* the three static fields — it hooks nothing. Each 16-char key prints once.

**The two TUTK constants — plain literals, via jadx (static, no device):**

`se_license` and `se_authkey` are fixed constants compiled into the app. Decompile your own APK and
grep for them:

```bash
jadx -d /tmp/rola-src --no-res "/path/to/rola.apk" 2>/dev/null   # ~2-4 min
grep -rhoE 'TUTK_SDK_Set_License_Key\("[^"]+"\)' /tmp/rola-src/sources | head -1   # -> se_license
grep -rhoE 'authKey = "[^"]+"'                     /tmp/rola-src/sources | head -1   # -> se_authkey
```

Paste all five into the add-on's **Configuration** tab (see [`../docs/EBO-SE.md`](../docs/EBO-SE.md)).
These are Enabot's app-level constants — don't share them. The three `SignatureHelper` keys may match
your EBO HOME `payload_key`/`sign_key`; worth trying those first.
