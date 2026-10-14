"""Apify Actor wrapper around the IPTVV Canada trial automation.

Reads the Actor input, maps it onto the environment variables that
``iptvvcanada_automation`` reads at import time, runs the (synchronous,
Selenium-based) flow in a worker thread, and reports the result to the Apify
dataset / key-value store and, optionally, to a webhook callback.
"""
import asyncio
import os

from apify import Actor

# Maps Actor input keys -> the env vars iptvvcanada_automation reads at import.
# Only non-empty values are applied so the module's own defaults still win.
_ENV_MAP = {
    "twoCaptchaApiKey": "TWOCAPTCHA_API_KEY",
    "webhookAuthToken": "WEBHOOK_AUTH_TOKEN",
    "emailBackend": "IPTVV_EMAIL_BACKEND",
    "emailFallbacks": "IPTVV_EMAIL_FALLBACKS",
    "tmailyDomain": "TMAILY_DOMAIN",
    "boomlifyApiKey": "BOOMLIFY_API_KEY",
    "boomlifyEmailTime": "BOOMLIFY_EMAIL_TIME",
    "mailcxApiToken": "MAILCX_API_TOKEN",
    "mailcxDomain": "MAILCX_DOMAIN",
    "gmailAddress": "IPTVV_GMAIL_ADDRESS",
    "gmailAppPassword": "IPTVV_GMAIL_APP_PASSWORD",
    "baseUrl": "IPTVV_BASE_URL",
    "emailMaxWaitSeconds": "IPTVV_EMAIL_MAX_WAIT_SECONDS",
    "emailPollSeconds": "IPTVV_EMAIL_POLL_SECONDS",
    "cloudflareWaitSeconds": "IPTVV_CLOUDFLARE_WAIT_SECONDS",
    "iboPlayerCookie": "IPTVV_IBOPLAYER_COOKIE",
    "iboPlayerMacAddress": "IPTVV_IBOPLAYER_MAC_ADDRESS",
    "iboPlayerDeviceKey": "IPTVV_IBOPLAYER_DEVICE_KEY",
    "iboPlayerPlaylistUrlId": "IPTVV_IBOPLAYER_PLAYLIST_URL_ID",
    "iboPlayerPlaylistName": "IPTVV_IBOPLAYER_PLAYLIST_NAME",
    "githubToken": "IPTVV_GITHUB_TOKEN",
    "githubRepo": "IPTVV_GITHUB_REPO",
    "githubSecretName": "IPTVV_GITHUB_SECRET_NAME",
    "githubWorkflow": "IPTVV_GITHUB_WORKFLOW",
    "githubRef": "IPTVV_GITHUB_REF",
}


def _apply_input_to_env(actor_input):
    """Translate Actor input into the env vars the automation module expects."""
    for key, env_name in _ENV_MAP.items():
        value = actor_input.get(key)
        if value not in (None, ""):
            os.environ[env_name] = str(value)

    if actor_input.get("iboPlayerEnabled"):
        os.environ["IPTVV_IBOPLAYER_ENABLED"] = "True"

    if actor_input.get("githubEnabled"):
        os.environ["IPTVV_GITHUB_ENABLED"] = "True"

    # Apify runs Chrome headed under an Xvfb virtual display (see the actor's
    # `xvfb-run ...` launch command). Headed mode loads the proxy-auth extension
    # reliably (new-headless does not) and is less bot-detectable, so we do NOT
    # force headless here.
    os.environ.setdefault("HEADLESS", "False")
    os.environ["AUTO_EXIT"] = "True"
    # Apify containers can't write to /app; use a writable scratch dir for artifacts.
    os.environ.setdefault("IPTVV_DEBUG_DIR", "/tmp/iptvv-logs")


