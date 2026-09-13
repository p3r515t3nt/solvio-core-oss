"""Das Arbeitsregister einer Konversation — Verweise, keine Kopien.

Der Router braucht eine Antwort auf zwei Fragen, und beide sind Verweisfragen:
*laeuft die Arbeit, um die gerade gebeten wird, schon?* und *worauf bezieht
sich „und kannst du das gleich beheben"?*

**Konversationsgebunden by construction.** Das Register wird nicht aus den
Buechern der Laufzeit zusammengesucht und dann gefiltert — es wird aus den
Entscheidungen DIESER Konversation aufgebaut, und der Zustand wird je Verweis
nachgeschlagen. Fremde Arbeit kann so gar nicht erst hineingeraten; eine
Kennung, die jemand in einen Inhalt geschrieben hat, findet keinen Eintrag und
faellt damit weg (§3 des Vertrags).

**Alles, was ein Executor geschrieben hat, ist Information.** Ergebniszeilen
sind gekappt, redigiert und ausdruecklich als `untrusted_executor` gerahmt. Sie
duerfen den Einschaetzer informieren; autorisieren duerfen sie nichts — und in
ein torgemessenes Argument wandern sie nie.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from solvio.cognition import models as M
from solvio.logging_setup import get_logger

log = get_logger("cognition")

#: Die Rahmung, unter der Executor-Text in die Einschaetzung geht. Dasselbe
#: Wort wie in `deep/events.py` und `bots/answer.py` — zwei Woerter fuer
#: dieselbe Sache waeren zwei Wahrheiten.
CONTENT_TRUST = "untrusted_executor"

#: Zustaende, die „laeuft noch" bedeuten. Der Rest ist erledigt.
_ACTIVE_DEEP: frozenset[str] = frozenset({"queued", "running", "waiting_for_user"})


@dataclass
class WorkEntry:
    """Ein Eintrag des Registers. Eine Zeile, vier Felder, kein Text."""

    work_id: str
    route: str
    state: str
    summary: str = ""
    active: bool = False

    def as_line(self) -> str:
        summary = self.summary[:M.REGISTER_SUMMARY_CHARS]
        return f"{self.work_id} | {self.route} | {self.state} | {summary}"


@dataclass
class ContinuityView:
    """Was der Einschaetzer ueber die bisherige Arbeit sehen darf."""

    conversation_ref: str = ""
    entries: list[WorkEntry] = field(default_factory=list)
    #: Der Turn, zu dem eine Rueckfrage offen ist — leer, wenn keine offen ist.
    pending_clarification_turn: str = ""
    #: Der Gespraechsausschnitt (freigegebene 3000-Zeichen-Kappe).
    recent_context: str = ""

    def known_ids(self) -> set[str]:
        """Die Kennungen, die eine Fortsetzung ueberhaupt nennen darf."""
        return {entry.work_id for entry in self.entries if entry.work_id}

    def active_ids(self) -> set[str]:
        """Die Arbeit, die noch laeuft."""
        return {entry.work_id for entry in self.entries
                if entry.work_id and entry.active}

    def entry(self, work_id: str) -> WorkEntry | None:
        for candidate in self.entries:
            if candidate.work_id == work_id:
                return candidate
        return None

    def register_block(self) -> str:
        """Die Zeilen, die in die Einschaetzung gehen — mit ihrer Rahmung."""
        if not self.entries:
            return "Keine laufende oder juengste Arbeit in diesem Gespraech."
        lines = [entry.as_line() for entry in self.entries[:M.REGISTER_MAX]]
        return "\n".join(lines)


def _redact(text: str) -> str:
    """Zweites Netz. Eine Ergebniszeile ist Executor-Text."""
    try:
        from solvio.specialists.launcher import redact
        return redact(str(text or ""))
    except Exception:  # noqa: BLE001 - eine Rahmung darf nie stoeren
        return ""


async def build(dispatcher: Any, ledger: Any, *, conversation_ref: str,
                now: float = 0.0) -> ContinuityView:
    """Setzt das Register zusammen. Liest, schreibt nie.

    Fehlt eine der Quellen — keine Agentenlaufzeit angehaengt, kein tiefer
    Executor, kein Gespraechsspeicher —, faellt genau dieser Teil weg. Der
    Router laeuft dann mit weniger Kontext weiter; er faellt nicht aus.
    """
    del now  # das Register ist zustandsbezogen, nicht zeitbezogen
    view = ContinuityView(conversation_ref=str(conversation_ref or ""))
    if not view.conversation_ref:
        return view

    try:
        decisions = ledger.recent(view.conversation_ref, limit=M.REGISTER_MAX * 2)
    except Exception as exc:  # noqa: BLE001 - ein unlesbares Buch ist kein Absturz
        log.warning("cognition.register_unreadable", kind=type(exc).__name__)
        return view

    # Die juengste offene Rueckfrage — sie ueberlebt genau bis zur naechsten
    # Kommission dieser Konversation.
    for row in decisions:
        if row.get("outcome") == "clarification":
            view.pending_clarification_turn = str(row.get("turn_ref") or "")
            break
        if row.get("outcome") in ("dispatched", "handed_back", "refused", "failed"):
            break

    agent_states = _agent_state_reader(dispatcher)
    deep_states = await _deep_states(dispatcher)

    seen: set[str] = set()
    for row in decisions:
        produced = str(row.get("produced_ref") or "")
        if not produced or produced in seen:
            continue
        seen.add(produced)
        route = str(row.get("route_final") or "")
        state, summary, active = _resolve(produced, agent_states, deep_states)
        view.entries.append(WorkEntry(work_id=produced, route=route,
                                      state=state, summary=summary,
                                      active=active))
        if len(view.entries) >= M.REGISTER_MAX:
            break

    view.recent_context = _recent_context(dispatcher, view.conversation_ref)
    return view


def _resolve(produced: str, agent_states: Any,
             deep_states: dict[str, str]) -> tuple[str, str, bool]:
    """Der heutige Zustand eines Verweises — aus dem Buch, dem er gehoert."""
    if produced.startswith("at-") and agent_states is not None:
        return agent_states(produced)
    status = deep_states.get(produced, "")
    if status:
        return status, "", status in _ACTIVE_DEEP
    return "unbekannt", "", False


def _agent_state_reader(dispatcher: Any):
    """Ein Leser fuer Aufgabenkennungen — oder `None`, wenn keine Laufzeit da ist.

    Die Laufzeit wird ueber ein Attribut des Dispatchers erreicht und
    ausdruecklich NICHT importiert: ein Modulimport von `solvio.agent_runtime`
    im Kern waere genau die Zusage, die `SOLVIO_AGENT_RUNTIME=off` bricht.
    """
    orchestrator = getattr(dispatcher, "agent_runtime", None)
    book = getattr(orchestrator, "ledger", None)
    if book is None:
        return None

    def read(task_id: str) -> tuple[str, str, bool]:
        try:
            runs = book.runs_for_task(task_id)
        except Exception as exc:  # noqa: BLE001 - fail-soft statt raten
            log.info("cognition.task_unreadable", kind=type(exc).__name__)
            return "unbekannt", "", False
        if not runs:
            return "unbekannt", "", False
        run = runs[-1]
        state = str(getattr(run, "state", "") or "")
        summary = _redact(getattr(run, "result_summary", "") or "")
        finished = getattr(run, "finished_at", None)
        return state, summary, finished is None

    return read


async def _deep_states(dispatcher: Any) -> dict[str, str]:
    """Die Zustaende der tiefen Aufgaben. Leer, wenn kein Executor da ist."""
    runtime = getattr(dispatcher, "deep_runtime", None)
    if runtime is None:
        return {}
    try:
        handles = await runtime.list_tasks()
    except Exception as exc:  # noqa: BLE001 - eine Messung darf nie stoeren
        log.info("cognition.deep_unreadable", kind=type(exc).__name__)
        return {}
    states: dict[str, str] = {}
    for handle in handles or []:
        status = getattr(handle, "status", None)
        states[str(getattr(handle, "id", ""))] = str(
            getattr(status, "value", status) or "")
    return states


def _recent_context(dispatcher: Any, conversation_ref: str) -> str:
    """Der freigegebene Gespraechsausschnitt — dieselbe Kappe wie sonst."""
    store = getattr(dispatcher, "conversations", None)
    if store is None:
        return ""
    try:
        messages = store.recent_context(conversation_ref,
                                        max_chars=M.RECENT_CONTEXT_CHARS)
    except Exception as exc:  # noqa: BLE001 - ohne Ausschnitt laeuft es weiter
        log.info("cognition.context_unreadable", kind=type(exc).__name__)
        return ""
    lines = [f"{row.get('role', '')}: {row.get('text', '')}"
             for row in messages or []]
    return "\n".join(lines)[:M.RECENT_CONTEXT_CHARS]


def turn_text(dispatcher: Any, conversation_ref: str, turn_ref: str) -> str:
    """Der Text eines frueheren Turns — per Verweis, aus seinem eigenen Haus.

    Der Gespraechsspeicher bleibt die kanonische Heimat der Sprache des
    Nutzers. Der Router haelt eine Kennung und holt den Text beim Lesen; er
    legt keine zweite Kopie an.
    """
    store = getattr(dispatcher, "conversations", None)
    if store is None or not turn_ref:
        return ""
    try:
        messages = store.messages(conversation_ref)
    except Exception as exc:  # noqa: BLE001 - fail-soft
        log.info("cognition.turn_unreadable", kind=type(exc).__name__)
        return ""
    for row in reversed(messages or []):
        if row.get("source_turn_id") == turn_ref and row.get("role") == "user":
            return str(row.get("text") or "")
    return ""


def stamp() -> float:
    """Eine Uhr, die ein Test ersetzen kann."""
    return time.time()
