"""Fuenf Faehigkeiten — und die zweite, unabhaengige Herkunftsschranke.

**Warum auch die Recherche eine Schreibklasse ist.** Eine READ_ONLY-Einstufung
wuerde beide Politik-Schranken entwaffnen: `authority_refusal` laesst Lesendes
aus jeder Herkunft passieren, und selbst `EXTERNAL_UNTRUSTED × READ_ONLY` ist in
der Matrix DIREKT. Eine Faehigkeit, die einen stundenlangen,
inferenzverbrauchenden autonomen Lauf startet, ist kein Lesen — sie schreibt
eine dauerhafte Aufgabe. Der Praezedenzfall ist `background_create`: das Haus
akzeptiert seit der Hintergrundlaufzeit, dass das ANLEGEN autonomer Arbeit vom
Raummikrofon eine Freigabe kostet. `deep_research` bleibt der reibungsfreie Weg
fuer die schnelle, gedeckelte Einzelrecherche — die beiden konkurrieren nicht.

**Die Herkunftspruefung im Handler ist kein Duplikat der Matrix.** Die Matrix
sagt, was eine Zelle kostet (vom iPhone direkt, vom Raummikrofon Face ID). Der
Handler sagt, welche Herkuenfte ueberhaupt in Frage kommen — und das ist in V1
eine kuerzere Liste: kein Hintergrund, kein Fremdinhalt. Zwei unabhaengige
Schranken, keine Redundanz aus Versehen. Insbesondere schliesst sie den Fall,
den das Bedrohungsmodell als Fall 21 fuehrt: **ein Lauf kann keinen Lauf
gebaeren** — seine Herkunft ist `BACKGROUND_AUTOMATION`, und die steht hier
nicht.

Die Herkunft kommt aus dem vorhandenen Kontext des Routers
(`secret_vault.context`), nicht aus einem Argument: ein Modell kann sie damit
weder setzen noch faelschen.
"""
from __future__ import annotations

from typing import Any

from solvio.capabilities.contract import (CapabilityDeclined, CapabilityRefused,
                                          CapabilitySpec, ExecutionClass,
                                          ExecutorUnavailable)
from solvio.capabilities.policy import OriginClass
from solvio.logging_setup import get_logger
from solvio.security.mobile_approval.execution import (IDEMPOTENT_WRITE, READ_ONLY,
                                                       NON_IDEMPOTENT_WRITE)
from solvio.tools.base import RiskLevel

log = get_logger("agent_runtime")

MIN_OBJECTIVE = 12
MAX_OBJECTIVE = 2_000

#: Genau die drei Herkuenfte, aus denen in V1 eine Agentenaufgabe entstehen darf.
#: `BACKGROUND_AUTOMATION` fehlt ABSICHTLICH — das ist die Rekursionssperre.
CREATION_ORIGINS = frozenset({
    OriginClass.TRUSTED_INTERACTIVE_APP,
    OriginClass.ROOM_VOICE,
    OriginClass.LOCAL_OWNER,
})


#: `conversation_ref` und `predecessor` sind ADDITIV und optional, und sie
#: stehen ausdruecklich NICHT im Werkzeugschema, das das Sprachmodell sieht
#: (`tools/agent_capability_tools.py`). Nur Core-Aufrufer setzen sie. Weil sie
#: Argumente sind, ueberleben sie die Wiederholung nach einer Face-ID-Freigabe
#: woertlich — die Kontinuitaet geht also genau dann nicht verloren, wenn eine
#: Freigabe im Spiel ist. Beide sind VERWEISE: sie machen nichts strenger und
#: nichts milder, und keiner von beiden traegt Nutzertext.
SPECS: dict[str, CapabilitySpec] = {
    "agent_task_research": CapabilitySpec(
        name="agent_task_research", version=1,
        execution_class=ExecutionClass.BACKGROUND,
        base_risk=RiskLevel.MUTATING, semantics=NON_IDEMPOTENT_WRITE,
        input_schema={"type": "object", "properties": {
            "objective": {"type": "string"},
            "conversation_ref": {"type": "string"},
            "predecessor": {"type": "string"}}, "required": ["objective"]},
        executor="inline", timeout=30.0, cancellable=True,
        description="Beauftragt einen eigenstaendigen Rechercheauftrag, der laenger "
                    "laeuft als ein Gespraech, und meldet sich mit dem Ergebnis."),
    "agent_task_build": CapabilitySpec(
        name="agent_task_build", version=1,
        execution_class=ExecutionClass.BACKGROUND,
        base_risk=RiskLevel.MUTATING, semantics=NON_IDEMPOTENT_WRITE,
        input_schema={"type": "object", "properties": {
            "objective": {"type": "string"},
            "repository": {"type": "string"},
            "conversation_ref": {"type": "string"},
            "predecessor": {"type": "string"}}, "required": ["objective"]},
        executor="inline", timeout=30.0, cancellable=True,
        description="Beauftragt eine Code-Aenderung in einer isolierten Arbeitskopie. "
                    "Das Ergebnis wird vorbereitet, NICHT uebernommen — die "
                    "Uebernahme in die Produktion bleibt deine Entscheidung."),
    "agent_run_status": CapabilitySpec(
        name="agent_run_status", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "run_id": {"type": "string"}}},
        executor="inline", timeout=15.0,
        description="Sagt, wie weit ein Auftrag ist und was dabei herauskam."),
    "agent_run_cancel": CapabilitySpec(
        name="agent_run_cancel", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "run_id": {"type": "string"}}, "required": ["run_id"]},
        executor="inline", timeout=15.0,
        description="Bricht einen laufenden Auftrag ab."),
    "agent_run_resume": CapabilitySpec(
        name="agent_run_resume", version=1,
        execution_class=ExecutionClass.CONTROLLED,
        base_risk=RiskLevel.HARMLESS, semantics=IDEMPOTENT_WRITE,
        input_schema={"type": "object", "properties": {
            "run_id": {"type": "string"}}, "required": ["run_id"]},
        executor="inline", timeout=15.0,
        description="Nimmt einen Auftrag wieder auf, nachdem du die eine Handlung "
                    "erledigt hast, die nur du tun konntest."),
}


