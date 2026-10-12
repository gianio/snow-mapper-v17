#!/usr/bin/env bash
# Smoke test: install the debug APK on a running emulator/phone, launch it,
# and fail if it crashes or dies. Leaves a screenshot + log for a human to look
# at. Used by .github/workflows/android-build.yml; also works locally with a
# phone plugged in (USB debugging on):
#     bash scripts/android-smoke.sh android/app/build/outputs/apk/debug/app-debug.apk
set -u
APK="${1:?usage: android-smoke.sh path/to/app-debug.apk [out-dir]}"
OUT="${2:-smoke}"
PKG="ch.snowmapper.app"
mkdir -p "$OUT"

adb wait-for-device
# Wait for the system to finish booting, or `am start` fails at random.
for _ in $(seq 1 60); do
  [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && break
  sleep 2
done

adb install -r -g "$APK" || { echo "::error::adb install failed"; exit 1; }
adb logcat -c
adb shell am start -W -n "$PKG/.MainActivity" || { echo "::error::app did not start"; exit 1; }

# The first launch unpacks the bundled forecast and builds the map; give it time.
sleep 30
adb shell screencap -p /sdcard/smoke.png && adb pull /sdcard/smoke.png "$OUT/launch.png" >/dev/null
adb logcat -d > "$OUT/logcat.txt"

# The back button on the home screen must not crash (it replays Escape, then
# moves the app to the background).
adb shell input keyevent KEYCODE_BACK
sleep 3
adb logcat -d > "$OUT/logcat-after-back.txt"

fail=0
if grep -q "FATAL EXCEPTION" "$OUT/logcat-after-back.txt"; then
  echo "::error::the app crashed -- see logcat-after-back.txt"
  grep -A 25 "FATAL EXCEPTION" "$OUT/logcat-after-back.txt" | head -60
  fail=1
fi
if ! adb shell pidof "$PKG" >/dev/null; then
  echo "::error::$PKG is not running after launch + back"
  fail=1
fi

# Web-side errors are reported, not fatal: the map tiles / live data need the
# network and an emulator's is flaky.
echo "--- JS console errors (informational) ---"
grep -E "E Capacitor/Console|Uncaught" "$OUT/logcat.txt" | head -30 || true

[ "$fail" = 0 ] && echo "smoke test passed: installed, launched, survived back"
exit "$fail"
