# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Run explicitly on Windows: isolated hidden caption inspection by default.

``--interactive`` retains the complete resize/media/tray smoke, including a
visible harness window and test notification. The default never shows a window,
uses mouse input, contacts the configured production service, or claims physical
dragging was verified. Evidence is written beneath ignored UI screenshots.
"""
from __future__ import annotations

import ctypes
import json
import sys
import threading
import time
import subprocess
import argparse
import base64
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.desktop_web import WebDesktopApp, _resource_path
from app.tray import TrayController
from app.native_notifications import APP_NAME, register_identity, show_toast
from engine.audio import get_ffmpeg
from engine.settings import SettingsStore


def isolated_profile(prefix: str) -> Path:
    directory = ROOT / '.ui-demo-data'
    directory.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix=prefix, dir=directory))
    store = SettingsStore(base_dir=profile)
    settings = store.defaults()
    settings.update(subscription_server_url='', subscription_license_public_key='',
                    account_login_token='', account_user_id='', license_token='',
                    agent_enabled=False, title_optimization_enabled=False,
                    agent_api_key='', worker_auto_start=False, local_api_enabled=False,
                    task_notifications=False, launch_at_startup=False, setup_completed=False)
    store.save(settings)
    return profile


def interactive_main() -> None:
    profile = isolated_profile('native-interactive-')
    app = WebDesktopApp(data_dir=profile, demo=True, start_worker=False)
    app.bridge.sync_subscription_in_background = lambda: None
    app.bridge.sync_product_notifications_in_background = lambda: None
    preview_path = profile / 'native-preview.mp4'
    subprocess.run([get_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=640x360:rate=24", "-t", "2", "-pix_fmt", "yuv420p", str(preview_path)],
                   check=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    preview_id = app.controller.media.import_file(str(preview_path)).item["id"]
    tray = TrayController(icon_path=_resource_path("assets", "icons", "foilpage.ico"), show=app._show_from_api, quit_app=app._request_quit, version="UI 验证")
    assert tray.start(), "native tray should become available"
    app.tray = tray
    app._notification_identity = register_identity(_resource_path("assets", "icons", "foilpage.png"))
    evidence = {}
    fix = app._fix_native_window

    def exercise() -> None:
        try:
            fix()
            assert app._window.events.loaded.wait(15), "WebView must finish loading"
            evidence["brand_icon_loaded"] = app._window.evaluate_js("document.querySelector('.brand img').naturalWidth > 0")
            assert evidence["brand_icon_loaded"], "application brand icon must load in WebView2"
            hwnd = app._native_hwnd()
            assert hwnd
            user32 = ctypes.windll.user32
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            style = user32.GetWindowLongW(hwnd, -16)
            assert style & 0x00040000, "WS_THICKFRAME must be enabled"
            assert style & 0x00010000, "maximize box must be enabled"
            assert not style & 0x00C00000, "native caption/icon must be hidden"
            for _ in range(30):
                evidence["custom_window_chrome"] = app._window.evaluate_js("document.getElementById('windowChrome').hidden === false")
                if evidence["custom_window_chrome"]:
                    break
                time.sleep(.1)
            assert evidence["custom_window_chrome"]
            for _ in range(50):
                if app._window.evaluate_js("window.__appReady === true"):
                    break
                time.sleep(.1)
            evidence["manual_start_setup"] = app._window.evaluate_js("AppState.currentView === 'setup' && document.querySelector('#setupDownloadDirBtn') !== null")
            assert evidence["manual_start_setup"]
            evidence["navigation_order"] = app._window.evaluate_js("Array.from(document.querySelectorAll('.nav-row[data-page]')).map(n=>n.dataset.page)")
            order = evidence["navigation_order"]
            assert order.index('setup') < order.index('workbench') and order.index('subscription') + 1 == order.index('settings')
            evidence["integrated_caption"] = app._window.evaluate_js("""(()=>{
                const chrome=document.getElementById('windowChrome');
                const topbar=document.querySelector('.topbar');
                const main=document.querySelector('.main').getBoundingClientRect();
                const box=chrome.getBoundingClientRect();
                return {inside_page:topbar.contains(chrome), top:box.top,
                    main_top:main.top, app_height:document.querySelector('.app').getBoundingClientRect().height,
                    viewport_height:innerHeight,
                    buttons:Array.from(chrome.querySelectorAll('button')).map(b=>({width:b.getBoundingClientRect().width,height:b.getBoundingClientRect().height}))};
            })()""")
            caption = evidence["integrated_caption"]
            assert caption["inside_page"] and caption["top"] >= caption["main_top"]
            assert caption["app_height"] == caption["viewport_height"], caption
            assert all(b["width"] == b["height"] == 44 for b in caption["buttons"]), caption
            evidence["notification_brand"] = APP_NAME
            app._window.show()
            app._window.resize(640, 700)
            time.sleep(.3)
            width = app._window.evaluate_js("innerWidth")
            assert 500 <= width <= 700, width
            evidence["narrow_window_width"] = width
            user32.IsIconic.argtypes = [ctypes.c_void_p]
            app._window.evaluate_js("document.getElementById('windowMinimize').click()")
            for _ in range(30):
                if user32.IsIconic(hwnd):
                    break
                time.sleep(.1)
            assert user32.IsIconic(hwnd), "page minimize button must minimize the native window"
            app._window.restore()
            evidence["page_minimize_button"] = True
            app._window.evaluate_js("document.getElementById('windowMaximize').click()")
            for _ in range(20):
                time.sleep(.1)
                maximized_width = app._window.evaluate_js("innerWidth")
                if maximized_width > width:
                    break
            evidence["maximized_window_width"] = maximized_width
            assert maximized_width > width, f"maximized viewport remained {maximized_width}"
            for _ in range(30):
                if app._window.evaluate_js("!document.getElementById('windowMaximize').disabled"):
                    break
                time.sleep(.1)
            app._window.evaluate_js("document.getElementById('windowMaximize').click()")
            for _ in range(30):
                if not user32.IsZoomed(hwnd):
                    break
                time.sleep(.1)
            assert not user32.IsZoomed(hwnd), "page restore button must restore the native window"
            evidence["page_maximize_and_restore_buttons"] = True
            # Native WebView2 caption hit testing handles physical dragging;
            # synthesized DOM mouse events must never move the window through
            # pywebview's per-mousemove JS/Python bridge in this mode.
            evidence["native_caption_enabled"] = app.bridge.get_window_chrome().get("native_drag") is True
            assert evidence["native_caption_enabled"], "the bundled WebView2 runtime must support native caption dragging"
            evidence["caption_fallback_disabled"] = app._window.evaluate_js(
                "Array.from(document.querySelectorAll('.window-drag-region')).every(el=>!el.classList.contains('window-drag-fallback'))")
            assert evidence["caption_fallback_disabled"]
            from ctypes import wintypes
            user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
            before = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(before))
            app._window.evaluate_js(f"""(()=>{{
                const el=document.querySelector('.topbar.window-drag-region');
                const r=el.getBoundingClientRect();
                const x=r.left+r.width/2,y=r.top+r.height/2;
                el.dispatchEvent(new MouseEvent('mousedown',{{bubbles:true,button:0,clientX:x,clientY:y}}));
                window.dispatchEvent(new MouseEvent('mousemove',{{bubbles:true,screenX:{before.left}+x+30,screenY:{before.top}+y+24}}));
                window.dispatchEvent(new MouseEvent('mouseup',{{bubbles:true}}));
            }})()""")
            after = wintypes.RECT()
            for _ in range(30):
                user32.GetWindowRect(hwnd, ctypes.byref(after))
                time.sleep(.01)
            evidence["caption_drag_delta"] = {"x":after.left-before.left,"y":after.top-before.top}
            assert after.left == before.left and after.top == before.top, evidence["caption_drag_delta"]
            evidence["synthetic_mousemove_bridge_inactive"] = True
            evidence["resize_and_maximize"] = True
            # Exercise actual WebView2 local-file objects, not fabricated JS File names.
            app._window.evaluate_js("switchView('import')")
            for _ in range(50):
                if app._window.evaluate_js("!!document.getElementById('mediaDropZone')") and app.bridge._media_drop_element:
                    break
                time.sleep(.1)
            from System.Windows.Forms import MethodInvoker

            def devtools(method, params):
                pending = []
                app._window.native.Invoke(MethodInvoker(lambda: pending.append(
                    app._window.native.webview.CoreWebView2.CallDevToolsProtocolMethodAsync(
                        method, json.dumps(params)))))
                assert pending[0].Wait(5000), f"DevTools timeout: {method}"
                return json.loads(str(pending[0].Result))

            drop_point = app._window.evaluate_js("(()=>{const r=document.querySelector('#mediaDropZone').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()")
            data = {"items": [], "files": [str(preview_path.resolve())], "dragOperationsMask": 1}
            previous_drop_jobs = [job['id'] for job in app.controller.queue.list_jobs()
                                  if job['source_path'] == str(preview_path.resolve()) and job['status'] in ('queued', 'running', 'retryable')]
            if previous_drop_jobs:
                app.controller.cancel_jobs(previous_drop_jobs)
            jobs_before = len(app.controller.queue.list_jobs())
            old_job_ids = {job['id'] for job in app.controller.queue.list_jobs()}
            for event_type in ("dragEnter", "dragOver", "drop"):
                devtools("Input.dispatchDragEvent", {"type":event_type, **drop_point, "data":data})
            for _ in range(80):
                if len(app.controller.queue.list_jobs()) > jobs_before:
                    break
                time.sleep(.1)
            imported_jobs = app.controller.queue.list_jobs()
            assert len(imported_jobs) == jobs_before + 1, "Native file drop must enqueue exactly one task"
            assert any(job['source_path'] == str(preview_path.resolve()) and job['status'] == 'queued' for job in imported_jobs)
            app._window.evaluate_js("switchView('workbench')")
            evidence['native_file_drop_enqueued_persistent_job'] = True
            # Release this harness-owned task so subsequent library deletion remains permitted.
            dropped_job = next(job for job in imported_jobs if job['id'] not in old_job_ids and job['source_path'] == str(preview_path.resolve()))
            app.controller.cancel_jobs([dropped_job['id']])
            guide = app.bridge.get_agent_setup_guide()
            assert guide['ok'] and '"data_dir"' in guide['markdown'] and str(app.settings_store.base_dir).replace('\\', '\\\\') in guide['markdown'], "Native guide must contain the real JSON-encoded data directory"
            evidence['native_agent_guide_available'] = True
            app._window.evaluate_js("switchView('videos')")
            selector = json.dumps(f'[data-video-id="{preview_id}"] .video-cover')
            for _ in range(50):
                if app._window.evaluate_js(f"!!document.querySelector({selector})"):
                    break
                time.sleep(.1)
            for _ in range(50):
                evidence["real_cover_loaded"] = app._window.evaluate_js(
                    f"Boolean(document.querySelector({selector}+' img')?.naturalWidth > 0)")
                if evidence["real_cover_loaded"]:
                    break
                time.sleep(.1)
            assert evidence["real_cover_loaded"]
            app._window.evaluate_js(f"document.querySelector({selector}).click()")
            for _ in range(50):
                video = app._window.evaluate_js("(()=>{const v=document.querySelector('.video-player');return v?{ready:v.readyState,width:v.videoWidth,duration:v.duration,time:v.currentTime,error:v.error?.code||0}:null})()")
                if video and video.get("ready", 0) >= 2 and video.get("time", 0) > 0:
                    break
                time.sleep(.1)
            assert video and video["width"] == 640 and video["time"] > 0 and not video["error"], video
            evidence["real_video_decoded_and_playing"] = video
            app._window.evaluate_js("document.querySelector('.modal-x').click()")
            assert app._window.evaluate_js("document.querySelectorAll('.video-player').length === 0")
            evidence["closing_player_releases_media"] = True
            app._window.evaluate_js(f"document.querySelector({selector}).closest('.video-card').dispatchEvent(new MouseEvent('contextmenu',{{bubbles:true,clientX:350,clientY:240}}))")
            assert app._window.evaluate_js("document.querySelectorAll('.video-context-menu [role=menuitem]').length === 2")
            app._window.evaluate_js("document.querySelector('.video-context-menu button.danger').click()")
            assert app._window.evaluate_js("document.querySelector('.video-delete-note').textContent.includes('保留本地原文件')")
            app._window.evaluate_js("document.querySelector('#videoDeleteConfirm').click()")
            for _ in range(50):
                if app._window.evaluate_js(f"!document.querySelector({selector})"):
                    break
                time.sleep(.1)
            assert app._window.evaluate_js(f"!document.querySelector({selector})") and preview_path.exists()
            evidence["native_video_remove_preserves_imported_source"] = True
            app._window.evaluate_js("document.getElementById('windowHide').click()")
            user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
            for _ in range(30):
                if not user32.IsWindowVisible(hwnd):
                    break
                time.sleep(.1)
            assert not user32.IsWindowVisible(hwnd), "page close button must hide to tray"
            assert tray.available
            evidence["close_to_tray"] = True
            evidence["notification_identity_registered"] = app._notification_identity
            assert app._notification_identity
            evidence["notification_submitted"] = show_toast("下载成功（样式测试）", "原视频标题测试：这是一条验证通知。")
            assert evidence["notification_submitted"]
            time.sleep(1)
        except Exception as exc:
            evidence["error"] = repr(exc)
        finally:
            # Same callback used by the tray menu, dispatched off its own loop.
            tray.quit_app()

    app._fix_native_window = exercise
    watchdog = threading.Timer(45, app._quit)
    watchdog.daemon = True
    watchdog.start()
    app.start()
    watchdog.cancel()
    assert app._closed and app.tray is None
    assert not tray.available
    evidence["tray_and_window_closed"] = True
    path = ROOT / "_webview_ui_shots" / "product" / "native-smoke.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(evidence, ensure_ascii=False))
    assert "error" not in evidence


def background_main() -> None:
    profile = isolated_profile('native-caption-')
    app = WebDesktopApp(data_dir=profile, demo=True, start_worker=False,
                        initially_hidden=True, window_title='FoilPage：后台窗口质检')
    app.bridge.sync_subscription_in_background = lambda: None
    app.bridge.sync_product_notifications_in_background = lambda: None
    evidence = {'profile': str(profile), 'mode': 'hidden_read_only_caption',
                'production_server_configured': bool(app.controller.settings.get('subscription_server_url')),
                'physical_drag_verified': False, 'mouse_input_used': False,
                'window_show_requested': False}
    assert not evidence['production_server_configured'] and app.local_api is None
    output = ROOT / '.ui-audit' / 'release-1'
    output.mkdir(parents=True, exist_ok=True)
    fix = app._fix_native_window

    def exercise() -> None:
        try:
            fix()
            assert app._window.events.loaded.wait(15), 'Hidden WebView must finish loading'
            for _ in range(60):
                if app._window.evaluate_js('window.__appReady === true'):
                    break
                time.sleep(.1)
            assert app._window.evaluate_js('window.__appReady === true')
            hwnd = app._native_hwnd()
            assert hwnd
            user32 = ctypes.WinDLL('user32', use_last_error=True)
            from ctypes import wintypes
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            user32.SendMessageW.restype = ctypes.c_ssize_t
            evidence['window_visible'] = bool(user32.IsWindowVisible(hwnd))
            assert not evidence['window_visible'], 'Background QA must never expose its harness window'
            evidence['native_caption_enabled'] = app.bridge.get_window_chrome().get('native_drag') is True
            assert evidence['native_caption_enabled'], 'Bundled WebView2 must enable native non-client regions'
            regions_js = """(()=>({
                regions:Array.from(document.querySelectorAll('.window-drag-region')).map(el=>({
                    class:el.className,region:el.style.getPropertyValue('-webkit-app-region'),
                    fallback:el.classList.contains('window-drag-fallback'),box:el.getBoundingClientRect().toJSON()})),
                controls:Array.from(document.querySelectorAll('.window-control')).map(el=>({
                    id:el.id,region:getComputedStyle(el).getPropertyValue('-webkit-app-region')})),
                title_region:getComputedStyle(document.querySelector('#pageTitle')).getPropertyValue('-webkit-app-region'),
                sdk_visible:document.visibilityState
            }))()"""
            evidence['caption_dom'] = app._window.evaluate_js(regions_js)
            assert len(evidence['caption_dom']['regions']) == 4
            assert all(item['region'] == 'drag' and not item['fallback'] for item in evidence['caption_dom']['regions'])
            assert all(item['region'] == 'no-drag' for item in evidence['caption_dom']['controls'])
            edge = next(item for item in evidence['caption_dom']['regions'] if 'window-drag-edge' in item['class'])
            assert edge['box']['height'] == 12, edge
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            x, y = rect.left + (rect.right - rect.left)//2, rect.top + 1
            hit = user32.SendMessageW(hwnd, 0x0084, 0, (x & 0xffff) | ((y & 0xffff) << 16))
            evidence['read_only_resize_hit'] = int(hit)
            assert hit == 12, 'WM_NCHITTEST must preserve the top resize edge'
            from System.Windows.Forms import MethodInvoker
            sdk = []

            def inspect_sdk() -> None:
                control = app._window.native.webview
                owners = [control, control.CoreWebView2]
                controller = getattr(control, 'CoreWebView2Controller', None)
                if controller is not None:
                    owners.append(controller)
                methods = [(owner, method) for owner in owners for method in owner.GetType().GetMethods()
                           if method.Name == 'GetNonClientRegionAtPoint']
                result = {'non_client_regions_enabled': bool(control.CoreWebView2.Settings.IsNonClientRegionSupportEnabled),
                          'hit_api_available': bool(methods),
                          'hit_api_signatures': [str(method) for _owner, method in methods]}
                if methods and len(list(methods[0][1].GetParameters())) == 1:
                    from System.Drawing import Point
                    result['caption_hit'] = str(methods[0][0].GetNonClientRegionAtPoint(Point(300, 52)))
                sdk.append(result)

            app._window.native.Invoke(MethodInvoker(inspect_sdk))
            evidence['sdk_inspection'] = sdk[0]
            assert sdk[0]['non_client_regions_enabled']
            app._window.evaluate_js("openModal('后台质检','<p>模态打开时禁用拖动</p>')")
            time.sleep(.05)
            evidence['modal_drag_disabled'] = app._window.evaluate_js("document.querySelector('.app').inert && Array.from(document.querySelectorAll('.window-drag-region')).every(el=>el.style.getPropertyValue('-webkit-app-region')==='no-drag')")
            assert evidence['modal_drag_disabled']
            app._window.evaluate_js('closeModal()')
            time.sleep(.05)
            evidence['modal_drag_restored'] = app._window.evaluate_js("Array.from(document.querySelectorAll('.window-drag-region')).every(el=>el.style.getPropertyValue('-webkit-app-region')==='drag')")
            assert evidence['modal_drag_restored']
            # The UI-only synthetic account gate opens the guide without creating
            # a login token, modifying saved user settings or contacting a server.
            app._window.evaluate_js("AppState.settings.account_access={required:true,allowed:true,expires_at:Date.now()/1000+60,reason:'verified'};AccountAccess.sync();Onboarding.show();document.querySelector('.ob-chapter-choice[data-chapter=environment] .ob-start').click()")
            before = app._window.evaluate_js('Onboarding.__shengjianOnboardingInternals.state().index')
            app._window.evaluate_js("document.querySelector('.ob-body').dispatchEvent(new MouseEvent('click',{bubbles:true,clientX:0,clientY:0}))")
            after = app._window.evaluate_js('Onboarding.__shengjianOnboardingInternals.state().index')
            evidence['guide_blank_advanced_once'] = after == before + 1
            assert evidence['guide_blank_advanced_once']
            app._window.evaluate_js("document.querySelector('[aria-label=\"跳过本轮全部引导\"]').click()")
            screenshots = []

            def capture() -> None:
                screenshots.append(app._window.native.webview.CoreWebView2.CallDevToolsProtocolMethodAsync(
                    'Page.captureScreenshot', json.dumps({'format': 'png', 'captureBeyondViewport': False})))

            app._window.native.Invoke(MethodInvoker(capture))
            assert screenshots[0].Wait(5000)
            screenshot = json.loads(str(screenshots[0].Result))
            png = output / 'native-caption-hidden.png'
            png.write_bytes(base64.b64decode(screenshot['data']))
            evidence['screenshot'] = str(png)
            assert not user32.IsWindowVisible(hwnd)
        except Exception as exc:
            evidence['error'] = repr(exc)
        finally:
            app._quit()

    app._fix_native_window = exercise
    watchdog = threading.Timer(60, app._quit)
    watchdog.daemon = True
    watchdog.start()
    app.start()
    watchdog.cancel()
    evidence['closed'] = app._closed
    path = output / 'native-caption-background.json'
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(evidence, ensure_ascii=False))
    assert 'error' not in evidence and app._closed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interactive', action='store_true', help='Show a separate harness window and test toast; requires explicit interactive authorization')
    args = parser.parse_args()
    (interactive_main if args.interactive else background_main)()
