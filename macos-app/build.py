"""Build a local native macOS app with ClientProbe and guarded Accessibility DesktopDriver."""
from pathlib import Path
import plistlib
import shutil
import struct
import subprocess
import sys

p = Path(__file__).resolve().parents[1]
if str(p) not in sys.path:
    sys.path.insert(0, str(p))
from agent_hub import __version__

a = p / "macos-app"
subprocess.run(
    ["/usr/bin/clang", "-fobjc-arc", "-framework", "Cocoa", "-framework", "WebKit", "-mmacosx-version-min=11.0", str(a / "main.m"), "-o", str(a / "AgentHub")],
    check=True,
)
subprocess.run(["/usr/bin/clang", "-fobjc-arc", "-framework", "Cocoa", str(a / "icon.m"), "-o", str(a / "make-icon")], check=True)
subprocess.run([str(a / "make-icon"), str(a / "AgentHub.iconset")], check=True)
# Modern ICNS entries contain PNG payloads; avoid iconutil's environment-dependent conversion.
chunks = []
for tag, name in [
    ("icp4", "icon_16x16.png"),
    ("icp5", "icon_32x32.png"),
    ("icp6", "icon_32x32@2x.png"),
    ("ic07", "icon_128x128.png"),
    ("ic08", "icon_256x256.png"),
    ("ic09", "icon_512x512.png"),
    ("ic10", "icon_512x512@2x.png"),
]:
    data = (a / "AgentHub.iconset" / name).read_bytes()
    chunks.append(tag.encode() + struct.pack(">I", len(data) + 8) + data)
payload = b"".join(chunks)
(a / "AgentHub.icns").write_bytes(b"icns" + struct.pack(">I", len(payload) + 8) + payload)

b = a / "本地Agent工作台.app"
c = b / "Contents"
r = c / "Resources"
s = r / "server"
(c / "MacOS").mkdir(parents=True, exist_ok=True)
s.mkdir(parents=True, exist_ok=True)
shutil.copy2(a / "AgentHub", c / "MacOS" / "AgentHub")
shutil.copy2(a / "AgentHub.icns", r / "AgentHub.icns")
if (s / "agent_hub").exists():
    shutil.rmtree(s / "agent_hub")
shutil.copytree(p / "agent_hub", s / "agent_hub", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
shutil.copy2(a / "launch_background.py", s / "launch_background.py")

subprocess.run(
    ["/usr/bin/clang", "-fobjc-arc", "-framework", "Cocoa", "-mmacosx-version-min=11.0", str(a / "client_probe.m"), "-o", str(a / "ClientProbe")],
    check=True,
)
shutil.copy2(a / "ClientProbe", r / "ClientProbe")
subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(r / "ClientProbe")], check=True)

subprocess.run(
    [
        "/usr/bin/clang",
        "-fobjc-arc",
        "-framework",
        "Cocoa",
        "-framework",
        "ApplicationServices",
        "-mmacosx-version-min=11.0",
        str(a / "desktop_driver.m"),
        "-o",
        str(a / "DesktopDriver"),
    ],
    check=True,
)
shutil.copy2(a / "DesktopDriver", r / "DesktopDriver")
subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(r / "DesktopDriver")], check=True)

info = {
    "CFBundleIdentifier": "local.agenthub.desktop",
    "CFBundleName": "本地Agent工作台",
    "CFBundleDisplayName": "本地 Agent 工作台",
    "CFBundleExecutable": "AgentHub",
    "CFBundlePackageType": "APPL",
    "CFBundleShortVersionString": __version__,
    "CFBundleVersion": "7",
    "CFBundleIconFile": "AgentHub",
    "LSMinimumSystemVersion": "11.0",
    "NSHighResolutionCapable": True,
    "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    # Portable tilde path expanded at runtime — never hardcode a local username.
    "HubWorkspace": "~/Library/Application Support/LocalAgentHub/workspace",
}
with (c / "Info.plist").open("wb") as f:
    plistlib.dump(info, f)
subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(b)], check=True)
subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(b)], check=True)
print(b)
