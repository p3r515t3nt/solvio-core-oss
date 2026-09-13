"""Das Agent Run Ledger — es fuehrt Handlungen, keine Gedanken (ADR-0029).

Was hier geprueft wird, ist nicht „schreibt und liest korrekt", sondern die vier
Entscheidungen, die das Buch von einem Transkriptspeicher unterscheiden:

* die Zustandsmaschine ist eine geschlossene Tabelle, und eine verbotene Kante
  **wirft** — sie wird nicht bloss vermieden;
* jede freitextige Schreibstelle verweigert Geheimnisgestalt, statt sie zu
  bereinigen;
* es gibt **keine Spalte, in die ein Transkript passt** — geprueft per AST ueber
  das Schema, nicht per Zusage im Kommentar;
* Rechte, Kaskaden und Aufbewahrung sind Mechanik: 0700/0600 inklusive der
  WAL-Beidateien, `foreign_keys` je Verbindung, Deckel als Zahl.
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal, require_raises  # noqa: E402
enforce_assertions()

# Umgebungsumlenkung VOR jedem solvio-Import: ein Test schreibt nie in das
# produktive Buch, und `SOLVIO_STATE_DIR` haengt an mehr als nur dieser Datei.
_TMP = tempfile.mkdtemp(prefix="solvio-agent-ledger-")
os.environ["SOLVIO_STATE_DIR"] = _TMP
os.environ["SOLVIO_AGENT_RUNS_DB"] = os.path.join(_TMP, "agent_runs.sqlite3")

from solvio.agent_runtime import store as S  # noqa: E402
from solvio.secret_vault.firewall import CredentialRefused  # noqa: E402

#: Eine Zeichenkette in Schluesselgestalt. Kein echter Wert — die Gestalt genuegt,
#: und genau darum geht es: das Buch prueft die Gestalt, nicht die Gueltigkeit.
KEY_SHAPED = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

STORE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "src", "solvio", "agent_runtime", "store.py")


def _ledger(name: str = "") -> S.AgentRunLedger:
    folder = tempfile.mkdtemp(prefix="solvio-ledger-")
    return S.AgentRunLedger(os.path.join(folder, name or "agent_runs.sqlite3"))


def _task_and_run(ledger: S.AgentRunLedger, scope: str = S.SCOPE_RESEARCH):
    task = ledger.create_task(objective="Finde heraus, warum X klemmt", scope=scope,
                              created_origin="TRUSTED_INTERACTIVE_APP",
                              created_principal="local-owner")
    run = ledger.create_run(task_id=task.task_id)
    return task, run


# =====================================================================
# Schema, Rechte, Migration
# =====================================================================

def t_the_ledger_lives_where_the_house_puts_its_books():
    """Pfad-Override schlaegt Umgebung schlaegt Vorgabe — Reihenfolge des Brokers."""
    require(S.resolve_path("/tmp/explicit.sqlite3").endswith("/tmp/explicit.sqlite3"),
            "ein ausdruecklicher Pfad wird nicht ueberstimmt")
    require(S.resolve_path().startswith(_TMP), "die Umgebung lenkt um")
    require(os.path.isabs(S.resolve_path()), "der Pfad ist absolut")


def t_the_file_and_both_wal_companions_are_narrow():
    """Der WAL-Modus legt `-wal` und `-shm` mit der umask des PROZESSES an, nicht
    mit den Rechten der Datenbank. Im `-wal` stehen die zuletzt geschriebenen
    Buchzeilen — 0644 dort waere dasselbe Leck wie 0644 an der Datei."""
    ledger = _ledger()
    task, run = _task_and_run(ledger)
    ledger.record_event(run.run_id, "state_changed", "damit ein WAL entsteht")

    directory = os.path.dirname(ledger.path)
    require_equal(os.stat(directory).st_mode & 0o777, 0o700, "Verzeichnisrechte")
    seen = 0
    for suffix in ("", "-wal", "-shm"):
        path = ledger.path + suffix
        if not os.path.exists(path):
            continue
        seen += 1
        mode = os.stat(path).st_mode & 0o777
        require(not (mode & 0o077), f"{suffix or 'db'} steht auf {oct(mode)} offen")
    require(seen >= 1, "keine Datei gefunden — der Test prueft nichts")
    require(ledger.permissions_ok(), "permissions_ok widerspricht dem Befund")


def t_reopening_an_existing_book_is_additive_and_lossless():
    """`CREATE TABLE IF NOT EXISTS` ruehrt eine vorhandene Tabelle nicht an. Ein
    zweites Oeffnen darf deshalb weder scheitern noch Zeilen verlieren."""
    folder = tempfile.mkdtemp(prefix="solvio-ledger-")
    path = os.path.join(folder, "agent_runs.sqlite3")
    first = S.AgentRunLedger(path)
    task, run = _task_and_run(first)

    second = S.AgentRunLedger(path)
    require(second.get_run(run.run_id) is not None, "der Lauf ist nach dem Wiederoeffnen fort")
    require(second.get_task(task.task_id) is not None, "die Aufgabe ist fort")


def t_the_identifiers_are_core_minted_and_prefixed():
    """Kennungen entstehen im Core (`secrets.token_hex`), nie beim Modell."""
    ledger = _ledger()
    task, run = _task_and_run(ledger)
    step = ledger.create_step(run_id=run.run_id, seq=1, kind="plan")
    artifact = ledger.add_artifact(run_id=run.run_id, kind="report",
                                   path="/dev/null", sha256="0" * 64, size=0)
    for value, prefix in ((task.task_id, "at-"), (run.run_id, "ar-"),
                          (step.step_id, "as-"), (artifact.artifact_id, "aa-")):
        require(value.startswith(prefix), f"{value} traegt nicht {prefix}")
        require_equal(len(value), len(prefix) + 16, f"{value} hat nicht 16 Hexstellen")
    require(len({S.new_run_id() for _ in range(200)}) == 200, "Kennungen wiederholen sich")


# =====================================================================
# Die geschlossene Zustandsmaschine
# =====================================================================

def t_the_transition_table_is_closed_and_total():
    """Jeder Zustand steht in der Tabelle, und jedes Ziel ist ein bekannter Zustand."""
    for state, targets in S.TRANSITIONS.items():
        require(state in S.ALL_STATES, f"{state} ist kein bekannter Zustand")
        for target in targets:
            require(target in S.ALL_STATES, f"{state} zeigt auf unbekanntes {target}")
    for terminal in S.TERMINAL_STATES:
        require_equal(sorted(S.TRANSITIONS[terminal]), [],
                      f"{terminal} ist nicht endgueltig")


def t_every_unfinished_state_can_still_end():
    """Live gefunden, teuer: `CREATED` konnte nicht scheitern.

    Ein Bau-Lauf legt seine Arbeitskopie an, BEVOR er plant — also noch in
    `CREATED`. Als das misslang, wollte der Lauf `FAILED` werden, die Tabelle
    hatte diese Kante nicht, der Fehler wurde verschluckt, und der Takt
    versuchte es alle zwei Sekunden erneut. 297 Mal, bis von Hand gestoppt.

    Die Regel dagegen ist einfacher als jede Einzelkante: **wer laeuft, muss
    aufhoeren koennen.** Dieser Test prueft sie fuer jeden Zustand, auch fuer
    die, die es noch nicht gibt.
    """
    for state in sorted(S.ALL_STATES - S.TERMINAL_STATES):
        targets = S.TRANSITIONS[state]
        require(S.FAILED in targets, f"{state} kann nicht scheitern")
        require(S.CANCELLED in targets, f"{state} kann nicht abgebrochen werden")
        require(S.INTERRUPTED in targets or state == S.INTERRUPTED,
                f"{state} ueberlebt keinen Neustart")
        require(state not in targets, f"{state} zeigt auf sich selbst")


def t_a_run_that_never_planned_can_fail():
    """Die Kante von oben, einmal wirklich gegangen — nicht nur in der Tabelle."""
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    require_equal(ledger.get_run(run.run_id).state, S.CREATED, "startet nicht in CREATED")

    ledger.transition(run.run_id, S.FAILED, failure_category="plan_invalid",
                      result_summary="Die Arbeitskopie liess sich nicht anlegen.")
    ended = ledger.get_run(run.run_id)
    require_equal(ended.state, S.FAILED, "der Lauf endete nicht")
    require(ended.terminal, "der Lauf gilt weiter als offen — genau das drehte sich im Kreis")


def t_a_forbidden_edge_raises_instead_of_being_avoided():
    """`SUCCEEDED → RUNNING` ist kein Versehen, das ein Aufrufer meiden soll."""
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    ledger.transition(run.run_id, S.PLANNING)
    ledger.transition(run.run_id, S.RUNNING)
    ledger.transition(run.run_id, S.SUCCEEDED)

    error = require_raises(S.LedgerTransitionError,
                           ledger.transition, run.run_id, S.RUNNING,
                           message="ein terminaler Lauf lief wieder an")
    require_equal(error.current, S.SUCCEEDED, "der Ausgangszustand steht im Fehler")
    require_equal(error.wanted, S.RUNNING, "das Ziel steht im Fehler")
    require_equal(ledger.get_run(run.run_id).state, S.SUCCEEDED, "der Zustand kippte doch")


def t_a_self_transition_is_not_a_free_pass():
    """RUNNING → RUNNING steht in keiner Zeile der Tabelle. Wer es benutzt,
    verwischt genau die Ereignisspur, die das Buch fuehren soll."""
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    ledger.transition(run.run_id, S.PLANNING)
    ledger.transition(run.run_id, S.RUNNING)
    require_raises(S.LedgerTransitionError, ledger.transition, run.run_id, S.RUNNING,
                   message="ein Selbstuebergang lief durch")


def t_an_unknown_state_is_refused_before_it_reaches_the_database():
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    require_raises(S.LedgerVocabularyError, ledger.transition, run.run_id, "ALMOST_DONE",
                   message="ein erfundener Zustand wurde geschrieben")


def t_a_failure_category_outside_the_closed_list_is_refused():
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    ledger.transition(run.run_id, S.PLANNING)
    require_raises(S.LedgerVocabularyError, ledger.transition, run.run_id, S.FAILED,
                   failure_category="der_agent_hatte_keine_lust",
                   message="eine erfundene Fehlerkategorie wurde geschrieben")


def t_every_transition_writes_an_event():
    """Jeder Uebergang schreibt eine Zeile in das Ereignisjournal — das ist die
    Zusage, aus der die Chronik und der Neustart-Abgleich leben."""
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    before = len(ledger.events_for_run(run.run_id))
    ledger.transition(run.run_id, S.PLANNING)
    ledger.transition(run.run_id, S.RUNNING)
    after = ledger.events_for_run(run.run_id)
    require_equal(len(after) - before, 2, "nicht jeder Uebergang wurde gebucht")
    require(all(event.kind == "state_changed" for event in after[-2:]), "falsche Ereignisart")


def t_parked_runs_do_not_hold_a_runtime_slot():
    """Ein Lauf in WAITING_APPROVAL/WAITING_USER wartet auf einen Menschen. Wenn
    er einen Slot belegte, koennte ein einziger unbeantworteter Freigabedialog
    die ganze Laufzeit verstopfen (Architektur §5/§9)."""
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    ledger.transition(run.run_id, S.PLANNING)
    ledger.transition(run.run_id, S.RUNNING)
    require_equal(len(ledger.active_runs()), 1, "ein laufender Lauf belegt einen Slot")

    ledger.transition(run.run_id, S.WAITING_APPROVAL)
    require_equal(len(ledger.open_runs()), 1, "der Lauf ist weiterhin offen")
    require_equal(len(ledger.active_runs()), 0, "ein parkender Lauf belegt einen Slot")

    ledger.transition(run.run_id, S.RUNNING)
    ledger.transition(run.run_id, S.WAITING_USER)
    require_equal(len(ledger.active_runs()), 0, "WAITING_USER belegt einen Slot")


# =====================================================================
# Die Firewall an jeder Freitextstelle
# =====================================================================

def t_a_key_in_the_objective_refuses_the_write():
    ledger = _ledger()
    require_raises(CredentialRefused, ledger.create_task,
                   objective=f"Nutze bitte {KEY_SHAPED} fuer den Zugriff",
                   scope=S.SCOPE_RESEARCH, created_origin="ROOM_VOICE",
                   created_principal="local-owner",
                   message="ein Schluessel landete im Auftragstext")


def t_a_key_in_every_free_text_column_refuses_the_write():
    """Jede einzelne Freitextstelle, nicht nur die offensichtliche.

    Das ist der Test, der die Regel traegt: es genuegt nicht, dass EINE Spalte
    geschuetzt ist. Ein Buch mit einer ungeschuetzten Spalte ist ein Buch ohne
    Schutz — Material sucht sich die offene Stelle.
    """
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    step = ledger.create_step(run_id=run.run_id, seq=1, kind="specialist")
    ledger.transition(run.run_id, S.PLANNING)

    require_raises(CredentialRefused, ledger.record_event,
                   run.run_id, "step_finished", f"Der Spezialist meldete {KEY_SHAPED}",
                   message="ein Schluessel landete in einer Ereigniszeile")
    require_raises(CredentialRefused, ledger.update_step, step.step_id,
                   summary=f"gefunden: {KEY_SHAPED}",
                   message="ein Schluessel landete in einer Schrittzusammenfassung")
    require_raises(CredentialRefused, ledger.transition, run.run_id, S.RUNNING,
                   result_summary=f"Ergebnis: {KEY_SHAPED}",
                   message="ein Schluessel landete im Ergebnistext")
    require_raises(CredentialRefused, ledger.set_run_fields, run.run_id,
                   boundary={"was": f"melde dich mit {KEY_SHAPED} an"},
                   message="ein Schluessel landete in einer Nutzergrenze")


def t_the_refusal_never_names_the_value_that_caused_it():
    """Eine Ablehnung, die den Wert zitiert, ist die Ablage, die sie verhindern
    sollte — nur an einer Stelle, die niemand pruefte."""
    ledger = _ledger()
    try:
        ledger.create_task(objective=f"Schluessel {KEY_SHAPED}", scope=S.SCOPE_RESEARCH,
                           created_origin="ROOM_VOICE", created_principal="local-owner")
    except CredentialRefused as exc:
        text = f"{exc} {exc.reason} {exc.where} {exc.human_message}"
        require(KEY_SHAPED not in text, "die Ablehnung zitiert den Wert")
        require(KEY_SHAPED[:12] not in text, "die Ablehnung zitiert einen Anfang des Werts")
    else:
        require(False, "die Ablehnung blieb aus")


def t_a_refused_write_leaves_nothing_behind():
    """Verweigert heisst verweigert: keine halbe Zeile, kein Platzhalter."""
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    before = len(ledger.events_for_run(run.run_id))
    try:
        ledger.record_event(run.run_id, "step_finished", f"x {KEY_SHAPED}")
    except CredentialRefused:
        pass
    require_equal(len(ledger.events_for_run(run.run_id)), before,
                  "eine verweigerte Zeile wurde doch geschrieben")


def t_harmless_text_still_goes_through_the_redaction_net():
    """Zweites Netz: Formen, die das eine Praedikat des Hauses nicht kennt, faengt
    die Redaktion des Starters — sichtbar, statt still."""
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    ledger.update_step(
        ledger.create_step(run_id=run.run_id, seq=1, kind="specialist").step_id,
        summary="Das Werkzeug nannte refresh_token = abcdefghijklmnop im Fehlertext")
    step = ledger.steps_for_run(run.run_id)[0]
    require("abcdefghijklmnop" not in step.summary, "die Redaktion griff nicht")
    require("<entfernt>" in step.summary, "es wurde nicht redigiert, sondern verworfen")


# =====================================================================
# Es gibt keine Spalte, in die ein Transkript passt
# =====================================================================

def t_the_schema_has_no_column_that_could_hold_a_transcript():
    """Der eigentliche ADR-0029-Test — am Schema, nicht am Vorsatz.

    Geprueft werden die SPALTENNAMEN des Schemas gegen eine Liste von Woertern,
    die einen Gedankengang, ein Transkript oder einen Rohprompt bezeichnen.
    Eine Spalte `reasoning` oder `transcript` waere die eine Aenderung, mit der
    das Buch zum groessten privaten Datenbestand des Systems wuerde.
    """
    forbidden = ("reasoning", "thought", "thoughts", "chain_of_thought", "cot",
                 "scratchpad", "transcript", "messages", "conversation",
                 "prompt", "raw_output", "raw_response", "stdout", "stderr",
                 "token", "secret", "credential", "auth", "header", "bearer")
    columns: list[str] = []
    for line in S.SCHEMA.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("--", "PRAGMA", "CREATE", ")", "UNIQUE")):
            continue
        columns.append(stripped.split()[0].lower())
    require(len(columns) > 30, f"das Schema wurde nicht gelesen ({len(columns)} Spalten)")

    # Eine Spalte auf `_ref`/`_refs`/`_id` ist ausdruecklich erlaubt — sie ist
    # der VERWEIS, der das Kopieren ersetzt. `conversation_ref` haelt eine
    # Kennung, `conversation` hielte den Gespraechsverlauf; genau diese
    # Unterscheidung IST die Entscheidung von ADR-0029. Der Deckel darunter
    # bleibt: ein Verweis ist auf MAX_REF gekappt, ein Verlauf passt nicht hinein.
    def _is_reference(name: str) -> bool:
        return name.endswith(("_ref", "_refs", "_id"))

    offenders = [c for c in columns
                 if not _is_reference(c)
                 and any(word == c or word in c.split("_") for word in forbidden)]
    require_equal(offenders, [], f"das Schema traegt eine Transkriptspalte: {offenders}")

    # Und die Gegenprobe: die Ausnahme darf nicht alles durchlassen. Ein
    # `transcript_ref` waere ein Verweis auf ein Transkript — also ein Ort, an
    # dem eines entstuende.
    require(not any(c.startswith(("transcript", "prompt", "reasoning", "cot"))
                    for c in columns),
            "eine Spalte verweist auf Material, das es nicht geben darf")


def t_no_write_method_takes_open_ended_keyword_arguments():
    """`**kwargs` in eine Datenbank hinein ist die Stelle, an der spaeter ein
    Feld landet, das niemand benannt hat. Jede Schreibstelle nennt ihre Felder.

    `set_run_fields(**fields)` ist ausdruecklich erlaubt: es prueft seine Namen
    gegen eine Allowlist und wirft bei allem anderen — das ist der Test darunter.
    """
    tree = ast.parse(open(STORE_PATH, encoding="utf-8").read())
    ledger_class = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.ClassDef) and node.name == "AgentRunLedger")
    offenders = []
    for node in ledger_class.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in ("set_run_fields",):
            continue
        if node.args.kwarg is not None:
            offenders.append(node.name)
    require_equal(offenders, [], f"offene Schreibstellen: {offenders}")


def t_the_one_kwargs_method_refuses_every_name_it_does_not_know():
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    require_raises(S.LedgerVocabularyError, ledger.set_run_fields, run.run_id,
                   reasoning="ein langer Gedankengang",
                   message="ein unbekanntes Feld wurde angenommen")


def t_the_length_caps_are_real():
    """Ein Deckel, den niemand prueft, ist ein Kommentar."""
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    step = ledger.create_step(run_id=run.run_id, seq=1, kind="specialist")
    ledger.update_step(step.step_id, summary="a" * 5_000)
    require_equal(len(ledger.get_step(step.step_id).summary), S.MAX_STEP_SUMMARY,
                  "der Schrittdeckel greift nicht")
    ledger.record_event(run.run_id, "step_finished", "b" * 5_000)
    require_equal(len(ledger.events_for_run(run.run_id)[-1].summary), S.MAX_EVENT_SUMMARY,
                  "der Ereignisdeckel greift nicht")
    task = ledger.create_task(objective="c" * 20_000, scope=S.SCOPE_RESEARCH,
                              created_origin="ROOM_VOICE", created_principal="o")
    require_equal(len(ledger.get_task(task.task_id).objective), S.MAX_OBJECTIVE,
                  "der Auftragsdeckel greift nicht")


# =====================================================================
# Kaskaden, Deckel, Aufbewahrung
# =====================================================================

def t_deleting_a_run_leaves_no_orphan_row():
    """`foreign_keys` gilt PRO VERBINDUNG. Im Schema wirkte es genau einmal —
    auf der Verbindung, die das Schema anlegte. Danach feuerte nie eine Kaskade;
    aufgefallen ist das im Proactive-Store, als eine Vorab-Autorisierung das
    Loeschen ihrer Automatisierung ueberlebte."""
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    ledger.create_step(run_id=run.run_id, seq=1, kind="plan")
    ledger.record_event(run.run_id, "step_started", "los")
    ledger.add_artifact(run_id=run.run_id, kind="report", path="/dev/null",
                        sha256="0" * 64, size=0)

    ledger.delete_run(run.run_id)
    require_equal(ledger.steps_for_run(run.run_id), [], "ein Schritt ueberlebte")
    require_equal(ledger.events_for_run(run.run_id), [], "eine Ereigniszeile ueberlebte")
    require_equal(ledger.artifacts_for_run(run.run_id), [], "ein Artefakt ueberlebte")


def t_the_event_cap_prunes_oldest_first():
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    saved = S.MAX_EVENTS_PER_RUN
    S.MAX_EVENTS_PER_RUN = 10
    try:
        for number in range(30):
            ledger.record_event(run.run_id, "budget_event", f"Zeile {number}")
        events = ledger.events_for_run(run.run_id, limit=100)
        require_equal(len(events), 10, "der Deckel greift nicht")
        require("Zeile 29" in events[-1].summary, "die juengste Zeile fehlt")
        require(all("Zeile 0" != e.summary for e in events), "die aelteste blieb")
    finally:
        S.MAX_EVENTS_PER_RUN = saved


def t_retention_removes_only_terminal_expired_runs():
    """Aufbewahrung 90 Tage. Ein offener Lauf wird NIE gepruent — auch nicht,
    wenn er alt ist: er ist die Frage „was laeuft da noch?"."""
    ledger = _ledger()
    task, old_run = _task_and_run(ledger)
    ledger.transition(old_run.run_id, S.PLANNING)
    ledger.transition(old_run.run_id, S.RUNNING)
    ledger.transition(old_run.run_id, S.SUCCEEDED)

    open_run = ledger.create_run(task_id=task.task_id)
    ledger.transition(open_run.run_id, S.PLANNING)

    future = time.time() + S.RETENTION_SECONDS + 3600
    report = ledger.prune(now=future)
    require_equal(report["runs_removed"], 1, "es wurde nicht genau ein Lauf gepruent")
    require(ledger.get_run(old_run.run_id) is None, "der terminale Lauf blieb")
    require(ledger.get_run(open_run.run_id) is not None, "ein OFFENER Lauf wurde gepruent")
    require(ledger.get_task(task.task_id) is not None,
            "die Aufgabenkopfzeile wurde mit gepruent")


