"""Integrationsöversikt och AI-leverantör."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.deps import DbDep, PrincipalDep, WebPrincipalDep, owned_vehicle_ids
from app.api.errors import ApiError, bad_request
from app.jobs import enqueue
from app.models.enums import IntegrationProvider, IntegrationStatus
from app.services import audit, integrations
from app.services import settings as settings_svc
from app.services.classify import llm

router = APIRouter(prefix="/integrations", tags=["Integration health"])

PROVIDERS = (
    IntegrationProvider.VOLVO,
    IntegrationProvider.MICROSOFT,
    IntegrationProvider.HOME_ASSISTANT,
    IntegrationProvider.AI,
)
STATUS_LABELS = {
    IntegrationStatus.NOT_CONFIGURED: "Inte konfigurerad",
    IntegrationStatus.CONFIGURED: "Konfigurerad – inte ansluten",
    IntegrationStatus.DEMO: "Demoläge",
    IntegrationStatus.CONNECTED: "Ansluten",
    IntegrationStatus.NEEDS_REAUTH: "Behöver anslutas på nytt",
    IntegrationStatus.ERROR: "Fel",
}


class AIConfig(BaseModel):
    provider: str = Field(pattern="^(none|anthropic|openai)$")
    model: str = Field(default="", max_length=120)
    api_key: str | None = Field(default=None, max_length=500)


def _ai_view(db, user_id) -> dict[str, Any]:
    conn = integrations.get(db, user_id, IntegrationProvider.AI)
    ai = settings_svc.get_section(db, user_id, "ai")
    view = integrations.public_view(conn, IntegrationProvider.AI)
    view.update(
        {
            "provider_name": ai["provider"],
            "model": ai["model"] or {"anthropic": llm.DEFAULT_ANTHROPIC_MODEL, "openai": llm.DEFAULT_OPENAI_MODEL}.get(ai["provider"], ""),
            "default_models": {"anthropic": llm.DEFAULT_ANTHROPIC_MODEL, "openai": llm.DEFAULT_OPENAI_MODEL},
            "has_key": {p: bool(integrations.secrets(conn).get(f"{p}_api_key")) for p in ("anthropic", "openai")},
            "privacy": {
                "sends": "Platsetiketter/orter, veckodag, tid, sträcka, regelsignaler och tidigare bekräftade syften.",
                "never_sends": "Koordinater, VIN, hemadress och privata platsnamn.",
                "send_calendar_subjects": ai["send_calendar_subjects"],
                "send_calendar_body": ai["send_calendar_body"],
                "send_street_addresses": ai["send_street_addresses"],
            },
            "rule_engine_only": ai["provider"] == "none",
        }
    )
    view.pop("config", None)
    return view


@router.get("", summary="Status för alla integrationer")
def overview(principal: PrincipalDep, db: DbDep) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for provider in PROVIDERS:
        if provider == IntegrationProvider.AI:
            view = _ai_view(db, principal.user.id)
        else:
            view = integrations.public_view(integrations.get(db, principal.user.id, provider), provider)
            # Konfigurationen kan innehålla adresser/konton men aldrig hemligheter.
        view["status_label"] = STATUS_LABELS.get(IntegrationStatus(view["status"]), view["status"])
        view["is_connected"] = view["status"] == IntegrationStatus.CONNECTED
        view["is_demo"] = view["status"] == IntegrationStatus.DEMO
        out[provider] = view
    return out


@router.get("/ai", summary="AI-leverantörens status")
def ai_status(principal: PrincipalDep, db: DbDep) -> dict[str, Any]:
    return _ai_view(db, principal.user.id)


@router.put("/ai", summary="Välj AI-leverantör, modell och API-nyckel")
def ai_configure(body: AIConfig, principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    user_id = principal.user.id
    conn = integrations.get_or_create(db, user_id, IntegrationProvider.AI)
    if body.api_key and body.provider != "none":
        integrations.set_secrets(conn, {f"{body.provider}_api_key": body.api_key.strip()})
    has_key = bool(integrations.secrets(conn).get(f"{body.provider}_api_key")) if body.provider != "none" else False
    if body.provider != "none" and not has_key:
        raise bad_request("Ange en API-nyckel för den valda leverantören.", "missing_api_key")
    settings_svc.update_section(db, user_id, "ai", {"provider": body.provider, "model": body.model.strip()})
    # Nyckeln är sparad men inte verifierad förrän ett lyckat test/anrop har gjorts.
    conn.status = IntegrationStatus.NOT_CONFIGURED if body.provider == "none" else IntegrationStatus.CONFIGURED
    conn.last_error = None
    audit.record(db, principal.actor, "integration.ai_configured", "integration", conn.id,
                 after={"provider": body.provider, "model": body.model})
    db.commit()
    return _ai_view(db, user_id)


@router.post("/ai/test", summary="Verifiera nyckel och modell mot leverantören")
def ai_test(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    user_id = principal.user.id
    ai = settings_svc.get_section(db, user_id, "ai")
    conn = integrations.get(db, user_id, IntegrationProvider.AI)
    if ai["provider"] == "none" or conn is None:
        raise bad_request("Ingen AI-leverantör är vald.", "not_configured")
    key = integrations.secrets(conn).get(f"{ai['provider']}_api_key")
    if not key:
        raise bad_request("API-nyckel saknas.", "missing_api_key")
    provider = llm.build_provider(ai["provider"], key, ai["model"] or None)
    try:
        name = provider.ping()  # type: ignore[union-attr]
    except llm.LLMError as exc:
        integrations.mark_error(conn, str(exc), needs_reauth=exc.auth)
        if not exc.auth:
            conn.status = IntegrationStatus.ERROR
        db.commit()
        raise ApiError(502, str(exc), "ai_error") from exc
    conn.status = IntegrationStatus.CONNECTED
    integrations.mark_success(conn)
    db.commit()
    return {"ok": True, "model": name, **_ai_view(db, user_id)}


@router.delete("/ai", summary="Ta bort API-nycklar och stäng av AI (reglerna fortsätter fungera)")
def ai_disconnect(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    user_id = principal.user.id
    conn = integrations.get(db, user_id, IntegrationProvider.AI)
    if conn is not None:
        integrations.disconnect(db, conn)
    settings_svc.update_section(db, user_id, "ai", {"provider": "none"})
    audit.record(db, principal.actor, "integration.disconnected", "integration", None, after={"provider": "ai"})
    db.commit()
    return _ai_view(db, user_id)


@router.post("/ai/classify-pending", summary="Kör AI-klassificering av oavgjorda resor i bakgrunden")
def ai_classify_pending(principal: WebPrincipalDep, db: DbDep) -> dict[str, Any]:
    vehicles = owned_vehicle_ids(db, principal.user.id)
    for vid in vehicles:
        enqueue("classify.pending", user_id=str(principal.user.id), vehicle_id=str(vid))
    return {"queued": len(vehicles)}
