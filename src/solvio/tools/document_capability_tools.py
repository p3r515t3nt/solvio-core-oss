"""Sprachwerkzeuge fuer Suche und native Analyse von Gmail-Anhaengen."""
from __future__ import annotations

from typing import Any

from solvio.capabilities.documents import SPECS, WARNING
from solvio.capabilities.envelope import CapabilityResult
from solvio.logging_setup import get_logger
from solvio.tools.base import RiskLevel, ToolResult

log = get_logger("tools")

_SCHEMAS: dict[str, dict[str, Any]] = {
    "document_find": {
        "description": "Findet den passenden Gmail-Anhang, etwa eine Rechnung von "
                       "einer Firma, ohne seinen Inhalt zu lesen. " + WARNING,
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Gmail-Suchanfrage"},
            "limit": {"type": "integer"}}, "required": ["query"]}},
    "document_ask": {
        "description": "Liest einen zuvor eindeutig gefundenen PDF- oder Bildanhang "
                       "nativ und beantwortet eine Frage dazu oder fasst ihn zusammen. "
                       + WARNING,
        "parameters": {"type": "object", "properties": {
            "document_ref": {"type": "object",
                             "description": "Unveraenderter Verweis aus document_find"},
            "question": {"type": "string"},
            "expected_content_type": {"type": "string"}},
            "required": ["document_ref", "question"]}},
}


class DocumentCapabilityTool:
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
            return ToolResult(False, data={"content_trust": "untrusted_document",
                                          "warning": WARNING},
                              error="no_trusted_context",
                              human_message="Ich kann gerade nicht sicher feststellen, wer fragt. " + WARNING)
        result: CapabilityResult = await self.router.execute(
            self.capability, args, trust=context.trust,
            provenance=self.gate.provenance_for(args), principal=context.principal,
            origin=context.origin, commanded=context.commanded)
        message = result.human_message or ""
        if WARNING not in message:
            message = (message + " " + WARNING).strip()
        if result.succeeded:
            return ToolResult(True, data=result.data, human_message=message)
        return ToolResult(False, data=result.data, human_message=message,
                          error=f"{result.outcome.value}:{result.reason}"
                          if result.reason else result.outcome.value)


def document_capability_tools(router: Any, gate: Any) -> list[DocumentCapabilityTool]:
    return [DocumentCapabilityTool(name, router, gate) for name in sorted(SPECS)]
