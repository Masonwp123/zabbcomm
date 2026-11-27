"""The internet-facing FastAPI app: the check-in endpoint and its request guard."""

import asyncio
import contextlib
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Callable
from typing import Annotated

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, StringConstraints
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .deny import DenyList, uninstall_signature
from .ids import slugify
from .monitor import Monitor
from .persistence import SnapshotStore
from .registry import LimitReachedError, Registry
from .settings import Settings, load_settings
from .zabbix import DryRunSender, Sender, ZabbixSender

log = logging.getLogger("zabbcomm")

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_NONCE = re.compile(r"[0-9a-f]{32}")

MAX_BODY_BYTES = 8192
"""A check-in with the longest allowed names, fully escaped, is well under this."""

PROXY_WARNING_INTERVAL = 3600
"""Seconds between repeats of the 'proxy isn't trusted' warning."""


def _no_control_chars(value: str) -> str:
    if _CONTROL_CHARS.search(value):
        raise ValueError("must not contain control characters")
    return value


Name = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    AfterValidator(_no_control_chars),
]


class CheckIn(BaseModel):
    org: Name
    device: Name
    agent_version: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, max_length=32),
            AfterValidator(_no_control_chars),
        ]
        | None
    ) = None
    is_server: bool | None = None


class RequestGuard:
    """Protects the internet-facing side. Anything but GET / HEAD needs the bearer token,
    checked before any of the body is read, and bodies are capped at MAX_BODY_BYTES."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return

        authorization = dict(scope["headers"]).get(b"authorization", b"")
        scheme, _, credentials = authorization.partition(b" ")
        if scheme.lower() != b"bearer" or not secrets.compare_digest(
            credentials.strip(), self.token
        ):
            response = JSONResponse(
                {"detail": "invalid or missing token"},
                status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return

        body = bytearray()
        more_body = True
        while more_body:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more_body = message.get("more_body", False)
            if len(body) > MAX_BODY_BYTES:
                response = JSONResponse({"detail": "request body too large"}, 413)
                await response(scope, receive, send)
                return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)


def create_app(
    settings: Settings | None = None,
    *,
    sender: Sender | None = None,
    clock: Callable[[], float] = time.time,
) -> FastAPI:
    """App factory (`uvicorn zabbcomm:create_app --factory`). Tests pass their own
    settings, sender and clock."""
    settings = settings if settings is not None else load_settings()
    registry = Registry(
        settings.max_orgs,
        settings.max_devices_per_org,
        settings.confirm_window,
        settings.site_ipv6_prefix,
    )
    store = SnapshotStore(settings.data_dir / "registry.json")
    deny_list = DenyList(settings.data_dir / "deny.json")
    token = settings.api_token.get_secret_value()
    if sender is None:
        sender = (
            DryRunSender()
            if settings.zabbix_dry_run
            else ZabbixSender(settings.zabbix_server, settings.zabbix_port)
        )
    proxy_warned_at = float("-inf")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        registry.replace_all(store.load())
        log.info("loaded %d devices from %s", len(registry.devices()), store.path)
        monitor = Monitor(
            settings, registry, sender, store, started_at=clock(), deny_list=deny_list
        )
        task = asyncio.create_task(monitor.run_forever(clock))
        try:
            yield
        finally:
            await monitor.stop(task)

    def warn_if_proxy_ignored(request: Request, ip: str | None) -> None:
        """uvicorn uses X-Forwarded-For only from FORWARDED_ALLOW_IPS. If a proxy sent it
        and wasn't trusted, every device seems to share the proxy's address."""
        nonlocal proxy_warned_at
        forwarded = request.headers.get("x-forwarded-for")
        if not forwarded or ip in {part.strip() for part in forwarded.split(",")}:
            return
        if clock() - proxy_warned_at < PROXY_WARNING_INTERVAL:
            return
        proxy_warned_at = clock()
        log.warning(
            "check-ins arrive through a proxy at %s that isn't in FORWARDED_ALLOW_IPS, so "
            "its address is recorded instead of each device's and sites can't be told "
            "apart. Add it to FORWARDED_ALLOW_IPS.",
            ip,
        )

    # Internet-facing: no interactive docs or schema.
    app = FastAPI(
        title="zabbcomm", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.registry = registry
    app.add_middleware(RequestGuard, token=token)

    @app.post("/api/v1/checkin", status_code=status.HTTP_204_NO_CONTENT)
    async def checkin(body: CheckIn, request: Request) -> Response:
        # `async def` on purpose: it runs on the event loop (a plain `def` would run in a
        # threadpool), so registry updates can never interleave with a monitor cycle.
        ip = request.client.host if request.client else None
        warn_if_proxy_ignored(request, ip)
        deny_list.refresh()
        if deny_list.denies(slugify(body.org), slugify(body.device)):
            registry.remove_denied(deny_list.denies)
            log.warning("denied check-in %s / %s from %s", body.org, body.device, ip)
            nonce = request.headers.get("x-zabbcomm-nonce", "")
            headers = {}
            if _NONCE.fullmatch(nonce):
                headers["X-Zabbcomm-Uninstall"] = uninstall_signature(token, nonce)
            return JSONResponse({"detail": "device denied"}, status.HTTP_410_GONE, headers)
        try:
            registry.check_in(
                body.org, body.device, ip, body.agent_version, clock(), body.is_server
            )
        except LimitReachedError as exc:
            log.warning("rejected check-in %s / %s from %s: %s", body.org, body.device, ip, exc)
            raise HTTPException(status.HTTP_403_FORBIDDEN, "registration limit reached") from None
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
