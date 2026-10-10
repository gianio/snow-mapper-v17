"""Offline test for apple-app/scripts/ci-signing.py on a Capacitor-style pbxproj."""
import importlib.util
import plistlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ci_signing", ROOT / "apple-app" / "scripts" / "ci-signing.py")
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)

fails = []
def check(name, ok, detail=""):
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)

PBX = """// !$*UTF8*$!
{
/* Begin XCBuildConfiguration section */
		504EC3141FED79650016851F /* Debug */ = {
			isa = XCBuildConfiguration;
			buildSettings = {
				ALWAYS_SEARCH_USER_PATHS = NO;
				SDKROOT = iphoneos;
			};
			name = Debug;
		};
		504EC3171FED79650016851F /* Debug */ = {
			isa = XCBuildConfiguration;
			buildSettings = {
				ASSETCATALOG_COMPILER_APPICON_NAME = AppIcon;
				CODE_SIGN_STYLE = Automatic;
				CURRENT_PROJECT_VERSION = 1;
				INFOPLIST_FILE = App/Info.plist;
				MARKETING_VERSION = 1.0;
				PRODUCT_BUNDLE_IDENTIFIER = ch.snowmapper.app;
				TARGETED_DEVICE_FAMILY = "1,2";
			};
			name = Debug;
		};
		504EC3181FED79650016851F /* Release */ = {
			isa = XCBuildConfiguration;
			buildSettings = {
				CODE_SIGN_STYLE = Automatic;
				INFOPLIST_FILE = App/Info.plist;
				PRODUCT_BUNDLE_IDENTIFIER = ch.snowmapper.app;
			};
			name = Release;
		};
/* End XCBuildConfiguration section */
}
"""
out, n = cs.patch_pbxproj(PBX, "ABCDE12345", "Snowmapper AppStore", "1.0.0", "42")
check("both app configurations patched, project-level one left alone", n == 2, str(n))
check("manual signing", out.count("CODE_SIGN_STYLE = Manual;") == 2 and "Automatic" not in out)
check("team and quoted profile name", out.count("DEVELOPMENT_TEAM = ABCDE12345;") == 2
      and out.count('PROVISIONING_PROFILE_SPECIFIER = "Snowmapper AppStore";') == 2)
check("distribution identity", out.count('"CODE_SIGN_IDENTITY[sdk=iphoneos*]" = "Apple Distribution";') == 2)
check("build number and version", out.count("CURRENT_PROJECT_VERSION = 42;") == 2 and out.count("MARKETING_VERSION = 1.0.0;") == 2)
check("iPhone only", out.count("TARGETED_DEVICE_FAMILY = 1;") == 2 and '"1,2"' not in out)
first = out.split("504EC3171FED79650016851F")[0]
check("project-level config untouched", "DEVELOPMENT_TEAM" not in first)
out2, _ = cs.patch_pbxproj(out, "ABCDE12345", "Snowmapper AppStore", "1.0.0", "43")
check("idempotent (no duplicate keys on a second run)", out2.count("DEVELOPMENT_TEAM") == 2 and "= 43;" in out2)

with tempfile.TemporaryDirectory() as d:
    ip = Path(d) / "Info.plist"
    ip.write_bytes(plistlib.dumps({"CFBundleVersion": "$(CURRENT_PROJECT_VERSION)", "CFBundleDisplayName": "Snowmapper"}))
    cs.patch_info_plist(ip, "1.0.0", "42")
    info = plistlib.loads(ip.read_bytes())
    check("Info.plist version/build/encryption", info["CFBundleShortVersionString"] == "1.0.0"
          and info["CFBundleVersion"] == "42" and info["ITSAppUsesNonExemptEncryption"] is False
          and info["CFBundleDisplayName"] == "Snowmapper")

print("\nCI SIGNING OK" if not fails else f"\n{len(fails)} FAILED")
sys.exit(1 if fails else 0)
