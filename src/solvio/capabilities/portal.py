"""Angemeldete Portale — der Unterbau, noch nicht der Alltag.

Diese Stufe beweist einen Weg, sie oeffnet ihn nicht fuer alles. Es gibt genau
ein konfiguriertes Portal, genau zwei folgenreiche Aktionen, und beide brauchen
einen Menschen mit einem iPhone.

Die Reihenfolge ist der Inhalt:

    Bindung nachschlagen — konfiguriert, nicht von der Seite gelesen
      -> Seite befragen, OHNE etwas einzutragen
        -> Manifest bauen, mit Alias statt Geheimnis
          -> Freigabe am iPhone
            -> Geheimnis aus dem Tresor
              -> ausfuehren, wenn die Seite noch passt

Der dritte Schritt ist der, den man weglassen moechte und nicht darf. Ein Wert,
der vor der Freigabe im Feld steht, ist bereits verraten: moderne Seiten lesen
Eingaben beim Tippen mit, speichern Entwuerfe automatisch und schicken Telemetrie
los, lange bevor jemand auf „Absenden" drueckt. „Es ist ja noch nichts
abgeschickt" ist deshalb kein Argument, sondern ein Irrtum.

Und was zurueckkommt, bleibt fremd. Eine Anmeldung macht eine Webseite nicht
vertrauenswuerdig — ein Bankportal, ein Arbeitgeberportal, eine Behoerdenseite
koennen fremden Inhalt tragen wie jede andere Seite auch. `content_trust` bleibt
`untrusted_web`, nach der Anmeldung genau wie davor.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.contract import (
    CapabilityDeclined, CapabilityRefused, CapabilitySpec, ExecutionClass,
    ExecutorUnavailable,
)
from solvio.contracts.untrusted import neutralize
from solvio.logging_setup import get_logger
from solvio.portal.binding import binding_for_url, BINDINGS, binding_for
from solvio.portal.client import AmbiguousPortalOutcome, PortalUnavailable
from solvio.portal.manifest import LOGIN, ActionManifest, FieldBinding
from solvio.portal.readout import reduce_for
from solvio.portal.vault import PASSWORD, USERNAME, VaultError
# Der Import registriert die Studio-Ortskenntnis. Sie steht ausdruecklich in
# einem eigenen Modul: was nur fuer EIN Portal gilt, gehoert nicht in die
# Grundlage, die alle Portale teilen.
from solvio.portal import studio_readout  # noqa: F401
from solvio.security.mobile_approval.execution import NON_IDEMPOTENT_WRITE, READ_ONLY
from solvio.tools.base import RiskLevel

log = get_logger("portal")

#: Auch eine angemeldete Seite bleibt fremder Inhalt.
CONTENT_TRUST = "untrusted_web"

PREVIEW_CHARS = 900

SPECS: dict[str, CapabilitySpec] = {
    "portal_list": CapabilitySpec(
        name="portal_list", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {}},
        executor="portal", timeout=15.0,
        description="Nennt die eingerichteten Portale und ob ein Zugang hinterlegt ist."),
    "portal_open": CapabilitySpec(
        name="portal_open", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "portal": {"type": "string"}}, "required": ["portal"]},
        executor="portal", timeout=90.0, cancellable=True,
        description="Oeffnet die Anmeldeseite eines Portals. Traegt nichts ein."),
    "portal_login": CapabilitySpec(
        name="portal_login", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
        input_schema={"type": "object", "properties": {
            "session": {"type": "string"}}, "required": ["session"]},
        executor="portal", timeout=120.0,
        description="Meldet sich an. Braucht die Freigabe auf dem iPhone."),
    "portal_read": CapabilitySpec(
        name="portal_read", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "session": {"type": "string"}}, "required": ["session"]},
        executor="portal", timeout=60.0, cancellable=True,
        description="Liest die geoeffnete Portalseite."),
    "portal_status": CapabilitySpec(
        name="portal_status", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "session": {"type": "string"}}, "required": ["session"]},
        executor="portal", timeout=60.0, cancellable=True,
        description="Fasst Kontostand, Kennzahlen und erkennbare Hinweise des "
                    "Portals zusammen. Aendert nichts."),
    "portal_close": CapabilitySpec(
        name="portal_close", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "session": {"type": "string"}}, "required": ["session"]},
        executor="portal", timeout=45.0,
        description="Beendet die Sitzung und loescht das Browserprofil."),
}

_MESSAGES = {
    "unknown_portal": "Dieses Portal kenne ich nicht.",
    "unknown_session": "Diese Portalsitzung habe ich nicht offen.",
    "no_credential": "Fuer dieses Portal ist kein Zugang hinterlegt.",
    "origin_not_bound": "Diese Adresse gehoert nicht zu dem Portal.",
    "approval_drift": "Die Seite hat sich geaendert, seit du zugestimmt hast — "
                      "ich habe nichts gemacht.",
    "form_not_found": "Das Anmeldeformular finde ich dort nicht.",
    "login_failed": "Die Anmeldung wurde abgelehnt.",
    "session_expired": "Die Portalsitzung ist nicht mehr angemeldet.",
    "portal_unavailable": "Der Portal-Arbeiter laeuft nicht — es ist nichts passiert.",
}


def _neutralize_deep(value: Any) -> Any:
    """Jede Zeichenkette aus einer Reduktion durch dieselbe Entschaerfung.

    Eine Reduktion liest Seiteninhalt. Dass sie ihn ordnet, macht ihn nicht
    vertrauenswuerdiger — eine Anweisung in einem Kontonamen bleibt eine
    Anweisung in einem Kontonamen.
    """
    if isinstance(value, str):
        return neutralize(value, limit=200)
    if isinstance(value, list):
        return [_neutralize_deep(item) for item in value[:24]]
    if isinstance(value, dict):
        return {str(key): _neutralize_deep(item) for key, item in value.items()}
    return value


class PortalCapabilities:
    """Die Handler. Autoritaet kommt vom Router — und nie von der Seite."""

    def __init__(self, client: Any, vault: Any, router: Any = None) -> None:
        self.client = client
        self.vault = vault
        #: Nur zum Aufraeumen: wird eine Sitzung geschlossen, ist eine noch offene
        #: Anmeldefreigabe dazu gegenstandslos.
        self.router = router
        #: Was gerade zur Freigabe steht, je Sitzung. Das Manifest lebt hier und
        #: nicht in den Argumenten: der Digest bindet es ohnehin, und so bleibt
        #: die Werkzeugflaeche klein.
        self._pending: dict[str, ActionManifest] = {}

    # -- Lesen ---------------------------------------------------------------
    async def list_portals(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        entries = []
        for portal_id, binding in sorted(BINDINGS.items()):
            entries.append({
                "portal": portal_id,
                "adresse": binding.login_origin,
                "zugang": binding.credential_alias,
                # Nur ob, nie was. Der Alias ist eine Kennung, kein Geheimnis.
                "hinterlegt": self.vault.has(binding.credential_alias, PASSWORD),
            })
        return {"portale": entries, "content_trust": CONTENT_TRUST}

    async def open(self, arguments: dict[str, Any]) -> dict[str, Any]:
        binding = self._binding(arguments.get("portal", ""))
        if not self.vault.has(binding.credential_alias, PASSWORD):
            raise CapabilityDeclined("no_credential", _MESSAGES["no_credential"])
        try:
            session = await self.client.open_session(binding)
            reply = await self.client.navigate(session, binding.login_url)
        except PortalUnavailable as exc:
            raise ExecutorUnavailable("portal_unavailable") from exc
        if not reply.get("ok"):
            raise CapabilityDeclined(str(reply.get("reason", "navigation_failed")),
                                     _MESSAGES.get(str(reply.get("reason", "")),
                                                   "Die Seite liess sich nicht laden."))
        return {"session": session, "portal": binding.portal_id,
                "url": reply.get("url", ""),
                "titel": neutralize(reply.get("title", ""), limit=200),
                "angemeldet": bool(reply.get("authenticated")),
                "content_trust": CONTENT_TRUST}

    async def read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        session = str(arguments.get("session", ""))
        try:
            reply = await self.client.read(session)
        except PortalUnavailable as exc:
            raise ExecutorUnavailable("portal_unavailable") from exc
        if not reply.get("ok"):
            raise CapabilityDeclined("unknown_session", _MESSAGES["unknown_session"])
        return {"session": session, "url": reply.get("url", ""),
                "titel": neutralize(reply.get("title", ""), limit=200),
                "text": neutralize(reply.get("text", ""), limit=12000),
                "angemeldet": bool(reply.get("authenticated")),
                "content_trust": CONTENT_TRUST}

    async def status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Der eine Lese-Ablauf: was steht dort, und stimmt etwas nicht?

        Datensparsam nach §9: es geht nicht die Seite ans Modell, sondern eine
        geordnete Auswahl — Konto, Bereiche, Ueberschriften, ausgewiesene
        Hinweise, beschriftete Zahlen. Der lange Fliesstext bleibt hier.
        """
        session = str(arguments.get("session", ""))
        try:
            reply = await self.client.read(session, structured=True)
        except PortalUnavailable as exc:
            raise ExecutorUnavailable("portal_unavailable") from exc
        if not reply.get("ok"):
            raise CapabilityDeclined("unknown_session", _MESSAGES["unknown_session"])
        if not reply.get("authenticated"):
            # Nicht so tun, als waere die Sitzung noch gueltig. Eine abgelaufene
            # Anmeldung liefert dieselbe Seite wie eine nie erfolgte.
            raise CapabilityDeclined("session_expired", _MESSAGES["session_expired"])

        structure = reply.get("structure") or {}
        def pick(name: str, limit: int) -> list[str]:
            return [neutralize(x, limit=200) for x in (structure.get(name) or [])[:limit]]

        url = str(reply.get("url", ""))
        result = {"session": session, "url": url,
                  "titel": neutralize(reply.get("title", ""), limit=200),
                  "konto": pick("account", 6),
                  "bereiche": pick("sections", 20),
                  "ueberschriften": pick("headings", 12),
                  "hinweise": pick("alerts", 8),
                  "kennzahlen": pick("metrics", 20),
                  "content_trust": CONTENT_TRUST}

        # Die generische Auswahl bleibt stehen. Kennt dieses Portal eine eigene
        # Reduktion, tritt sie DANEBEN — nicht an ihre Stelle. So bleibt ein
        # Bericht vergleichbar, auch wenn eine Ortskenntnis morgen ins Leere
        # greift, und niemand muss raten, welche Felder gerade gelten.
        binding = binding_for_url(url)
        if binding is not None:
            detail = reduce_for(binding.portal_id, reply)
            if detail:
                result["auswertung"] = _neutralize_deep(detail)
        return result

    async def close(self, arguments: dict[str, Any]) -> dict[str, Any]:
        session = str(arguments.get("session", ""))
        self._pending.pop(session, None)
        # Wer die Sitzung schliesst, will sich nicht mehr anmelden. Eine
        # Freigabe, die das noch anbietet, ist eine Falle.
        if self.router is not None:
            await self.router.abandon_outstanding("portal_login")
        try:
            reply = await self.client.close_session(session)
        except PortalUnavailable as exc:
            raise ExecutorUnavailable("portal_unavailable") from exc
        return {"session": session, "beendet": bool(reply.get("ok")),
                "content_trust": CONTENT_TRUST}

    # -- Anmelden ------------------------------------------------------------
    async def prepare_login(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Baut das Manifest — und traegt dabei NICHTS in die Seite ein.

        Wird vom Router aufgerufen, bevor die Freigabe angefordert wird. Was hier
        entsteht, ist der Text, den der Nutzer sieht, und zugleich das, wogegen
        die Seite spaeter geprueft wird.
        """
        session = str(arguments.get("session", ""))
        manifest = await self._build(session)
        self._pending[session] = manifest
        return manifest.approval_arguments()

    async def login(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Fuehrt die freigegebene Anmeldung aus. Erst jetzt faellt ein Geheimnis."""
        session = str(arguments.get("session", ""))
        manifest = self._pending.get(session)
        if manifest is None:
            raise CapabilityDeclined("unknown_session", _MESSAGES["unknown_session"])
        binding = self._binding(manifest.portal_id)

        # Der einzige Ort, an dem ein Wert den Tresor verlaesst. Er geht direkt
        # ueber die Naht zum Arbeiter und steht in keinem Argument, keinem
        # Freigabetext und keinem Protokoll.
        try:
            secrets = {
                f"{binding.credential_alias}#user":
                    self.vault.get(binding.credential_alias, USERNAME),
                binding.credential_alias:
                    self.vault.get(binding.credential_alias, PASSWORD),
            }
        except VaultError as exc:
            raise CapabilityDeclined("no_credential", _MESSAGES["no_credential"]) from exc

        try:
            reply = await self.client.execute(session, manifest, secrets=secrets,
                                              action_id=manifest.digest()[:16])
        except AmbiguousPortalOutcome as exc:
            # Eine ausgebliebene Antwort heisst nicht „nichts ist passiert".
            from solvio.capabilities.contract import AmbiguousExecution
            raise AmbiguousExecution("portal_login") from exc
        except PortalUnavailable as exc:
            raise ExecutorUnavailable("portal_unavailable") from exc
        finally:
            secrets.clear()
            self._pending.pop(session, None)

        if not reply.get("ok"):
            reason = str(reply.get("reason", "login_failed"))
            message = _MESSAGES.get(reason, _MESSAGES["login_failed"])
            if reason in ("approval_drift", "origin_not_bound"):
                raise CapabilityRefused(reason, message)
            raise CapabilityDeclined(reason, message)
        if not reply.get("authenticated"):
            raise CapabilityDeclined("login_failed", _MESSAGES["login_failed"])

        log.info("portal.login_completed", portal=manifest.portal_id,
                 permit_used=bool(reply.get("permit_used")))
        return {"session": session, "portal": manifest.portal_id,
                "angemeldet": True, "url": reply.get("url", ""),
                "titel": neutralize(reply.get("title", ""), limit=200),
                "auszug": neutralize(reply.get("text", ""), limit=PREVIEW_CHARS),
                "content_trust": CONTENT_TRUST}

    # -- intern --------------------------------------------------------------
    def _binding(self, portal_id: str):
        binding = binding_for(str(portal_id or "").strip())
        if binding is None:
            raise CapabilityDeclined("unknown_portal", _MESSAGES["unknown_portal"])
        return binding

    async def _build(self, session: str) -> ActionManifest:
        try:
            probe = await self.client.probe(session)
        except PortalUnavailable as exc:
            raise ExecutorUnavailable("portal_unavailable") from exc
        if not probe.get("ok"):
            raise CapabilityDeclined(str(probe.get("reason", "form_not_found")),
                                     _MESSAGES["form_not_found"])
        # Welches Portal, entscheidet die Herkunft — nicht die Seite.
        binding = None
        for candidate in BINDINGS.values():
            if candidate.login_origin == probe.get("origin"):
                binding = candidate
                break
        if binding is None:
            raise CapabilityRefused("origin_not_bound", _MESSAGES["origin_not_bound"])
        return ActionManifest(
            portal_id=binding.portal_id, origin=str(probe["origin"]),
            page_url=str(probe["url"]), action_type=LOGIN,
            target=binding.form_selector or binding.submit_selector,
            # Die deklarierte Schreibmethode, nicht die aus dem DOM gelesene.
            method=binding.login_method, page_signature=str(probe["page_signature"]),
            fields=(FieldBinding("Benutzer", binding.username_selector,
                                 alias=f"{binding.credential_alias}#user"),
                    FieldBinding("Passwort", binding.password_selector,
                                 alias=binding.credential_alias)),
            credential_alias=binding.credential_alias, principal="local-owner")


def register(router: Any, capabilities: PortalCapabilities) -> list[str]:
    handlers = {
        "portal_list": capabilities.list_portals,
        "portal_open": capabilities.open,
        "portal_login": capabilities.login,
        "portal_read": capabilities.read,
        "portal_status": capabilities.status,
        "portal_close": capabilities.close,
    }
    for name, handler in handlers.items():
        # Nur die Anmeldung erklaert sich vor der Freigabe ausfuehrlicher, als das
        # Modell gefragt hat. Alles andere ist lesend und braucht keine.
        describe = capabilities.prepare_login if name == "portal_login" else None
        router.register(SPECS[name], handler, describe=describe)
    if getattr(capabilities, "router", None) is None:
        capabilities.router = router
    return sorted(handlers)