def t_pruning_is_idempotent():
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    ledger.transition(run.run_id, S.PLANNING)
    ledger.transition(run.run_id, S.RUNNING)
    ledger.transition(run.run_id, S.CANCELLED)
    future = time.time() + S.RETENTION_SECONDS + 3600
    first = ledger.prune(now=future)
    second = ledger.prune(now=future)
    require_equal(first["runs_removed"], 1, "der erste Lauf pruente nicht")
    require_equal(second["runs_removed"], 0, "der zweite Lauf pruente noch einmal")


# =====================================================================
# Geschlossene Vokabulare an den uebrigen Stellen
# =====================================================================

def t_unknown_step_kinds_events_and_artifacts_are_refused():
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    require_raises(S.LedgerVocabularyError, ledger.create_step,
                   run_id=run.run_id, seq=1, kind="freestyle",
                   message="eine erfundene Schrittart wurde angelegt")
    require_raises(S.LedgerVocabularyError, ledger.record_event,
                   run.run_id, "irgendwas", "x",
                   message="eine erfundene Ereignisart wurde gebucht")
    require_raises(S.LedgerVocabularyError, ledger.add_artifact,
                   run_id=run.run_id, kind="transcript", path="/dev/null",
                   sha256="0" * 64, size=0,
                   message="ein Transkript-Artefakt wurde angelegt")
    require_raises(S.LedgerVocabularyError, ledger.create_task,
                   objective="x", scope="deploy", created_origin="ROOM_VOICE",
                   created_principal="o",
                   message="ein erfundener Scope wurde angelegt")


