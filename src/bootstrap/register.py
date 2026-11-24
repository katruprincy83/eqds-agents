"""Self-service specialist registration (no moderator code change, no moderator redeploy, no approval step for the demo).

A specialist is an app named `<prefix>-sp-<key><suffix>` with its own codebase and bundle (scripts/new_agent.py creates one).
Its spec is what the running app serves at /info. `register` checks it, grants it the access it declares, lets the moderator call it and writes its registry row.
The moderator reads the registry, so the agent is available as soon as the moderator refreshes (60 s cache, or at once
through the UI's registration page or `POST /registry/refresh`).
"""
from __future__ import annotations

import time

import httpx

from eqds_agentkit import contract, lakebase
from eqds_agentkit.registry import upsert_specialist

from eqds_agentkit.specs import REQUIRED, RegistrationError, spec_from_info, validate_spec  # noqa: F401

from . import steps


def spec_from_app(ctx, key: str) -> dict:
    """CLI path: read /info from the deployed app with the caller's own login (the job cannot: Apps refuses its token)."""
    app = ctx.w.apps.get(ctx.env.app_name(f"sp-{key}"))
    r = httpx.get(f"{app.url.rstrip('/')}/info", headers=ctx.w.config.authenticate(), timeout=60)
    if r.status_code != 200:
        raise RegistrationError(f"{app.url}/info returned {r.status_code}")
    return spec_from_info(r.json(), key)


def check_contract(ctx, key: str, url: str) -> str:
    """Call the specialist once the way the moderator will and require a valid answer back."""
    h = ctx.w.config.authenticate()
    r = httpx.get(f"{url}/healthz", headers=h, timeout=60)
    if r.status_code != 200:
        raise RegistrationError(f"{url}/healthz returned {r.status_code}" + (f" (redirect to {r.headers.get('location', '')[:120]})" if r.headers.get("location") else ""))
    body = contract.request("Registration check. Answer in one short sentence that you are ready.", run_id=f"register-{int(time.time())}",
                            round=1, key=key, caller="registration")
    r = httpx.post(f"{url}/invocations", json=body, headers=h, timeout=240)
    if r.status_code != 200:
        where = r.headers.get("location", "")
        raise RegistrationError(f"{url}/invocations returned {r.status_code}" + (f" (redirect to {where[:120]})" if where else f": {r.text[:160]}"))
    try:
        out = contract.parse_response(r.json())
    except Exception as e:  # noqa: BLE001
        raise RegistrationError(f"response does not follow the specialist contract: {str(e)[:120]}") from e
    if not (out.text or out.claims):
        raise RegistrationError("specialist returned no text and no claims")
    return (out.text or "")[:120]


def register(ctx, key: str, *, contract_checked: bool = False, spec: dict | None = None) -> dict:
    say = ctx.say
    sp = validate_spec(spec if spec is not None else spec_from_app(ctx, key), key)  # the agent's own app is the source of truth
    comp = f"sp-{key}"
    name = ctx.env.app_name(comp)
    try:
        app = ctx.w.apps.get(name)
    except Exception as e:  # noqa: BLE001
        raise RegistrationError(f"app {name} is not deployed ({str(e)[:80]}). Deploy the agent app first.") from e
    if not app.url:
        raise RegistrationError(f"app {name} has no URL yet (is it running?)")
    url = app.url.rstrip("/")
    say(f"1/5 spec ok: {key} ({sp['display_name']}, tag {sp['tag_prefix']}) -> {url}")

    conn = lakebase.connect(steps._lb_target(ctx))
    try:
        clash = conn.execute("SELECT key FROM registry.specialists WHERE tag_prefix = %s AND key <> %s", (sp["tag_prefix"], key)).fetchone()
        if clash:
            raise RegistrationError(f"tag_prefix {sp['tag_prefix']} is already used by '{clash['key']}'")
        # An agent can only answer a real call once it has been granted access to its data and models, so the live contract check
        # comes AFTER the grants. From the UI the job cannot make the call itself (its token is refused by the agent apps), so the job
        # leaves the row disabled and the UI makes the call with its own identity, then enables it.
        say("2/5 spec and app ok" + (" (shallow contract check done by the UI)" if contract_checked else ""))
        steps.apply_component_grants(ctx, conn, comp, app.service_principal_client_id, steps.access_of(sp))
        say("3/5 access granted to the agent's service principal")
        steps._grant_app_use(ctx, comp, ["moderator"])  # the moderator may call it
        say("4/5 moderator may call the agent")
        upsert_specialist(conn, sp, url, ctx.env.target, name)
        if contract_checked:
            conn.execute("UPDATE registry.specialists SET enabled = false WHERE key = %s", (key,))
            say("5/5 registry row written (disabled until the UI's live check passes)")
        else:
            reply = check_contract(ctx, key, url)
            say(f"5/5 contract ok: {reply!r}")
            conn.execute("UPDATE registry.specialists SET enabled = true WHERE key = %s", (key,))
            say("registry row enabled")
    finally:
        conn.close()
    return {"key": key, "url": url, "app": name}


def set_enabled(ctx, key: str, enabled: bool) -> None:
    conn = lakebase.connect(steps._lb_target(ctx))
    try:
        n = conn.execute("UPDATE registry.specialists SET enabled = %s, updated_at = now() WHERE key = %s RETURNING key", (enabled, key)).fetchone()
        if not n:
            raise RegistrationError(f"'{key}' is not registered")
    finally:
        conn.close()


def unregister(ctx, key: str) -> None:
    """Remove the registry row (the app and its grants stay; register again to bring it back)."""
    conn = lakebase.connect(steps._lb_target(ctx))
    try:
        conn.execute("DELETE FROM registry.specialists WHERE key = %s", (key,))
    finally:
        conn.close()


def listing(ctx) -> list[dict]:
    conn = lakebase.connect(steps._lb_target(ctx))
    try:
        return conn.execute("SELECT key, display_name, tag_prefix, enabled, app, version, owner FROM registry.specialists ORDER BY key").fetchall()
    finally:
        conn.close()
