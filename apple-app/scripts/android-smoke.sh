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

# CI emulators sometimes drop off adb for a few seconds ("device offline").
# That is the harness, not the app: reconnect and retry before judging.
device_back() {
  adb reconnect offline >/dev/null 2>&1
  timeout 90 adb wait-for-device >/dev/null 2>&1
}
a() {  # adb with one reconnect-and-retry; never waits forever for a lost device
  timeout 180 adb "$@" && return 0
  adb get-state 2>/dev/null | grep -q device && return 1
  echo "(adb lost the device -- reconnecting)"
  # bounded: a device that never comes back must end the run with the
  # "emulator is gone" message below, not hang until the job times out
  device_back && timeout 120 adb "$@"
}

adb wait-for-device
# Wait for the system to finish booting, or `am start` fails at random.
for _ in $(seq 1 60); do
  [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && break
  sleep 2
done

a install -r -g "$APK" || { echo "::error::adb install failed"; exit 1; }
a logcat -c
# stream the log to a file from the start: when the emulator itself dies (it
# has, about a minute after launch), `logcat -d` afterwards gets nothing, and
# this file is the only record of what the app and the GPU were doing
( adb logcat -v time > "$OUT/logcat-stream.txt" 2>&1 & echo $! > "$OUT/.logcat.pid" )
( while sleep 5; do adb shell dumpsys meminfo "$PKG" 2>/dev/null | grep -E "TOTAL|Graphics|GL mtrack" | head -3 | sed "s/^/[$(date +%T)] /"; done > "$OUT/meminfo.txt" 2>&1 & echo $! > "$OUT/.mem.pid" )
a shell am start -W -n "$PKG/.MainActivity" || { echo "::error::app did not start"; exit 1; }

# The first launch unpacks the bundled forecast and builds the map; give it time.
sleep 30
a shell screencap -p /sdcard/smoke.png && a pull /sdcard/smoke.png "$OUT/launch.png" >/dev/null
a logcat -d > "$OUT/logcat.txt"

# The back button on the home screen must not crash (it replays Escape, then
# moves the app to the background).
a shell input keyevent KEYCODE_BACK
sleep 3
a logcat -d > "$OUT/logcat-after-back.txt"

if ! adb get-state 2>/dev/null | grep -q device; then
  echo "::error::the emulator is gone (adb: device offline) -- an infrastructure failure, not an app result"
  echo "--- last lines streamed before it went (logcat-stream.txt) ---"
  tail -n 60 "$OUT/logcat-stream.txt" 2>/dev/null | grep -vE "^\s*$" | tail -n 60
  echo "--- app memory (meminfo.txt) ---"; tail -n 12 "$OUT/meminfo.txt" 2>/dev/null
  for f in "$OUT/.logcat.pid" "$OUT/.mem.pid"; do kill "$(cat "$f" 2>/dev/null)" 2>/dev/null; rm -f "$f"; done
  exit 1
fi

fail=0
# Only our own process counts: the emulator's Google Play services crash on
# their own now and then (CrisisAlerts in gms.persistent), which says nothing
# about the app. The line after "FATAL EXCEPTION" names the process.
if grep -A 1 "FATAL EXCEPTION" "$OUT/logcat-after-back.txt" | grep -q "Process: $PKG,"; then
  echo "::error::the app crashed -- see logcat-after-back.txt"
  grep -A 25 "FATAL EXCEPTION" "$OUT/logcat-after-back.txt" | head -60
  fail=1
elif grep -q "FATAL EXCEPTION" "$OUT/logcat-after-back.txt"; then
  echo "::warning::another process crashed in the emulator (not $PKG) -- ignored"
fi
if ! a shell pidof "$PKG" >/dev/null; then
  echo "::error::$PKG is not running after launch + back"
  fail=1
fi

# Web-side errors are reported, not fatal: the map tiles / live data need the
# network and an emulator's is flaky.
echo "--- JS console errors (informational) ---"
grep -E "E Capacitor/Console|Uncaught" "$OUT/logcat.txt" | head -30 || true

for f in "$OUT/.logcat.pid" "$OUT/.mem.pid"; do kill "$(cat "$f" 2>/dev/null)" 2>/dev/null; rm -f "$f"; done
echo "--- app memory (meminfo.txt) ---"; tail -n 6 "$OUT/meminfo.txt" 2>/dev/null || true
[ "$fail" = 0 ] && echo "smoke test passed: installed, launched, survived back"
exit "$fail"
