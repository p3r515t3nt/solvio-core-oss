"""DEBT-0104 — was der Mensch freigibt, ist auch das, was hinausgeht.

Der Befund kam aus der Architekturpruefung zu Approval Policy V2 und war kein
Randfall: `gmail_send_draft` prueft vor dem Versand den gespeicherten Entwurf,
aber bis hierher nur auf Empfaenger und Betreff. Der TEXT wurde nicht
verglichen — und gesendet wird der Entwurf ueber seine Kennung, also das, was
bei Gmail liegt. Haette ihn zwischen Freigabe und Versand irgendetwas geaendert
(ein zweiter Client, eine Synchronisierung, ein Skript), waere ein anderer Text
hinausgegangen als der, den der Mensch Wort fuer Wort auf dem Display gelesen
hat.

Das ist gerade jetzt entscheidend: seit Approval Policy V2 darf ein bewusst am
iPhone gesprochener Versand OHNE zweite Face-ID-Runde laufen. Eine Bindung, die
den Inhalt auslaesst, waere damit vom Komfortgewinn zur Luecke geworden.

Geprueft wird deshalb auf ROUTER-Ebene und nicht nur am Handler: der Weg, den
eine echte Anfrage nimmt, ist der Weg, der halten muss.
"""
from __future__ import annotations

import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))

from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

from solvio.capabilities.envelope import CapabilityOutcome  # noqa: E402
from solvio.capabilities.gmail import (  # noqa: E402
    SPECS, GmailCapabilities, _normalized, register as register_gmail,
)
from solvio.capabilities.invocation import (  # noqa: E402
    CapabilityInvocationGate, voice_trust,
)
from solvio.capabilities.policy import OriginClass  # noqa: E402
from solvio.capabilities.router import CapabilityRouter  # noqa: E402


class _Provider:
    """Ein Postfach, das seinen Entwurf so herausgibt, wie Gmail es tut.

    Wichtig fuer die Aussagekraft: der Inhalt kommt base64url-kodiert im
    `payload`, nicht als bequemes Feld. Genau diese Kodierung war der Grund,
    warum der fehlende Vergleich lange niemandem auffiel.
    """

    def __init__(self) -> None:
        self.drafts: dict[str, dict] = {}
        self.sent: list[str] = []

    async def create_draft(self, *, to, subject, body):
        draft_id = f"d-{len(self.drafts) + 1}"
        self.drafts[draft_id] = {"to": to, "subject": subject, "body": body,
                                 "attachments": ()}
        return {"id": draft_id}

    async def get_draft(self, draft_id):
        stored = self.drafts.get(draft_id)
        if stored is None:
            return None
        parts = [{"mimeType": "text/plain",
                  "body": {"data": base64.urlsafe_b64encode(
                      stored["body"].encode("utf-8")).decode("ascii")}}]
        for name in stored["attachments"]:
            parts.append({"mimeType": "application/pdf", "filename": name,
                          "body": {"attachmentId": "a1"}})
        return {"id": draft_id, "message": {"payload": {
            "headers": [{"name": "To", "value": stored["to"]},
                        {"name": "Subject", "value": stored["subject"]}],
            "mimeType": "multipart/mixed", "parts": parts}}}

    async def send_draft(self, draft_id):
        self.sent.append(draft_id)
        return {"id": f"m-{len(self.sent)}"}


def _stack():
    provider = _Provider()
    router = CapabilityRouter()
    gate = CapabilityInvocationGate()
    register_gmail(router, GmailCapabilities(provider))
    return provider, router, gate


async def _send(router, gate, args, said, *, origin=OriginClass.ROOM_VOICE):
    gate.begin_turn(session_id="s", turn_id="t", principal="pi", origin=origin,
                    trust=voice_trust(True), user_text=said)
    context = gate.context()
    return await router.execute("gmail_send_draft", args, trust=context.trust,
                                provenance=gate.provenance_for(args),
                                principal=context.principal,
                                origin=context.origin,
                                commanded=context.commanded)


_SAID = "Schick die Mail an max@example.com ab."


# =====================================================================
# Der Inhalt ist gebunden
# =====================================================================

async def t_ein_unveraenderter_entwurf_geht_hinaus():
    """Die Gegenprobe zuerst. Ohne sie waere die Regel nur streng."""
    provider, router, gate = _stack()
    await provider.create_draft(to="max@example.com", subject="Hallo",
                                body="Wir sehen uns um acht.")
    result = await _send(router, gate,
                         {"draft_id": "d-1", "to": "max@example.com",
                          "subject": "Hallo", "body": "Wir sehen uns um acht."},
                         _SAID, origin=OriginClass.TRUSTED_INTERACTIVE_APP)
    require_equal(result.outcome, CapabilityOutcome.SUCCESS, str(result))
    require_equal(provider.sent, ["d-1"], "die Mail ging nicht raus")


async def t_ein_geaenderter_text_geht_nicht_hinaus():
    """DER DEFEKT. Freigegeben war ein Satz, im Entwurf steht ein anderer."""
    provider, router, gate = _stack()
    await provider.create_draft(to="max@example.com", subject="Hallo",
                                body="Wir sehen uns um acht.")
    # Jemand aendert den Entwurf zwischen Freigabe und Versand.
    provider.drafts["d-1"]["body"] = "Bitte ueberweise 500 Euro auf DE00."
    result = await _send(router, gate,
                         {"draft_id": "d-1", "to": "max@example.com",
                          "subject": "Hallo", "body": "Wir sehen uns um acht."},
                         _SAID, origin=OriginClass.TRUSTED_INTERACTIVE_APP)
    require_equal(result.outcome, CapabilityOutcome.REJECTED_BY_POLICY, str(result))
    require_equal(result.reason, "draft_body_mismatch", str(result))
    require(not provider.sent, "ein fremder Text ist hinausgegangen")


