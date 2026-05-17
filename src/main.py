#!/usr/bin/env python3
"""Bili Monitor - Android entry point.

Uses ONLY basic Kivy widgets. NO canvas/Animation.
Uses Android system font for CJK support.
"""

import sys
import os


def _crash(msg):
    try:
        with open("crash.txt", "w") as f:
            f.write(msg)
    except Exception:
        pass


sys.excepthook = lambda *a: _crash("".join(__import__("traceback").format_exception(*a)))

from kivy.app import App
from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label

PORT = 8199
APP_VERSION = "1.0.5"

_CJK_FONT = None
for _p in [
    "/system/fonts/NotoSansCJK-Regular.ttc",
    "/system/fonts/NotoSansSC-Regular.otf",
    "/system/fonts/DroidSansFallback.ttf",
    "/system/fonts/NotoSansSC-Regular.ttf",
]:
    if os.path.exists(_p):
        _CJK_FONT = _p
        break


def _start_foreground_service():
    try:
        from jnius import autoclass
        from android import mActivity

        PythonService = autoclass("org.kivy.android.PythonService")
        PythonService.start(
            "MOGU-bili监控器",
            "监控服务运行中",
        )

        Context = autoclass("android.content.Context")
        NotificationManager = autoclass("android.app.NotificationManager")
        Notification = autoclass("android.app.Notification")
        NotificationChannel = autoclass("android.app.NotificationChannel")
        PendingIntent = autoclass("android.app.PendingIntent")
        Intent = autoclass("android.content.Intent")

        ctx = mActivity.getApplicationContext()
        channel_id = "bili_monitor_service"
        nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE)

        if nm and hasattr(nm, 'getNotificationChannel'):
            try:
                existing = nm.getNotificationChannel(channel_id)
                if not existing:
                    channel = NotificationChannel(
                        channel_id,
                        "Bili Monitor Service",
                        NotificationManager.IMPORTANCE_LOW,
                    )
                    channel.setShowBadge(False)
                    nm.createNotificationChannel(channel)
            except Exception:
                pass

        try:
            intent = Intent(ctx, autoclass("org.kivy.android.PythonActivity"))
            intent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
            p_intent = PendingIntent.getActivity(
                ctx, 0, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
            )

            builder = Notification.Builder(ctx, channel_id)
            builder.setContentTitle("MOGU-bili监控器")
            builder.setContentText("监控服务运行中，点击返回")
            builder.setSmallIcon(autoclass("android.R$drawable").ic_dialog_info)
            builder.setContentIntent(p_intent)
            builder.setOngoing(True)
            notification = builder.build()
            mActivity.startForeground(1, notification)
        except Exception:
            pass

    except Exception as e:
        try:
            with open("fg_service_error.txt", "w") as f:
                f.write(str(e))
        except Exception:
            pass


def _acquire_wakelock():
    """Acquire a partial wake lock to keep CPU running in background."""
    try:
        from jnius import autoclass
        from android import mActivity

        Context = autoclass("android.content.Context")
        PowerManager = autoclass("android.os.PowerManager")
        ctx = mActivity.getApplicationContext()
        pm = ctx.getSystemService(Context.POWER_SERVICE)
        wake_lock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "BiliMonitor::WakeLock")
        wake_lock.acquire()
        return wake_lock
    except Exception as e:
        try:
            with open("wakelock_error.txt", "w") as f:
                f.write(str(e))
        except Exception:
            pass
        return None


