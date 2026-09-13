"""Das Agent Run Ledger — es fuehrt Handlungen, keine Gedanken (ADR-0029).

Es beantwortet: *Was hast du getan? Welche Agenten haben daran gearbeitet?
Woher kommt dieses Ergebnis? Was laeuft noch? Warum ist das fehlgeschlagen?* —
auch nach einem Absturz.

Es ist ausdruecklich NICHT die Zugriffsspur des Tresors, nicht das
Zahlungsbuch, nicht das Freigabejournal. Wo einer dieser Orte schon eine
Wahrheit fuehrt, **verweist** das Ledger (`approval_id`, `execution_id`,
`call_id`) statt zu kopieren.

Drei Entscheidungen stecken im Code und nicht in einer Ermahnung:

* **Es gibt keine Spalte, in die ein Transkript passt.** Freitext ist einzeln
  benannt (nie `**kwargs`) und laengengedeckelt. Ein Gedankengang hat hier
  keinen Ort — nicht, weil er gefiltert wuerde, sondern weil keiner existiert.
* **Geheimnisgestalt laesst den Schreibvorgang scheitern**, statt bereinigt zu
  werden. Eine Ledger-Zeile ist eine Aussage, und eine halbe Aussage ueber
  etwas, das man nicht zeigen darf, ist schlechter als keine. Erst danach
  laeuft der Text noch durch die Redaktion des Starters — als zweites Netz fuer
  Formen, die das eine Praedikat des Hauses nicht kennt, nicht als Ersatz fuer
  die Verweigerung.
* **Die Zustandsmaschine ist eine geschlossene Tabelle.** Eine verbotene Kante
  wirft. `SUCCEEDED → RUNNING` ist keine Nachlaessigkeit, die ein Aufrufer
  vermeiden soll, sondern ein `LedgerTransitionError`.

Hauskonvention vollstaendig: eigene Datei unter `SOLVIO_STATE_DIR`,
Pfad-Override `SOLVIO_AGENT_RUNS_DB` fuer Tests, Verzeichnis 0700, Datei und
`-wal`/`-shm` 0600 unter enger umask, WAL aus dem Schemakopf, `foreign_keys`
und `busy_timeout` **je Verbindung** (die Lehre des Proactive-Stores: im Schema
wirkt `foreign_keys` genau einmal, naemlich auf der Verbindung, die das Schema
anlegt — danach feuert keine Kaskade mehr), additive Migration, Verbindungen je
Operation mit explizitem `close()`.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass, field

from solvio.logging_setup import get_logger

log = get_logger("agent_runtime")

DEFAULT_PATH = "~/.solvio/agent_runs.sqlite3"

#: Testschalter. Ein Test schreibt nie in das produktive Buch.
PATH_ENV = "SOLVIO_AGENT_RUNS_DB"

#: Wo die Artefakte liegen — Dateien, nicht Datenbankinhalt.
ARTIFACT_DIRNAME = "agent_runs"


def state_dir() -> str:
    return os.environ.get("SOLVIO_STATE_DIR", os.path.expanduser("~/.solvio"))


def resolve_path(path: str = "") -> str:
    """Wohin das Buch gehoert. Ausdrueckliches Argument schlaegt Umgebung schlaegt
    Vorgabe — dieselbe Reihenfolge wie beim Buch des Brokers."""
    chosen = path or os.environ.get(PATH_ENV, "")
    if not chosen:
        chosen = os.path.join(state_dir(), "agent_runs.sqlite3")
    return os.path.abspath(os.path.expanduser(chosen))


def artifact_root(run_id: str = "") -> str:
    """`~/.solvio/agent_runs/<run_id>/` — neben dem Buch, nicht darin."""
    base = os.path.join(state_dir(), ARTIFACT_DIRNAME)
    return os.path.join(base, run_id) if run_id else base


# =====================================================================
# Geschlossene Vokabulare
# =====================================================================

#: Zustaende eines Laufs (Architektur §5).
CREATED = "CREATED"
PLANNING = "PLANNING"
RUNNING = "RUNNING"
WAITING_SPECIALIST = "WAITING_SPECIALIST"
WAITING_CAPABILITY = "WAITING_CAPABILITY"
WAITING_APPROVAL = "WAITING_APPROVAL"
WAITING_USER = "WAITING_USER"
VERIFYING = "VERIFYING"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"
INTERRUPTED = "INTERRUPTED"

#: Endzustaende. Endgueltig, kein Wiedereintritt.
TERMINAL_STATES = frozenset({SUCCEEDED, FAILED, CANCELLED})

#: Zustaende, in denen ein Lauf **parkt** und deshalb KEINEN Laufzeit-Slot
#: belegt (Architektur §5/§9): ein wartender Lauf verstopft die Laufzeit nicht.
PARKED_STATES = frozenset({WAITING_APPROVAL, WAITING_USER})

#: Die geschlossene Uebergangstabelle. Jede Kante ist Code; was nicht dasteht,
#: wirft. Muster: `payment/intent.py`.
#:
#: **Eine Abweichung vom Wortlaut der Architektur, mit Grund.** Die Tabelle in
#: §5 gibt `INTERRUPTED` eine eingehende Kante nur aus den WAITING_*-Zustaenden
#: und aus VERIFYING. §12 verlangt aber, dass beim Neustart **alle**
#: nicht-terminalen Laeufe INTERRUPTED werden — und ein Absturz trifft einen
#: Lauf fast immer in PLANNING oder RUNNING, also genau dort, wo die Tabelle
#: keine Kante hat. Beides zusammen ist nicht erfuellbar.
#:
#: Aufgeloest wird zugunsten von §12, weil das die SICHERHEITSaussage ist:
#: „nichts wird nach einem Neustart still als Erfolg verbucht". Die Kanten
#: kosten nichts an Strenge — `INTERRUPTED` ist ausschliesslich das Ergebnis der
#: Neustart-Abstimmung, kein Zustand, den die Laufzeit im Betrieb ansteuert, und
#: kein Weg aus einem Endzustand heraus (die drei bleiben leer).
#: **Zweite Abweichung, ebenfalls live erzwungen: jeder nicht-terminale Zustand
#: muss ENDEN koennen.** Die Tabelle gab `CREATED` keine Kante nach `FAILED` —
#: konsequent gedacht, denn ein gerade angenommener Lauf hat noch nichts getan,
#: woran er scheitern koennte. Er hat aber: das Anlegen der Arbeitskopie liegt
#: VOR der Planung. Ein Lauf, dessen Klon misslang, wollte `FAILED` werden, die
#: Tabelle verbot es, der Fehler wurde verschluckt, der Lauf blieb
#: nicht-terminal — und der Takt versuchte es alle zwei Sekunden neu. Gemessen:
#: 297 identische Fehlschlaege, bis von Hand gestoppt wurde.
#:
#: Die Lehre ist nicht „diese eine Kante fehlte", sondern dass eine von Hand
#: gepflegte Tabelle sie wieder verlieren kann. Deshalb steht die Regel jetzt
#: als Regel: `_EXITS` wird JEDEM nicht-terminalen Zustand zugerechnet. Das ist
#: keine Lockerung — es ist die Bedingung dafuer, dass ein Lauf ueberhaupt
#: ehrlich enden kann. Die Endzustaende bleiben leer, und ein Selbstuebergang
#: entsteht dabei nicht (`- {state}`).
_EXITS = frozenset({FAILED, CANCELLED, INTERRUPTED})

_EDGES: dict[str, frozenset[str]] = {
    CREATED: frozenset({PLANNING}),
    PLANNING: frozenset({RUNNING, FAILED, CANCELLED, INTERRUPTED}),
    RUNNING: frozenset({WAITING_SPECIALIST, WAITING_CAPABILITY, WAITING_APPROVAL,
                        WAITING_USER, VERIFYING, SUCCEEDED, FAILED, CANCELLED,
                        INTERRUPTED}),
    WAITING_SPECIALIST: frozenset({RUNNING, FAILED, CANCELLED, INTERRUPTED}),
    WAITING_CAPABILITY: frozenset({RUNNING, FAILED, CANCELLED, INTERRUPTED}),
    WAITING_APPROVAL: frozenset({RUNNING, FAILED, CANCELLED, INTERRUPTED}),
    WAITING_USER: frozenset({RUNNING, FAILED, CANCELLED, INTERRUPTED}),
    VERIFYING: frozenset({RUNNING, SUCCEEDED, FAILED, CANCELLED, INTERRUPTED}),
    INTERRUPTED: frozenset({RUNNING, FAILED, CANCELLED}),
    SUCCEEDED: frozenset(),
    FAILED: frozenset(),
    CANCELLED: frozenset(),
}

#: Die geschlossene Tabelle: die Kanten oben, plus fuer jeden nicht-terminalen
#: Zustand garantiert der Weg hinaus.
TRANSITIONS: dict[str, frozenset[str]] = {
    state: targets if state in TERMINAL_STATES else (targets | _EXITS) - {state}
    for state, targets in _EDGES.items()
}

ALL_STATES = frozenset(TRANSITIONS)

#: Schrittarten (Architektur §6). Geschlossen.
STEP_KINDS = frozenset({
    "plan", "specialist", "capability", "verify", "user_boundary",
    "knowledge_proposal", "harvest", "summary",
})

#: Schrittzustaende.
STEP_STATES = frozenset({
    "pending", "running", "waiting", "succeeded", "failed", "denied",
    "skipped", "unknown",
})

#: Ereignisarten. Geschlossen — die Chronik liest genau diese.
EVENT_KINDS = frozenset({
    "state_changed", "step_started", "step_finished", "approval_requested",
    "approval_resolved", "boundary_opened", "boundary_resumed", "budget_event",
    "recovered", "notice_sent",
})

#: Fehlerkategorien. Geschlossen (Ledger-Schema).
FAILURE_CATEGORIES = frozenset({
    "plan_invalid", "specialist_unavailable", "specialist_failed", "quota",
    "capability_failed", "approval_denied", "approval_expired", "policy_denied",
    "recovery_required", "budget_exhausted", "loop_detected", "timeout",
    "interrupted", "workspace_conflict", "cancelled_by_user",
    # Ein Bau-Lauf, der kein Arbeitsergebnis hinterlaesst. Live gelernt: das
    # sah wie ein Erfolg aus und war eine leere Zusage.
    "no_result",
    # Der Planer hat einen strukturell ungueltigen Schritt vorgeschlagen —
    # eine Faehigkeit ohne ihre Pflichtangabe — und nach der Nachplanung
    # denselben noch einmal. Live gelernt: das lief vorher als
    # `budget_exhausted` und verschwieg damit, WER nicht weiterkam.
    "planner_invalid_step",
})

#: Zustaende einer wartenden Start-Anfrage. Geschlossen wie alles hier.
START_WAITING = "WAITING"
START_TAKEN = "TAKEN"
START_CLOSED = "CLOSED"
START_STATES = frozenset({START_WAITING, START_TAKEN, START_CLOSED})

#: Aufgabenzustaende.
TASK_ACTIVE = "active"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"
TASK_STATES = frozenset({TASK_ACTIVE, TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED})

#: Arten von Artefakten.
ARTIFACT_KINDS = frozenset({"report", "diff", "test_report", "proposal", "log_excerpt"})

#: Scopes einer Aufgabe. Strukturell getrennt schon bei der Erzeugung.
SCOPE_RESEARCH = "research"
SCOPE_BUILD = "build"
SCOPES = frozenset({SCOPE_RESEARCH, SCOPE_BUILD})

# -- Laengendeckel ------------------------------------------------------------
#
# Sie sind der Grund, warum kein Transkript hineinpasst. Ein Deckel, den man
# erhoehen muss, um ein Transkript abzulegen, ist eine sichtbare Entscheidung —
# genau das ist beabsichtigt.
MAX_OBJECTIVE = 4_000
MAX_RESULT_SUMMARY = 4_000
MAX_STEP_SUMMARY = 600
MAX_EVENT_SUMMARY = 300
MAX_REF = 200
MAX_BOUNDARY_JSON = 4_000

#: Aufbewahrung: wie bei Konversationen.
RETENTION_SECONDS = 90 * 24 * 3600

#: Ereigniszeilen je Lauf. Aelteste zuerst gepruent.
MAX_EVENTS_PER_RUN = 2_000


class LedgerError(RuntimeError):
    """Basis: etwas am Buch stimmt nicht."""


class LedgerTransitionError(LedgerError):
    """Eine Kante, die es in der Tabelle nicht gibt. Fail-closed."""

    def __init__(self, run_id: str, current: str, wanted: str) -> None:
        super().__init__(f"invalid_transition:{current}->{wanted}")
        self.run_id = run_id
        self.current = current
        self.wanted = wanted


class LedgerVocabularyError(LedgerError):
    """Ein Wort ausserhalb eines geschlossenen Vokabulars."""

    def __init__(self, field_name: str, value: str) -> None:
        super().__init__(f"unknown_{field_name}:{value}")
        self.field_name = field_name
        self.value = value


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id            TEXT PRIMARY KEY,
    objective          TEXT NOT NULL,
    scope              TEXT NOT NULL,
    target_repo        TEXT NOT NULL DEFAULT '',
    created_at         REAL NOT NULL,
    created_origin     TEXT NOT NULL,
    created_principal  TEXT NOT NULL,
    conversation_ref   TEXT NOT NULL DEFAULT '',
    predecessor_ref    TEXT NOT NULL DEFAULT '',
    state              TEXT NOT NULL,
    budget             TEXT NOT NULL,
    updated_at         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_runs (
    run_id             TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
    parent_run_id      TEXT NOT NULL DEFAULT '',
    attempt            INTEGER NOT NULL DEFAULT 1,
    state              TEXT NOT NULL,
    plan_revision      INTEGER NOT NULL DEFAULT 0,
    created_at         REAL NOT NULL,
    started_at         REAL,
    finished_at        REAL,
    outcome            TEXT NOT NULL DEFAULT '',
    failure_category   TEXT NOT NULL DEFAULT '',
    result_summary     TEXT NOT NULL DEFAULT '',
    boundary           TEXT NOT NULL DEFAULT '',
    workspace_path     TEXT NOT NULL DEFAULT '',
    branch_ref         TEXT NOT NULL DEFAULT '',
    tokens_planner     INTEGER NOT NULL DEFAULT 0,
    specialist_seconds REAL NOT NULL DEFAULT 0,
    specialist_count   INTEGER NOT NULL DEFAULT 0,
    updated_at         REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_open ON agent_runs(state) WHERE finished_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_runs_task ON agent_runs(task_id, created_at);

CREATE TABLE IF NOT EXISTS agent_steps (
    step_id            TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    seq                INTEGER NOT NULL,
    kind               TEXT NOT NULL,
    state              TEXT NOT NULL,
    attempt            INTEGER NOT NULL DEFAULT 1,
    specialist_profile TEXT NOT NULL DEFAULT '',
    specialist_role    TEXT NOT NULL DEFAULT '',
    capability         TEXT NOT NULL DEFAULT '',
    call_id            TEXT NOT NULL DEFAULT '',
    approval_id        TEXT NOT NULL DEFAULT '',
    execution_id       TEXT NOT NULL DEFAULT '',
    outcome_reason     TEXT NOT NULL DEFAULT '',
    child_pgid         INTEGER NOT NULL DEFAULT 0,
    child_started_at   REAL NOT NULL DEFAULT 0,
    child_executable   TEXT NOT NULL DEFAULT '',
    commit_ref         TEXT NOT NULL DEFAULT '',
    artifact_refs      TEXT NOT NULL DEFAULT '',
    summary            TEXT NOT NULL DEFAULT '',
    started_at         REAL,
    finished_at        REAL,
    UNIQUE(run_id, seq, attempt)
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON agent_steps(run_id, seq, attempt);

CREATE TABLE IF NOT EXISTS agent_events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       REAL NOT NULL,
    run_id   TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    step_id  TEXT NOT NULL DEFAULT '',
    kind     TEXT NOT NULL,
    summary  TEXT NOT NULL,
    ref      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_run ON agent_events(run_id, id);

CREATE TABLE IF NOT EXISTS agent_artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES agent_runs(run_id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    path        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON agent_artifacts(run_id, created_at);

-- Auftraege, die auf eine Freigabe warten, BEVOR es sie als Lauf gibt.
--
-- Live gefunden, und es war der schwerste Fund dieses Milestones: der Nutzer
-- gab per Face ID frei, und nichts geschah. Die Freigabe ging auf EXPIRED, die
-- Tabelle `execution_attempts` blieb bei null Eintraegen.
--
-- Der Grund ist eine Henne-Ei-Luecke. Ein Lauf, der auf eine Freigabe wartet,
-- hat einen Poller — `_poll_approval` im Takt. Der Auftrag, der den Lauf erst
-- ERZEUGT, hatte keinen: die Anfragekennung stand im Umschlag des Werkzeugs
-- und starb mit dem Gespraechszug.
--
-- Diese Zeile ueberlebt den Zug. Sie ist ausdruecklich KEINE Autoritaet: sie
-- haelt nur, was noetig ist, um denselben Aufruf unveraendert zu wiederholen —
-- und die Herkunft steht dabei, weil sie in den Freigabe-Digest eingeht und
-- niemals neu erfunden werden darf.
CREATE TABLE IF NOT EXISTS pending_starts (
    request_id  TEXT PRIMARY KEY,
    capability  TEXT NOT NULL,
    arguments   TEXT NOT NULL,
    principal   TEXT NOT NULL,
    origin      TEXT NOT NULL,
    commanded   INTEGER NOT NULL,
    state       TEXT NOT NULL,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_starts_state
    ON pending_starts(state, created_at);
"""

