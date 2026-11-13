# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Expose the frontend's methods without exposing Bridge-owned Python objects."""
from __future__ import annotations

import inspect
from functools import wraps


# Keep this explicit: a future public Bridge helper must not silently become RPC.
# The frontend contract test checks these names against js/api.js.
WEBVIEW_API_METHODS = frozenset({
    "get_product_notifications", "pick_video_scan_directory", "open_url",
    "account_logout", "activate_subscription_demo", "apply_setup_fixes",
    "pick_gpu_pack",
    "install_gpu_pack",
    "batch_agent_translate", "begin_browser_pairing", "begin_subscription",
    "browser_status", "buyer_verification_poll", "buyer_verification_start",
    "buyer_verification_stop", "cache_status", "cancel_account_login",
    "cancel_tasks", "check_subscription_service", "check_update", "cleanup_cache",
    "cleanup_orphans", "clear_completed", "complete_account_login", "data_reconcile",
    "delete_all_models", "delete_files", "delete_model", "delete_tasks", "delete_texts",
    "delete_video", "download_from_url", "download_model", "email_login_start",
    "email_login_verify", "export_backup", "export_diagnostics", "export_subtitles",
    "get_account_devices", "get_agent_setup_guide", "get_agent_control_config", "get_batch_agent_status",
    "get_bootstrap", "get_documents", "get_downloaded_videos", "get_models", "get_agent_activity",
    "get_video_scan_status", "cancel_video_scan", "get_failed_task_retention",
    "get_reader", "get_settings", "get_setup_check", "get_subscription_contract",
    "get_subscription_plans", "get_subscription_status", "get_summary", "get_tag_index",
    "get_tasks", "get_task_cover", "get_translation_status", "get_task_gallery", "get_video_preview", "get_window_chrome", "get_worker",
    "google_login_cancel", "google_login_start", "web_login_start", "web_login_poll", "web_login_cancel", "hide_window", "import_backup",
    "import_files", "minimize_window", "open_chrome_extensions", "open_directory",
    "open_output_dir", "open_path", "open_video_location", "open_video_page",
    "pick_and_import", "pick_folder_and_import", "pick_notification_sound", "pick_open_file", "pick_save_file",
    "pick_setting_dir", "play_media", "preview_notification_sound", "quit_app", "redeem_subscription_order", "redeem_beta_card",
    "refresh_subscription", "requeue_tasks", "retranscribe_tasks", "retry_downloads",
    "translate_text",
    "run_subscription_acceptance", "save_settings", "set_category", "set_job_tags",
    "set_launch_at_startup", "set_native_theme", "system_notify", "toggle_maximize_window",
    "transcribe_download", "unbind_account_device", "update_account_profile",
    "update_reader_text", "wechat_login_poll", "wechat_login_start", "worker_toggle",
    "get_manual_payment_config", "list_manual_payment_requests", "create_manual_payment_request",
    "submit_manual_payment_request", "get_manual_payment_request", "cancel_manual_payment_request",
    "get_alipay_payment_config", "list_alipay_orders", "create_alipay_order",
    "get_alipay_order", "query_alipay_order", "open_alipay_cashier",
})


def _rpc_function(method):
    @wraps(method)
    def invoke(*args, **kwargs):
        return method(*args, **kwargs)

    # pywebview.expose uses getfullargspec; a bound method would include `self`.
    # Its public signature keeps all real arguments and defaults without shifting.
    invoke.__signature__ = inspect.signature(method)
    return invoke


def expose_bridge(window, bridge) -> None:
    """Register only top-level functions; leave recursive API fallback empty."""
    if window._js_api is not None:
        raise ValueError("WebView must have js_api=None before exposing the frontend")
    functions = []
    for name in sorted(WEBVIEW_API_METHODS):
        method = getattr(bridge, name)
        if not inspect.ismethod(method) or method.__self__ is not bridge:
            raise TypeError(f"Bridge RPC must be a bound method: {name}")
        functions.append(_rpc_function(method))
    window.expose(*functions)
