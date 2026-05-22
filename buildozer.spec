[app]
title = MOGU-bili监控器
package.name = bilibilimonitor
package.domain = org
version = 1.0.5
source.dir = src
source.include_exts = py,png,jpg,kv,atlas,html,json,ttf,otf
requirements = python3==3.11.5,kivy==2.3.0,requests,python-dotenv,Flask==3.0.0,Werkzeug==3.0.1,pyjnius,urllib3,charset-normalizer,certifi,idna,markupsafe==2.1.3,Jinja2==3.1.2,itsdangerous==2.1.2,click==8.1.7,blinker==1.6.2
orientation = portrait
fullscreen = 0
icon.filename = %(source.dir)s/../icon.png
presplash.filename = %(source.dir)s/../presplash.png

android.permissions = INTERNET,ACCESS_NETWORK_STATE,ACCESS_WIFI_STATE,FOREGROUND_SERVICE,WAKE_LOCK
android.api = 33
android.minapi = 24
android.ndk = 25b
android.accept_sdk_license = True
android.archs = arm64-v8a
android.allow_backup = True
android.apptheme = @android:style/Theme.NoTitleBar
android.wakelock = True
android.add_aars =
android.gradle_dependencies =
android.usesCleartextTraffic = True

# p4a configuration
p4a.branch = v2024.01.21
p4a.local_recipes =

# Build options
log_level = 2
warn_on_root = 1

[buildozer]
log_level = 2
warn_on_root = 1