async def t_auch_eine_kleine_aenderung_faellt_auf():
    """Ein einziges Wort genuegt — sonst waere die Bindung nur Dekoration."""
    provider, router, gate = _stack()
    await provider.create_draft(to="max@example.com", subject="Hallo",
                                body="Ich komme um acht.")
    provider.drafts["d-1"]["body"] = "Ich komme nicht um acht."
    result = await _send(router, gate,
                         {"draft_id": "d-1", "to": "max@example.com",
                          "subject": "Hallo", "body": "Ich komme um acht."},
                         _SAID, origin=OriginClass.TRUSTED_INTERACTIVE_APP)
    require_equal(result.reason, "draft_body_mismatch", str(result))
    require(not provider.sent, "eine Verneinung wurde mitgesendet")


async def t_ein_angehaengtes_dokument_stoppt_den_versand():
    """Ein Anhang stand in keinem Freigabetext — also geht er nicht mit.

    SOLVIO haengt selbst nie etwas an. Was hier trotzdem eines hat, ist
    anderswo veraendert worden, und der Mensch hat es nie gesehen.
    """
    provider, router, gate = _stack()
    await provider.create_draft(to="max@example.com", subject="Hallo",
                                body="Anbei.")
    provider.drafts["d-1"]["attachments"] = ("gehaltsabrechnung.pdf",)
    result = await _send(router, gate,
                         {"draft_id": "d-1", "to": "max@example.com",
                          "subject": "Hallo", "body": "Anbei."},
                         _SAID, origin=OriginClass.TRUSTED_INTERACTIVE_APP)
    require_equal(result.reason, "draft_has_attachments", str(result))
    require(not provider.sent, "ein unfreigegebener Anhang ging raus")


async def t_empfaenger_und_betreff_bleiben_gebunden():
    """Die beiden alten Pruefungen sind nicht durch die neue ersetzt worden."""
    for field, value, reason in (("to", "eve@example.com", "draft_recipient_mismatch"),
                                 ("subject", "Rechnung", "draft_subject_mismatch")):
        provider, router, gate = _stack()
        await provider.create_draft(to="max@example.com", subject="Hallo",
                                    body="Text.")
        provider.drafts["d-1"][field] = value
        result = await _send(router, gate,
                             {"draft_id": "d-1", "to": "max@example.com",
                              "subject": "Hallo", "body": "Text."},
                             _SAID, origin=OriginClass.TRUSTED_INTERACTIVE_APP)
        require_equal(result.reason, reason, f"{field}: {result}")
        require(not provider.sent, f"{field} war nicht gebunden")


# =====================================================================
# Nachsichtig gegenueber Darstellung, streng gegenueber Inhalt
# =====================================================================

def t_zeilenenden_sind_kein_inhaltlicher_unterschied():
    """Sonst scheiterte jeder zweite Versand an Transportkosmetik.

    Eine Bindung, die an `\\r\\n` zerbricht, wuerde in der Praxis abgeschaltet —
    und eine abgeschaltete Bindung schuetzt nichts.
    """
    require_equal(_normalized("Hallo\r\nWelt  \n\n"), _normalized("Hallo\nWelt"),
                  "Zeilenenden wurden als Inhalt gewertet")


def t_ein_zusaetzliches_wort_ist_ein_inhaltlicher_unterschied():
    require(_normalized("Bitte kommen") != _normalized("Bitte nicht kommen"),
            "eine Verneinung galt als derselbe Text")


# =====================================================================
# Die Bindung haengt am Weg, nicht am Handler
# =====================================================================

def t_der_freigabetext_enthaelt_den_ganzen_inhalt():
    """Was gebunden wird, muss der Mensch auch sehen koennen.

    Der Digest laeuft ueber den angezeigten Text. Stuende der Inhalt nicht
    darin, waere die Bindung ein Versprechen ueber etwas Unsichtbares.
    """
    from solvio.capabilities.approval_gateway import render_action
    text = render_action(SPECS["gmail_send_draft"],
                         {"draft_id": "d-1", "to": "max@example.com",
                          "subject": "Hallo", "body": "Wir sehen uns um acht."},
                         "iPhone-App")
    for part in ("max@example.com", "Hallo", "Wir sehen uns um acht.",
                 "Angefragt über: iPhone-App"):
        require(part in text, f"{part!r} fehlte im Freigabetext:\n{text}")


def t_der_versandweg_prueft_den_inhalt_wirklich():
    """Gegen die naheliegendste Regression: jemand entfernt den Vergleich.

    Geprueft wird am AST, nicht an einem Kommentar: dass der Handler den
    gespeicherten Text ueberhaupt gegen den freigegebenen haelt.
    """
    import ast
    import inspect
    from solvio.capabilities import gmail as G
    source = inspect.getsource(G.GmailCapabilities.send_draft)
    tree = ast.parse(source.strip())
    reasons = {node.args[0].value for node in ast.walk(tree)
               if isinstance(node, ast.Call)
               and getattr(node.func, "id", "") == "CapabilityRefused"
               and node.args and isinstance(node.args[0], ast.Constant)}
    for needed in ("draft_body_mismatch", "draft_has_attachments",
                   "draft_recipient_mismatch", "draft_subject_mismatch"):
        require(needed in reasons,
                f"{needed} fehlt im Versandweg — die Bindung ist unvollstaendig")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
