"""Der kognitive Router — er waehlt, WER denkt, nie, WAS gilt.

Zwischen Gespraech und Arbeit steht genau EINE Core-eigene
Entscheidungsschicht. Sie entscheidet die Route und die Modellstufe. Sie
entscheidet **nie** Herkunft, Aktionsklasse, Risiko, Provenienz, Vertrauen,
`commanded` oder eine Freigabepflicht — diese Stempel kommen weiterhin
ausschliesslich aus dem Turn-Tor des Cores und aus der Matrix von ADR-0022,
deren Eingaben „never from model text" bleiben.

**Der Wirkkanal ist genau einer.** Alles, was dieser Router in der Welt
bewirkt, laeuft durch `capabilities.execute(<Name aus der Routentabelle>)` —
eine einzige Aufrufstelle, von einem Quelltext-Scan festgehalten. Die
Routentabelle ist das einzige Vokabular, das er nennen kann; `memory_*`,
`secret_*`, Zahlungsnamen und `background_*` sind keine Routen und damit
strukturell unerreichbar.

**Jede Route ist mindestens so gegated wie ihre heutige Direktbelichtung.** Der
Router aendert, WER waehlt — nicht, was die Wahl kostet. Ein Bauauftrag vom
Raum-Mikrofon kostet Face ID, weil die Matrix das sagt, und der Router kennt
keinen Weg daran vorbei.

**Der Merkzettel wird mitgeschrieben.** Eine Freigabe, die den Auftrag erst
erzeugt, braucht ihn — das war der schwerste Fund der Agentenlaufzeit: der
Nutzer gab per Face ID frei, und nichts geschah. Weil diese Kommission ein
zweiter Aufrufer derselben Faehigkeiten ist, traegt sie dieselbe Pflicht, und
sie erfuellt sie mit demselben Helfer statt mit einer zweiten Fassung davon.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from typing import Any

from solvio.capabilities.envelope import CapabilityOutcome, CapabilityResult
from solvio.cognition import continuity as C
from solvio.cognition import models as M
from solvio.cognition import policy as PO
from solvio.cognition import taxonomy as TX
from solvio.cognition.assessor import Assessor, ModelCall
from solvio.cognition.ledger import (CognitionLedger, new_decision_id,
                                     objective_digest)
from solvio.cognition.types import (EscalationEvent, ModelTier, Route,
                                    RoutingDecision, TaskAssessment)
from solvio.logging_setup import get_logger

log = get_logger("cognition")

#: Route → Faehigkeit. **Die einzige Faehigkeitsflaeche des Routers.**
#:
#: `RESEARCH_QUICK` zeigt auf `research_quick`, nicht auf `deep_research` — die
#: NATIVE Websuche des Anbieters beantwortet eine aktuelle Frage im selben
#: Turn, statt in den Hermes-Deep-Pfad zu laufen (`FIRST_WAIT` 25 s, „ich
#: melde mich"). Die tiefe Recherche bleibt vollstaendig erreichbar — als
#: eigenes, direkt registriertes Werkzeug (`deep_capability_tools.py`), nicht
#: mehr ueber diese Tabelle.
ROUTE_TARGET: dict[Route, str] = {
    Route.RESEARCH_QUICK: "research_quick",
    Route.CONSULT: "bot_consult",
    Route.DIAGNOSE: "system_diagnose",
    Route.AGENT_RESEARCH: "agent_task_research",
    Route.AGENT_BUILD: "agent_task_build",
}

#: Die Werkzeuge, die im aktiven Modus vor dem Modell verborgen werden — genau
#: die, die `ROUTE_TARGET` heute nennt. `deep_task_status`, `deep_cancel`,
#: `agent_run_*`, `system_diagnose` und `system_heal` bleiben sichtbar: sie
#: sind lesend, harmlos oder die Art von Abbruch, die der Mensch sofort will.
#: `deep_research` bleibt ABSICHTLICH sichtbar — kein Route-Eintrag zeigt mehr
#: auf sie, der Router legt sie also nicht mehr um.
HIDDEN_IN_ACTIVE: tuple[str, ...] = (
    "research_quick", "bot_consult", "agent_task_research", "agent_task_build")

#: Die Faehigkeiten, deren Freigabe einen Merkzettel braucht. Dieselbe Menge
#: wie im Werkzeug — ein Merkzettel unter einem anderen Namen wuerde die
#: Freigabe des Nutzers still verbrauchen, ohne etwas zu starten.
CREATION_TARGETS: frozenset[str] = frozenset(
    {"agent_task_research", "agent_task_build"})


@dataclass
class Commission:
    """Was der Router zurueckgibt. Der Umschlag fuer das Sprachmodell."""

    ok: bool = False
    data: dict | None = None
    human_message: str = ""
    error: str = ""


def _path_token(text: str) -> str:
    """Ein konkreter absoluter Pfad in der Aeusserung — oder nichts.

    Der Router hat KEINEN Repository-Aufloeser. Er reicht `repository` nur
    weiter, wenn der Mensch einen Pfad gesagt hat; sonst laesst er das Feld
    weg, und die freigegebene Regel der Laufzeit entscheidet wie bisher (ein
    blosses Wort bedeutet dort „nichts angegeben").
    """
    for word in str(text or "").split():
        stripped = word.strip("\"'`,;()[]")
        if stripped[:1] in ("/", "~") and len(stripped) > 2:
            return stripped
    return ""


class CognitiveRouter:
    """Die Schicht. Haelt Buch und Bruecke, nie Autoritaet."""

    def __init__(self, dispatcher: Any, *, mode: str = "off",
                 ledger: Any = None, assessor: Any = None,
                 clock: Any = None) -> None:
        self.dispatcher = dispatcher
        self.mode = mode
        self.ledger = ledger if ledger is not None else CognitionLedger()
        self._assessor = assessor
        self._clock = clock if clock is not None else time.time
        #: Schattenaufgaben. Eine Referenz, damit sie nicht eingesammelt
        #: werden, bevor sie gelaufen sind.
        self._shadow_tasks: set[Any] = set()

    # -- Zugaenge, alle spaet gelesen ----------------------------------
    #
    # Der Broker haengt am SERVER, nicht am Dispatcher — das war der Fehler der
    # ersten Verdrahtung der Agentenlaufzeit, und er machte den Planer stumm.
    # Deshalb wird hier jedes Mal beim AUFRUF nachgesehen und nie beim
    # Anhaengen gemerkt.

    @property
    def capabilities(self) -> Any:
        return getattr(self.dispatcher, "capabilities", None)

    @property
    def assessor(self) -> Assessor:
        if self._assessor is not None:
            return self._assessor
        return Assessor(broker=getattr(self.dispatcher, "provider_broker", None))

    @property
    def agent_ledger(self) -> Any:
        orchestrator = getattr(self.dispatcher, "agent_runtime", None)
        return getattr(orchestrator, "ledger", None)

    def known_targets(self) -> set[str]:
        """Welche Faehigkeiten heute ueberhaupt registriert sind.

        Deep, Bots, Agentenlaufzeit und Arzt haengen sich SPAET und unabhaengig
        an; jede kann fehlen, waehrend SOLVIO laeuft. Eine fehlende Route ist
        `executor_unavailable`, kein Absturz.
        """
        router = self.capabilities
        if router is None:
            return set()
        try:
            return set(router.names())
        except Exception:  # noqa: BLE001 - fail-soft statt raten
            return set()

    # =================================================================
    # Die Kommission
    # =================================================================

    async def commission(self, context: Any, *, auftrag: str = "") -> Commission:
        """Ein work-foermiger Turn, eingeordnet und beauftragt.

        `context` ist der LEBENDE Turn-Kontext des Tors. Alles, was Autoritaet
        traegt, kommt aus ihm: `trust`, `origin`, `principal`, `commanded`. Der
        Router baut keinen eigenen und faengt keinen zweiten Turn an.

        `auftrag` ist das eine Argument des Werkzeugs. Es steht hier, damit das
        Modell sich festlegt, was es beauftragt — eingeschaetzt wird der
        **abgeschlossene Turn-Text**, und der gewinnt immer.
        """
        del auftrag  # nicht aufgezeichnet und nie staerker als das Transkript
        started = time.monotonic()
        user_text = str(getattr(context, "user_text", "") or "")
        conversation_ref = str(getattr(context, "conversation_id", "") or "")
        turn_ref = str(getattr(context, "turn_id", "") or "")
        origin = str(getattr(getattr(context, "origin", ""), "value", "") or "")
        digest = objective_digest(user_text)
        now = float(self._clock())

        row = RoutingDecision(decision_id=new_decision_id(), at=now,
                              conversation_ref=conversation_ref,
                              turn_ref=turn_ref, origin=origin,
                              objective_digest=digest,
                              tier=ModelTier.NONE.value)

        view = await C.build(self.dispatcher, self.ledger,
                             conversation_ref=conversation_ref)

        # -- Zaeune VOR jedem Modellaufruf (die DEBT-0132-Lehre) --------
        guarded = self._guard(row, view, digest=digest, now=now)
        if guarded is not None:
            answer, guarded_row = guarded
            self._record(guarded_row, started)
            return answer

        # -- Die Einschaetzung -----------------------------------------
        #
        # `fortschritt` gehoert dem AUFRUFER und ueberlebt deshalb den Abbruch.
        # Ohne ihn buchte der Zeitmantel eine Kommission, die eine
        # `gpt-5.4`-Eskalation bezahlt hatte, als `tier=none`,
        # `escalation_event=""`, `tokens=0` — der Broker hatte gebucht, das
        # Entscheidungsbuch leugnete es. Zwei Wahrheiten ueber dieselbe Ausgabe,
        # und die teurere waere die unsichtbare gewesen.
        fortschritt: dict[str, Any] = {"event": EscalationEvent.NONE,
                                       "tier": ModelTier.NONE, "tokens": 0}
        try:
            assessment, event, calls = await asyncio.wait_for(
                self._assess(user_text=user_text, view=view,
                             conversation_ref=conversation_ref,
                             fortschritt=fortschritt),
                timeout=M.COMMISSION_TIMEOUT)
        except asyncio.TimeoutError:
            return self._fail(row, started, "assessment_unavailable",
                              event=fortschritt["event"],
                              tier=fortschritt["tier"],
                              tokens=int(fortschritt["tokens"]))
        except _AssessmentFailed as exc:
            return self._fail(row, started, exc.kind, event=exc.event,
                              tier=exc.tier, tokens=exc.tokens)

        row = replace(row, route_proposed=assessment.route.value,
                      confidence=assessment.confidence,
                      difficulty=assessment.difficulty,
                      preference=assessment.preference,
                      consult_role=assessment.consult_role,
                      tier=assessment.tier.value,
                      escalation_event=event.value,
                      tokens_assessment=sum(call.tokens for call in calls))

        # -- Die Wege ohne Wirkung -------------------------------------
        if assessment.route is Route.HAND_BACK:
            self._record(replace(row, route_final=assessment.route.value,
                                 outcome="handed_back"), started)
            return Commission(True, data={
                "weg": Route.HAND_BACK.value,
                "hinweis": "Beantworte das direkt."})

        if assessment.route is Route.CLARIFY:
            self._record(replace(row, route_final=assessment.route.value,
                                 outcome="clarification",
                                 failure_kind="clarification_required"), started)
            return Commission(True, data={
                "weg": Route.CLARIFY.value,
                "frage": assessment.clarification,
                "hinweis": "Stelle genau diese eine Rueckfrage."})

        if assessment.route is Route.REASON:
            return await self._reason(row, started, assessment, view)

        # -- Die Wege mit Wirkung --------------------------------------
        return await self._dispatch(row, started, assessment, view, context)

    # -- Zaeune --------------------------------------------------------

    def _guard(self, row: RoutingDecision, view: C.ContinuityView, *,
               digest: str, now: float,
               ) -> tuple[Commission, RoutingDecision] | None:
        """Dedup und Schleife — beide OHNE Modellaufruf.

        Die Lehre von DEBT-0132: Hermes wiederholte einen identischen Aufruf
        dreimal in sieben Sekunden und verbrannte 1 990 737 von 2 000 000
        Tagestoken. Eine Gleicharbeits-Erkennung, die hinter der Tageskappe
        sitzt, kommt zu spaet. Diese sitzt davor.
        """
        if not digest:
            return None
        try:
            history = self.ledger.with_digest(row.conversation_ref, digest,
                                              since=now - M.LOOP_WINDOW_SECONDS)
        except Exception as exc:  # noqa: BLE001 - ein unlesbares Buch ist kein Zaun
            log.warning("cognition.guard_unreadable", kind=type(exc).__name__)
            return None

        failed = [entry for entry in history
                  if entry.get("outcome") in ("failed", "refused")]
        if len(failed) >= M.LOOP_FAILED_BEFORE_REFUSAL:
            return (Commission(False, error="loop_detected",
                               human_message=TX.speak("loop_detected")),
                    replace(row, outcome="refused",
                            failure_kind="loop_detected"))

        active = view.active_ids()
        for entry in history:
            produced = str(entry.get("produced_ref") or "")
            if produced and produced in active:
                known = view.entry(produced)
                return (Commission(True, data={
                    "weg": Route.HAND_BACK.value,
                    "hinweis": "Das laeuft schon. Sage kurz den Stand.",
                    "arbeit": produced,
                    "zustand": known.state if known else "",
                    "content_trust": C.CONTENT_TRUST}),
                    replace(row, route_final=Route.HAND_BACK.value,
                            outcome="handed_back", continuity_ref=produced,
                            produced_ref=produced))
        return None

    # -- Die Einschaetzung ---------------------------------------------

    async def _assess(self, *, user_text: str, view: C.ContinuityView,
                      conversation_ref: str, fortschritt: dict | None = None,
                      ) -> tuple[TaskAssessment, EscalationEvent, list[ModelCall]]:
        """Hoechstens drei Aufrufe, hoechstens einer davon gross.

        E1 und E2 teilen sich das eine Mal: eine Kommission eskaliert die
        Einschaetzung hoechstens einmal. Es gibt keine Kante von einer
        Eskalation zur naechsten.
        """
        assessor = self.assessor
        register = view.register_block()
        clarified = ""
        if view.pending_clarification_turn:
            clarified = C.turn_text(self.dispatcher, conversation_ref,
                                    view.pending_clarification_turn)
        # Der Umfang, gegen den die Ueberlappung gemessen wird: dieser Turn und
        # — bei einer beantworteten Rueckfrage — der geklaerte davor.
        scope = user_text if not clarified else f"{clarified} {user_text}"
        calls: list[ModelCall] = []
        event = EscalationEvent.NONE
        spur = fortschritt if fortschritt is not None else {}

        def merken(*, tier: ModelTier | None = None,
                   ereignis: EscalationEvent | None = None,
                   tokens: int = 0) -> None:
            """Was bis hierher wirklich lief — auch wenn gleich abgebrochen wird."""
            if tier is not None:
                spur["tier"] = tier
            if ereignis is not None:
                spur["event"] = ereignis
            if tokens:
                spur["tokens"] = int(spur.get("tokens", 0)) + tokens

        async def attempt(tier: ModelTier, hint: str) -> TaskAssessment | str:
            # VOR dem Aufruf vermerkt: der Broker bucht vor, und ein Abbruch
            # mitten im Aufruf darf die Stufe nicht verschwinden lassen.
            merken(tier=tier)
            call = await assessor.assess(tier=tier, user_text=user_text,
                                         register=register,
                                         recent_context=view.recent_context,
                                         clarified_text=clarified,
                                         repair_hint=hint,
                                         ref=f"assess:{conversation_ref[:24]}")
            calls.append(call)
            merken(tokens=call.tokens)
            if not call.ok:
                raise _AssessmentFailed(
                    "assessment_quota" if TX.quota_denied(call.reason)
                    else "assessment_unavailable",
                    event=event, tier=tier,
                    tokens=sum(item.tokens for item in calls))
            try:
                return PO.validate(call.text, view=view, scope_text=scope,
                                   tier=tier)
            except PO.AssessmentInvalid as exc:
                log.info("cognition.assessment_rejected", reason=exc.reason,
                         tier=tier.value)
                return exc.reason

        first = await attempt(ModelTier.MINI, "")
        if isinstance(first, str):
            second = await attempt(ModelTier.MINI, first)
            if isinstance(second, str):
                # E1 — zweimal unbrauchbar. Einmal gross, dann ehrlich Schluss.
                event = EscalationEvent.ASSESSMENT_INVALID
                merken(ereignis=event)
                third = await attempt(ModelTier.LARGE, second)
                if isinstance(third, str):
                    raise _AssessmentFailed(
                        "assessment_unavailable", event=event,
                        tier=ModelTier.LARGE,
                        tokens=sum(item.tokens for item in calls))
                return third, event, calls
            first = second

        if PO.low_confidence(first) and PO.escalation_available(first, already=False):
            # E2 — einmal gross nachfragen. Das Ergebnis ist endgueltig.
            event = EscalationEvent.ASSESSMENT_LOW_CONFIDENCE
            merken(ereignis=event)
            again = await attempt(ModelTier.LARGE, "")
            if isinstance(again, str):
                # Auch gross unbrauchbar: die Mini-Einschaetzung stand gueltig
                # da und wird abgewertet, statt die Kommission wegzuwerfen.
                return self._downgraded(first), event, calls
            if PO.low_confidence(again):
                return self._downgraded(again), event, calls
            return again, event, calls
        return first, event, calls

    def _downgraded(self, assessment: TaskAssessment) -> TaskAssessment:
        """Die Abwertungstabelle — deterministisch, ohne zweite Rueckfrage."""
        route = PO.downgrade(assessment.route)
        if route is assessment.route:
            return assessment
        log.info("cognition.downgraded", **{"from": assessment.route.value,
                                            "to": route.value})
        clarification = assessment.clarification
        if route is Route.CLARIFY and not clarification:
            clarification = ("Soll ich das wirklich bauen — und was genau "
                             "soll danach anders sein?")
        return replace(assessment, route=route, clarification=clarification)

    # -- Nachdenken ----------------------------------------------------

    async def _reason(self, row: RoutingDecision, started: float,
                      assessment: TaskAssessment,
                      view: C.ContinuityView) -> Commission:
        """Eine gedachte Antwort. Information, nie Autoritaet."""
        tier, event = PO.tier_for_reason(assessment)
        call = await self.assessor.reason(
            tier=tier, user_text=assessment.objective,
            recent_context=view.recent_context,
            ref=f"reason:{row.conversation_ref[:24]}")
        caveat = ""
        if not call.ok and tier is ModelTier.LARGE:
            # Die Eskalation stand an und ging nicht. Der ehrliche Weg ist der
            # kleine Weg MIT Vermerk — ein stiller Rueckfall waere eine Luege.
            # `event` ist der Name, unter dem strukturiertes Logging seine
            # eigene Nachricht fuehrt — ein Schluesselwort dieses Namens
            # kollidiert. Gefunden vom eigenen Test, nicht vom Nachdenken.
            log.info("cognition.escalation_capped", ereignis=event.value)
            call = await self.assessor.reason(
                tier=ModelTier.MINI, user_text=assessment.objective,
                recent_context=view.recent_context,
                ref=f"reason:{row.conversation_ref[:24]}")
            tier = ModelTier.MINI
            caveat = "Gruendlicher habe ich es gerade nicht geschafft."
        if not call.ok:
            kind = ("assessment_quota" if TX.quota_denied(call.reason)
                    else "assessment_unavailable")
            return self._fail(row, started, kind, event=event, tier=tier,
                              tokens=row.tokens_assessment + call.tokens)
        self._record(replace(row, route_final=Route.REASON.value,
                             tier=tier.value, escalation_event=event.value,
                             outcome="dispatched",
                             tokens_assessment=row.tokens_assessment + call.tokens),
                     started)
        return Commission(True, data={
            "weg": Route.REASON.value,
            "antwort": call.text,
            "hinweis": ("Gib das in deinen eigenen Worten wieder, gesprochen "
                        "und kurz."),
            **({"vorbehalt": caveat} if caveat else {})})

    # -- Der eine Wirkkanal --------------------------------------------

    async def _dispatch(self, row: RoutingDecision, started: float,
                        assessment: TaskAssessment, view: C.ContinuityView,
                        context: Any) -> Commission:
        """`capabilities.execute` — die EINZIGE Aufrufstelle dieses Pakets.

        Die Stempel kommen aus dem lebenden Turn-Tor, genau wie bei jedem
        Werkzeug. Der Router hat keine Parameterflaeche, um sie zu setzen, und
        die Einschaetzung beeinflusst nur, WELCHE Route beauftragt wird.
        """
        target = ROUTE_TARGET.get(assessment.route, "")
        capabilities = self.capabilities
        if not target or capabilities is None:
            return self._fail(row, started, "not_routable")
        if target not in self.known_targets():
            return self._fail(row, started, "route_unavailable")

        arguments = self._arguments(assessment, view, context)
        gate = getattr(self.dispatcher, "capability_gate", None)
        provenance = gate.provenance_for(arguments) if gate is not None else None

        result: CapabilityResult = await capabilities.execute(
            target, arguments, trust=context.trust, provenance=provenance,
            principal=context.principal, origin=context.origin,
            commanded=context.commanded)

        self._remember_if_waiting(target, result, arguments, context)

        produced = _produced_ref(result)
        outcome = "dispatched" if result.succeeded else "failed"
        failure = "" if result.succeeded else _failure_kind(result)
        if result.outcome is CapabilityOutcome.APPROVAL_REQUIRED:
            # Eine wartende Freigabe ist KEIN Fehlschlag. Sie ist das System,
            # das arbeitet — und sie darf im Buch nicht wie ein Defekt aussehen.
            outcome, failure = "dispatched", ""
        self._record(replace(row, route_final=assessment.route.value,
                             outcome=outcome, failure_kind=failure,
                             produced_ref=produced,
                             continuity_ref=assessment.continuation_of),
                     started)

        if result.succeeded or result.outcome is CapabilityOutcome.APPROVAL_REQUIRED:
            data = dict(result.data) if isinstance(result.data, dict) else {}
            data.setdefault("weg", assessment.route.value)
            return Commission(True, data=data,
                              human_message=result.human_message or "")
        return Commission(False, data=result.data if isinstance(result.data, dict)
                          else None,
                          human_message=result.human_message or TX.speak(failure),
                          error=(f"{result.outcome.value}:{result.reason}"
                                 if result.reason else result.outcome.value))

    def _arguments(self, assessment: TaskAssessment, view: C.ContinuityView,
                   context: Any) -> dict:
        """Die Argumente der Zielfaehigkeit — und sonst nichts.

        Kein Routenhinweis, keine Zuversicht, keine Korrelationskennung: der
        Vertrag lehnt jeden nicht deklarierten Schluessel ab, und das ist
        richtig so. Der Zustand des Routers wohnt neben den Argumenten, nie in
        ihnen.
        """
        objective = assessment.objective
        route = assessment.route
        if route is Route.RESEARCH_QUICK:
            # `research_quick.SPECS["research_quick"].input_schema` verlangt
            # `question`, nicht `topic` (das ist `deep_research`s Schluessel).
            return {"question": objective[:800]}
        if route is Route.CONSULT:
            return {"role": assessment.consult_role, "question": objective[:2000]}
        if route is Route.DIAGNOSE:
            return {}
        arguments: dict[str, Any] = {"objective": objective[:2000]}
        if route is Route.AGENT_BUILD:
            spoken_path = _path_token(getattr(context, "user_text", ""))
            if spoken_path:
                arguments["repository"] = spoken_path
        reference = str(getattr(context, "conversation_id", "") or "")
        if reference:
            arguments["conversation_ref"] = reference
        predecessor = assessment.continuation_of
        if predecessor and predecessor in view.known_ids():
            arguments["predecessor"] = predecessor
        return arguments

    def _remember_if_waiting(self, target: str, result: CapabilityResult,
                             arguments: dict, context: Any) -> None:
        """Der Merkzettel — mit demselben Helfer wie das Werkzeug.

        Der Name im Zettel ist die ZIELFAEHIGKEIT, nie eine Route und nie
        `solvio_task`: ein Zettel unter einem Namen, den
        `start_approved_capability` nicht kennt, wuerde beim naechsten Takt
        geschlossen und dann abgelehnt — die Freigabe des Menschen waere
        verbraucht, ohne dass etwas lief.
        """
        if target not in CREATION_TARGETS:
            return
        book = self.agent_ledger
        if book is None:
            log.warning("cognition.memo_without_runtime", capability=target)
            return
        from solvio.tools.agent_capability_tools import remember_pending_start
        remember_pending_start(book, capability=target, result=result,
                               arguments=arguments, context=context)

    # -- Buchfuehrung --------------------------------------------------

    def _record(self, row: RoutingDecision, started: float) -> None:
        final = replace(row, duration_ms=int((time.monotonic() - started) * 1000))
        try:
            self.ledger.record(final)
        except Exception as exc:  # noqa: BLE001 - ein Buchfehler stoppt nie Arbeit
            log.warning("cognition.record_failed", kind=type(exc).__name__)

    def _fail(self, row: RoutingDecision, started: float, kind: str, *,
              event: EscalationEvent = EscalationEvent.NONE,
              tier: ModelTier | None = None, tokens: int = 0) -> Commission:
        """Ein ehrlicher Fehlschlag. Nichts faellt auf „kann ich nicht" zusammen."""
        self._record(replace(row, outcome="failed", failure_kind=kind,
                             escalation_event=event.value,
                             tier=(tier or ModelTier.NONE).value,
                             tokens_assessment=tokens or row.tokens_assessment),
                     started)
        return Commission(False, error=kind, human_message=TX.speak(kind))

    # -- Schattenmodus -------------------------------------------------

    async def observe_turn(self, *, user_text: str, conversation_ref: str,
                           turn_ref: str, origin: str, observed_kind: str,
                           observed_tool: str = "",
                           observed_approval: bool = False) -> None:
        """Was der Router GEWAEHLT HAETTE — neben dem, was wirklich geschah.

        **Rein passiv.** Diese Naht beauftragt nichts, verbirgt nichts,
        verzoegert nichts und beruehrt keine Autoritaet. Sie laeuft NACH der
        echten Antwort, in einer eigenen Aufgabe, nur auf der kleinen Stufe,
        mit Tageskappe — und sie schreibt genau eine Zeile.

        **Warum sie breiter misst als der Vertrag es beschrieb.** §18.1 hing die
        Messung an vier Arbeitswerkzeugen. Live gemessen am 2026-08-30: von
        fuenfzehn Aeusserungen erzeugten ZWEI eine Zeile. Unsichtbar blieben
        dabei genau die zwei Fragen, fuer die es die Messung gibt — ein Turn,
        in dem das Modell `system_diagnose` waehlte und dann ueber ein fremdes
        Geraet riet, und vier Turns, in denen es Arbeit schlicht uebersah. Eine
        Messung, die verpasste Arbeit nicht sehen kann, kann eine
        Aktivierungsentscheidung nicht tragen. §21 verlangt „divergence
        measurable on real traffic"; der Mechanismus von §18.1 hat diesen Zweck
        verfehlt, also weicht der Mechanismus.

        Was dadurch NICHT breiter wird: der aktive Modus. Dort sitzt der Router
        unveraendert hinter `solvio_task`, und der schnelle Weg bleibt der
        schnelle Weg. Dies hier ist ein Messinstrument, keine Produktarchitektur.
        """
        if self.mode != "shadow":
            return
        if not str(user_text or "").strip():
            return
        started = time.monotonic()
        now = float(self._clock())
        try:
            midnight = now - (now % 86400.0)
            if self.ledger.shadow_count_since(midnight) >= M.SHADOW_MAX_PER_DAY:
                return
        except Exception as exc:  # noqa: BLE001 - eine Messung darf nie stoeren
            log.info("cognition.shadow_cap_unreadable", kind=type(exc).__name__)
            return

        view = await C.build(self.dispatcher, self.ledger,
                             conversation_ref=conversation_ref)
        row = RoutingDecision(
            decision_id=new_decision_id(), at=now,
            conversation_ref=str(conversation_ref or ""),
            turn_ref=str(turn_ref or ""), origin=str(origin or ""),
            objective_digest=objective_digest(user_text),
            observed_tool=str(observed_tool or ""),
            observed_kind=str(observed_kind or ""),
            observed_approval=bool(observed_approval),
            outcome="shadow", tier=ModelTier.MINI.value)
        try:
            call = await self.assessor.assess(
                tier=ModelTier.MINI, user_text=user_text,
                register=view.register_block(),
                recent_context=view.recent_context,
                ref=f"assess:{conversation_ref[:24]}")
            if not call.ok:
                self._record(replace(row, failure_kind=(
                    "assessment_quota" if TX.quota_denied(call.reason)
                    else "assessment_unavailable")), started)
                return
            assessment = PO.validate(call.text, view=view, scope_text=user_text,
                                     tier=ModelTier.MINI)
        except PO.AssessmentInvalid:
            self._record(replace(row, failure_kind="not_routable"), started)
            return
        except Exception as exc:  # noqa: BLE001 - der Schatten faellt nie auf das Produkt
            log.info("cognition.shadow_failed", kind=type(exc).__name__)
            return
        self._record(replace(row, route_proposed=assessment.route.value,
                             route_final=assessment.route.value,
                             confidence=assessment.confidence,
                             difficulty=assessment.difficulty,
                             consult_role=assessment.consult_role,
                             tokens_assessment=call.tokens), started)


class _AssessmentFailed(Exception):
    """Der Einschaetzweg ist zu Ende — mit einem benannten Grund."""

    def __init__(self, kind: str, *, event: EscalationEvent, tier: ModelTier,
                 tokens: int = 0) -> None:
        super().__init__(kind)
        self.kind = kind
        self.event = event
        self.tier = tier
        self.tokens = tokens


def _produced_ref(result: CapabilityResult) -> str:
    """Der erzeugte Verweis — Aufgabenkennung vor Laufkennung.

    Die Aufgabenkennung ist die, auf die eine Fortsetzung zeigt: `predecessor`
    ist eine Aufgabe, kein Lauf.
    """
    data = result.data if isinstance(result.data, dict) else {}
    for key in ("task_id", "run_id"):
        value = str(data.get(key, "") or "")
        if value:
            return value
    return ""


def _failure_kind(result: CapabilityResult) -> str:
    """Der Fehlschlag der Faehigkeit, in das Vokabular des Routers uebersetzt.

    Uebersetzt wird nur, was der Router selbst benennen muss. Der Wortlaut fuer
    den Menschen bleibt der des Umschlags — der Gap Resolver haengt seinen
    `weiterweg` daran, und eine zweite, aermere Fassung waere ein Rueckschritt.
    """
    if result.outcome is CapabilityOutcome.EXECUTOR_UNAVAILABLE:
        return "route_unavailable"
    if result.outcome is CapabilityOutcome.REJECTED_BY_POLICY:
        return "refused_policy"
    return "not_routable"
