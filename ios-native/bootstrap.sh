#!/usr/bin/env bash
#
# Snowmapper native iOS — one-command setup for a Mac.
#
#   cd ios-native && ./bootstrap.sh
#
# Does everything needed to get from a fresh clone to an open Xcode project:
#   1. checks the tools it needs and says exactly how to install any that are missing
#   2. generates Resources/engine.js  (stdlib python only — no numpy/GDAL needed)
#   3. generates Snowmapper.xcodeproj from project.yml via XcodeGen
#   4. opens it (unless --no-open)
#
# Deliberately NOT required: an Apple Developer account. Building and running on
# the simulator needs none; only shipping to a device or TestFlight does.
set -euo pipefail

cd "$(dirname "$0")"
APP_DIR="$PWD"
REPO_ROOT="$(cd .. && pwd)"
OPEN_XCODE=1
[[ "${1:-}" == "--no-open" ]] && OPEN_XCODE=0

info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
fail()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- prerequisites
info "Checking prerequisites"

[[ "$(uname -s)" == "Darwin" ]] || fail "This needs macOS (Xcode only exists there).
     On Linux the CI workflow .github/workflows/swift-build.yml does the equivalent."

if ! xcode-select -p >/dev/null 2>&1; then
  fail "Xcode command line tools not found. Install Xcode from the App Store, then:
     sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer"
fi

# A Command Line Tools-only install cannot build iOS apps; it has no iOS SDK.
if ! xcrun --sdk iphoneos --show-sdk-path >/dev/null 2>&1; then
  fail "No iOS SDK found — you likely have only the Command Line Tools, not full Xcode.
     Install Xcode from the App Store, then:
     sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer"
fi
info "  Xcode:      $(xcodebuild -version | head -1)"
info "  iOS SDK:    $(xcrun --sdk iphoneos --show-sdk-version)"
info "  Swift:      $(swift --version 2>/dev/null | head -1)"

if ! command -v xcodegen >/dev/null 2>&1; then
  warn "xcodegen not installed. Installing via Homebrew…"
  command -v brew >/dev/null 2>&1 || fail "Homebrew not found. Install it from https://brew.sh
     then re-run, or install XcodeGen another way: https://github.com/yonaskolb/XcodeGen"
  brew install xcodegen
fi
info "  XcodeGen:   $(xcodegen --version 2>&1 | head -1)"

PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || fail "python3 not found (macOS ships one; or: brew install python)"
info "  Python:     $("$PYTHON" --version 2>&1)"

# ------------------------------------------------------------------- engine.js
# The SAME scoring engine the web app uses and that CI validates, so the physics
# cannot diverge between platforms. Generated, never committed.
info "Generating Resources/engine.js"
mkdir -p Resources
"$PYTHON" "$REPO_ROOT/tools/make_engine.py" -o "$APP_DIR/Resources/engine.js"

# Cheap sanity check: if node is around, run the engine's own suite so a broken
# engine is caught here rather than as a puzzling test failure inside Xcode.
if command -v node >/dev/null 2>&1; then
  info "Validating engine.js with tools/test_engine.js"
  node "$REPO_ROOT/tools/test_engine.js" "$APP_DIR/Resources/engine.js" | tail -2
else
  warn "node not found — skipping engine self-test (optional)"
fi

# ----------------------------------------------------------------- xcode project
info "Generating Snowmapper.xcodeproj from project.yml"
xcodegen generate --spec project.yml

cat <<'NEXT'

──────────────────────────────────────────────────────────────────────
Ready.

Run the tests from the command line (no signing needed):

  xcodebuild test \
    -project ios-native/Snowmapper.xcodeproj \
    -scheme Snowmapper \
    -destination 'platform=iOS Simulator,name=iPhone 16'

Or press ▶ in Xcode. The first screen is a dev harness, on purpose:

  • Engine    — should read 0.982 / 96%, the same values CI asserts.
                If it differs, the JavaScriptCore bridge is wrong, not the model.
  • Haptics   — tap each one and FEEL it. The intensity/sharpness values in
                HapticEngine.swift are untuned guesses; this is where you fix them.
  • Draw      — needs a real iPad + Apple Pencil. No simulator produces
                pressure or tilt, so the draw tool cannot be judged on one.

To ship to a device or TestFlight you then need an Apple Developer account
and a signing team, set in Xcode under Signing & Capabilities.
──────────────────────────────────────────────────────────────────────
NEXT
[[ $OPEN_XCODE -eq 1 ]] && { info "Opening Xcode"; open Snowmapper.xcodeproj; }