def t_the_counts_answer_the_probes_question_from_the_same_rows():
    """Die Probe erfindet keine zweite Wahrheit — sie zaehlt diese Zeilen."""
    ledger = _ledger()
    task, first = _task_and_run(ledger)
    ledger.transition(first.run_id, S.PLANNING)
    ledger.transition(first.run_id, S.RUNNING)
    ledger.transition(first.run_id, S.WAITING_APPROVAL)

    second = ledger.create_run(task_id=task.task_id)
    ledger.transition(second.run_id, S.PLANNING)

    counts = ledger.counts()
    require_equal(counts["open"], 2, "offene Laeufe")
    require_equal(counts["active"], 1, "aktive Laeufe")
    require_equal(counts["waiting_approval"], 1, "wartende Freigaben")


def t_a_stuck_run_is_visible_as_stuck():
    """„Steckend" ist definiert: nicht-terminal und ohne Ereignis seit mehr als
    der doppelten Schrittfrist. Ein Lauf, den niemand als steckend sieht, ist
    genau der Lauf, der niemandem auffaellt."""
    ledger = _ledger()
    _task, run = _task_and_run(ledger)
    ledger.transition(run.run_id, S.PLANNING)
    require_equal(ledger.counts(stuck_after=3600)["stuck"], [], "frisch und schon steckend")
    later = time.time() + 7200
    require_equal(ledger.counts(stuck_after=3600, now=later)["stuck"], [run.run_id],
                  "ein stehengebliebener Lauf faellt nicht auf")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