#: Spalten, die eine BESTEHENDE Ablage nachtraegt. Nur ADDITIV — es wird nie
#: eine Spalte entfernt und nie eine umgeschrieben. `CREATE TABLE IF NOT EXISTS`
#: ruehrt eine vorhandene Tabelle nicht an; ohne diese Wanderung kaeme eine neue
#: Spalte bei niemandem an, der die Datei schon hat.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "agent_runs": {},
    "agent_steps": {},
    "agent_tasks": {
        # Cognitive Router V1: worauf dieser Auftrag aufbaut. Ein VERWEIS auf
        # eine Aufgabe DERSELBEN Konversation, vom Core gesetzt — nie
        # Nutzertext und nie Modelltext.
        "predecessor_ref": "TEXT NOT NULL DEFAULT ''",
    },
    "pending_starts": {},
}


def _add_missing_columns(connection) -> None:
    """Traegt fehlende Spalten nach. Additiv, still, und ohne Datenverlust."""
    for table, columns in _ADDED_COLUMNS.items():
        if not columns:
            continue
        try:
            present = {row["name"] for row in
                       connection.execute(f"PRAGMA table_info({table})")}
        except sqlite3.DatabaseError:
            continue
        if not present:
            continue
        for name, declaration in columns.items():
            if name in present:
                continue
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            log.info("agent_runtime.column_added", table=table, column=name)


