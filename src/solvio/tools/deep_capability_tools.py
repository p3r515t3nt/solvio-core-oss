"""Die Bruecke vom Modell zur tiefen Recherche.

Dieselbe Bruecke wie bei Kalender, Home Assistant und Gmail, und aus demselben
Grund so duenn: das Modell nennt ein Thema, der Vertrag entscheidet alles andere.
Kein Feld dieser Schemata traegt Autoritaet — es gibt keinen Parameter fuer
„dringend", keinen fuer „freigegeben", keinen fuer den Auftraggeber. Wer fragt,
steht im Turn, nicht im Argument.

`risk_level` bleibt HARMLESS, weil die Entscheidung im Vertrag faellt und nicht
hier. Tiefe Recherche ist rein lesend; sie kommt ohne Freigabe aus, und das soll
sie auch, sonst wuerde jede Frage am iPhone haengen.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.deep import SPECS
from solvio.capabilities.envelope import CapabilityOutcome, CapabilityResult
from solvio.logging_setup import get_logger
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("tools")

_SCHEMAS: dict[str, dict[str, Any]] = {
    "deep_research": {
        "description": "Recherchiert ein Thema gruendlich im oeffentlichen Netz und "
                       "liefert Zusammenfassung, Quellen und offene Fragen. Dauert "
                       "laenger als eine normale Antwort.",
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string",
                      "description": "Das zu recherchierende Thema oder die Frage."}},
            "required": ["topic"]},
    },
    "deep_task_status": {
        "description": "Sagt, wie weit eine begonnene Recherche ist, und liefert das "
                       "Ergebnis, sobald es vorliegt.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string",
                        "description": "Die Kennung aus deep_research."}},
            "required": ["task_id"]},
    },
    "deep_cancel": {
        "description": "Bricht eine laufende Recherche ab.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string",
                        "description": "Die Kennung aus deep_research."}},
            "required": ["task_id"]},
    },
}


class DeepCapabilityTool:
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
            log.warning("deep_capability.no_trusted_context", capability=self.capability)
            return ToolResult(False, error="no_trusted_context",
                              human_message="Ich kann gerade nicht sicher feststellen, "
                                            "wer fragt — deshalb mache ich nichts.")
        result: CapabilityResult = await self.router.execute(
            self.capability, args, trust=context.trust,
            provenance=self.gate.provenance_for(args), principal=context.principal,
            origin=context.origin, commanded=context.commanded)
        return _speak(result)


def _speak(result: CapabilityResult) -> ToolResult:
    if result.succeeded:
        return ToolResult(True, data=result.data, human_message=result.human_message or "")
    if result.outcome is CapabilityOutcome.EXECUTOR_UNAVAILABLE:
        message = "Der Rechercheweg ist gerade nicht verfuegbar — es laeuft nichts."
    elif result.outcome is CapabilityOutcome.TIMEOUT:
        message = "Die Recherche hat zu lange gebraucht."
    else:
        message = result.human_message or "Das habe ich nicht ausgefuehrt."
    return ToolResult(False, data=result.data,
                      human_message=result.human_message or message,
                      error=f"{result.outcome.value}:{result.reason}" if result.reason
                      else result.outcome.value)


def deep_capability_tools(router: Any, gate: Any) -> list[DeepCapabilityTool]:
    return [DeepCapabilityTool(name, router, gate) for name in sorted(SPECS)]