class BiliApp(App):
    def build(self):
        from kivy.core.window import Window
        Window.clearcolor = (0.976, 0.984, 0.898, 1)
        self.root_box = BoxLayout(orientation="vertical", padding=30, spacing=15)
        fn = _CJK_FONT or ""
        self._lbl = Label(
            text=f"MOGU-bili\u76d1\u63a7\u5668 v{APP_VERSION}",
            font_size="28sp",
            halign="center",
            valign="middle",
            color=(0.31, 0.40, 0.0, 1),
            font_name=fn,
            size_hint_y=0.3,
        )
        self._status = Label(
            text="\u6b63\u5728\u521d\u59cb\u5316...",
            font_size="14sp",
            halign="center",
            valign="middle",
            color=(0.45, 0.48, 0.38, 1),
            font_name=fn,
            size_hint_y=0.2,
        )
        self._progress = Label(
            text="",
            font_size="12sp",
            halign="center",
            valign="middle",
            color=(0.55, 0.58, 0.48, 1),
            font_name=fn,
            size_hint_y=0.1,
        )
        self._dots = Label(
            text="",
            font_size="18sp",
            halign="center",
            valign="middle",
            color=(0.80, 0.82, 0.60, 1),
            font_name=fn,
            size_hint_y=0.1,
        )
        self.root_box.add_widget(self._lbl)
        self.root_box.add_widget(self._status)
        self.root_box.add_widget(self._progress)
        self.root_box.add_widget(self._dots)
        self._ticks = 0
        self._wv_done = False
        self._android_app = None
        self._dot_count = 0
        Clock.schedule_interval(self._animate_dots, 0.5)
        Clock.schedule_once(self._step1, 1)
        return self.root_box

    def _animate_dots(self, dt):
        if self._wv_done:
            return False
        self._dot_count = (self._dot_count + 1) % 4
        self._dots.text = "\u25cf" + "\u25cb" * 3
        dots = list(self._dots.text)
        for i in range(self._dot_count + 1):
            dots[i] = "\u25cf"
        for i in range(self._dot_count + 1, 4):
            dots[i] = "\u25cb"
        self._dots.text = "".join(dots)

    def _set_status(self, text, error=False):
        self._status.text = text
        if error:
            self._status.color = (0.73, 0.1, 0.1, 1)
        else:
            self._status.color = (0.45, 0.48, 0.38, 1)

    def _step1(self, dt):
        self._set_status("\u6b63\u5728\u52a0\u8f7d\u6a21\u5757...")
        self._progress.text = "1/6"
        try:
            import android_app
            self._android_app = android_app
            self._set_status("\u6a21\u5757\u52a0\u8f7d\u5b8c\u6210")
        except Exception as e:
            self._set_status(f"\u52a0\u8f7d\u5931\u8d25: {e}", error=True)
            _crash(f"step1: {e}")
            return
        Clock.schedule_once(self._step2, 0.3)

    def _step2(self, dt):
        self._set_status("\u6b63\u5728\u521d\u59cb\u5316\u914d\u7f6e...")
        self._progress.text = "2/6"
        try:
            self._android_app.init_app()
            self._set_status("\u914d\u7f6e\u521d\u59cb\u5316\u5b8c\u6210")
        except Exception as e:
            self._set_status(f"\u521d\u59cb\u5316\u5931\u8d25: {e}", error=True)
            _crash(f"step2: {e}")
            return
        Clock.schedule_once(self._step3, 0.3)

    def _step3(self, dt):
        self._set_status("\u6b63\u5728\u542f\u52a8\u670d\u52a1...")
        self._progress.text = "3/6"
        try:
            import threading
            # Non-daemon thread so server keeps running even if main thread pauses
            t = threading.Thread(target=self._server_thread, daemon=False)
            t.start()
            self._set_status("\u670d\u52a1\u5df2\u542f\u52a8")
        except Exception as e:
            self._set_status(f"\u542f\u52a8\u5931\u8d25: {e}", error=True)
            _crash(f"step3: {e}")
            return
        Clock.schedule_once(self._step4, 0.5)

    def _server_thread(self):
        try:
            self._android_app.start_server()
        except Exception as e:
            try:
                self._android_app.write_log("server_crash.txt", f"{e}")
            except Exception:
                pass

    def on_pause(self):
        """Keep app running when in background."""
        return True

    def on_resume(self):
        """Handle resume from background."""
        pass

    def _step4(self, dt):
        self._set_status("\u7b49\u5f85\u670d\u52a1\u5c31\u7eea...")
        self._progress.text = "4/6"
        Clock.schedule_interval(self._poll, 1)

    def _poll(self, dt):
        if self._wv_done:
            return False
        self._ticks += 1

        try:
            if self._android_app.check_server():
                self._set_status("\u670d\u52a1\u5c31\u7eea\uff0c\u52a0\u8f7d\u754c\u9762...")
                self._progress.text = "5/6"
                self._wv_done = True
                Clock.schedule_once(self._webview, 0.3)
                return False
        except Exception:
            pass

        try:
            err = self._android_app.get_error()
            if err:
                self._set_status(f"\u670d\u52a1\u9519\u8bef: {err[:80]}", error=True)
                return False
        except Exception:
            pass

        if self._ticks > 120:
            self._set_status("\u542f\u52a8\u8d85\u65f6 (120s)", error=True)
            return False

        self._set_status(f"\u7b49\u5f85\u670d\u52a1\u5c31\u7eea... {self._ticks}s")
        return True

    def _enable_cleartext(self):
        try:
            from jnius import autoclass
            from android import mActivity

            app_info = mActivity.getApplicationContext().getApplicationInfo()
            flags = app_info.flags
            if hasattr(app_info, 'FLAG_USES_CLEARTEXT_TRAFFIC'):
                if (flags & app_info.FLAG_USES_CLEARTEXT_TRAFFIC) == 0:
                    try:
                        app_info.flags = flags | app_info.FLAG_USES_CLEARTEXT_TRAFFIC
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            from jnius import autoclass
            from android import mActivity

            NetworkSecurityConfig = autoclass("android.security.NetworkSecurityConfig")
            DefaultConfig = autoclass("android.security.NetworkSecurityConfig$DefaultConfig")
            Builder = autoclass("android.security.NetworkSecurityConfig$Builder")
            cfg = Builder()
            cfg.setCleartextTrafficPermitted(True)
            built = cfg.build()

            app_ctx = mActivity.getApplicationContext()
            app_cls = app_ctx.getClass().getSuperclass()
            field = app_cls.getDeclaredField("mNetworkSecurityConfig")
            field.setAccessible(True)
            field.set(app_ctx, built)
        except Exception:
            pass

        try:
            from jnius import autoclass
            from android import mActivity

            app_cls = autoclass("android.app.Application")
            try:
                m = app_cls.getDeclaredMethod("setCleartextTrafficAllowed", [autoclass("java.lang.Boolean").TYPE])
                m.setAccessible(True)
                m.invoke(mActivity.getApplication(), [autoclass("java.lang.Boolean").valueOf(True)])
            except Exception:
                pass
        except Exception:
            pass

    def _webview(self, dt):
        _start_foreground_service()
        _acquire_wakelock()
        self._enable_cleartext()
        try:
            from jnius import autoclass
            from android import mActivity

            WV = autoclass("android.webkit.WebView")
            WVC = autoclass("android.webkit.WebViewClient")
            WS = autoclass("android.webkit.WebSettings")
            LL = autoclass("android.widget.LinearLayout")
            LP = autoclass("android.widget.LinearLayout$LayoutParams")
            AC = autoclass("android.graphics.Color")

            def setup():
                try:
                    a = mActivity
                    wv = WV(a)
                    wv.setWebViewClient(WVC())
                    s = wv.getSettings()
                    s.setJavaScriptEnabled(True)
                    s.setDomStorageEnabled(True)
                    s.setDatabaseEnabled(True)
                    s.setAllowFileAccess(True)
                    s.setMediaPlaybackRequiresUserGesture(False)
                    s.setMixedContentMode(WS.MIXED_CONTENT_ALWAYS_ALLOW)
                    s.setCacheMode(WS.LOAD_DEFAULT)
                    wv.setBackgroundColor(AC.parseColor("#f9fbe5"))
                    wv.loadUrl(f"http://127.0.0.1:{PORT}/")
                    layout = LL(a)
                    layout.setOrientation(LL.VERTICAL)
                    layout.setBackgroundColor(AC.parseColor("#f9fbe5"))
                    layout.addView(wv, LP(LP.MATCH_PARENT, LP.MATCH_PARENT))
                    a.setContentView(layout)
                except Exception as e:
                    try:
                        self._android_app.write_log("wv_error.txt", f"{e}")
                    except Exception:
                        pass

            from android.runnable import run_on_ui_thread
            run_on_ui_thread(setup)()
        except Exception as e:
            self._set_status(f"WebView error: {str(e)[:60]}", error=True)


BiliApp().run()
