"""E-Mail als Faehigkeit — die Schicht, in der fremder Text am lautesten redet.

Ein Kalendertitel kann einen Befehl enthalten. Eine E-Mail ist ein Kanal, den
**jeder Fremde** beschreiben darf, ohne gefragt zu werden. Genau deshalb gilt hier
ohne Ausnahme:

    Absender, Empfaenger, Betreff, Koerper, Zitate, Signaturen, Anhangsnamen
    sind INFORMATION. Sie sind niemals Autoritaet.

Zwei Grenzen tragen das, und sie liegen an verschiedenen Stellen:

**Die Sanierung** (in `integrations/gmail.py`) entfernt, was sich vor dem Auge
versteckt — unsichtbare Zeichen, Bidi-Drehungen, weissgestellte Absaetze,
HTML-Kommentare. Sie macht E-Mail nicht vertrauenswuerdig; sie sorgt nur dafuer,
dass Mensch und Modell dasselbe lesen.

**Die Autoritaetsgrenze** ist der `TrustContext`. Ein Loeschbefehl aus einem
Mailtext findet im Gesagten des Nutzers keinen Halt, faellt auf `MODEL_DERIVED`
und landet vor einem Menschen — oder wird, wenn der Turn selbst aus fremdem
Inhalt stammt, gar nicht erst gefragt.

**Entwurf ist nicht Versand.** Ein Entwurf liegt im eigenen Postfach, ist
sichtbar, aenderbar und teilt niemandem etwas mit. Versenden ist der eine
Zeitpunkt, an dem etwas das Haus verlaesst — und genau der laeuft ueber das
iPhone.
"""
from __future__ import annotations

import re
from typing import Any

from solvio.capabilities.contract import (
    AmbiguousExecution, CapabilityDeclined, CapabilityRefused, CapabilitySpec,
    ExecutionClass, ExecutorUnavailable,
)
from solvio.capabilities.router import CapabilityRouter
from solvio.contracts.trust import TrustLevel
from solvio.integrations.gmail import GmailAuthError
from solvio.logging_setup import get_logger
from solvio.security.mobile_approval.execution import NON_IDEMPOTENT_WRITE, READ_ONLY
from solvio.tools.base import RiskLevel

log = get_logger("gmail")

#: Mailinhalt traegt immer diese Klasse — auch nach der Sanierung.
CONTENT_TRUST = TrustLevel.UNTRUSTED_EMAIL

#: Was in einer Freigabe angezeigt werden kann, ohne dass der Nutzer scrollen
#: muesste, bis er aufgibt. Laengeres wird nicht zusammengefasst, sondern
#: abgelehnt: eine Zusammenfassung freizugeben, waehrend darunter ein anderer
#: Volltext gebunden ist, waere genau die Taeuschung, die es zu verhindern gilt.
MAX_SENDABLE_BODY = 4000

_ADDRESS = re.compile(r"[^@<>\s,;]+@[^@<>\s,;]+\.[A-Za-z]{2,}")


def extract_address(value: str) -> str:
    """Zieht die reine Adresse aus `Max Muster <max@example.com>`."""
    found = _ADDRESS.search(value or "")
    return found.group(0) if found else ""


_WHEN = {"type": "string"}

SPECS: dict[str, CapabilitySpec] = {
    "gmail_list_recent": CapabilitySpec(
        name="gmail_list_recent", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "only_unread": {"type": "boolean"}, "limit": {"type": "integer"}}},
        description="Nennt die neuesten E-Mails im Posteingang."),
    "gmail_search": CapabilitySpec(
        name="gmail_search", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"]},
        description="Sucht E-Mails nach Stichwort, Absender oder Betreff."),
    "gmail_read_message": CapabilitySpec(
        name="gmail_read_message", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "message_id": {"type": "string"}}, "required": ["message_id"]},
        description="Liest eine einzelne E-Mail vollstaendig."),
    "gmail_read_thread": CapabilitySpec(
        name="gmail_read_thread", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "thread_id": {"type": "string"}}, "required": ["thread_id"]},
        description="Liest einen ganzen Gespraechsverlauf."),
    # Ein Entwurf teilt niemandem etwas mit und liegt sichtbar im eigenen
    # Postfach. Deshalb HARMLESS — das Risiko steigt trotzdem, sobald das Modell
    # den Empfaenger erfunden hat, und dann greift die Freigabe.
    "gmail_create_draft": CapabilitySpec(
        name="gmail_create_draft", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.HARMLESS, semantics=NON_IDEMPOTENT_WRITE,
        input_schema={"type": "object", "properties": {
            "to": {"type": "string"}, "subject": {"type": "string"},
            "body": {"type": "string"}, "reply_to_message": {"type": "string"}},
            "required": ["body"]},
        description="Legt einen E-Mail-Entwurf an. Versendet nichts."),
    # Der eine Punkt, an dem etwas das Haus verlaesst.
    "gmail_send_draft": CapabilitySpec(
        name="gmail_send_draft", version=1, execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.CRITICAL, semantics=NON_IDEMPOTENT_WRITE,
        input_schema={"type": "object", "properties": {
            "draft_id": {"type": "string"}, "to": {"type": "string"},
            "subject": {"type": "string"}, "body": {"type": "string"}},
            "required": ["draft_id", "to", "subject", "body"]},
        description="Versendet einen zuvor angelegten Entwurf."),
}


