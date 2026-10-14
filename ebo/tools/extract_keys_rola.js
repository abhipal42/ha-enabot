/*
 * extract_keys_rola.js — read YOUR OWN ROLA app's EBO SE keys at runtime, with Frida.
 *
 * Prints the five values the add-on's `se_cloud` discovery needs, straight from your own running
 * app on your own phone. Nothing is shipped and nothing leaves your device — this just reads values
 * the app already holds, the same way tools/extract_keys.js does for the EBO HOME app.
 *
 *   add-on config field   source in the ROLA app                 what for
 *   ------------------------------------------------------------------------------------------
 *   se_body_key           SignatureHelper.bodyEncryptKeyS2        encrypt the login body (AES-GCM)
 *   se_sign_key           SignatureHelper.accessKeySecretS2       sign requests (HMAC-SHA1)
 *   se_uat_key            SignatureHelper.userAuthTokenKey        stream-start token (AES-CFB)
 *   se_license            TUTK_SDK_Set_License_Key(arg)           TUTK license (constant)
 *   se_authkey            St_IOTCConnectInput.authKey             TUTK connect authKey (constant)
 *
 * The first three are filled by the native loader (libeboSignature.so) after login; the last two
 * are read live when the app connects to your robot. So: start the script, LOG IN, then OPEN your
 * EBO SE in the app — each value prints once as the app uses it.
 *
 * USAGE (needs a rooted device/emulator + Frida server; see tools/README.md):
 *     frida -U -f com.enabot.ebo.intl -l extract_keys_rola.js        # spawn, then log in + open SE
 *     frida -U -n ROLA -l extract_keys_rola.js                        # or attach to the running app
 *
 * Do NOT share the printed values — they are Enabot's app-level constants, the same reason this
 * project never ships them. Paste them into the add-on's Configuration tab only.
 */
'use strict';

var seen = {};

function show(field, value, where) {
  if (value === null || value === undefined) return;
  var v = '' + value;
  if (!v.length || seen[field] === v) return;
  seen[field] = v;
  console.log('');
  console.log('  ==========================================================');
  console.log('  FOUND  ' + field + ' = "' + v + '"   (length ' + v.length + ')');
  console.log('         source: ' + where);
  console.log('  ==========================================================');
  console.log('');
}

Java.perform(function () {
  // 1) the three SignatureHelper keys (static fields filled by the native loader after login)
  var WANT = ['bodyEncryptKeyS2', 'accessKeySecretS2', 'userAuthTokenKey'];
  function readSig(where) {
    try {
      var S = Java.use('com.enabot.lib.signature.SignatureHelper');
      WANT.forEach(function (f) {
        try { var h = S[f]; if (h && h.value !== undefined) show(f, h.value, where); } catch (e) {}
      });
    } catch (e) {}
  }
  readSig('SignatureHelper (at start)');
  // re-read right after the loader runs (values appear late, once you log in)
  try {
    var S = Java.use('com.enabot.lib.signature.SignatureHelper');
    ['loadSingleResources', 'loadSignatureResources', 'updateSignature'].forEach(function (m) {
      if (!S[m]) return;
      S[m].overloads.forEach(function (ov) {
        ov.implementation = function () {
          var r = ov.apply(this, arguments);
          readSig('SignatureHelper.' + m + '()');
          return r;
        };
      });
    });
  } catch (e) { console.log('[!] SignatureHelper not loaded yet — log in to trigger it'); }

  // 2) se_license — the argument to TUTK_SDK_Set_License_Key(String)
  try {
    var G = Java.use('com.tutk.IOTC.TUTKGlobalAPIs');
    G.TUTK_SDK_Set_License_Key.overloads.forEach(function (ov) {
      ov.implementation = function (lic) {
        show('se_license (TUTK license)', lic, 'TUTK_SDK_Set_License_Key()');
        return ov.apply(this, arguments);
      };
    });
  } catch (e) {}

  // 3) se_authkey — St_IOTCConnectInput.authKey passed to IOTC_Connect_ByUIDEx(uid, slot, input)
  try {
    var I = Java.use('com.tutk.IOTC.IOTCAPIs');
    I.IOTC_Connect_ByUIDEx.overloads.forEach(function (ov) {
      ov.implementation = function () {
        try {
          var input = arguments[2];
          if (input && input.authKey && input.authKey.value) {
            show('se_authkey (TUTK authKey)', input.authKey.value, 'IOTC_Connect_ByUIDEx()');
          }
        } catch (e) {}
        return ov.apply(this, arguments);
      };
    });
  } catch (e) {}

  console.log('[*] ready — now LOG IN in the app, then OPEN your EBO SE. Values print as they appear.');
});