def current_origin() -> OriginClass:
    """Die Herkunft des laufenden Vorgangs — aus dem Router-Kontext.

    Kein Argument, keine Selbstauskunft. Ausserhalb eines Router-Aufrufs ist der
    Vorgabewert `UNSPECIFIED`, und der faellt unten durch: fail-closed.
    """
    try:
        from solvio.secret_vault import context as SC
        return SC.current().origin
    except Exception:  # noqa: BLE001
        return OriginClass.UNSPECIFIED


def _require_creation_origin() -> None:
    origin = current_origin()
    if origin not in CREATION_ORIGINS:
        log.warning("agent_runtime.creation_refused", origin=getattr(origin, "value", ""))
        raise CapabilityRefused(
            "untrusted_origin",
            "Aus dieser Richtung lege ich keine Auftraege an.")


def _looks_like(value: str, prefix: str) -> bool:
    """`<praefix>` plus 16 Hexziffern — sonst nichts.

    Eine Formpruefung, keine Existenzpruefung: ob es die Aufgabe gibt und ob
    sie zu diesem Gespraech gehoert, weiss die Laufzeit, und dort wird es auch
    entschieden. Hier faellt nur weg, was schon der Form nach keine vom Core
    gepraegte Kennung ist.
    """
    if not value.startswith(prefix):
        return False
    rest = value[len(prefix):]
    return len(rest) == 16 and all(c in "0123456789abcdef" for c in rest)


