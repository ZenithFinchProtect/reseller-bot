"""NFA relay: a tiny HTTP proxy the Cloudflare workers route through.

NFA blocks Cloudflare's egress IPs, but Railway's are fine, so the workers on
stock.nfaccount.com / nordicnfas.com forward their NFA calls here instead of
calling NFA directly. Requests must carry the shared secret in the
``X-Relay-Secret`` header (set RELAY_SECRET in the environment; the relay is
disabled when it's unset). Only the endpoint paths the sites actually use are
forwarded, so this can never become an open proxy.
"""
import hmac
import logging
import os

import aiohttp
from aiohttp import web

import config

log = logging.getLogger("reseller-bot.relay")

RELAY_SECRET = os.getenv("RELAY_SECRET", "").strip()
RELAY_PORT = int(os.getenv("PORT", os.getenv("RELAY_PORT", "8080")))

ALLOWED_PATHS = {
    "/api/v1/stock",
    "/api/v1/accounts",
    "/api/v1/activate",
    "/api/v1/create_exe",
    "/api/v1/create_keys",
    "/api/v1/check_account",
    "/api/v1/key_details",
    "/api/v1/unactivated_keys",
}


class RelayServer:
    def __init__(self, session):
        self.session = session
        self._runner = None

    async def start(self):
        if not RELAY_SECRET:
            log.info("RELAY_SECRET not set - NFA relay disabled")
            return
        app = web.Application()
        app.router.add_get("/relay/health", self._health)
        app.router.add_route("*", "/relay/{tail:.*}", self._relay)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", RELAY_PORT)
        await site.start()
        log.info("NFA relay listening on port %s", RELAY_PORT)

    async def stop(self):
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _health(self, request):
        return web.json_response({"status": "ok"})

    async def _relay(self, request):
        secret = request.headers.get("X-Relay-Secret", "")
        if not hmac.compare_digest(secret, RELAY_SECRET):
            return web.json_response(
                {"status": "error", "message": "Forbidden"}, status=403
            )
        path = "/" + request.match_info["tail"]
        if path not in ALLOWED_PATHS:
            return web.json_response(
                {"status": "error", "message": "Not found"}, status=404
            )
        url = f"{config.NFA_API_BASE}{path}"
        headers = {
            "X-API-Key": config.NFA_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = await request.read()
        try:
            async with self.session.request(
                request.method,
                url,
                params=request.rel_url.query,
                data=body if body else None,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                payload = await resp.read()
                return web.Response(
                    body=payload,
                    status=resp.status,
                    content_type="application/json",
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("relay upstream error: %s", exc)
            return web.json_response(
                {"status": "error", "message": "Upstream unavailable"}, status=502
            )
