/*
 * extract_keys_rola.js — read YOUR OWN ROLA app's EBO SE signing keys at runtime, with Frida.
 *
 * READ-ONLY: this does not hook or modify anything — it just reads three static string fields the
 * app's native loader fills in after you log in. (An earlier version hooked the native TUTK methods
 * and that crashed the app on spawn, so this version only polls fields and you ATTACH after login.)
 *
 *   add-on config field   source in the ROLA app
 *   ----------------------------------------------------------------
 *   se_body_key           SignatureHelper.bodyEncryptKeyS2   (AES-GCM login body)
 *   se_sign_key           SignatureHelper.accessKeySecretS2  (HMAC-SHA1 request sign)
 *   se_uat_key            SignatureHelper.userAuthTokenKey   (AES-CFB stream-start token)
 *
 * The two TUTK constants se_license / se_authkey are NOT here (they are plain string literals in
 * the app, read them from jadx — see tools/README.md).
 *
 * USAGE — ATTACH AFTER LOGIN (do not spawn):
 *   1) open ROLA in the emulator, LOG IN, and open your EBO SE (this makes the loader fill the keys)
 *   2) with ROLA in the foreground, attach:
 *        frida -U -F -l extract_keys_rola.js
 *      (or by name:  frida -U -n ROLA -l extract_keys_rola.js )
 *
 * Reads from YOUR OWN running app; nothing is shipped or sent anywhere. Don't share the values —
 * paste them into the add-on's Configuration tab only.
 */
'use strict';

var WANT = {
  bodyEncryptKeyS2:  'se_body_key',
  accessKeySecretS2: 'se_sign_key',
  userAuthTokenKey:  'se_uat_key'
};
var seen = {};

function tryRead() {
  var S;
  try { S = Java.use('com.enabot.lib.signature.SignatureHelper'); }
  catch (e) { return false; }           // class not loaded yet — keep polling
  var all = true;
  Object.keys(WANT).forEach(function (f) {
    if (seen[f]) return;
    var v = '';
    try { var h = S[f]; if (h && h.value !== undefined && h.value !== null) v = '' + h.value; } catch (e) {}
    if (v.length) {
      seen[f] = true;
      console.log('');
      console.log('  ========================================================');
      console.log('  ' + WANT[f] + '  =  "' + v + '"   (' + v.length + ' chars)');
      console.log('         [' + f + ']');
      console.log('  ========================================================');
    } else {
      all = false;
    }
  });
  return all;
}

Java.perform(function () {
  console.log('[*] attached. Reading SignatureHelper keys — if blank, LOG IN + open your EBO SE now.');
  var n = 0;
  var timer = setInterval(function () {
    n++;
    var done = tryRead();
    if (done) {
      console.log('\n[*] all three keys found. Paste them into the add-on config, then press Ctrl-D / quit.');
      clearInterval(timer);
    } else if (n >= 120) {              // ~3 minutes
      console.log('\n[!] gave up after 3 min. Make sure you logged in; then re-attach and try again.');
      clearInterval(timer);
    }
  }, 1500);
});
