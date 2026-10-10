#!/usr/bin/env python3
"""Prepare the generated Xcode project for a signed App Store build in CI.

`npx cap add ios` generates ios/App/App.xcodeproj with automatic signing and
no team. On a CI Mac there is no Xcode account to sign automatically with, so
the App target is switched to manual signing with the distribution profile
that the workflow installed. It also sets:

  * the version (CFBundleShortVersionString, e.g. 1.0.0) and the build number
    (CFBundleVersion) -- App Store Connect refuses a second upload with the
    same build number, so CI passes its run number;
  * iPhone only (TARGETED_DEVICE_FAMILY = 1): the app is portrait-only, and an
    iPad build that does not support every orientation is rejected at upload.

Only the App project is edited (the CocoaPods targets live in Pods.xcodeproj
and must keep signing off), and only the build configurations that carry the
app's bundle identifier.

Usage (from apple-app/):
    python3 scripts/ci-signing.py --team ABCDE12345 --profile "Snowmapper AppStore" \
        --version 1.0.0 --build 42
"""
import argparse
import plistlib
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
PBXPROJ = APP / "ios" / "App" / "App.xcodeproj" / "project.pbxproj"
INFO_PLIST = APP / "ios" / "App" / "App" / "Info.plist"
BUNDLE_ID = "ch.snowmapper.app"


def q(v: str) -> str:
    """pbxproj value: quoted unless it is a plain token."""
    return v if re.fullmatch(r"[A-Za-z0-9_.$/]+", v) else '"' + v.replace('"', '\\"') + '"'


def patch_pbxproj(text: str, team: str, profile: str, version: str, build: str) -> tuple:
    want = {
        "CODE_SIGN_STYLE": "Manual",
        "DEVELOPMENT_TEAM": team,
        "PROVISIONING_PROFILE_SPECIFIER": profile,
        '"CODE_SIGN_IDENTITY[sdk=iphoneos*]"': "Apple Distribution",
        "CODE_SIGN_IDENTITY": "Apple Distribution",
        "CURRENT_PROJECT_VERSION": build,
        "MARKETING_VERSION": version,
        "TARGETED_DEVICE_FAMILY": "1",
    }
    n = 0

    def fix(block: str) -> str:
        nonlocal n
        if BUNDLE_ID not in block and "PRODUCT_BUNDLE_IDENTIFIER" not in block:
            return block
        for key, val in want.items():
            line = f"{key} = {q(val)};"
            pat = re.compile(r"^(\s*)" + re.escape(key) + r" = [^;]*;", re.M)
            if pat.search(block):
                block = pat.sub(lambda m: m.group(1) + line, block, count=1)
            else:
                block = re.sub(r"(buildSettings = \{\n)(\s*)", lambda m: m.group(1) + m.group(2) + line + "\n" + m.group(2), block, count=1)
        n += 1
        return block

    out = re.sub(r"buildSettings = \{\n.*?\n\s*\};", lambda m: fix(m.group(0)), text, flags=re.S)
    return out, n


def patch_info_plist(path: Path, version: str, build: str) -> None:
    with path.open("rb") as fh:
        info = plistlib.load(fh)
    info["CFBundleShortVersionString"] = version
    info["CFBundleVersion"] = build
    # No custom cryptography (HTTPS only): answers TestFlight's export
    # compliance question up front, so the build is testable at once.
    info["ITSAppUsesNonExemptEncryption"] = False
    with path.open("wb") as fh:
        plistlib.dump(info, fh, sort_keys=False)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--team", required=True)
    ap.add_argument("--profile", required=True, help="name of the App Store provisioning profile")
    ap.add_argument("--version", required=True)
    ap.add_argument("--build", required=True)
    ap.add_argument("--pbxproj", default=str(PBXPROJ))
    ap.add_argument("--info", default=str(INFO_PLIST))
    a = ap.parse_args(argv)
    pbx = Path(a.pbxproj)
    text, n = patch_pbxproj(pbx.read_text(encoding="utf-8"), a.team, a.profile, a.version, a.build)
    if not n:
        print("::error::no build configuration with the app's bundle id found in project.pbxproj")
        return 1
    pbx.write_text(text, encoding="utf-8")
    patch_info_plist(Path(a.info), a.version, a.build)
    print(f"signing: manual, team {a.team}, profile '{a.profile}' in {n} configuration(s); "
          f"version {a.version} ({a.build}); iPhone only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