class AgentCapabilities:
    """Der duenne Handler-Satz. Er entscheidet nichts — er reicht an den
    Orchestrator weiter, der die Wahrheit besitzt."""

    def __init__(self, orchestrator) -> None:
        self.orchestrator = orchestrator

    # -- Erzeugung -----------------------------------------------------

    def _objective(self, arguments: dict[str, Any]) -> str:
        objective = str(arguments.get("objective", "") or "").strip()
        if len(objective) < MIN_OBJECTIVE:
            raise CapabilityDeclined("objective_too_short",
                                     "Was genau soll ich herausfinden oder bauen?")
        if len(objective) > MAX_OBJECTIVE:
            raise CapabilityDeclined("objective_too_long",
                                     "Das ist zu lang — bitte kuerzer fassen.")
        return objective

    @staticmethod
    def _refs(arguments: dict[str, Any]) -> tuple[str, str]:
        """Die zwei Verweise — geprueft auf FORM, nicht auf Inhalt.

        Sie sind vom Core gepraegt; eine Form, die nicht passt, ist deshalb
        kein Nutzerfehler, sondern ein Verdrahtungsfehler, und wird still
        fallengelassen statt gedeutet. Der Auftrag laeuft dann ohne
        Kontinuitaet — das ist weniger Komfort und kein Sicherheitsproblem.
        """
        conversation = str(arguments.get("conversation_ref", "") or "").strip()
        predecessor = str(arguments.get("predecessor", "") or "").strip()
        if not _looks_like(conversation, "c-"):
            conversation = ""
        if not _looks_like(predecessor, "at-"):
            predecessor = ""
        return conversation, predecessor

    async def research(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _require_creation_origin()
        objective = self._objective(arguments)
        conversation, predecessor = self._refs(arguments)
        return await self._create(objective, "research", "",
                                  conversation_ref=conversation,
                                  predecessor=predecessor)

    async def build(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _require_creation_origin()
        objective = self._objective(arguments)
        repository = str(arguments.get("repository", "") or "").strip()
        conversation, predecessor = self._refs(arguments)
        return await self._create(objective, "build", repository,
                                  conversation_ref=conversation,
                                  predecessor=predecessor)

    async def _create(self, objective: str, scope: str, repository: str, *,
                      conversation_ref: str = "",
                      predecessor: str = "") -> dict[str, Any]:
        from solvio.agent_runtime.orchestrator import CreationRefused

        if self.orchestrator is None:
            raise ExecutorUnavailable("agent_runtime_disabled")
        origin = current_origin()
        try:
            task, run = self.orchestrator.create_task(
                objective=objective, scope=scope,
                origin=getattr(origin, "value", str(origin)),
                principal="local-owner", target_repo=repository,
                conversation_ref=conversation_ref,
                predecessor_ref=predecessor)
        except CreationRefused as exc:
            if exc.reason == "no_builder_available":
                # Ehrliche Nichtfaehigkeit statt eines Laufs, der spaeter
                # scheitert: der Builder ist gemessen blockiert.
                raise CapabilityRefused(
                    "no_builder", "Ich habe zurzeit keinen Baumeister, dem ich "
                    "das anvertrauen darf.") from None
            raise CapabilityRefused("untrusted_origin",
                                    "Aus dieser Richtung lege ich keine "
                                    "Auftraege an.") from None
        return {"task_id": task.task_id, "run_id": run.run_id,
                "zustand": run.state,
                "zusammenfassung": ("Ich habe das aufgenommen und melde mich, "
                                    "wenn ich durch bin.")}

    # -- Lesen und Steuern ---------------------------------------------

    async def status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.orchestrator is None:
            raise ExecutorUnavailable("agent_runtime_disabled")
        ledger = self.orchestrator.ledger
        run_id = str(arguments.get("run_id", "") or "").strip()
        if not run_id:
            runs = ledger.recent_runs(limit=5)
            return {"laeufe": [_run_view(r) for r in runs]}
        run = ledger.get_run(run_id)
        if run is None:
            raise CapabilityDeclined("unknown_run", "Diesen Auftrag kenne ich nicht.")
        view = _run_view(run)
        view["schritte"] = [
            {"folge": s.seq, "art": s.kind, "zustand": s.state,
             "zusammenfassung": s.summary}
            for s in ledger.steps_for_run(run_id)]
        return view

    async def cancel(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.orchestrator is None:
            raise ExecutorUnavailable("agent_runtime_disabled")
        run_id = str(arguments.get("run_id", "") or "").strip()
        stopped = await self.orchestrator.cancel(run_id)
        if not stopped:
            raise CapabilityDeclined("not_cancellable",
                                     "Der Auftrag laeuft nicht mehr.")
        return {"run_id": run_id, "zustand": "CANCELLED",
                "zusammenfassung": "Abgebrochen."}

    async def resume(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.orchestrator is None:
            raise ExecutorUnavailable("agent_runtime_disabled")
        run_id = str(arguments.get("run_id", "") or "").strip()
        resumed = await self.orchestrator.resume(run_id)
        if not resumed:
            raise CapabilityDeclined("not_waiting",
                                     "Dieser Auftrag wartet nicht auf dich.")
        return {"run_id": run_id, "zustand": "RUNNING",
                "zusammenfassung": "Ich mache weiter."}


def _run_view(run) -> dict[str, Any]:
    """Die sichere Betriebssicht. Kein Gedankengang, kein Geheimnis, kein Prompt."""
    return {"run_id": run.run_id, "aufgabe": run.task_id, "zustand": run.state,
            "ergebnis": run.result_summary, "grund": run.failure_category,
            "spezialisten": run.specialist_count,
            "arbeitsergebnis": run.branch_ref}


#: Freigabetexte. Ohne sie rendert die Frage auf dem iPhone als nackter
#: Faehigkeitsname — und ein Mensch, der „agent_task_build" liest, bestaetigt
#: etwas, das er nicht gelesen hat.
LABELS: dict[str, tuple[str, dict[str, str]]] = {
    "agent_task_research": ("Eigenstaendige Recherche beauftragen", {
        "objective": "Auftrag"}),
    "agent_task_build": ("Code-Aenderung in einer Arbeitskopie vorbereiten", {
        "objective": "Auftrag", "repository": "Projekt"}),
    "agent_run_resume": ("Auftrag wieder aufnehmen", {
        "run_id": "Auftrag"}),
}


def register(router: Any, capabilities: AgentCapabilities) -> list[str]:
    handlers = {
        "agent_task_research": capabilities.research,
        "agent_task_build": capabilities.build,
        "agent_run_status": capabilities.status,
        "agent_run_cancel": capabilities.cancel,
        "agent_run_resume": capabilities.resume,
    }
    from solvio.capabilities.approval_gateway import ACTION_LABELS
    ACTION_LABELS.update(LABELS)
    for name, handler in handlers.items():
        router.register(SPECS[name], handler)
    return sorted(handlers)
