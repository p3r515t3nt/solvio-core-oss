"""Der Orchestrator: deterministische Zustandsmechanik mit wenigen, typisierten
Modellaufrufen.

Ausdruecklich **kein** Schwarm und **keine** Mehrheitsentscheidung — zwei
Modelle mit demselben Irrtum sind keine Bestaetigung. Was hier laeuft, ist eine
geschlossene Zustandsmaschine; das Modell darf an genau drei Stellen etwas
vorschlagen (Plan, Replan, Replan), und jeder Vorschlag laeuft durch eine
deterministische Core-Policy, bevor er etwas bedeutet.

Der Orchestrator ist Core-Code und damit Teil der Trusted Computing Base — wie
der Hintergrundlaeufer. Er stempelt Herkunft und Vertrauen selbst, immer, und
laesst nie Spezialistentext waehlen.

Drei Dinge, die er NICHT tut:

* **Er wartet nie blockierend auf einen Menschen.** Ein freigabepflichtiger
  Schritt PARKT den Lauf; keine Coroutine haengt an einer Face-ID-Abfrage.
* **Er verbucht nach einem Neustart nichts still als Erfolg.** Erst abgleichen,
  dann fortsetzen — oder ehrlich scheitern.
* **Er sucht keinen Weg um eine Ablehnung herum.** Eine abgelehnte Freigabe ist
  endgueltig, dieselbe Haltung wie beim Gap Resolver und beim
  Hintergrundlaeufer.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
from dataclasses import dataclass, field

from solvio.agent_runtime import (authority, boundaries, budget as BU,
                                  completion as CO, notices, planner as PL,
                                  specialists as SP, steps as ST)
from solvio.agent_runtime import store as S
from solvio.logging_setup import get_logger

log = get_logger("agent_runtime")

#: Wie oft der Takt schaut, ob es etwas zu tun gibt.
TICK_SECONDS = 2.0

#: Ab wann ein nicht-terminaler Lauf ohne Ereignis als „steckend" gilt:
#: die doppelte Schrittfrist.
STUCK_AFTER = 2 * 1800.0

#: Herkuenfte, aus denen eine Agentenaufgabe ueberhaupt entstehen darf.
#: Zweite, unabhaengige Schranke neben der Matrixzelle — kein Hintergrund und
#: kein Fremdinhalt legt in V1 Agentenaufgaben an.
CREATION_ORIGINS = frozenset({
    "trusted_interactive_app", "room_voice", "local_owner",
})


#: Warum ein Bau-Lauf ohne Ergebnis endete — in Worten, die ein Mensch liest.
#: Der Unterschied zaehlt: „nichts getan" ist etwas anderes als „etwas
#: zurueckgehalten", und beides ist etwas anderes als ein Fehlschlag unterwegs.
_HARVEST_WORDS = {
    "no_change": "Der Auftrag hat am Projekt nichts geaendert — es gibt kein "
                 "Arbeitsergebnis, das ich dir bereitlegen koennte.",
    "credential_shaped_content": "Ich habe das Arbeitsergebnis zurueckgehalten: "
                                 "darin stand etwas, das wie ein Zugang aussieht.",
    "no_workspace": "Der Arbeitsbereich fehlte — es gibt kein Ergebnis.",
    "": "Der Lauf hat kein Arbeitsergebnis hinterlassen.",
}


class CreationRefused(PermissionError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"agent_creation_refused:{reason}")
        self.reason = reason


@dataclass
class RunContext:
    """Die fluechtige Seite eines Laufs. Die dauerhafte steht im Ledger."""

    run_id: str
    task_id: str
    scope: str
    ledger: BU.BudgetLedger
    plan: PL.Plan | None = None
    cursor: int = 0
    approval_attempts: int = 0
    pending_step_id: str = ""
    context_notes: list[str] = field(default_factory=list)
    workspace: object = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    #: Was der Lauf WIRKLICH schon hat. Getrennt von `context_notes`, weil das
    #: dort ein Modellkontext ist und hier ein Arbeitsergebnis: Notizen duerfen
    #: gekuerzt und umformuliert werden, ein Befund nicht.
    findings: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    #: `faehigkeit|grund` jedes strukturell ungueltigen Schritts. Die
    #: Schleifenbremse in `BudgetLedger` zaehlt Argument-GESTALT und greift
    #: deshalb nicht, wenn der Planer denselben Fehler mit anderen Argumenten
    #: wiederholt — genau das ist live passiert.
    invalid_signatures: set[str] = field(default_factory=set)
    #: Der Grund, aus dem der Lauf vorzeitig fertig war. Leer = nicht geprueft
    #: oder nicht erfuellt.
    goal_met: str = ""


class Orchestrator:
    """Besitzt Aufgaben, Laeufe, Schritte und deren dauerhafte Wahrheit."""

    def __init__(self, *, ledger: S.AgentRunLedger, router=None,
                 control_plane=None, planner: PL.Planner | None = None,
                 proactive=None, workspaces=None, researcher=None,
                 max_concurrent: int = BU.MAX_CONCURRENT_RUNS) -> None:
        self.ledger = ledger
        self.router = router
        self.control_plane = control_plane
        self.planner = planner
        self.proactive = proactive
        self.workspaces = workspaces
        #: Der Hermes-Seam. Nicht der Router — `deep_*` bleibt gesperrt.
        self.researcher = researcher
        self.max_concurrent = max(1, int(max_concurrent))
        self.approvals = ST.ApprovalQueue()
        self._contexts: dict[str, RunContext] = {}
        #: Laeufe, die nicht enden KONNTEN (blockierter Uebergang). Der Takt
        #: laesst sie in Ruhe, statt sie endlos zu wiederholen. Siehe `_finish`.
        self._unfinishable: set[str] = set()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # =================================================================
    # Erzeugung
    # =================================================================

    def create_task(self, *, objective: str, scope: str, origin: str,
                    principal: str, target_repo: str = "",
                    conversation_ref: str = "",
                    predecessor_ref: str = "") -> tuple[S.AgentTask, S.AgentRun]:
        """Legt Aufgabe und ersten Lauf an.

        Die Herkunftspruefung hier ist die ZWEITE, unabhaengige Schranke. Die
        erste ist die Matrixzelle (`BACKGROUND × NORMAL_WRITE` verlangt Face
        ID). Beide zusammen sind keine Redundanz aus Versehen: eine sitzt in der
        eingefrorenen Politik, die andere im Handler dieser Familie.
        """
        if (origin or "").strip().lower() not in CREATION_ORIGINS:
            raise CreationRefused(f"origin:{origin}")
        if scope not in S.SCOPES:
            raise CreationRefused(f"scope:{scope}")
        if scope == S.SCOPE_BUILD and not self._build_available():
            raise CreationRefused("no_builder_available")

        plan_budget = BU.DEFAULTS[scope]
        task = self.ledger.create_task(
            objective=objective, scope=scope, created_origin=origin,
            created_principal=principal, target_repo=target_repo,
            conversation_ref=conversation_ref,
            predecessor_ref=predecessor_ref, budget=plan_budget.as_dict())
        run = self.ledger.create_run(task_id=task.task_id)
        self._contexts[run.run_id] = RunContext(
            run_id=run.run_id, task_id=task.task_id, scope=scope,
            ledger=BU.BudgetLedger(budget=plan_budget))
        log.info("agent_runtime.task_created", task_id=task.task_id,
                 run_id=run.run_id, scope=scope, origin=origin)
        return task, run

    def _build_available(self) -> bool:
        for key, spec in SP.usable_profiles().items():
            if spec.mode == SP.BUILDER and SP.builder_available(spec)[0]:
                return True
        return False

    # =================================================================
    # Der Takt
    # =================================================================

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.reconcile()
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop())
        log.info("agent_runtime.started", max_concurrent=self.max_concurrent)

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        log.info("agent_runtime.stopped")

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("agent_runtime.tick_failed", kind=type(exc).__name__)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=TICK_SECONDS)

    async def tick(self) -> None:
        """Einen Schritt aller berechtigten Laeufe. Idempotent und kurz."""
        await self._poll_pending_starts()
        for run in self.ledger.open_runs():
            if run.run_id in self._unfinishable:
                continue                      # siehe `_finish`: gegen dieselbe
                                              # Wand faehrt der Takt nicht zweimal
            if run.state in (S.WAITING_APPROVAL,):
                await self._poll_approval(run)
                continue
            if run.state in (S.WAITING_USER,):
                continue                      # wartet auf einen Menschen
            # Parkende Laeufe zaehlen NICHT gegen die Parallelitaet.
            if len(self.ledger.active_runs()) > self.max_concurrent and \
                    run.state == S.CREATED:
                continue
            await self._advance(run)

    # =================================================================
    # Die Zustandsmaschine
    # =================================================================

    async def _advance(self, run: S.AgentRun) -> None:
        context = self._contexts.get(run.run_id)
        if context is None:
            context = self._rebuild_context(run)
        if context.cancel.is_set():
            await self._finish(run.run_id, S.CANCELLED, "cancelled_by_user",
                               "Der Lauf wurde abgebrochen.")
            return
        try:
            if run.state == S.CREATED:
                self._prepare_workspace(run, context)
                self.ledger.transition(run.run_id, S.PLANNING)
            elif run.state == S.PLANNING:
                await self._do_plan(run, context)
            elif run.state in (S.RUNNING, S.INTERRUPTED):
                await self._do_next_step(run, context)
            elif run.state == S.VERIFYING:
                await self._do_verify(run, context)
        except BU.BudgetExhausted as exc:
            await self._finish(run.run_id, S.FAILED, exc.category,
                               f"Der Lauf ist an einer Grenze geendet: {exc.category}.")
        except PL.PlanInvalid as exc:
            # „Kein brauchbarer Plan" war die Antwort auf JEDEN Fehler dieses
            # Pfades — auch auf den, bei dem gar nichts geplant wurde, weil sich
            # die Arbeitskopie nicht anlegen liess. Live gemessen: der Nutzer
            # las „kein Plan", waehrend das Buch ein Klonproblem meinte. Ein
            # Grund, der auf alles passt, sagt nichts.
            if exc.reason == "workspace_unavailable":
                await self._finish(run.run_id, S.FAILED, "workspace_conflict",
                                   "Die Arbeitskopie liess sich nicht anlegen.")
            else:
                await self._finish(run.run_id, S.FAILED, "plan_invalid",
                                   "Ich konnte keinen brauchbaren Plan bilden.")
        except Exception as exc:  # noqa: BLE001
            # Kategorisch, nie Material: der TYP der Ausnahme steht im Buch,
            # ihr Text nicht. „Unerwartet gescheitert" allein zwang bisher dazu,
            # jeden Fehlschlag im Log zu suchen — das ist keine ehrliche
            # Auskunft, sondern eine bequeme.
            kind = type(exc).__name__
            log.error("agent_runtime.step_failed", run_id=run.run_id, kind=kind)
            await self._finish(run.run_id, S.FAILED, "capability_failed",
                               f"Der Lauf ist unerwartet gescheitert ({kind}).")

    def _prepare_workspace(self, run: S.AgentRun, context: RunContext) -> None:
        """Ein `build`-Lauf bekommt seinen Klon, BEVOR er plant.

        Live gefunden: der Klon wurde nie angelegt. Der Planer plante einen
        Builder, der Schritt-Executor fand keinen Arbeitsort und lehnte ab —
        richtig, aber der Arbeitsort haette da sein muessen. Ein Bau-Lauf ohne
        Arbeitsbereich ist kein Bau-Lauf.

        Der Klon entsteht VOR der Planung, damit der Planer nicht etwas
        vorschlaegt, das erst danach moeglich wird.
        """
        if context.scope != S.SCOPE_BUILD or self.workspaces is None:
            return
        if run.workspace_path:
            return                      # nach einem Neustart schon vorhanden
        # Kennt das Buch keinen Arbeitsbereich fuer diesen Lauf, dann gehoert
        # ein Rest unter seiner Kennung KEINEM Schreiber — er ist Bruchstueck
        # eines abgebrochenen Versuchs. Nur dann wird er entfernt. Die Sperre
        # `workspace_exists` bleibt fuer jeden anderen Fall scharf: sie ist es,
        # die zwei Schreiber auseinanderhaelt.
        with contextlib.suppress(Exception):
            self.workspaces.discard_incomplete(run.run_id)
        task = self.ledger.get_task(run.task_id)
        try:
            workspace = self.workspaces.clone(
                run.run_id, (task.target_repo if task else "") or "")
        except Exception as exc:  # noqa: BLE001
            # Nur `kind=WorkspaceError` stand hier — und das ist keine Auskunft.
            # Der GRUND ist es (`repository_missing`, `workspace_exists`, …),
            # und er ist eine geschlossene Vokabel, kein freier Text. `detail`
            # ist ein aufgeloester Pfad, nie ein uebergebener Wert.
            log.error("agent_runtime.workspace_failed", run_id=run.run_id,
                      kind=type(exc).__name__,
                      reason=getattr(exc, "reason", None),
                      detail=getattr(exc, "detail", None))
            raise PL.PlanInvalid("workspace_unavailable", type(exc).__name__) from exc
        self.ledger.set_run_fields(run.run_id, workspace_path=workspace.path)
        context.workspace = workspace
        self.ledger.record_event(run.run_id, "state_changed",
                                 "Arbeitskopie angelegt — der Produktivbaum bleibt unberuehrt.")

    def _rebuild_context(self, run: S.AgentRun) -> RunContext:
        """Nach einem Neustart lebt nur das Ledger. Der fluechtige Teil entsteht
        daraus neu — mit dem Budget der Aufgabe, nicht mit einem frischen."""
        task = self.ledger.get_task(run.task_id)
        scope = task.scope if task else S.SCOPE_RESEARCH
        limits = BU.Budget.from_dict(task.budget if task else None)
        steps = self.ledger.steps_for_run(run.run_id)
        ledger = BU.BudgetLedger(budget=limits, started_at=run.started_at or time.time())
        ledger.steps = len(steps)
        ledger.specialist_invocations = len(
            [s for s in steps if s.kind == "specialist"])
        ledger.plan_revisions = run.plan_revision
        context = RunContext(run_id=run.run_id, task_id=run.task_id, scope=scope,
                             ledger=ledger, cursor=len(steps))
        self._contexts[run.run_id] = context
        return context

    def _note_predecessor(self, task: S.AgentTask, context: RunContext) -> None:
        """Der Befund des Vorgaengers erreicht den Planer als KONTEXT.

        Und ausdruecklich nicht als Ziel. Das Ziel eines Auftrags sind die
        Worte des Nutzers; Executor-Prosa in einem torgemessenen Argument waere
        eine Provenienzwaesche, gegen die der eingefrorene Vertrag geschrieben
        ist. Ueber den Kontextkanal stempelt die fail-closed Kette der Laufzeit
        alles Spezialisten-Abgeleitete ohnehin als `UNTRUSTED_CONTENT` — es
        informiert, es weist nicht an.
        """
        reference = str(getattr(task, "predecessor_ref", "") or "")
        if not reference:
            return
        vorgaenger = self.ledger.get_task(reference)
        if vorgaenger is None or vorgaenger.conversation_ref != task.conversation_ref:
            # Ein Verweis auf eine fremde oder verschwundene Aufgabe traegt
            # nichts bei. Er wird weggelassen, nicht gedeutet.
            log.info("agent_runtime.predecessor_ignored", task_id=task.task_id)
            return
        runs = self.ledger.runs_for_task(reference)
        summary = str(getattr(runs[-1], "result_summary", "") or "") if runs else ""
        if not summary:
            return
        note = f"[vorgaenger {reference}] {summary[:900]}"
        if note not in context.context_notes:
            context.context_notes.append(note)

    async def _do_plan(self, run: S.AgentRun, context: RunContext) -> None:
        task = self.ledger.get_task(run.task_id)
        if task is None:
            await self._finish(run.run_id, S.FAILED, "plan_invalid", "Aufgabe fort.")
            return
        if self.planner is None:
            await self._finish(run.run_id, S.FAILED, "specialist_unavailable",
                               "Der Planer ist nicht verfuegbar.")
            return
        allowed = self._allowed_profiles(task)
        known = self._known_capabilities()
        self._note_predecessor(task, context)
        # Das Ereignis-Ordinal: 0 fuer die erste Planung, danach die Zahl der
        # bisherigen Nachplanungen. Es entscheidet die STUFE, nie die Anzahl —
        # wer zweimal nachplanen musste, hat kein Formatproblem.
        plan, call = await self.planner.plan(
            goal=task.objective, scope=task.scope, allowed_profiles=allowed,
            known_capabilities=known, ledger=context.ledger, run_id=run.run_id,
            context="\n".join(context.context_notes[-5:]),
            event_ordinal=int(getattr(run, "plan_revision", 0) or 0))
        context.plan = plan
        context.cursor = 0
        self.ledger.set_run_fields(run.run_id, tokens_planner=call.tokens)
        self.ledger.record_event(run.run_id, "state_changed",
                                 f"Plan mit {len(plan.steps)} Schritten.")
        # NUR aus PLANNING heraus. Eine Nachplanung ist ein EREIGNIS im Zustand
        # RUNNING, kein Zustandswechsel — `RUNNING → RUNNING` steht in keiner
        # Zeile der Tabelle und wirft zu Recht. Live gefunden: der Replan liess
        # den ganzen Lauf mit „unerwartet gescheitert" enden.
        if self.ledger.get_run(run.run_id).state == S.PLANNING:
            self.ledger.transition(run.run_id, S.RUNNING)

    def _allowed_profiles(self, task) -> set[str]:
        """Welche Profile ein Plan ueberhaupt nennen darf.

        Zwei strukturelle Schranken, beide live gelernt:

        * ein Builder gehoert nur in einen `build`-Auftrag;
        * ein Profil, das ein Repository BRAUCHT, gehoert nur dorthin, wo es
          eines GIBT. Sonst bekommt ein CLI-Ermittler einen leeren Ordner als
          cwd und scheitert an einer Frage, die er nie beantworten konnte.
        """
        has_workspace = task.scope == S.SCOPE_BUILD
        allowed = set()
        for key, spec in SP.usable_profiles().items():
            if spec.mode == SP.BUILDER and not has_workspace:
                continue
            if spec.needs_workspace and not has_workspace:
                continue
            if spec.provider == SP.HERMES and self.researcher is None:
                continue          # ehrlich: ohne Seam kein Rechercheprofil
            allowed.add(key)
        return allowed

    def _known_capabilities(self) -> set[str]:
        if self.router is None:
            return set()
        names = getattr(self.router, "names", None)
        try:
            catalogue = set(names()) if callable(names) else set()
        except Exception:  # noqa: BLE001
            catalogue = set()
        return {n for n in catalogue if not authority.is_blocked(n)}

    async def _do_next_step(self, run: S.AgentRun, context: RunContext) -> None:
        if run.state == S.INTERRUPTED:
            self.ledger.transition(run.run_id, S.RUNNING)
        if context.plan is None or context.cursor >= len(context.plan.steps):
            self.ledger.transition(run.run_id, S.VERIFYING)
            return

        context.ledger.check_step()
        planned = context.plan.steps[context.cursor]
        seq = context.cursor + 1
        # Der Nachplan faengt bei Schritt 1 wieder an — und `UNIQUE(run_id, seq,
        # attempt)` haelt das zu Recht auf. Die Planrevision IST der Versuch;
        # dafuer gibt es die Spalte. Live gefunden: ohne das endete jeder Lauf
        # mit Nachplanung an einem IntegrityError, den der generische Fang zu
        # „unerwartet gescheitert" verwischte.
        attempt = context.ledger.plan_revisions + 1

        if planned.kind == "specialist":
            await self._run_specialist_step(run, context, planned, seq, attempt)
        elif planned.kind == "capability":
            await self._run_capability_step(run, context, planned, seq, attempt)
        elif planned.kind == "knowledge_proposal":
            await self._run_proposal_step(run, context, planned, seq, attempt)
        else:
            # `verify` als Planschritt ist ein Hinweis; die echte Pruefung macht
            # der Orchestrator am Ende. Der Schritt wird ehrlich uebersprungen.
            step = self.ledger.create_step(run_id=run.run_id, seq=seq,
                                           kind=planned.kind, attempt=attempt)
            self.ledger.update_step(step.step_id, state="skipped",
                                    summary="Vom Orchestrator selbst erledigt.")
            context.cursor += 1
        context.ledger.note_step()

    # -- Spezialistenschritt -------------------------------------------

    async def _run_specialist_step(self, run, context, planned, seq,
                                   attempt: int = 1) -> None:
        context.ledger.check_specialist()
        digest = context.ledger.guard_attempt("specialist", planned.profile,
                                              planned.instruction)
        spec = SP.profile(planned.profile)
        if spec.mode == SP.BUILDER and context.scope != S.SCOPE_BUILD:
            # Strukturell, nicht als Absichtserklaerung: der Schritt-Executor
            # prueft den SCOPE der Aufgabe.
            raise PL.PlanInvalid("builder_in_research_scope", planned.profile)

        step = self.ledger.create_step(run_id=run.run_id, seq=seq, kind="specialist",
                                       attempt=attempt,
                                       specialist_profile=spec.key,
                                       specialist_role=spec.role)
        self.ledger.update_step(step.step_id, state="running", started=True)
        self.ledger.transition(run.run_id, S.WAITING_SPECIALIST)
        self.ledger.record_event(run.run_id, "step_started",
                                 f"Spezialist {spec.key} beauftragt.",
                                 step_id=step.step_id)

        # Ein Profil ohne Arbeitsort bekommt auch keinen erfunden. Frueher stand
        # hier ein leerer Briefing-Ordner — das war genau der Fehler.
        workdir = run.workspace_path if spec.needs_workspace else ""
        if spec.needs_workspace and not workdir:
            raise PL.PlanInvalid("profile_needs_workspace", spec.key)
        request = SP.SpecialistRequest(profile=spec.key, objective=planned.instruction,
                                       workdir=workdir, run_id=run.run_id,
                                       context="\n".join(context.context_notes[-3:]))
        outcome = await SP.run_specialist(request, researcher=self.researcher)
        context.ledger.note_specialist()
        result = outcome.result

        # Bei einem Fehlschlag gehoert der GRUND ins Buch, nicht nur das Wort
        # `nonzero_exit`. Der Text ist bereits redigiert und gekappt.
        note = (result.recommended_path or (result.findings[0] if result.findings
                                            else result.reason))
        if not result.ok and outcome.stderr_note:
            note = f"{result.reason}: {outcome.stderr_note}"
        self.ledger.update_step(
            step.step_id, state="succeeded" if result.ok else "failed",
            summary=note[:600],
            child_pgid=outcome.pgid, child_started_at=outcome.started_at,
            child_executable=outcome.executable, finished=True,
            outcome_reason=("ok" if result.ok else f"failed:{result.reason}"))
        self.ledger.set_run_fields(
            run.run_id, specialist_count=run.specialist_count + 1,
            specialist_seconds=run.specialist_seconds + float(result.elapsed or 0.0))
        self.ledger.transition(run.run_id, S.RUNNING)

        if not result.ok:
            if outcome.quota:
                await self._finish(run.run_id, S.FAILED, "quota",
                                   "Das Kontingent des Spezialisten ist erschoepft.")
                return
            context.ledger.note_low_value(digest)
            if not planned.optional:
                await self._replan(run, context, f"Schritt {seq} scheiterte")
                return
        else:
            if not result.usable:
                context.ledger.note_low_value(digest)
            # Spezialistenausgabe ist Kenntnisstand — DATEN, gekennzeichnet.
            context.context_notes.append(
                f"[{spec.key}] " + "; ".join(result.findings[:3]))
            # Befunde und Quellen werden HIER festgehalten, nicht erst am Ende.
            # Live gelernt: sie lagen fertig im Journal und erreichten den
            # Menschen nie, weil ein spaeterer Schritt scheiterte und niemand
            # sie mehr in die Hand nahm.
            self._keep_findings(context, result)
            self._maybe_complete(run, context, result, seq)
        context.cursor += 1

    def _keep_findings(self, context: RunContext, result) -> None:
        """Was ein Schritt erarbeitet hat, ueberlebt den Schritt.

        Nur die STRUKTURIERTEN Felder — `findings` und `evidence`. Der
        Rohauszug bleibt draussen: er ist fuer einen Menschen gedacht, der
        nachsieht, und er hat im Ledger, in der Meldung und im Artefakt nichts
        zu suchen.
        """
        for finding in (getattr(result, "findings", []) or []):
            text = SP.redact_specialist_output(str(finding)).strip()
            if text and text not in context.findings:
                context.findings.append(text)
        for source in (getattr(result, "evidence", []) or []):
            text = SP.redact_specialist_output(str(source)).strip()
            if text and text not in context.sources:
                context.sources.append(text)

    def _maybe_complete(self, run: S.AgentRun, context: RunContext,
                        result, seq: int) -> None:
        """Ist das Ziel mit diesem Schritt erfuellt, endet der Plan hier.

        Die Entscheidung ist NICHT „Spezialist gelungen = Lauf gelungen": sie
        haelt das urspruengliche Ziel gegen die strukturierte Ausgabe (siehe
        `completion.evaluate`). Und sie beendet den Lauf nicht selbst — sie
        kuerzt nur den Plan. Der Abschluss laeuft danach durch dieselbe
        Pruefung wie jeder andere Lauf, damit es keinen zweiten Erfolgspfad
        gibt, der die Erlaubnisliste von `_do_verify` umgeht.
        """
        if context.plan is None or context.goal_met:
            return
        task = self.ledger.get_task(run.task_id)
        verdict = CO.evaluate(goal=task.objective if task else "",
                              result=result, scope=context.scope)
        if not verdict.satisfied:
            return
        remaining = len(context.plan.steps) - seq
        context.goal_met = verdict.reason
        context.plan = PL.Plan(goal=context.plan.goal,
                               steps=context.plan.steps[:seq],
                               note=context.plan.note)
        log.info("agent_runtime.goal_satisfied", run_id=run.run_id,
                 reason=verdict.reason, sources=len(verdict.sources),
                 dropped_steps=max(0, remaining))
        self.ledger.record_event(
            run.run_id, "state_changed",
            f"Das Ziel ist mit Schritt {seq} erfuellt — "
            f"{max(0, remaining)} geplante Schritte entfallen.")

    def _briefing_dir(self, run_id: str) -> str:
        folder = os.path.join(S.artifact_root(run_id), "briefing")
        os.makedirs(folder, mode=0o700, exist_ok=True)
        return folder

    # -- Faehigkeitsschritt --------------------------------------------

    async def _run_capability_step(self, run, context, planned, seq,
                                   attempt: int = 1) -> None:
        if self.router is None:
            raise PL.PlanInvalid("router_unavailable")
        context.ledger.guard_attempt("capability", planned.capability,
                                     repr(sorted(planned.arguments)))
        step = self.ledger.create_step(run_id=run.run_id, seq=seq, kind="capability",
                                       attempt=attempt,
                                       capability=planned.capability)
        self.ledger.update_step(step.step_id, state="running", started=True)

        # Ein strukturell unvollstaendiger Schritt erreicht den Router gar nicht
        # erst. Er wurde vorher trotzdem ausgefuehrt — und die Ablehnung des
        # Routers war richtig, aber sie kam zu spaet, um noch etwas zu retten.
        flaw = self._structural_flaw(planned)
        if flaw:
            await self._refuse_invalid_step(run, context, planned, step, flaw)
            return

        # Freigabepflichtige Schritte sind global serialisiert. Bekommt dieser
        # Lauf den Platz nicht, wartet er — als Ereignis sichtbar, nicht still.
        if not self.approvals.acquire(run.run_id):
            self.ledger.update_step(step.step_id, state="waiting")
            self.ledger.record_event(
                run.run_id, "budget_event",
                f"Wartet auf den Freigabeplatz von {self.approvals.holder[:12]}.",
                step_id=step.step_id)
            return

        task = self.ledger.get_task(run.task_id)
        # Die Quellkette: planer-eigene Argumente sind MODEL_DERIVED, alles aus
        # Spezialistenausgabe Abgeleitete UNTRUSTED_CONTENT.
        sources = {name: (authority.SOURCE_SPECIALIST if context.context_notes
                          else authority.SOURCE_PLANNER)
                   for name in planned.arguments}
        outcome = await ST.execute_capability(
            self.router, name=planned.capability, arguments=planned.arguments,
            sources=sources, run_id=run.run_id, task_id=run.task_id,
            when=time.strftime("%Y-%m-%d"), cancel_token=context.cancel)

        self.ledger.update_step(step.step_id, call_id=outcome.call_id,
                                approval_id=outcome.approval_id,
                                outcome_reason=outcome.outcome_reason)

        if outcome.state == "waiting":
            context.pending_step_id = step.step_id
            context.approval_attempts += 1
            self.ledger.update_step(step.step_id, state="waiting")
            self.ledger.transition(run.run_id, S.WAITING_APPROVAL)
            self.ledger.record_event(run.run_id, "approval_requested",
                                     "Eine Freigabe wurde angefragt.",
                                     step_id=step.step_id, ref=outcome.approval_id)
            return

        self.approvals.release(run.run_id)
        await self._settle_capability(run, context, step, outcome, planned)

    def _structural_flaw(self, planned) -> str:
        """Fehlt dem geplanten Schritt eine Pflichtangabe seines Vertrags?

        Geprueft wird ausschliesslich auf FEHLENDE Pflichtargumente — bewusst
        schwaecher als `router._validate`, das zusaetzlich unbekannte Schluessel
        und Typen ablehnt. Die Richtung ist Absicht: was hier durchfaellt, waere
        auch dort durchgefallen, also lehnt diese Pruefung nie etwas ab, das der
        Router zugelassen haette. Der Router bleibt die Autoritaet; das hier ist
        eine vorgezogene Kopie SEINER Regel, keine zweite.
        """
        getter = getattr(self.router, "spec", None)
        if not callable(getter):
            return ""                   # kein Vertrag greifbar: nicht raten
        spec = None
        with contextlib.suppress(Exception):
            spec = getter(planned.capability)
        schema = getattr(spec, "input_schema", None) or {}
        arguments = planned.arguments or {}
        for key in (schema.get("required") or []):
            if key not in arguments:
                return f"missing_argument:{key}"
        return ""

    async def _refuse_invalid_step(self, run, context, planned, step,
                                   flaw: str) -> None:
        """Der ungueltige Schritt wird gebucht, nicht ausgefuehrt — und beim
        ZWEITEN Mal endet der Lauf, statt ihn ein drittes Mal zu planen.

        Die Schleifenbremse in `BudgetLedger` half hier nicht: sie zaehlt einen
        Digest ueber die Argument-GESTALT, und der Planer lieferte jedes Mal
        eine andere. Gezaehlt wird deshalb, was wirklich gleich war — die
        Faehigkeit und ihr struktureller Mangel.
        """
        signature = f"{planned.capability}|{flaw}"
        wiederholt = signature in context.invalid_signatures
        context.invalid_signatures.add(signature)
        self.ledger.update_step(
            step.step_id, state="failed", finished=True,
            outcome_reason=f"planner_invalid_step:{flaw}",
            summary=f"Der geplante Schritt war unvollstaendig ({flaw}) — "
                    "er wurde nicht ausgefuehrt.")
        log.info("agent_runtime.planner_invalid_step", run_id=run.run_id,
                 capability=planned.capability, flaw=flaw, repeated=wiederholt)
        # Der Planer erfaehrt den GRUND. Vorher stand in seinem Kontext nur
        # „Eine Faehigkeit scheiterte" — daraus konnte er nichts lernen, und
        # genau deshalb schlug er denselben Schritt wieder vor.
        context.context_notes.append(
            f"[core] {planned.capability} fehlte eine Pflichtangabe ({flaw}); "
            "so nicht noch einmal")
        if wiederholt:
            await self._finish(
                run.run_id, S.FAILED, "planner_invalid_step",
                "Ich habe denselben unvollstaendigen Schritt zweimal geplant "
                "und hoere damit auf, statt es ein drittes Mal zu versuchen.")
            return
        if planned.optional:
            context.cursor += 1
            return
        await self._replan(run, context, "Ein geplanter Schritt war unvollstaendig")

    async def _settle_capability(self, run, context, step, outcome, planned) -> None:
        if outcome.state == "succeeded":
            self.ledger.update_step(step.step_id, state="succeeded", finished=True,
                                    summary=outcome.human_message[:600])
            context.cursor += 1
            return
        if outcome.state == "denied":
            # Eine Ablehnung ist endgueltig. Es wird KEIN anderer Weg gesucht.
            self.ledger.update_step(step.step_id, state="denied", finished=True,
                                    summary="Die Freigabe wurde abgelehnt.")
            if planned.optional:
                context.cursor += 1
                return
            await self._finish(run.run_id, S.FAILED, "approval_denied",
                               "Du hast das abgelehnt — ich suche keinen anderen Weg.")
            return
        if outcome.state == "unknown":
            # RECOVERY_REQUIRED: gebucht und gemeldet, nie wiederholt.
            self.ledger.update_step(step.step_id, state="unknown", finished=True,
                                    summary="Der Ausgang ist ungewiss.")
            await self._finish(run.run_id, S.FAILED, "recovery_required",
                               "Ich weiss nicht sicher, ob das durchging — bitte pruef es.")
            return

        self.ledger.update_step(step.step_id, state="failed", finished=True,
                                summary=outcome.human_message[:600])
        if outcome.failure_category == "policy_denied":
            boundary = boundaries.policy_refusal(planned.capability, step.step_id)
            await self._open_boundary(run.run_id, boundary)
            return
        if planned.optional:
            context.cursor += 1
            return
        await self._replan(run, context, "Eine Faehigkeit scheiterte")

    # -- Freigabe-Polling ----------------------------------------------

    async def _poll_pending_starts(self) -> None:
        """Auftraege aufnehmen, deren erste Freigabe inzwischen erteilt ist.

        **Der schwerste Fund dieses Milestones, und seine Reparatur.** Der
        Nutzer gab per Face ID frei — und nichts geschah. Die Anfrage lief auf
        EXPIRED, mit null Ausfuehrungsversuchen.

        Der Grund war eine Henne-Ei-Luecke: ein Lauf, der auf eine Freigabe
        wartet, hat `_poll_approval`. Der Auftrag, der den Lauf erst ERZEUGT,
        hatte niemanden — die Anfragekennung stand im Umschlag des Werkzeugs
        und starb mit dem Gespraechszug. Wer danach nicht zufaellig nochmal
        dasselbe sagte, wartete auf ein Ergebnis, das nie kam.

        Dieselbe Bauform wie `_poll_approval`, eine Ebene hoeher — und mit
        derselben Zurueckhaltung:

        * Gelesen wird ueber `read_approval_state`, nicht ueber den Router:
          dessen `not_approved` kann alles heissen. Ein LESEFEHLER ist kein
          NEIN, sondern „weiter warten".
        * Wiederholt wird UNVERAENDERT — dieselben Argumente, derselbe
          Prinzipal, dieselbe Herkunft. Die Herkunft geht in den Digest ein und
          wird nie neu bestimmt: was am Raummikrofon freigegeben wurde, darf
          nicht als etwas Vertrauteres wiederkommen.
        * Der Wiederholer gewinnt KEINE Autoritaet. Er legt eine Kennung erneut
          vor; ob daraus eine Ausfuehrung wird, entscheidet unveraendert der
          Freigabeweg — Digest, Geraetebeweis, Einmaligkeit des Versuchs.

        Ausdruecklich nicht gebaut: ein Rueckruf an der Entscheidungsstelle.
        Das verschoebe Ausfuehrungsautoritaet dorthin, wo nur entschieden wird.
        """
        if self.control_plane is None:
            return
        try:
            wartende = self.ledger.waiting_starts()
        except Exception as exc:  # noqa: BLE001
            log.warning("agent_runtime.pending_starts_unreadable",
                        kind=type(exc).__name__)
            return
        for eintrag in wartende:
            request_id = eintrag["request_id"]
            state = await ST.read_approval_state(self.control_plane, request_id)
            if state == ST.PENDING:
                continue
            if state != ST.APPROVED:
                # DENIED und EXPIRED sind beide endgueltig. Eine abgelehnte
                # Freigabe wird nicht umgangen, und eine verfallene nicht
                # stillschweigend erneuert.
                self.ledger.close_pending_start(request_id, S.START_CLOSED)
                log.info("agent_runtime.start_not_approved",
                         capability=eintrag["capability"], state=state)
                continue

            # Genommen wird VOR dem Ausfuehren: ein Absturz dazwischen darf
            # nicht zu einem zweiten Versuch fuehren.
            self.ledger.close_pending_start(request_id, S.START_TAKEN)
            await self._start_approved(eintrag, request_id)

    async def _start_approved(self, eintrag: dict, request_id: str) -> None:
        """Den freigegebenen Auftrag genau einmal wiederholen."""
        from solvio.capabilities.policy import OriginClass

        try:
            origin = OriginClass(eintrag["origin"])
        except ValueError:
            # Eine Herkunft, die es nicht mehr gibt, wird NICHT geraten.
            log.warning("agent_runtime.start_origin_unknown",
                        capability=eintrag["capability"])
            return
        try:
            # Ueber `steps`, nicht von hier: `router.execute` steht in genau
            # EINEM Modul, und ein Test haelt das fest. Die Zusage ist mehr
            # wert als die zwei Zeilen, die sie hier kostet.
            result = await ST.start_approved_capability(
                self.router, name=eintrag["capability"],
                arguments=dict(eintrag["arguments"]),
                request_id=request_id, principal=eintrag["principal"],
                origin=origin, commanded=eintrag["commanded"])
        except Exception as exc:  # noqa: BLE001
            log.error("agent_runtime.start_resume_failed",
                      capability=eintrag["capability"], kind=type(exc).__name__)
            return
        ok = getattr(result, "succeeded", False)
        log.info("agent_runtime.start_resumed", capability=eintrag["capability"],
                 ok=bool(ok), reason=getattr(result, "reason", "") or "")
        if not ok:
            await notices.send(self.proactive, notices.Notice(
                run_id="", kind="failed",
                summary="Die Freigabe war da, aber der Auftrag liess sich nicht "
                        "mehr starten."))

    async def _poll_approval(self, run: S.AgentRun) -> None:
        """Der Zustand wird GELESEN, nie aus `not_approved` gefolgert."""
        context = self._contexts.get(run.run_id) or self._rebuild_context(run)
        step = self.ledger.get_step(context.pending_step_id) if context.pending_step_id \
            else None
        if step is None:
            steps = [s for s in self.ledger.steps_for_run(run.run_id)
                     if s.state == "waiting"]
            step = steps[-1] if steps else None
        if step is None:
            self.ledger.transition(run.run_id, S.RUNNING)
            return

        state = await ST.read_approval_state(self.control_plane, step.approval_id)
        if state == ST.PENDING:
            return                                   # weiter parken

        if state == ST.APPROVED:
            planned = self._planned_for(context, step)
            outcome = await ST.execute_capability(
                self.router, name=step.capability,
                arguments=planned.arguments if planned else {},
                sources={}, run_id=run.run_id, task_id=run.task_id,
                when=time.strftime("%Y-%m-%d"),
                approval_request_id=step.approval_id, cancel_token=context.cancel)
            self.approvals.release(run.run_id)
            self.ledger.record_event(run.run_id, "approval_resolved",
                                     "Die Freigabe wurde erteilt.",
                                     step_id=step.step_id, ref=step.approval_id)
            self.ledger.transition(run.run_id, S.RUNNING)
            await self._settle_capability(run, context, step, outcome,
                                          planned or PL.PlannedStep(kind="capability"))
            return

        if state == ST.DENIED:
            self.approvals.release(run.run_id)
            self.ledger.record_event(run.run_id, "approval_resolved",
                                     "Die Freigabe wurde abgelehnt.",
                                     step_id=step.step_id, ref=step.approval_id)
            self.ledger.update_step(step.step_id, state="denied", finished=True)
            self.ledger.transition(run.run_id, S.RUNNING)
            await self._finish(run.run_id, S.FAILED, "approval_denied",
                               "Du hast das abgelehnt — ich suche keinen anderen Weg.")
            return

        # EXPIRED / EXECUTING / CONSUMED / FAILED → neu anfragen, gedeckelt.
        self.approvals.release(run.run_id)
        self.ledger.transition(run.run_id, S.RUNNING)
        if context.approval_attempts >= ST.MAX_APPROVAL_REQUESTS:
            boundary = boundaries.UserBoundary(
                kind=boundaries.PRODUCT_DECISION, step_id=step.step_id,
                action=f"Gib „{step.capability}“ frei, wenn du das naechste Mal hinsiehst",
                reason=("die Freigabefrage ist mehrfach verfallen, bevor du sie "
                        "gesehen hast"),
                resume_hint="danach nehme ich den Lauf wieder auf")
            await self._open_boundary(run.run_id, boundary)
            return
        self.ledger.record_event(run.run_id, "approval_resolved",
                                 "Die Freigabe ist verfallen — ich frage neu.",
                                 step_id=step.step_id)

    def _planned_for(self, context: RunContext, step) -> PL.PlannedStep | None:
        if context.plan is None:
            return None
        index = max(0, step.seq - 1)
        if index < len(context.plan.steps):
            return context.plan.steps[index]
        return None

    # -- Wissensvorschlag ----------------------------------------------

    async def _run_proposal_step(self, run, context, planned, seq,
                                 attempt: int = 1) -> None:
        """Ein Vorschlag ist ein Artefakt plus Meldung. Es gibt KEINEN
        Schreibpfad in Wissen oder Gedaechtnis — nicht gefiltert, sondern nicht
        verdrahtet."""
        step = self.ledger.create_step(run_id=run.run_id, seq=seq,
                                       kind="knowledge_proposal", attempt=attempt)
        folder = S.artifact_root(run.run_id)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        path = os.path.join(folder, f"proposal-{step.step_id}.md")
        body = SP.redact_specialist_output(planned.instruction)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, 0o600)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        artifact = self.ledger.add_artifact(run_id=run.run_id, kind="proposal",
                                            path=path, sha256=digest,
                                            size=len(body.encode("utf-8")))
        self.ledger.update_step(step.step_id, state="succeeded", finished=True,
                                artifact_refs=[artifact.artifact_id],
                                summary="Wissensvorschlag abgelegt (nicht uebernommen).")
        context.cursor += 1

    # -- Pruefung und Abschluss ----------------------------------------

    #: Schrittzustaende, die einen Lauf gelingen lassen duerfen. Alles andere —
    #: auch `unknown`, `pending`, `running`, `waiting` — tut es NICHT.
    SETTLED_OK = frozenset({"succeeded", "skipped"})

    async def _do_verify(self, run: S.AgentRun, context: RunContext) -> None:
        """Erfolg ist die Ausnahme, die BELEGT werden muss — nicht der Rest.

        Live gefunden, und es war der schwerste Fund der Abnahme: geprueft wurde
        auf `failed`/`denied`, und ein Schritt mit UNGEWISSEM Ausgang
        (`unknown`) fiel durch das Raster. Ein Lauf, dessen Kindprozess nach
        einem Neustart nicht mehr eindeutig zuzuordnen war, endete damit als
        SUCCEEDED — mit dem Satz „Alle Schritte haben ein Ergebnis".

        Genau das verbietet die Architektur woertlich: nichts wird nach einem
        Neustart still als erfolgreich verbucht. Deshalb steht hier jetzt eine
        Erlaubnisliste statt einer Sperrliste: was nicht nachweislich erledigt
        ist, laesst den Lauf nicht gelingen.
        """
        steps = self.ledger.steps_for_run(run.run_id)
        unsettled = [s for s in steps if s.state not in self.SETTLED_OK]
        uncertain = [s for s in unsettled if s.state == "unknown"]
        step = self.ledger.create_step(run_id=run.run_id,
                                       seq=len(steps) + 1, kind="verify")
        if uncertain:
            # Ein ungewisser Ausgang bleibt manuell. Er wird NIE wiederholt und
            # nie als Erfolg verbucht — die Idempotenzmechanik der Zahlungs- und
            # Freigabeschicht ist die einzige Wahrheit darueber, ob etwas
            # passiert ist.
            self.ledger.update_step(
                step.step_id, state="unknown", finished=True,
                summary=f"{len(uncertain)} Schritte mit ungewissem Ausgang.")
            await self._finish(
                run.run_id, S.FAILED, "recovery_required",
                "Ich weiss bei einem Schritt nicht sicher, ob er durchging — "
                "bitte sieh nach, bevor wir etwas wiederholen.")
            return
        if unsettled:
            self.ledger.update_step(
                step.step_id, state="failed", finished=True,
                summary=f"{len(unsettled)} Schritte ohne Ergebnis.")
            await self._finish(run.run_id, S.FAILED, "specialist_failed",
                               "Der Lauf ist ohne belastbares Ergebnis geendet.")
            return
        self.ledger.update_step(step.step_id, state="succeeded", finished=True,
                                summary="Alle Schritte haben ein Ergebnis.")
        harvested, why = await self._harvest(run, context)
        summary = self._summarise(context)
        if harvested:
            summary = (f"{summary} Das Ergebnis liegt als {harvested} bereit — "
                       "uebernehmen ist deine Entscheidung.")
            await self._finish(run.run_id, S.SUCCEEDED, "", summary)
            return
        if why:
            # Live gefunden: der Builder legte die Datei an und committete sie
            # nicht. Die Ernte nahm den unveraenderten Zweig, der Lauf hiess
            # „fertig", und SOLVIO sagte „das Ergebnis liegt bereit". Es lag
            # nichts bereit. Ein Bau-Lauf ohne Arbeitsergebnis ist kein Erfolg —
            # der Docstring von `_harvest` versprach das laengst, der Code tat
            # es nicht.
            await self._finish(run.run_id, S.FAILED, "no_result",
                               _HARVEST_WORDS.get(why, _HARVEST_WORDS[""]))
            return
        await self._finish(run.run_id, S.SUCCEEDED, "", summary)

    async def _harvest(self, run: S.AgentRun,
                       context: RunContext) -> tuple[str, str]:
        """Das Ergebnis eines Bau-Laufs in das Ernte-Repo des Cores.

        Erst pruefen, dann holen — die Pruefung ist die Kredentialgrenze des
        Codex-Builders, und sie steht ausdruecklich VOR dem Fetch. Verweigert
        sie, endet der Lauf ehrlich ohne Ergebnis statt mit einem halben.

        Der Produktivbaum wird dabei nie beruehrt: geerntet wird in
        `~/.solvio/agent_harvest.git`, nicht in das Zielrepo.
        """
        if context.scope != S.SCOPE_BUILD or self.workspaces is None:
            return "", ""
        workspace = context.workspace
        if workspace is None:
            return "", "no_workspace"
        steps = self.ledger.steps_for_run(run.run_id)
        step = self.ledger.create_step(run_id=run.run_id, seq=len(steps) + 1,
                                       kind="harvest")
        try:
            ref = self.workspaces.harvest(workspace)
        except Exception as exc:  # noqa: BLE001
            reason = getattr(exc, "reason", type(exc).__name__)
            log.warning("agent_runtime.harvest_failed", run_id=run.run_id,
                        reason=reason)
            self.ledger.update_step(step.step_id, state="failed", finished=True,
                                    summary=f"Ernte verweigert: {reason}")
            return "", reason
        self.ledger.set_run_fields(run.run_id, branch_ref=ref)
        self.ledger.update_step(step.step_id, state="succeeded", finished=True,
                                summary=f"Als {ref} bereitgestellt.")
        return ref, ""

    def _summarise(self, context: RunContext) -> str:
        if not context.context_notes:
            return "Der Lauf ist durch."
        return SP.redact_specialist_output(" ".join(context.context_notes))[:900]

    async def _replan(self, run: S.AgentRun, context: RunContext, why: str) -> None:
        """REPLANNING wird von genau zwei Orchestrator-Ereignissen ausgeloest —
        Schrittfehlschlag und Pruefbefund —, NIE von Planerausgabe selbst. Es
        gibt keine versteckte Planerschleife."""
        try:
            context.ledger.check_revision()
        except BU.BudgetExhausted:
            await self._finish(run.run_id, S.FAILED, "budget_exhausted",
                               "Ich komme so nicht weiter.")
            return
        context.ledger.note_revision()
        context.context_notes.append(f"[core] {why}")
        self.ledger.set_run_fields(run.run_id,
                                   plan_revision=run.plan_revision + 1)
        self.ledger.record_event(run.run_id, "state_changed",
                                 f"Neuer Plan noetig: {why}.")
        # RUNNING → ... → PLANNING gibt es in der Tabelle nicht; der Umweg ueber
        # VERIFYING waere gelogen. Der Lauf plant im selben Zustand neu.
        await self._do_plan(self.ledger.get_run(run.run_id), context)

    async def _open_boundary(self, run_id: str,
                             boundary: boundaries.UserBoundary) -> None:
        self.ledger.set_run_fields(run_id, boundary=boundary.as_dict())
        self.ledger.transition(run_id, S.WAITING_USER)
        self.ledger.record_event(run_id, "boundary_opened", boundary.action[:300],
                                 step_id=boundary.step_id)
        await notices.send(self.proactive, notices.Notice(
            run_id=run_id, kind="boundary", summary=boundary.message,
            priority="hoch"))

    async def _finish(self, run_id: str, state: str, category: str,
                      message: str) -> None:
        run = self.ledger.get_run(run_id)
        if run is None or run.terminal:
            return
        self.approvals.release(run_id)
        try:
            self.ledger.transition(run_id, state, failure_category=category,
                                   result_summary=message)
        except S.LedgerTransitionError as exc:
            # Frueher stand hier ein `suppress`. Das war die zweite Haelfte der
            # Endlosschleife: der Uebergang war verboten, niemand erfuhr es, der
            # Lauf blieb nicht-terminal, und der Takt begann von vorn — 297 Mal.
            #
            # Weiterwerfen waere die falsche Antwort: `tick` faengt nichts, ein
            # einzelner unbeendbarer Lauf wuerde also den Takt ALLER Laeufe
            # toeten. Stattdessen wird er hier festgehalten: laut im Log, als
            # Ereignis im Buch, und der Takt fasst ihn nicht mehr an. Er bleibt
            # sichtbar offen — das ist die Wahrheit, und der Startabgleich
            # macht beim naechsten Start INTERRUPTED daraus.
            log.error("agent_runtime.finish_blocked", run_id=run_id,
                      current=exc.current, wanted=exc.wanted)
            self._unfinishable.add(run_id)
            with contextlib.suppress(Exception):
                self.ledger.record_event(
                    run_id, "state_changed",
                    "Der Lauf kann nicht enden — er wird nicht weiter versucht.")
            return
        task_state = {S.SUCCEEDED: S.TASK_COMPLETED, S.FAILED: S.TASK_FAILED,
                      S.CANCELLED: S.TASK_CANCELLED}.get(state, S.TASK_ACTIVE)
        with contextlib.suppress(Exception):
            self.ledger.set_task_state(run.task_id, task_state)
        # Ein GESCHEITERTER Bau-Lauf behaelt seinen Arbeitsbereich: dort steht,
        # was der Builder wirklich getan hat, und ohne ihn bleibt nur das Wort
        # „gescheitert". Der Startabgleich raeumt ihn beim naechsten Start auf —
        # das Zeitfenster fuer eine Nachschau kostet nichts.
        if self.workspaces is not None and run.workspace_path and state != S.FAILED:
            with contextlib.suppress(Exception):
                self.workspaces.cleanup(run_id)
        # Was erarbeitet wurde, geht nicht verloren, weil etwas anderes
        # scheiterte. Live gelernt: eine fertige Antwort mit drei Quellen lag im
        # Journal, und die Meldung sagte „Ich komme so nicht weiter" mit
        # `findings=[]`. Das war nicht falsch und trotzdem irrefuehrend.
        findings, sources = self._collect_findings(run_id)
        with contextlib.suppress(Exception):
            self._write_report(run_id, findings, sources)
        if state == S.FAILED and findings:
            message = (f"{message} Was ich bis dahin herausgefunden habe, "
                       "liegt bei — abgeschlossen ist der Auftrag damit nicht.")
        await notices.send(self.proactive, notices.Notice(
            run_id=run_id, kind=state.lower(), summary=message,
            findings=tuple(findings) + tuple(f"Quelle: {s}" for s in sources)))
        self.ledger.record_event(run_id, "notice_sent", "Ergebnis gemeldet.")
        self._contexts.pop(run_id, None)

    def _collect_findings(self, run_id: str) -> tuple[list[str], list[str]]:
        """Befunde und Quellen des Laufs — aus dem Kontext, sonst aus dem Buch.

        Der Rueckfall auf das Ledger ist kein Zierrat: nach einem Neustart gibt
        es den fluechtigen Kontext nicht mehr, und ein Lauf, der DANN endet,
        haette sonst wieder eine leere Meldung.
        """
        context = self._contexts.get(run_id)
        if context is not None and (context.findings or context.sources):
            return list(context.findings[:8]), list(context.sources[:12])
        findings: list[str] = []
        with contextlib.suppress(Exception):
            for step in self.ledger.steps_for_run(run_id):
                if step.kind == "specialist" and step.state == "succeeded":
                    text = (step.summary or "").strip()
                    if text and text not in findings:
                        findings.append(text)
        return findings[:8], []

    def _write_report(self, run_id: str, findings: list[str],
                      sources: list[str]) -> str:
        """Legt Befunde und Quellen als Artefakt ab. STRUKTUR, kein Transkript.

        Bewusst JSON und bewusst nur diese zwei Listen: was hier nicht
        aufgezaehlt ist, kommt auch nicht hinein. Ein Artefakt, das den
        Gedankengang oder den Rohtext eines Spezialisten traegt, waere genau
        der Kanal, den die Laengendeckel des Ledgers verhindern sollen.
        """
        if not findings and not sources:
            return ""
        folder = S.artifact_root(run_id)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        path = os.path.join(folder, f"befunde-{run_id}.json")
        if os.path.exists(path):
            return ""                   # ein Lauf endet einmal
        body = json.dumps({"lauf": run_id, "befunde": findings[:8],
                           "quellen": sources[:12],
                           "herkunft": notices.CONTENT_TRUST},
                          ensure_ascii=False, indent=1)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        os.chmod(path, 0o600)
        raw = body.encode("utf-8")
        artifact = self.ledger.add_artifact(
            run_id=run_id, kind="report", path=path,
            sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
        return artifact.artifact_id

    # =================================================================
    # Nutzerhandlungen
    # =================================================================

    async def cancel(self, run_id: str) -> bool:
        run = self.ledger.get_run(run_id)
        if run is None or run.terminal:
            return False
        context = self._contexts.get(run_id)
        if context is not None:
            context.cancel.set()
        await self._finish(run_id, S.CANCELLED, "cancelled_by_user",
                           "Der Lauf wurde abgebrochen.")
        return True

    async def resume(self, run_id: str) -> bool:
        """Wiederaufnahme an einer Nutzergrenze. Idempotent."""
        run = self.ledger.get_run(run_id)
        if run is None or run.state != S.WAITING_USER:
            return False
        self.ledger.set_run_fields(run_id, boundary="")
        self.ledger.transition(run_id, S.RUNNING)
        self.ledger.record_event(run_id, "boundary_resumed",
                                 "Der Nutzer hat die Grenze erledigt.")
        context = self._contexts.get(run_id) or self._rebuild_context(run)
        context.approval_attempts = 0
        return True

    # =================================================================
    # Neustart-Abgleich
    # =================================================================

    async def reconcile(self) -> dict:
        """Erst abgleichen, dann fortsetzen — nie blind wiederholen.

        Nichts wird nach einem Neustart still als Erfolg verbucht. Ein
        `capability`-Schritt mit ungewissem Ausgang bleibt manuell; eine
        Freigabe ist nach dem Neustart ohnehin verfallen und wird neu angefragt.
        """
        marked, killed, reported = [], [], []
        for run in self.ledger.open_runs():
            if run.state in (S.WAITING_USER,):
                continue                     # eine Grenze ueberlebt den Neustart
            if run.state != S.INTERRUPTED:
                with contextlib.suppress(S.LedgerTransitionError):
                    self.ledger.transition(
                        run.run_id, S.INTERRUPTED,
                        summary="Der Core endete waehrend des Laufs.")
                    marked.append(run.run_id)
            self.ledger.record_event(run.run_id, "recovered",
                                     "Neustart erkannt — Abgleich laeuft.")
            for step in self.ledger.steps_for_run(run.run_id):
                verdict = self._reconcile_step(step)
                if verdict == "killed":
                    killed.append(step.step_id)
                elif verdict == "reported":
                    reported.append(step.step_id)
        stale = []
        if self.workspaces is not None:
            with contextlib.suppress(Exception):
                stale = self.workspaces.reconcile(self.ledger)
        log.info("agent_runtime.reconciled", interrupted=len(marked),
                 killed=len(killed), reported=len(reported), stale=len(stale))
        return {"interrupted": marked, "killed": killed, "reported": reported,
                "stale_workspaces": stale}

    def _reconcile_step(self, step) -> str:
        """Ein verwaister Unterprozess wird NUR bei voller Uebereinstimmung
        beendet: pgid UND Startzeit UND Programmpfad.

        Die PID-1072-Lehre: eine PID ist kein Besitztitel. Ein recyceltes
        Prozesspaar zu toeten waere ein Schaden, den niemand mit dem Lauf in
        Verbindung braechte.
        """
        if step.state != "running" or not step.child_pgid:
            return "none"
        if not process_group_matches(step.child_pgid, step.child_started_at,
                                     step.child_executable):
            self.ledger.update_step(step.step_id, state="unknown", finished=True,
                                    summary="Kindprozess nicht eindeutig — gemeldet.")
            return "reported"
        import signal
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(step.child_pgid, signal.SIGKILL)
        self.ledger.update_step(step.step_id, state="failed", finished=True,
                                summary="Verwaister Kindprozess beendet.")
        return "killed"


def process_group_matches(pgid: int, started_at: float, executable: str) -> bool:
    """Alle drei muessen passen. Bei Unsicherheit: NICHT toeten, sondern melden."""
    if not pgid or pgid <= 1:
        return False
    try:
        import subprocess
        out = subprocess.run(["/bin/ps", "-o", "lstart=,comm=", "-g", str(pgid)],
                             capture_output=True, text=True, timeout=5)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    if out.returncode != 0 or not out.stdout.strip():
        return False
    if executable and os.path.basename(executable) not in out.stdout:
        return False
    if started_at:
        try:
            import time as _t
            for line in out.stdout.strip().splitlines():
                stamp = " ".join(line.split()[:5])
                parsed = _t.mktime(_t.strptime(stamp, "%a %b %d %H:%M:%S %Y"))
                if abs(parsed - started_at) <= 5:
                    return True
            return False
        except (ValueError, OverflowError):
            return False
    return True
