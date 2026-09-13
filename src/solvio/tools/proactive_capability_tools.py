"""Die Bruecke vom Modell zu Hintergrundaufgaben und Meldungen.

Auffaellig ist wieder, was die Schemata NICHT haben: kein Cron-Feld, kein
Intervall in Sekunden, keine Liste von Faehigkeiten, die frei zu fuellen waere.
Das Modell sagt in gewoehnlichen Worten, wann etwas passieren soll, und der Core
uebersetzt das in einen getippten Zeitplan — oder fragt nach. Ein Feld, in das
`* * * * *` passt, gibt es nicht.

Der Wortlaut des Auftrags wandert ausdruecklich NICHT durch die Argumente. Die
Faehigkeit holt ihn selbst aus dem Aufruf-Gate — dort steht, wer fragt und was
gesagt wurde, und beides kann das Modell nicht setzen.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.envelope import CapabilityOutcome, CapabilityResult
from solvio.capabilities.proactive import ALLOWED_ACTIONS, SPECS
from solvio.logging_setup import get_logger
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("tools")

_ERLAUBT = ", ".join(ALLOWED_ACTIONS)

_SCHEMAS: dict[str, dict[str, Any]] = {
    "background_create": {
        "description": (
            "Legt eine Aufgabe an, die spaeter von selbst laeuft — einmalig, "
            "taeglich, woechentlich oder als regelmaessige Beobachtung. Nur "
            "lesende Aktionen: " + _ERLAUBT + " oder 'recherche'. "
            "Meldungen landen im Posteingang und werden beim naechsten "
            "Gespraech genannt; aufs geschlossene iPhone kann SOLVIO nichts "
            "schicken."),
        "parameters": {"type": "object", "properties": {
            "titel": {"type": "string", "description": "Kurzer Name der Aufgabe."},
            "wann": {"type": "string",
                     "description": "In gewoehnlichen Worten: 'in 20 Minuten', "
                                    "'taeglich um 7:30', 'montags 8:00', "
                                    "'alle 15 Minuten'."},
            "aktion": {"type": "string",
                       "description": "Eine der erlaubten Lese-Faehigkeiten "
                                      "oder 'recherche'."},
            "argumente": {"type": "object",
                          "description": "Argumente der gewaehlten Faehigkeit."},
            "thema": {"type": "string", "description": "Nur bei 'recherche'."},
            "bedingung": {"type": "string",
                          "description": "Nur melden, wenn das im Ergebnis "
                                         "vorkommt."},
            "melden": {"type": "string",
                       "description": "immer | bei_aenderung | wenn_zutrifft"},
        }, "required": ["titel", "wann", "aktion"]},
    },
    "background_list": {
        "description": "Nennt die angelegten Hintergrundaufgaben mit ihrem "
                       "naechsten Termin.",
        "parameters": {"type": "object", "properties": {}},
    },
    "background_get": {
        "description": "Zeigt eine Aufgabe mit ihren letzten Laeufen.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}}, "required": ["id"]},
    },
    "background_pause": {
        "description": "Pausiert eine Aufgabe. Sie bleibt erhalten.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}}, "required": ["id"]},
    },
    "background_resume": {
        "description": "Nimmt eine pausierte Aufgabe wieder auf.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}}, "required": ["id"]},
    },
    "background_delete": {
        "description": "Loescht eine Aufgabe endgueltig.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}}, "required": ["id"]},
    },
    "background_require_approval": {
        "description": ("Nimmt einer wiederkehrenden Aufgabe die Vorab-Freigabe. "
                        "Die Aufgabe laeuft weiter, fragt aber wieder jedes Mal "
                        "nach. Nutze das, wenn Gregor sagt, er wolle bei einer "
                        "Automatisierung wieder gefragt werden."),
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}}, "required": ["id"]},
    },
    "background_run_now": {
        "description": "Laesst eine Aufgabe sofort faellig werden, statt auf "
                       "den naechsten Termin zu warten.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}}, "required": ["id"]},
    },
    "proactive_list": {
        "description": "Nennt die ungelesenen Meldungen, die im Hintergrund "
                       "entstanden sind. Nutze das, wenn Gregor fragt, was es "
                       "Neues gibt.",
        "parameters": {"type": "object", "properties": {
            "anzahl": {"type": "integer", "description": "Hoechstens so viele."}}},
    },
    "proactive_get": {
        "description": "Zeigt eine Meldung vollstaendig.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}}, "required": ["id"]},
    },
    "proactive_mark_read": {
        "description": "Markiert eine Meldung als gelesen, damit sie nicht "
                       "noch einmal genannt wird.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "string"}}, "required": ["id"]},
    },
}


class ProactiveCapabilityTool:
    risk_level = RiskLevel.HARMLESS
    expose_to_llm = True

    def __init__(self, capability: str, router: Any, gate: Any) -> None:
        self.name = capability
        self.capability = capability
        self.router = router
        self.gate = gate

    def schema(self) -> dict[str, Any]:
        entry = _SCHEMAS[self.capability]
        return {"type": "function", "name": self.name,
                "description": entry["description"], "parameters": entry["parameters"]}

    async def run(self, arguments: dict) -> ToolResult:
        args = dict(arguments or {})
        context = self.gate.context() if self.gate is not None else None
        if context is None or not context.has_principal:
            log.warning("proactive_capability.no_trusted_context",
                        capability=self.capability)
            return ToolResult(False, error="no_trusted_context",
                              human_message="Ich kann gerade nicht sicher feststellen, "
                                            "wer fragt — deshalb mache ich nichts.")
        # Der Wortlaut des Auftrags wird NICHT in die Argumente geschrieben: der
        # Schema-Pruefer des Routers weist unbekannte Argumente ab, und das zu
        # Recht. Die Faehigkeit fragt das Gate selbst — dort steht die Wahrheit.
        result: CapabilityResult = await self.router.execute(
            self.capability, args, trust=context.trust,
            provenance=self.gate.provenance_for(args), principal=context.principal,
            origin=context.origin, commanded=context.commanded)
        return _speak(result)


def _speak(result: CapabilityResult) -> ToolResult:
    if result.succeeded:
        return ToolResult(True, data=result.data,
                          human_message=result.human_message or "")
    if result.outcome is CapabilityOutcome.EXECUTOR_UNAVAILABLE:
        message = "Der Hintergrund laeuft gerade nicht — es ist nichts passiert."
    else:
        message = result.human_message or "Das habe ich nicht ausgefuehrt."
    return ToolResult(False, data=result.data,
                      human_message=result.human_message or message,
                      error=f"{result.outcome.value}:{result.reason}" if result.reason
                      else result.outcome.value)


def proactive_capability_tools(router: Any, gate: Any) -> list[ProactiveCapabilityTool]:
    return [ProactiveCapabilityTool(name, router, gate) for name in sorted(SPECS)]