async def _apply_proxy_to_env(actor_input):
    """Resolve an Apify residential proxy URL and expose it as IPTVV_PROXY_URL.

    IPTVV's Cloudflare IP-blocks datacenter IPs, so browser egress must go
    through a residential exit. Defaults to the RESIDENTIAL group in Canada
    (the target site is iptvv.ca); both are overridable via input.
    """
    if not actor_input.get("useApifyProxy", True):
        Actor.log.info("Apify proxy disabled by input; browser will use a direct connection")
        return

    groups_raw = actor_input.get("proxyGroups") or "RESIDENTIAL"
    groups = [g.strip() for g in str(groups_raw).split(",") if g.strip()]
    country = (actor_input.get("proxyCountry") or "CA").strip() or None

    try:
        proxy_cfg = await Actor.create_proxy_configuration(
            groups=groups, country_code=country
        )
        if proxy_cfg is None:
            Actor.log.warning("No Apify proxy available on this account; using direct connection")
            return
        proxy_url = await proxy_cfg.new_url()
        os.environ["IPTVV_PROXY_URL"] = proxy_url
        Actor.log.info(f"Browser will egress via Apify proxy (groups={groups}, country={country})")
    except Exception as exc:  # noqa: BLE001 - proxy is best-effort; fall back to direct
        Actor.log.warning(f"Failed to set up Apify proxy ({exc}); using direct connection")


async def main():
    async with Actor:
        actor_input = await Actor.get_input() or {}
        _apply_input_to_env(actor_input)
        await _apply_proxy_to_env(actor_input)

        user_id = actor_input.get("userId")
        callback_url = actor_input.get("callbackUrl")

        # Import only AFTER env vars are set: the module reads them at import time.
        import iptvvcanada_automation as bot

        try:
            Actor.log.info("Starting IPTVV Canada trial automation...")
            result = await asyncio.to_thread(bot.run_automation)
        except (bot.CloudflareBlockedError, bot.TrialRejectedError) as exc:
            await _report_failure(bot, callback_url, user_id, exc)
            await Actor.fail(status_message=f"{type(exc).__name__}: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - surface any failure to the run
            Actor.log.exception("Automation failed")
            await _report_failure(bot, callback_url, user_id, exc)
            await Actor.fail(status_message=f"Automation failed: {exc}")
            return

        Actor.log.info("Credentials extracted successfully")
        output = {"status": "success", **result}
        await Actor.push_data(output)
        await Actor.set_value("OUTPUT", output)

        if callback_url:
            await asyncio.to_thread(
                bot.send_webhook_callback,
                callback_url,
                user_id,
                "success",
                result["username"],
                result["password"],
                result["host"],
                result["m3u_url"],
            )


async def _upload_debug_artifacts():
    """Push the newest screenshot/HTML snapshot to the KV store for inspection.

    The automation writes debug artifacts to IPTVV_DEBUG_DIR on the container's
    ephemeral disk; copy the latest of each to the run's key-value store so they
    can be downloaded from the Apify Console after a failure.
    """
    import glob

    debug_dir = os.environ.get("IPTVV_DEBUG_DIR", "/tmp/iptvv-logs")
    for ext, key, content_type in (
        ("html", "DEBUG_PAGE", "text/html"),
        ("png", "DEBUG_SCREENSHOT", "image/png"),
    ):
        try:
            matches = glob.glob(os.path.join(debug_dir, f"*.{ext}"))
            if not matches:
                continue
            newest = max(matches, key=os.path.getmtime)
            with open(newest, "rb") as fh:
                await Actor.set_value(key, fh.read(), content_type=content_type)
            Actor.log.info(f"Uploaded {os.path.basename(newest)} to KV store as {key}")
        except Exception as exc:  # noqa: BLE001 - diagnostics are best-effort
            Actor.log.warning(f"Could not upload {ext} debug artifact: {exc}")


async def _report_failure(bot, callback_url, user_id, exc):
    """Persist the failure to the dataset/KV store and fire the webhook."""
    output = {"status": "failed", "error": str(exc)}
    await Actor.push_data(output)
    await Actor.set_value("OUTPUT", output)
    await _upload_debug_artifacts()
    if callback_url:
        await asyncio.to_thread(
            bot.send_webhook_callback,
            callback_url,
            user_id,
            "failed",
            None,
            None,
            None,
            None,
            str(exc),
        )