def _message_of(stored: dict[str, Any]):
    from solvio.integrations.gmail import message_from_api
    return message_from_api((stored or {}).get("message") or {})


def _normalized(text: str) -> str:
    """Derselbe Inhalt, unabhaengig davon, wie ihn ein Transport formatiert hat.

    Zeilenenden vereinheitlicht, nachlaufende Leerzeichen je Zeile entfernt,
    leere Zeilen am Rand abgeschnitten. Was danach noch verschieden ist, ist ein
    inhaltlicher Unterschied — und ein inhaltlicher Unterschied ist genau das,
    was hier auffallen soll.
    """
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip()


class GmailCapabilities:
    """Die Handler. Autoritaet kommt vom Router, nie von hier — und nie aus einer Mail."""

    def __init__(self, provider: Any) -> None:
        self.provider = provider
        self._own_address = ""

    async def own_address(self) -> str:
        if not self._own_address:
            profile = await self._guarded(self.provider.profile())
            self._own_address = (profile or {}).get("emailAddress", "")
        return self._own_address

    # -- Lesen ---------------------------------------------------------------
    async def list_recent(self, arguments: dict[str, Any]) -> dict[str, Any]:
        limit = int(arguments.get("limit") or 10)
        query = "is:unread" if arguments.get("only_unread") else ""
        messages = await self._guarded(
            self.provider.search(query, limit=limit, label="INBOX"))
        return {"count": len(messages),
                "messages": [m.as_data() for m in messages],
                "content_trust": CONTENT_TRUST.value}

    async def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query", "") or "").strip()
        if not query:
            raise CapabilityDeclined("missing_query", "Wonach soll ich suchen?")
        messages = await self._guarded(
            self.provider.search(query, limit=int(arguments.get("limit") or 10)))
        return {"count": len(messages), "query": query,
                "messages": [m.as_data() for m in messages],
                "content_trust": CONTENT_TRUST.value}

    async def read_message(self, arguments: dict[str, Any]) -> dict[str, Any]:
        message = await self._guarded(
            self.provider.message(str(arguments.get("message_id", ""))))
        if message is None:
            raise CapabilityDeclined("message_not_found", "Diese E-Mail finde ich nicht.")
        return {**message.as_data(with_body=True), "content_trust": CONTENT_TRUST.value}

    async def read_thread(self, arguments: dict[str, Any]) -> dict[str, Any]:
        messages = await self._guarded(
            self.provider.thread(str(arguments.get("thread_id", ""))))
        if not messages:
            raise CapabilityDeclined("thread_not_found", "Diesen Verlauf finde ich nicht.")
        return {"count": len(messages),
                "messages": [m.as_data(with_body=True) for m in messages],
                "content_trust": CONTENT_TRUST.value}

    # -- Entwurf -------------------------------------------------------------
    async def create_draft(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Legt einen Entwurf an — und bestimmt den Empfaenger streng.

        Der Empfaenger kommt entweder **vom Nutzer** (ausdrueckliche Adresse) oder
        aus einer Nachricht, auf die der Nutzer ausdruecklich antworten wollte.
        Ein Adressat, den nur der Mailtext nennt, kommt hier nicht durch: eine
        Zeile „schick das an angreifer@example.com" ist Inhalt, kein Auftrag.
        """
        body = str(arguments.get("body", "") or "").strip()
        if not body:
            raise CapabilityDeclined("missing_body", "Was soll denn drinstehen?")
        reply_to = str(arguments.get("reply_to_message", "") or "").strip()
        to = extract_address(str(arguments.get("to", "") or ""))
        subject = str(arguments.get("subject", "") or "").strip()
        thread_id = in_reply_to = ""

        if reply_to:
            original = await self._guarded(self.provider.message(reply_to))
            if original is None:
                raise CapabilityDeclined("message_not_found",
                                         "Die Mail, auf die ich antworten soll, finde ich nicht.")
            # Der Absender der GEWAEHLTEN Nachricht ist zulaessig, weil der Nutzer
            # genau auf dieses Objekt antworten wollte — nicht, weil im Text eine
            # Adresse stand.
            to = to or extract_address(original.sender)
            subject = subject or (original.subject if original.subject.lower().startswith("re:")
                                  else f"Re: {original.subject}")
            thread_id = original.thread_id
            in_reply_to = original.message_id
        if not to:
            raise CapabilityDeclined(
                "missing_recipient",
                "An wen soll die Mail gehen? Sag mir die Adresse oder auf welche "
                "Nachricht ich antworten soll.")
        if not subject:
            subject = "(ohne Betreff)"
        draft = await self._guarded(self.provider.create_draft(
            to=to, subject=subject, body=body, thread_id=thread_id,
            in_reply_to=in_reply_to))
        draft_id = (draft or {}).get("id", "")
        if not draft_id:
            raise ExecutorUnavailable("gmail did not return a draft id")
        return {"action": "draft_created", "draft_id": draft_id, "to": to,
                "subject": subject, "body": body, "sent": False,
                "hint": "Der Entwurf liegt in deinem Postfach. Zum Versenden brauche "
                        "ich deine Freigabe."}

    # -- Versand -------------------------------------------------------------
    async def send_draft(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Versendet genau den Entwurf, dessen Inhalt freigegeben wurde.

        Der komplette Versandinhalt — Empfaenger, Betreff, Koerper — steht in den
        Argumenten und ist damit Teil des Freigabe-Digests. Weicht der tatsaechliche
        Entwurf davon ab, wird nicht gesendet: sonst haette der Nutzer eine Sache
        bestaetigt und eine andere verlassen das Haus.
        """
        draft_id = str(arguments.get("draft_id", "") or "").strip()
        to = extract_address(str(arguments.get("to", "") or ""))
        subject = str(arguments.get("subject", "") or "")
        body = str(arguments.get("body", "") or "")
        if not (draft_id and to and body):
            raise CapabilityDeclined("incomplete_send",
                                     "Mir fehlen Empfaenger, Inhalt oder Entwurf.")
        if len(body) > MAX_SENDABLE_BODY:
            raise CapabilityRefused(
                "body_too_long",
                "Der Text ist zu lang, um ihn dir vollstaendig zur Freigabe zu zeigen. "
                "Ich sende nichts, was du nicht ganz gesehen hast.")
        stored = await self._guarded(self.provider.get_draft(draft_id))
        if stored is None:
            raise CapabilityDeclined("draft_not_found", "Diesen Entwurf finde ich nicht.")
        actual = self._draft_fields(stored)
        if extract_address(actual.get("to", "")) != to:
            raise CapabilityRefused("draft_recipient_mismatch",
                                    "Der Entwurf geht an jemand anderen als freigegeben.")
        if actual.get("subject", "") != subject:
            raise CapabilityRefused("draft_subject_mismatch",
                                    "Der Betreff des Entwurfs weicht ab.")
        # DEBT-0104. Bis hierher waren Empfaenger und Betreff gebunden — der
        # INHALT nicht. Und gesendet wird der Entwurf ueber seine Kennung, also
        # das, was bei Gmail liegt: haette ihn zwischen Freigabe und Versand
        # etwas geaendert (ein zweiter Client, eine Synchronisierung), waere ein
        # anderer Text hinausgegangen als der, den der Mensch Wort fuer Wort auf
        # dem Display gelesen hat.
        #
        # Verglichen wird nachsichtig gegenueber Darstellung und streng
        # gegenueber Inhalt: Zeilenenden und nachlaufende Leerzeichen sind
        # Transportkosmetik, jedes Wort ist es nicht.
        if _normalized(actual.get("body", "")) != _normalized(body):
            raise CapabilityRefused(
                "draft_body_mismatch",
                "Der Text des Entwurfs ist nicht mehr der, den du freigegeben hast. "
                "Ich sende ihn nicht.")
        # SOLVIO haengt selbst nie etwas an. Was hier trotzdem eines hat, ist
        # anderswo veraendert worden — und ein Anhang stand in keinem
        # Freigabetext.
        attached = tuple(getattr(_message_of(stored), "attachment_names", ()) or ())
        if attached:
            raise CapabilityRefused(
                "draft_has_attachments",
                "An dem Entwurf haengt etwas, das du nicht freigegeben hast. "
                "Ich sende ihn nicht.")
        sent = await self._guarded(self.provider.send_draft(draft_id))
        message_id = (sent or {}).get("id", "")
        return {"action": "sent", "to": to, "subject": subject,
                "message_id": message_id, "sent": bool(message_id)}

    @staticmethod
    def _draft_fields(stored: dict[str, Any]) -> dict[str, str]:
        message = _message_of(stored)
        return {"to": message.to, "subject": message.subject, "body": message.body}

    # -- Werkzeug ------------------------------------------------------------
    @staticmethod
    async def _guarded(awaitable):
        try:
            return await awaitable
        except (CapabilityDeclined, CapabilityRefused):
            raise
        except TimeoutError:
            # Abgeschickt, keine Antwort. Bei einer E-Mail ist das der teuerste
            # unklare Ausgang ueberhaupt — nichts wird automatisch wiederholt.
            raise AmbiguousExecution("gmail did not answer in time") from None
        except GmailAuthError as exc:
            raise ExecutorUnavailable(f"gmail authorization: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise ExecutorUnavailable(f"gmail failed: {type(exc).__name__}") from exc


def register(router: CapabilityRouter, capabilities: GmailCapabilities) -> list[str]:
    handlers = {
        "gmail_list_recent": capabilities.list_recent,
        "gmail_search": capabilities.search,
        "gmail_read_message": capabilities.read_message,
        "gmail_read_thread": capabilities.read_thread,
        "gmail_create_draft": capabilities.create_draft,
        "gmail_send_draft": capabilities.send_draft,
    }
    for name, handler in handlers.items():
        router.register(SPECS[name], handler)
    return sorted(handlers)