# =====================================================================
# Kennungen — vom Core gepraegt, nie vom Modell
# =====================================================================

def new_task_id() -> str:
    return "at-" + secrets.token_hex(8)


def new_run_id() -> str:
    return "ar-" + secrets.token_hex(8)


def new_step_id() -> str:
    return "as-" + secrets.token_hex(8)


def new_artifact_id() -> str:
    return "aa-" + secrets.token_hex(8)


# =====================================================================
# Die Firewall an jeder freitextigen Schreibstelle
# =====================================================================

def _refuse_credentials(*texts: str, where: str) -> None:
    """Verweigert, statt zu redigieren — der Zaun des Tresors am Buch.

    Ein Lauf traegt freien Text aus Spezialistenausgabe, Auftragstexten und
    Fehlermeldungen. Genau dort ist ein Token schon einmal in eine Datenbank
    gelangt, die niemand fuer geheimnisbehaftet hielt (Proactive-Store). Eine
    Ledger-Zeile ist eine Aussage: sie wird nicht halb geschrieben.
    """
    from solvio.secret_vault.firewall import any_credential, refuse_if_credential
    found = any_credential(*texts)
    if found:
        refuse_if_credential(found, where=where)


def _safe_text(text: str, limit: int, *, where: str) -> str:
    """Die eine Behandlung fuer jeden Freitext: verweigern, redigieren, deckeln.

    Reihenfolge ist Absicht. Die Verweigerung kommt ZUERST — sonst haette die
    Redaktion die Geheimnisgestalt entfernt und der Schreibvorgang waere still
    durchgelaufen, also genau das Bereinigen, das ADR-0029 ausschliesst. Die
    Redaktion des Starters laeuft danach als zweites Netz fuer Formen, die das
    eine Praedikat des Hauses nicht kennt.
    """
    value = str(text or "")
    _refuse_credentials(value, where=where)
    from solvio.specialists.launcher import redact
    return redact(value)[:limit]


