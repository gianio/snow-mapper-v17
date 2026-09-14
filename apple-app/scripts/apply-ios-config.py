#!/usr/bin/env python3
"""Apply the iOS-specific config into the generated native project.

`npx cap add ios` writes a stock Info.plist with none of the purpose strings
Apple requires, and no privacy manifest. The README used to tell you to paste
both in by hand, which is fine once on your own Mac but impossible in CI and
easy to forget on a re-generated project. This does it idempotently:

  1. merges every key from ios-config/Info.plist.additions.xml into
     ios/App/App/Info.plist (existing keys are left alone, so hand edits win)
  2. copies ios-config/PrivacyInfo.xcprivacy next to it and registers the file
     in the Xcode project so it actually ships in the bundle

Run from apple-app/ after `npx cap sync ios`:
    python3 scripts/apply-ios-config.py
"""
import plistlib
import re
import shutil
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
SRC_ADDITIONS = APP / "ios-config" / "Info.plist.additions.xml"
SRC_PRIVACY = APP / "ios-config" / "PrivacyInfo.xcprivacy"
IOS_APP = APP / "ios" / "App" / "App"
INFO_PLIST = IOS_APP / "Info.plist"
PBXPROJ = APP / "ios" / "App" / "App.xcodeproj" / "project.pbxproj"


def parse_additions(text: str) -> dict:
    """Read the <key>/<value> fragment file into a dict.

    The file is a bare fragment (no <plist> root) so it can be pasted straight
    into Info.plist by hand; wrap it to make it parseable.
    """
    body = re.sub(r"<\?xml[^>]*\?>", "", text)
    body = re.sub(r"<!DOCTYPE[^>]*>", "", body)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    wrapped = ('<?xml version="1.0" encoding="UTF-8"?>'
               '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
               '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
               f"<plist version=\"1.0\"><dict>{body}</dict></plist>")
    return plistlib.loads(wrapped.encode("utf-8"))


def merge_info_plist() -> int:
    additions = parse_additions(SRC_ADDITIONS.read_text(encoding="utf-8"))
    with INFO_PLIST.open("rb") as fh:
        info = plistlib.load(fh)
    added = []
    for key, value in additions.items():
        if key in info and info[key] == value:
            continue
        if key in info:
            print(f"  keep existing {key} (already set to something else)")
            continue
        info[key] = value
        added.append(key)
    if added:
        with INFO_PLIST.open("wb") as fh:
            plistlib.dump(info, fh, sort_keys=False)
    print(f"Info.plist: added {len(added)} key(s){': ' + ', '.join(added) if added else ''}")
    return len(added)


def install_privacy_manifest() -> None:
    dest = IOS_APP / "PrivacyInfo.xcprivacy"
    shutil.copyfile(SRC_PRIVACY, dest)
    print(f"copied {dest.relative_to(APP)}")

    # Register it in the Xcode project so it is bundled, not just on disk.
    pbx = PBXPROJ.read_text(encoding="utf-8")
    if "PrivacyInfo.xcprivacy" in pbx:
        print("project.pbxproj: already references PrivacyInfo.xcprivacy")
        return

    file_ref = "AA00PRIV0001"
    build_file = "AA00PRIV0002"
    pbx = pbx.replace(
        "/* End PBXFileReference section */",
        f'\t\t{file_ref} /* PrivacyInfo.xcprivacy */ = {{isa = PBXFileReference; '
        'lastKnownFileType = text.xml; path = PrivacyInfo.xcprivacy; '
        'sourceTree = "<group>"; };\n'
        "/* End PBXFileReference section */", 1)
    pbx = pbx.replace(
        "/* End PBXBuildFile section */",
        f'\t\t{build_file} /* PrivacyInfo.xcprivacy in Resources */ = {{isa = PBXBuildFile; '
        f'fileRef = {file_ref} /* PrivacyInfo.xcprivacy */; }};\n'
        "/* End PBXBuildFile section */", 1)

    # Add to the App target's Resources build phase (the one containing the
    # generated public/ web assets) and to the App group.
    m = re.search(r"(isa = PBXResourcesBuildPhase;.*?files = \(\n)", pbx, re.S)
    if m:
        pbx = pbx[:m.end(1)] + f"\t\t\t\t{build_file} /* PrivacyInfo.xcprivacy in Resources */,\n" + pbx[m.end(1):]
    else:
        print("  WARNING: no Resources build phase found — add the file in Xcode manually")

    m = re.search(r"(/\* App \*/ = \{\n\s*isa = PBXGroup;\n\s*children = \(\n)", pbx)
    if m:
        pbx = pbx[:m.end(1)] + f"\t\t\t\t{file_ref} /* PrivacyInfo.xcprivacy */,\n" + pbx[m.end(1):]

    PBXPROJ.write_text(pbx, encoding="utf-8")
    print("project.pbxproj: registered PrivacyInfo.xcprivacy")


def main() -> int:
    if not INFO_PLIST.exists():
        print(f"error: {INFO_PLIST} not found — run `npx cap add ios` first.", file=sys.stderr)
        return 1
    for src in (SRC_ADDITIONS, SRC_PRIVACY):
        if not src.exists():
            print(f"error: missing {src}", file=sys.stderr)
            return 1
    merge_info_plist()
    install_privacy_manifest()
    print("iOS config applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
