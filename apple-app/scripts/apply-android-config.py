#!/usr/bin/env python3
"""Apply the Android-specific config into the generated native project.

The Android twin of apply-ios-config.py + ci-signing.py. `npx cap add android`
writes a stock project; like ios/, android/ is NOT committed -- it is generated
from capacitor.config.ts on every build, so this script is where everything
Android-only lives. Idempotent:

  1. AndroidManifest.xml
     * location permissions (the map's "where am I" + tour recording; the
       background-geolocation plugin merges its own foreground-service ones)
     * portrait only, matching the iOS build (UISupportedInterfaceOrientations)
     No CAMERA / READ_MEDIA_* on purpose: the photo <input> goes through the
     system camera app and the Android photo picker, which need neither -- and
     declaring them would add a Play "photo and video permissions" review.
  2. app/build.gradle
     * versionName (e.g. 1.0.0, same as the iOS CFBundleShortVersionString)
       and versionCode (must grow with every Play upload -- CI passes its run
       number, exactly like the iOS build number)
     * release signing from environment variables, so the upload key never
       touches the repo:
         ANDROID_KEYSTORE_FILE, ANDROID_KEYSTORE_PASSWORD,
         ANDROID_KEY_ALIAS, ANDROID_KEY_PASSWORD
       Unset -> release builds are unsigned (debug builds are always signed
       with the debug key and install on any phone).

Run from apple-app/ after `npx cap sync android`:
    python3 scripts/apply-android-config.py [--version 1.0.0] [--build 42]
"""
import argparse
import json
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
MANIFEST = APP / "android" / "app" / "src" / "main" / "AndroidManifest.xml"
GRADLE = APP / "android" / "app" / "build.gradle"

PERMISSIONS = (
    "android.permission.ACCESS_COARSE_LOCATION",
    "android.permission.ACCESS_FINE_LOCATION",
)

SIGNING_BLOCK = """    // Release signing from the environment (CI secrets / your shell); see
    // scripts/apply-android-config.py. Unset -> unsigned release build.
    signingConfigs {
        release {
            if (System.getenv('ANDROID_KEYSTORE_FILE')) {
                storeFile file(System.getenv('ANDROID_KEYSTORE_FILE'))
                storePassword System.getenv('ANDROID_KEYSTORE_PASSWORD')
                keyAlias System.getenv('ANDROID_KEY_ALIAS')
                keyPassword System.getenv('ANDROID_KEY_PASSWORD')
            }
        }
    }
"""


def patch_manifest(text: str) -> tuple:
    changes = []
    for perm in PERMISSIONS:
        if f'android:name="{perm}"' in text:
            continue
        text = text.replace(
            "</manifest>",
            f'    <uses-permission android:name="{perm}" />\n</manifest>', 1)
        changes.append(perm.rsplit(".", 1)[1])

    m = re.search(r'<activity\b[^>]*android:name="\.MainActivity"[^>]*>', text, re.S)
    if not m:
        raise SystemExit("error: MainActivity not found in AndroidManifest.xml")
    tag = m.group(0)
    if "android:screenOrientation" not in tag:
        new_tag = tag.replace('android:name=".MainActivity"',
                              'android:name=".MainActivity"\n'
                              '            android:screenOrientation="portrait"', 1)
        text = text[:m.start()] + new_tag + text[m.end():]
        changes.append("portrait")
    return text, changes


def patch_gradle(text: str, version: str, build: str) -> tuple:
    changes = []
    new, n = re.subn(r"versionCode\s+\d+", f"versionCode {int(build)}", text, count=1)
    if n != 1:
        raise SystemExit("error: versionCode not found in app/build.gradle")
    new, n = re.subn(r'versionName\s+"[^"]*"', f'versionName "{version}"', new, count=1)
    if n != 1:
        raise SystemExit("error: versionName not found in app/build.gradle")
    if new != text:
        changes.append(f"version {version} ({build})")
    text = new

    if "signingConfigs" not in text:
        new = text.replace("    buildTypes {", SIGNING_BLOCK + "    buildTypes {", 1)
        new, n = re.subn(
            r"(buildTypes\s*\{\s*release\s*\{\n)",
            r"\1            if (System.getenv('ANDROID_KEYSTORE_FILE')) {\n"
            r"                signingConfig signingConfigs.release\n"
            r"            }\n",
            new, count=1)
        if n != 1 or new == text:
            raise SystemExit("error: buildTypes.release not found in app/build.gradle")
        text = new
        changes.append("release signing from env")
    return text, changes


def main() -> int:
    pkg = json.loads((APP / "package.json").read_text(encoding="utf-8"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=pkg.get("version", "1.0.0"),
                    help="versionName (default: package.json version)")
    ap.add_argument("--build", default="1",
                    help="versionCode, a positive integer that grows with every upload")
    args = ap.parse_args()
    if not args.build.isdigit() or int(args.build) < 1:
        print(f"error: --build must be a positive integer, got {args.build!r}", file=sys.stderr)
        return 1

    for f in (MANIFEST, GRADLE):
        if not f.exists():
            print(f"error: {f} not found -- run `npx cap add android` first.", file=sys.stderr)
            return 1

    text, ch = patch_manifest(MANIFEST.read_text(encoding="utf-8"))
    MANIFEST.write_text(text, encoding="utf-8")
    print(f"AndroidManifest.xml: {', '.join(ch) if ch else 'already configured'}")

    text, ch = patch_gradle(GRADLE.read_text(encoding="utf-8"), args.version, args.build)
    GRADLE.write_text(text, encoding="utf-8")
    print(f"app/build.gradle: {', '.join(ch) if ch else 'already configured'}")
    print("Android config applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