def _require(vocabulary: frozenset[str], value: str, field_name: str) -> str:
    if value not in vocabulary:
        raise LedgerVocabularyError(field_name, str(value))
    return value


# =====================================================================
# Domaenentypen
# =====================================================================

@dataclass
class AgentTask:
    task_id: str
    objective: str
    scope: str
    target_repo: str = ""
    created_at: float = 0.0
    created_origin: str = ""
    created_principal: str = ""
    conversation_ref: str = ""
    predecessor_ref: str = ""
    state: str = TASK_ACTIVE
    budget: dict = field(default_factory=dict)
    updated_at: float = 0.0


@dataclass
class AgentRun:
    run_id: str
    task_id: str
    parent_run_id: str = ""
    attempt: int = 1
    state: str = CREATED
    plan_revision: int = 0
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    outcome: str = ""
    failure_category: str = ""
    result_summary: str = ""
    boundary: str = ""
    workspace_path: str = ""
    branch_ref: str = ""
    tokens_planner: int = 0
    specialist_seconds: float = 0.0
    specialist_count: int = 0
    updated_at: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def parked(self) -> bool:
        return self.state in PARKED_STATES


@dataclass
class AgentStep:
    step_id: str
    run_id: str
    seq: int
    kind: str
    state: str = "pending"
    attempt: int = 1
    specialist_profile: str = ""
    specialist_role: str = ""
    capability: str = ""
    call_id: str = ""
    approval_id: str = ""
    execution_id: str = ""
    outcome_reason: str = ""
    child_pgid: int = 0
    child_started_at: float = 0.0
    child_executable: str = ""
    commit_ref: list = field(default_factory=list)
    artifact_refs: list = field(default_factory=list)
    summary: str = ""
    started_at: float | None = None
    finished_at: float | None = None


@dataclass
class AgentEvent:
    id: int
    at: float
    run_id: str
    step_id: str
    kind: str
    summary: str
    ref: str = ""


@dataclass
class AgentArtifact:
    artifact_id: str
    run_id: str
    kind: str
    path: str
    sha256: str
    bytes: int
    created_at: float


def _column(row, name: str, default: str = "") -> str:
    """Eine Spalte, die es geben SOLLTE. Fehlt sie, gilt die Vorgabe.

    Die Wanderung traegt sie beim Oeffnen nach; dieser Riegel steht daneben,
    weil ein `sqlite3.Row` bei einem unbekannten Namen wirft und ein Buch, das
    beim Lesen wirft, schlimmer ist als ein leeres Feld.
    """
    try:
        return str(row[name] or default)
    except (IndexError, KeyError):
        return default


