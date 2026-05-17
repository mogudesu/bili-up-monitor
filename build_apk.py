#!/usr/bin/env python3
"""Build APK with cleartext HTTP fix and Flask 3.0 + Werkzeug 3.x upgrade.

Buildozer's android.usesCleartextTraffic doesn't reliably inject
the manifest attribute and network security config on all versions.
Also, python-for-android ignores version pins in buildozer.spec,
so we must manually replace Flask/Werkzeug after the build.

This script:
  1. Syncs src/ to the WSL build directory
  2. Runs buildozer
  3. Replaces Flask 2.x + Werkzeug 2.x with Flask 3.0 + Werkzeug 3.x
     in the python bundle (fixes url_quote ImportError)
  4. Patches AndroidManifest.xml + copies network_security_config.xml
  5. Re-runs just the gradle assembleDebug step
  6. Copies the final APK to dist/
"""

import subprocess
import sys
import os
import hashlib

WSL_BUILD_DIR = "/home/mogu/bilibili-monitor-build"
DIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")
MANIFEST_REL = ".buildozer/android/platform/build-arm64-v8a/dists/bilibilimonitor/src/main/AndroidManifest.xml"
RES_XML_REL = ".buildozer/android/platform/build-arm64-v8a/dists/bilibilimonitor/src/main/res/xml"
GRADLE_REL = ".buildozer/android/platform/build-arm64-v8a/dists/bilibilimonitor"
APK_REL = ".buildozer/android/platform/build-arm64-v8a/dists/bilibilimonitor/build/outputs/apk/debug/bilibilimonitor-debug.apk"
APK_NAME = "bilibilimonitor-1.0.5-arm64-v8a-debug.apk"
BUNDLE_DIR = ".buildozer/android/platform/build-arm64-v8a/dists/bilibilimonitor/_python_bundle__arm64-v8a/_python_bundle/site-packages"
PYINSTALL_DIR = ".buildozer/android/platform/build-arm64-v8a/build/python-installs/bilibilimonitor/arm64-v8a"

NSC_XML = """<?xml version="1.0" encoding="utf-8"?>
<network-security-config>
    <base-config cleartextTrafficPermitted="true">
        <trust-anchors>
            <certificates src="system" />
        </trust-anchors>
    </base-config>
    <domain-config cleartextTrafficPermitted="true">
        <domain includeSubdomains="true">127.0.0.1</domain>
        <domain includeSubdomains="true">localhost</domain>
        <domain includeSubdomains="true">10.0.0.0</domain>
    </domain-config>
</network-security-config>
"""


def wsl(cmd, check=True):
    r = subprocess.run(["wsl", "--", "bash", "-c", cmd], capture_output=True, text=True)
    print(r.stdout, end="")
    if r.stderr:
        print(r.stderr, end="", file=sys.stderr)
    if check and r.returncode != 0:
        print(f"Command failed with exit code {r.returncode}", file=sys.stderr)
        sys.exit(1)
    return r


def upgrade_flask_werkzeug():
    print("=== Step 3: Upgrade Flask + Werkzeug in python bundle ===")
    bundle = f"{WSL_BUILD_DIR}/{BUNDLE_DIR}"
    pyinstall = f"{WSL_BUILD_DIR}/{PYINSTALL_DIR}"

    print("  Installing Flask==3.0.0 and Werkzeug==3.0.1 into python-installs...")
    wsl(
        f"pip3 install --target={pyinstall} Flask==3.0.0 Werkzeug==3.0.1 "
        f"--no-deps --upgrade 2>&1 | tail -5"
    )

    print("  Verifying Flask version (should NOT reference url_quote)...")
    r = wsl(f"grep 'url_quote' {pyinstall}/flask/helpers.py 2>/dev/null; echo DONE", check=False)
    if "url_quote" in r.stdout:
        print("  WARNING: Flask still references url_quote! Upgrade may have failed.", file=sys.stderr)
    else:
        print("  OK: Flask 3.0 confirmed (no url_quote reference).")

    print("  Replacing Flask + Werkzeug in _python_bundle/site-packages...")
    wsl(f"rm -rf {bundle}/flask {bundle}/flask-*.dist-info {bundle}/werkzeug {bundle}/werkzeug-*.dist-info")
    wsl(f"cp -r {pyinstall}/flask {bundle}/flask")
    wsl(f"cp -r {pyinstall}/flask-*.dist-info {bundle}/")
    wsl(f"cp -r {pyinstall}/werkzeug {bundle}/werkzeug")
    wsl(f"cp -r {pyinstall}/werkzeug-*.dist-info {bundle}/")

    print("  Compiling .py to .pyc in bundle...")
    hostpy = f"{WSL_BUILD_DIR}/.buildozer/android/platform/build-arm64-v8a/build/other_builds/hostpython3/desktop/hostpython3/native-build/python3"
    wsl(
        f"if [ -f {hostpy} ]; then "
        f"  {hostpy} -m compileall -b {bundle}/flask {bundle}/werkzeug 2>&1 | tail -3; "
        f"else "
        f"  python3 -m compileall -b {bundle}/flask {bundle}/werkzeug 2>&1 | tail -3; "
        f"fi"
    )

    print("  Verifying bundle Flask version...")
    r = wsl(f"grep 'url_quote' {bundle}/flask/helpers.py 2>/dev/null; echo DONE", check=False)
    if "url_quote" in r.stdout:
        print("  WARNING: Bundle Flask still references url_quote!", file=sys.stderr)
    else:
        print("  OK: Bundle Flask 3.0 confirmed.")