def _to_task(row) -> AgentTask:
    try:
        budget = json.loads(row["budget"] or "{}")
    except ValueError:
        budget = {}
    return AgentTask(
        task_id=row["task_id"], objective=row["objective"], scope=row["scope"],
        target_repo=row["target_repo"], created_at=row["created_at"],
        created_origin=row["created_origin"], created_principal=row["created_principal"],
        conversation_ref=row["conversation_ref"],
        predecessor_ref=_column(row, "predecessor_ref"), state=row["state"],
        budget=budget if isinstance(budget, dict) else {}, updated_at=row["updated_at"])


def _to_run(row) -> AgentRun:
    return AgentRun(
        run_id=row["run_id"], task_id=row["task_id"], parent_run_id=row["parent_run_id"],
        attempt=row["attempt"], state=row["state"], plan_revision=row["plan_revision"],
        created_at=row["created_at"], started_at=row["started_at"],
        finished_at=row["finished_at"], outcome=row["outcome"],
        failure_category=row["failure_category"], result_summary=row["result_summary"],
        boundary=row["boundary"], workspace_path=row["workspace_path"],
        branch_ref=row["branch_ref"], tokens_planner=row["tokens_planner"],
        specialist_seconds=row["specialist_seconds"],
        specialist_count=row["specialist_count"], updated_at=row["updated_at"])


def _json_list(raw: str) -> list:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _to_step(row) -> AgentStep:
    return AgentStep(
        step_id=row["step_id"], run_id=row["run_id"], seq=row["seq"], kind=row["kind"],
        state=row["state"], attempt=row["attempt"],
        specialist_profile=row["specialist_profile"], specialist_role=row["specialist_role"],
        capability=row["capability"], call_id=row["call_id"],
        approval_id=row["approval_id"], execution_id=row["execution_id"],
        outcome_reason=row["outcome_reason"], child_pgid=row["child_pgid"],
        child_started_at=row["child_started_at"], child_executable=row["child_executable"],
        commit_ref=_json_list(row["commit_ref"]), artifact_refs=_json_list(row["artifact_refs"]),
        summary=row["summary"], started_at=row["started_at"], finished_at=row["finished_at"])


def _to_event(row) -> AgentEvent:
    return AgentEvent(id=row["id"], at=row["at"], run_id=row["run_id"],
                      step_id=row["step_id"], kind=row["kind"],
                      summary=row["summary"], ref=row["ref"])


def _to_artifact(row) -> AgentArtifact:
    return AgentArtifact(artifact_id=row["artifact_id"], run_id=row["run_id"],
                         kind=row["kind"], path=row["path"], sha256=row["sha256"],
                         bytes=row["bytes"], created_at=row["created_at"])


# =====================================================================
# Der Speicher
# =====================================================================

class AgentRunLedger:
    """Verbindungen je Operation, WAL, enge Rechte, geschlossene Vokabulare."""

    def __init__(self, path: str = "") -> None:
        self.path = resolve_path(path)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(directory, 0o700)
        # Der WAL-Modus legt zwei Beidateien an, und SQLite legt sie mit der
        # umask des Prozesses an — nicht mit den Rechten der Datenbank. Ohne die
        # enge umask waeren `-wal` und `-shm` 0644, waehrend die Datenbank 0600
        # ist; im `-wal` stehen die zuletzt geschriebenen Buchzeilen.
        previous = os.umask(0o077)
        try:
            with self._open() as connection:
                connection.executescript(SCHEMA)
                _add_missing_columns(connection)
        finally:
            os.umask(previous)
        # Auch ein bestehender Satz bekommt die engen Rechte: ein frueher zu
        # grosszuegig angelegter repariert sich damit selbst.
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                os.chmod(self.path + suffix, 0o600)

    # -- Verbindungen --------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        # Beide gelten PRO VERBINDUNG, nicht pro Datenbank. Im Schema wirkte
        # `foreign_keys` genau einmal — auf der Verbindung, die das Schema
        # anlegte. Danach feuert keine Kaskade mehr.
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        # Es ist ein Journal: der Festschreibepunkt wird fsynct.
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextlib.contextmanager
    def _open(self):
        """Eine Verbindung, die auch wieder zugeht.

        `with sqlite3.connect(...) as c:` sieht aus wie ein Schliessen und ist
        keines — es ist ein TRANSAKTIONS-Kontext. Ohne das `close()` im
        `finally` laufen unter einem Sekundentakt die Verbindungen auf, bis
        SQLite mit `unable to open database file` aufgibt.
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def permissions_ok(self) -> bool:
        """Nicht reparieren, sondern melden: Rechte, die einmal offen standen,
        koennten bereits gelesen worden sein."""
        for suffix in ("", "-wal", "-shm"):
            try:
                mode = os.stat(self.path + suffix).st_mode & 0o777
            except OSError:
                continue
            if mode & 0o077:
                return False
        return True

    # -- Aufgaben ------------------------------------------------------

    def create_task(self, *, objective: str, scope: str, created_origin: str,
                    created_principal: str, target_repo: str = "",
                    conversation_ref: str = "", predecessor_ref: str = "",
                    budget: dict | None = None,
                    task_id: str = "") -> AgentTask:
        """Der Auftragstext steht GENAU EINMAL im Buch — hier."""
        _require(SCOPES, scope, "scope")
        now = time.time()
        task = AgentTask(
            task_id=task_id or new_task_id(),
            objective=_safe_text(objective, MAX_OBJECTIVE, where="agent_task.objective"),
            scope=scope, target_repo=str(target_repo or ""), created_at=now,
            created_origin=str(created_origin or ""),
            created_principal=str(created_principal or ""),
            conversation_ref=str(conversation_ref or ""),
            predecessor_ref=str(predecessor_ref or "")[:MAX_REF],
            state=TASK_ACTIVE,
            budget=dict(budget or {}), updated_at=now)
        with self._open() as connection:
            connection.execute(
                "INSERT INTO agent_tasks (task_id, objective, scope, target_repo,"
                " created_at, created_origin, created_principal, conversation_ref,"
                " predecessor_ref, state, budget, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (task.task_id, task.objective, task.scope, task.target_repo,
                 task.created_at, task.created_origin, task.created_principal,
                 task.conversation_ref, task.predecessor_ref, task.state,
                 json.dumps(task.budget, ensure_ascii=False), task.updated_at))
        return task

    def get_task(self, task_id: str) -> AgentTask | None:
        with self._open() as connection:
            row = connection.execute(
                "SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)).fetchone()
        return _to_task(row) if row else None

    def set_task_state(self, task_id: str, state: str) -> None:
        _require(TASK_STATES, state, "task_state")
        with self._open() as connection:
            connection.execute("UPDATE agent_tasks SET state=?, updated_at=? WHERE task_id=?",
                               (state, time.time(), task_id))

    # -- Laeufe --------------------------------------------------------

    def create_run(self, *, task_id: str, parent_run_id: str = "", attempt: int = 1,
                   run_id: str = "") -> AgentRun:
        now = time.time()
        run = AgentRun(run_id=run_id or new_run_id(), task_id=task_id,
                       parent_run_id=str(parent_run_id or ""), attempt=int(attempt),
                       state=CREATED, created_at=now, updated_at=now)
        with self._open() as connection:
            connection.execute(
                "INSERT INTO agent_runs (run_id, task_id, parent_run_id, attempt, state,"
                " plan_revision, created_at, outcome, failure_category, result_summary,"
                " boundary, workspace_path, branch_ref, tokens_planner,"
                " specialist_seconds, specialist_count, updated_at)"
                " VALUES (?,?,?,?,?,0,?,'','','','','','',0,0,0,?)",
                (run.run_id, run.task_id, run.parent_run_id, run.attempt, run.state,
                 run.created_at, run.updated_at))
        self.record_event(run.run_id, "state_changed", f"Lauf angelegt ({CREATED}).")
        return run

    def get_run(self, run_id: str) -> AgentRun | None:
        with self._open() as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
        return _to_run(row) if row else None

    def runs_for_task(self, task_id: str) -> list[AgentRun]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_runs WHERE task_id=? ORDER BY created_at",
                (task_id,)).fetchall()
        return [_to_run(row) for row in rows]

    def open_runs(self) -> list[AgentRun]:
        """Alle nicht-terminalen Laeufe — die Frage des Startabgleichs."""
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        with self._open() as connection:
            rows = connection.execute(
                f"SELECT * FROM agent_runs WHERE state NOT IN ({placeholders})"
                " ORDER BY created_at", tuple(sorted(TERMINAL_STATES))).fetchall()
        return [_to_run(row) for row in rows]

    def active_runs(self) -> list[AgentRun]:
        """Laeufe, die einen Laufzeit-Slot belegen: offen UND nicht parkend."""
        return [run for run in self.open_runs() if not run.parked]

    def recent_runs(self, limit: int = 50) -> list[AgentRun]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_runs ORDER BY created_at DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [_to_run(row) for row in rows]

    def transition(self, run_id: str, wanted: str, *, summary: str = "",
                   failure_category: str = "", result_summary: str = "",
                   outcome: str = "") -> AgentRun:
        """Die einzige Stelle, an der sich der Zustand eines Laufs aendert.

        Sie validiert den Ausgangszustand, schreibt einen Ereignissatz und
        setzt die Zeitstempel. Eine unerlaubte Kante wirft — auch dann, wenn
        der gewuenschte Zustand derselbe ist wie der aktuelle: ein
        Selbstuebergang steht in keiner Zeile der Tabelle.
        """
        _require(ALL_STATES, wanted, "state")
        if failure_category:
            _require(FAILURE_CATEGORIES, failure_category, "failure_category")
        now = time.time()
        with self._open() as connection:
            row = connection.execute(
                "SELECT * FROM agent_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise LedgerError(f"unknown_run:{run_id}")
            current = row["state"]
            if wanted not in TRANSITIONS.get(current, frozenset()):
                raise LedgerTransitionError(run_id, current, wanted)
            started_at = row["started_at"]
            if started_at is None and wanted in (PLANNING, RUNNING):
                started_at = now
            finished_at = now if wanted in TERMINAL_STATES else row["finished_at"]
            resolved_outcome = outcome or row["outcome"]
            if wanted in TERMINAL_STATES and not resolved_outcome:
                resolved_outcome = {SUCCEEDED: "succeeded", FAILED: "failed",
                                    CANCELLED: "cancelled"}[wanted]
            safe_result = (_safe_text(result_summary, MAX_RESULT_SUMMARY,
                                      where="agent_run.result_summary")
                           if result_summary else row["result_summary"])
            connection.execute(
                "UPDATE agent_runs SET state=?, started_at=?, finished_at=?, outcome=?,"
                " failure_category=?, result_summary=?, updated_at=? WHERE run_id=?",
                (wanted, started_at, finished_at, resolved_outcome,
                 failure_category or row["failure_category"], safe_result, now, run_id))
        note = summary or f"{current} → {wanted}"
        self.record_event(run_id, "state_changed", note)
        return self.get_run(run_id)  # type: ignore[return-value]

    # -- Auftraege, die auf ihre erste Freigabe warten ------------------

    def remember_pending_start(self, *, request_id: str, capability: str,
                               arguments: dict, principal: str, origin: str,
                               commanded: bool) -> None:
        """Was noetig ist, um denselben Aufruf spaeter unveraendert zu wiederholen.

        **Keine Autoritaet, sondern ein Merkzettel.** Die Zeile bewirkt nichts;
        sie erlaubt nur, eine Anfragekennung erneut vorzulegen. Ob daraus eine
        Ausfuehrung wird, entscheidet unveraendert der Freigabeweg — Digest,
        Geraetebeweis, Einmaligkeit.

        Die HERKUNFT steht ausdruecklich dabei und wird nie neu erfunden: sie
        geht in den Freigabe-Digest ein. Eine per Raumstimme freigegebene
        Anfrage darf nicht als etwas Vertrauteres wiederholt werden.
        """
        if not request_id or not capability:
            raise LedgerError("pending start needs a request_id and a capability")
        now = time.time()
        blob = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False)
        with self._open() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pending_starts"
                " (request_id, capability, arguments, principal, origin,"
                "  commanded, state, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (request_id, capability, blob, principal or "", origin or "",
                 1 if commanded else 0, START_WAITING, now, now))
        log.info("agent_runtime.start_parked", capability=capability)

    def waiting_starts(self) -> list[dict]:
        """Die Anfragen, die noch auf eine Entscheidung warten."""
        with self._open() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_starts WHERE state=? ORDER BY created_at",
                (START_WAITING,)).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            try:
                entry["arguments"] = json.loads(entry["arguments"])
            except (ValueError, TypeError):
                entry["arguments"] = {}
            entry["commanded"] = bool(entry["commanded"])
            out.append(entry)
        return out

    def close_pending_start(self, request_id: str, state: str) -> None:
        """Eine wartende Anfrage abschliessen — genommen oder erledigt."""
        if state not in START_STATES:
            raise LedgerError(f"unknown start state: {state}")
        with self._open() as conn:
            conn.execute(
                "UPDATE pending_starts SET state=?, updated_at=? WHERE request_id=?",
                (state, time.time(), request_id))

    def set_run_fields(self, run_id: str, **fields) -> None:
        """Die nicht-zustandsbehafteten Felder eines Laufs. Einzeln benannt —
        es gibt hier ausdruecklich kein `**kwargs` in die Datenbank hinein."""
        allowed = {
            "plan_revision": int, "workspace_path": str, "branch_ref": str,
            "tokens_planner": int, "specialist_seconds": float,
            "specialist_count": int, "parent_run_id": str,
        }
        sets, values = [], []
        for name, caster in allowed.items():
            if name in fields:
                sets.append(f"{name}=?")
                values.append(caster(fields[name]))
        if "boundary" in fields:
            payload = fields["boundary"]
            text = json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) \
                else str(payload or "")
            sets.append("boundary=?")
            values.append(_safe_text(text, MAX_BOUNDARY_JSON, where="agent_run.boundary"))
        unknown = set(fields) - set(allowed) - {"boundary"}
        if unknown:
            raise LedgerVocabularyError("run_field", ",".join(sorted(unknown)))
        if not sets:
            return
        sets.append("updated_at=?")
        values.extend([time.time(), run_id])
        with self._open() as connection:
            connection.execute(f"UPDATE agent_runs SET {', '.join(sets)} WHERE run_id=?",
                               tuple(values))

    # -- Schritte ------------------------------------------------------

    def create_step(self, *, run_id: str, seq: int, kind: str, attempt: int = 1,
                    specialist_profile: str = "", specialist_role: str = "",
                    capability: str = "", step_id: str = "") -> AgentStep:
        _require(STEP_KINDS, kind, "step_kind")
        step = AgentStep(step_id=step_id or new_step_id(), run_id=run_id, seq=int(seq),
                         kind=kind, state="pending", attempt=int(attempt),
                         specialist_profile=str(specialist_profile or ""),
                         specialist_role=str(specialist_role or ""),
                         capability=str(capability or ""))
        with self._open() as connection:
            connection.execute(
                "INSERT INTO agent_steps (step_id, run_id, seq, kind, state, attempt,"
                " specialist_profile, specialist_role, capability)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (step.step_id, step.run_id, step.seq, step.kind, step.state, step.attempt,
                 step.specialist_profile, step.specialist_role, step.capability))
        return step

    def get_step(self, step_id: str) -> AgentStep | None:
        with self._open() as connection:
            row = connection.execute(
                "SELECT * FROM agent_steps WHERE step_id=?", (step_id,)).fetchone()
        return _to_step(row) if row else None

    def steps_for_run(self, run_id: str) -> list[AgentStep]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_steps WHERE run_id=? ORDER BY seq, attempt",
                (run_id,)).fetchall()
        return [_to_step(row) for row in rows]

    def update_step(self, step_id: str, *, state: str = "", summary: str = "",
                    call_id: str = "", approval_id: str = "", execution_id: str = "",
                    outcome_reason: str = "", child_pgid: int | None = None,
                    child_started_at: float | None = None,
                    child_executable: str | None = None,
                    commit_ref: list | None = None,
                    artifact_refs: list | None = None,
                    started: bool = False, finished: bool = False) -> None:
        """Felder eines Schritts. Jedes einzeln benannt — es gibt keine Stelle,
        an der ein Adapter ein beliebiges Feld hineinreicht."""
        if state:
            _require(STEP_STATES, state, "step_state")
        sets, values = [], []
        if state:
            sets.append("state=?"); values.append(state)
        if summary:
            sets.append("summary=?")
            values.append(_safe_text(summary, MAX_STEP_SUMMARY, where="agent_step.summary"))
        for name, value in (("call_id", call_id), ("approval_id", approval_id),
                            ("execution_id", execution_id),
                            ("outcome_reason", outcome_reason)):
            if value:
                sets.append(f"{name}=?"); values.append(str(value)[:MAX_REF])
        if child_pgid is not None:
            sets.append("child_pgid=?"); values.append(int(child_pgid))
        if child_started_at is not None:
            sets.append("child_started_at=?"); values.append(float(child_started_at))
        if child_executable is not None:
            sets.append("child_executable=?"); values.append(str(child_executable)[:MAX_REF])
        if commit_ref is not None:
            sets.append("commit_ref=?")
            values.append(json.dumps([str(c)[:64] for c in commit_ref], ensure_ascii=False))
        if artifact_refs is not None:
            sets.append("artifact_refs=?")
            values.append(json.dumps([str(a)[:64] for a in artifact_refs], ensure_ascii=False))
        if started:
            sets.append("started_at=?"); values.append(time.time())
        if finished:
            sets.append("finished_at=?"); values.append(time.time())
        if not sets:
            return
        values.append(step_id)
        with self._open() as connection:
            connection.execute(f"UPDATE agent_steps SET {', '.join(sets)} WHERE step_id=?",
                               tuple(values))

    # -- Ereignisse ----------------------------------------------------

    def record_event(self, run_id: str, kind: str, summary: str, *,
                     step_id: str = "", ref: str = "") -> None:
        """Append-only. Genau EIN Verweis je Zeile, nie Material."""
        _require(EVENT_KINDS, kind, "event_kind")
        safe = _safe_text(summary, MAX_EVENT_SUMMARY, where="agent_event.summary")
        with self._open() as connection:
            connection.execute(
                "INSERT INTO agent_events (at, run_id, step_id, kind, summary, ref)"
                " VALUES (?,?,?,?,?,?)",
                (time.time(), run_id, str(step_id or ""), kind, safe, str(ref or "")[:MAX_REF]))
            self._cap_events(connection, run_id)

    def _cap_events(self, connection, run_id: str) -> None:
        """Aelteste zuerst. Ein Lauf, der Ereignisse produziert, darf das Buch
        nicht sprengen — und die Kappe steht als Zahl da, nicht als Hoffnung."""
        total = connection.execute(
            "SELECT COUNT(*) FROM agent_events WHERE run_id=?", (run_id,)).fetchone()[0]
        if total <= MAX_EVENTS_PER_RUN:
            return
        connection.execute(
            "DELETE FROM agent_events WHERE id IN ("
            " SELECT id FROM agent_events WHERE run_id=? ORDER BY id LIMIT ?)",
            (run_id, total - MAX_EVENTS_PER_RUN))

    def events_for_run(self, run_id: str, limit: int = 200) -> list[AgentEvent]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_events WHERE run_id=? ORDER BY id DESC LIMIT ?",
                (run_id, int(limit))).fetchall()
        return [_to_event(row) for row in reversed(rows)]

    def recent_events(self, limit: int = 100) -> list[AgentEvent]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_events ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [_to_event(row) for row in rows]

    # -- Artefakte -----------------------------------------------------

    def add_artifact(self, *, run_id: str, kind: str, path: str, sha256: str,
                     size: int, artifact_id: str = "") -> AgentArtifact:
        _require(ARTIFACT_KINDS, kind, "artifact_kind")
        artifact = AgentArtifact(artifact_id=artifact_id or new_artifact_id(),
                                 run_id=run_id, kind=kind, path=str(path),
                                 sha256=str(sha256), bytes=int(size),
                                 created_at=time.time())
        with self._open() as connection:
            connection.execute(
                "INSERT INTO agent_artifacts (artifact_id, run_id, kind, path, sha256,"
                " bytes, created_at) VALUES (?,?,?,?,?,?,?)",
                (artifact.artifact_id, artifact.run_id, artifact.kind, artifact.path,
                 artifact.sha256, artifact.bytes, artifact.created_at))
        return artifact

    def artifacts_for_run(self, run_id: str) -> list[AgentArtifact]:
        with self._open() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_artifacts WHERE run_id=? ORDER BY created_at",
                (run_id,)).fetchall()
        return [_to_artifact(row) for row in rows]

    # -- Aufbewahrung --------------------------------------------------

    def prune(self, *, now: float = 0.0) -> dict:
        """Terminal und abgelaufen wird beim Oeffnen gepruent.

        `agent_tasks` bleiben als Kopfzeilen stehen — die Frage „was habe ich
        dich damals gebeten?" ueberlebt laenger als das Material dazu. Die
        Kaskaden auf Schritte, Ereignisse und Artefakte haengen an
        `foreign_keys=ON` je Verbindung; ein Test loescht einen Lauf und
        verlangt, dass keine verwaiste Ereigniszeile uebrig bleibt.
        """
        current = now or time.time()
        cutoff = current - RETENTION_SECONDS
        placeholders = ",".join("?" for _ in TERMINAL_STATES)
        with self._open() as connection:
            rows = connection.execute(
                f"SELECT run_id, workspace_path FROM agent_runs"
                f" WHERE state IN ({placeholders}) AND finished_at IS NOT NULL"
                f" AND finished_at < ?", tuple(sorted(TERMINAL_STATES)) + (cutoff,)
            ).fetchall()
            run_ids = [row["run_id"] for row in rows]
            for run_id in run_ids:
                connection.execute("DELETE FROM agent_runs WHERE run_id=?", (run_id,))
        return {"runs_removed": len(run_ids), "run_ids": run_ids}

    def delete_run(self, run_id: str) -> None:
        """Nur fuer Aufraeumen und Tests. Die Kaskade ist die eigentliche Zusage."""
        with self._open() as connection:
            connection.execute("DELETE FROM agent_runs WHERE run_id=?", (run_id,))

    # -- Zaehlungen fuer Probe und Chronik ------------------------------

    def counts(self, *, stuck_after: float = 0.0, now: float = 0.0) -> dict:
        """Was die Probe wissen will — aus denselben Zeilen, ohne zweite Wahrheit."""
        current = now or time.time()
        open_runs = self.open_runs()
        stuck = []
        if stuck_after > 0:
            with self._open() as connection:
                for run in open_runs:
                    row = connection.execute(
                        "SELECT MAX(at) AS last FROM agent_events WHERE run_id=?",
                        (run.run_id,)).fetchone()
                    last = (row["last"] if row and row["last"] else run.created_at)
                    if current - last > stuck_after:
                        stuck.append(run.run_id)
        return {
            "open": len(open_runs),
            "active": len([r for r in open_runs if not r.parked]),
            "waiting_approval": len([r for r in open_runs if r.state == WAITING_APPROVAL]),
            "waiting_user": len([r for r in open_runs if r.state == WAITING_USER]),
            "interrupted": len([r for r in open_runs if r.state == INTERRUPTED]),
            "stuck": stuck,
        }