def patch_werkzeug_bundle():
    """Patch final libpybundle.so copies so the packaged runtime is self-contained."""
    patch_script = r'''
import gzip
import io
import hashlib
import pathlib
import py_compile
import shutil
import tarfile
import tempfile

root = pathlib.Path("__ROOT__")
hostpy = root / ".buildozer/android/platform/build-arm64-v8a/build/other_builds/hostpython3/desktop/hostpython3/native-build/python3"
pyinstall = root / "__PYINSTALL_DIR__"
urls_py = pyinstall / "werkzeug" / "urls.py"
if not urls_py.exists():
    raise SystemExit(f"missing Werkzeug urls.py: {urls_py}")

patch_marker = "# BilibiliMonitor werkzeug url_quote compatibility"
patch_code = r"""

# BilibiliMonitor werkzeug url_quote compatibility
try:
    from urllib.parse import quote as _bm_quote, unquote as _bm_unquote
    from urllib.parse import quote_plus as _bm_quote_plus, unquote_plus as _bm_unquote_plus

    def url_quote(value, charset="utf-8", errors="strict", safe="/:"):
        return _bm_quote(value, safe=safe, encoding=charset, errors=errors)

    def url_unquote(value, charset="utf-8", errors="replace"):
        return _bm_unquote(value, encoding=charset, errors=errors)

    def url_quote_plus(value, charset="utf-8", errors="strict", safe=""):
        return _bm_quote_plus(value, safe=safe, encoding=charset, errors=errors)

    def url_unquote_plus(value, charset="utf-8", errors="replace"):
        return _bm_unquote_plus(value, encoding=charset, errors=errors)
except Exception:
    pass
"""

text = urls_py.read_text(encoding="utf-8")
if patch_marker not in text:
    urls_py.write_text(text.rstrip() + patch_code + "\n", encoding="utf-8")

tmp = pathlib.Path(tempfile.mkdtemp(prefix="bm_werkzeug_patch_"))
patched_pyc = tmp / "urls.pyc"
py_compile.compile(str(urls_py), cfile=str(patched_pyc), doraise=True)

bundle_paths = [
    root / ".buildozer/android/platform/build-arm64-v8a/dists/bilibilimonitor/libs/arm64-v8a/libpybundle.so",
    root / ".buildozer/android/platform/build-arm64-v8a/dists/bilibilimonitor/build/intermediates/merged_jni_libs/debug/out/arm64-v8a/libpybundle.so",
    root / ".buildozer/android/platform/build-arm64-v8a/dists/bilibilimonitor/build/intermediates/merged_native_libs/debug/out/lib/arm64-v8a/libpybundle.so",
    root / ".buildozer/android/platform/build-arm64-v8a/dists/bilibilimonitor/build/intermediates/stripped_native_libs/debug/out/lib/arm64-v8a/libpybundle.so",
]
target_name = "_python_bundle/site-packages/werkzeug/urls.pyc"
patched = 0
for bundle in bundle_paths:
    if not bundle.exists():
        continue
    raw_tar = tmp / (bundle.name + ".tar")
    with gzip.open(bundle, "rb") as gz:
        raw_tar.write_bytes(gz.read())
    rebuilt_tar = tmp / (bundle.name + ".patched.tar")
    with tarfile.open(raw_tar, "r") as src, tarfile.open(rebuilt_tar, "w") as dst:
        replaced = False
        for member in src.getmembers():
            if member.name == target_name:
                data = patched_pyc.read_bytes()
                info = tarfile.TarInfo(member.name)
                info.size = len(data)
                info.mode = member.mode
                info.mtime = member.mtime
                dst.addfile(info, io.BytesIO(data))
                replaced = True
            else:
                f = src.extractfile(member) if member.isfile() else None
                dst.addfile(member, f)
                if f:
                    f.close()
        if not replaced:
            raise SystemExit(f"{target_name} not found in {bundle}")
    with open(rebuilt_tar, "rb") as src, gzip.open(bundle, "wb", compresslevel=9) as gz:
        shutil.copyfileobj(src, gz)
    patched += 1

if patched == 0:
    raise SystemExit("no libpybundle.so paths were patched")

strings_xml = root / ".buildozer/android/platform/build-arm64-v8a/dists/bilibilimonitor/src/main/res/values/strings.xml"
if not strings_xml.exists():
    raise SystemExit(f"missing strings.xml: {strings_xml}")
version_seed = hashlib.sha1()
for bundle in bundle_paths:
    if bundle.exists():
        version_seed.update(bundle.read_bytes())
new_version = "bm-" + version_seed.hexdigest()
text = strings_xml.read_text(encoding="utf-8")
old_text = text
text = __import__("re").sub(
    r'(<string name="private_version">)([^<]*)(</string>)',
    r'\1' + new_version + r'\3',
    text,
)
if text == old_text:
    raise SystemExit("private_version string was not updated")
strings_xml.write_text(text, encoding="utf-8")

print(f"Patched final libpybundle Werkzeug url helpers in {patched} files; private_version={new_version}")
'''
    patch_script = (
        patch_script
        .replace("__ROOT__", WSL_BUILD_DIR)
        .replace("__PYINSTALL_DIR__", PYINSTALL_DIR)
    )
    script_path = os.path.join(DIST_DIR, "_patch_werkzeug_bundle.py")
    with open(script_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(patch_script)
    wsl(f"{WSL_BUILD_DIR}/.buildozer/android/platform/build-arm64-v8a/build/other_builds/hostpython3/desktop/hostpython3/native-build/python3 /mnt/f/AI/CODEBUDDY/bilibili-monitor/dist/_patch_werkzeug_bundle.py")


def patch_android_bundle_refresh():
    java_rel = ".buildozer/android/platform/build-arm64-v8a/dists/bilibilimonitor/src/main/java/org/kivy/android/PythonActivity.java"
    patch_script = r"""
import pathlib

path = pathlib.Path("__ROOT__") / "__JAVA_REL__"
text = path.read_text(encoding="utf-8")
needle = 'PythonUtil.unpackAsset(mActivity, "private", app_root_file, true);\n            PythonUtil.unpackPyBundle(mActivity, getApplicationInfo().nativeLibraryDir + "/" + "libpybundle", app_root_file, false);'
replacement = '''PythonUtil.unpackAsset(mActivity, "private", app_root_file, true);
            PythonUtil.recursiveDelete(new File(app_root_file, "_python_bundle"));
            new File(app_root_file, "libpybundle.version").delete();
            PythonUtil.unpackPyBundle(mActivity, getApplicationInfo().nativeLibraryDir + "/" + "libpybundle", app_root_file, false);'''
if replacement not in text:
    if needle not in text:
        raise SystemExit("PythonActivity bundle unpack call not found")
    text = text.replace(needle, replacement)
    path.write_text(text, encoding="utf-8")
print("Patched PythonActivity to refresh _python_bundle on startup")
"""
    patch_script = patch_script.replace("__ROOT__", WSL_BUILD_DIR).replace("__JAVA_REL__", java_rel)
    script_path = os.path.join(DIST_DIR, "_patch_android_bundle_refresh.py")
    with open(script_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(patch_script)
    wsl(f"python3 /mnt/f/AI/CODEBUDDY/bilibili-monitor/dist/_patch_android_bundle_refresh.py")


def main():
    os.makedirs(DIST_DIR, exist_ok=True)

    print("=== Step 1: Sync project files to WSL ===")
    wsl(f"rm -rf {WSL_BUILD_DIR}/src && cp -r /mnt/f/AI/CODEBUDDY/bilibili-monitor/src {WSL_BUILD_DIR}/")
    wsl(f"cp /mnt/f/AI/CODEBUDDY/bilibili-monitor/buildozer.spec {WSL_BUILD_DIR}/buildozer.spec")
    wsl(f"cp /mnt/f/AI/CODEBUDDY/bilibili-monitor/icon.png {WSL_BUILD_DIR}/icon.png")
    wsl(f"cp /mnt/f/AI/CODEBUDDY/bilibili-monitor/presplash.png {WSL_BUILD_DIR}/presplash.png")
    wsl(
        f"rm -f {WSL_BUILD_DIR}/.buildozer/android/app/sitecustomize.py "
        f"{WSL_BUILD_DIR}/.buildozer/android/app/runtime_compat.py",
        check=False,
    )

    print("=== Step 2: Run buildozer ===")
    build_cmd = (
        "export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 && "
        "export PATH=/usr/lib/jvm/java-17-openjdk-amd64/bin:/home/mogu/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin && "
        f"cd {WSL_BUILD_DIR} && /usr/bin/python3 -m buildozer android debug 2>&1"
    )
    r = subprocess.run(["wsl", "--", "bash", "-c", build_cmd])
    if r.returncode != 0:
        print("Buildozer build failed!", file=sys.stderr)
        sys.exit(1)

    upgrade_flask_werkzeug()
    patch_werkzeug_bundle()
    patch_android_bundle_refresh()

    print("=== Step 4: Patch AndroidManifest.xml ===")
    manifest_path = f"{WSL_BUILD_DIR}/{MANIFEST_REL}"
    wsl(
        f"if grep -q 'usesCleartextTraffic' {manifest_path}; then "
        f"  echo 'usesCleartextTraffic already present'; "
        f"else "
        f"  sed -i 's/<application/<application android:usesCleartextTraffic=\"true\"/g' {manifest_path} && "
        f"  echo 'Added usesCleartextTraffic'; "
        f"fi"
    )
    wsl(
        f"if grep -q 'networkSecurityConfig' {manifest_path}; then "
        f"  echo 'networkSecurityConfig already present'; "
        f"else "
        f"  sed -i 's#<application#<application android:networkSecurityConfig=\"@xml/network_security_config\"#g' {manifest_path} && "
        f"  echo 'Added networkSecurityConfig'; "
        f"fi"
    )

    print("=== Step 5: Copy network_security_config.xml ===")
    res_xml_dir = f"{WSL_BUILD_DIR}/{RES_XML_REL}"
    wsl(f"mkdir -p {res_xml_dir}")
    nsc_path = f"{res_xml_dir}/network_security_config.xml"
    wsl(f"cat > {nsc_path} << 'XMLEOF'\n{NSC_XML}\nXMLEOF")

    print("=== Step 6: Re-run gradle assembleDebug ===")
    gradle_dir = f"{WSL_BUILD_DIR}/{GRADLE_REL}"
    gradle_cmd = (
        "export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64 && "
        "export PATH=/usr/lib/jvm/java-17-openjdk-amd64/bin:/home/mogu/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin && "
        f"cd {gradle_dir} && "
        "chmod +x gradlew && "
        "./gradlew assembleDebug 2>&1"
    )
    r = subprocess.run(["wsl", "--", "bash", "-c", gradle_cmd])
    if r.returncode != 0:
        print("Gradle assembleDebug failed!", file=sys.stderr)
        sys.exit(1)

    print("=== Step 7: Copy APK to dist/ ===")
    apk_src = f"{WSL_BUILD_DIR}/{APK_REL}"
    apk_dst = os.path.join(DIST_DIR, APK_NAME)
    wsl(f"cp {apk_src} /mnt/f/AI/CODEBUDDY/bilibili-monitor/dist/{APK_NAME}")
    wsl(f"mkdir -p /mnt/f/AI/CODEBUDDY/bilibili-monitor/bin && cp {apk_src} /mnt/f/AI/CODEBUDDY/bilibili-monitor/bin/{APK_NAME}")

    print(f"\n{'='*60}")
    print(f"SUCCESS: {apk_dst}")
    size_mb = os.path.getsize(apk_dst) / (1024 * 1024)
    print(f"Size: {size_mb:.1f} MB")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
