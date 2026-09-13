"""Sichtbarkeit: Probe, Chronik, Doctor-Playbook und die fuenf Nexus-Routen.

Die Fragen hier sind die des Betriebs, nicht die der Sicherheit — mit zwei
Ausnahmen, die beides sind:

* **Was hinausgeht, ist Betriebswahrheit.** Kein Gedankengang, kein Prompt,
  keine Rohausgabe, kein Geheimnis. Eine Runs-Seite, die den Rohtext eines
  Spezialisten zeigte, waere derselbe Ausgang wie ein Ledger-Feld.
* **Der Arzt genehmigt nichts und bricht nichts ab.** Sein einziges Vorgehen
  ist der Abgleich — oertlich, umkehrbar, idempotent. Ein Playbook, das einen
  aktiven Lauf beendete, waere eine Nutzerhandlung ohne Nutzer.

`unknown` ist nie gruen: wenn das Buch nicht lesbar ist, ist das eine Auskunft
und keine Beruhigung.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

_TMP = tempfile.mkdtemp(prefix="solvio-surface-")
os.environ["SOLVIO_STATE_DIR"] = _TMP
os.environ["SOLVIO_AGENT_RUNS_DB"] = os.path.join(_TMP, "agent_runs.sqlite3")

from solvio.agent_runtime import boundaries as B  # noqa: E402
from solvio.agent_runtime import endpoint as E  # noqa: E402
from solvio.agent_runtime import health as H  # noqa: E402
from solvio.agent_runtime import store as S  # noqa: E402
from solvio.control_center import activity as ACT  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


def _ledger() -> S.AgentRunLedger:
    folder = tempfile.mkdtemp(prefix="solvio-surface-db-")
    return S.AgentRunLedger(os.path.join(folder, "agent_runs.sqlite3"))


def _task_and_run(ledger, scope=S.SCOPE_RESEARCH):
    task = ledger.create_task(objective="Finde heraus warum X klemmt", scope=scope,
                              created_origin="local_owner", created_principal="o")
    return task, ledger.create_run(task_id=task.task_id)


# =====================================================================
# Die Probe
# =====================================================================

def t_an_empty_runtime_is_healthy_and_says_so():
    ledger = _ledger()
    word, reason = H.assess(ledger=ledger)
    require_equal(word, "healthy", f"leer aber {word}")
    require("keine offenen" in reason, f"unklarer Grund: {reason}")


def t_a_waiting_run_is_not_a_defect():
    """Ein Lauf, der auf einen Menschen wartet, ist eine offene Bitte — kein
    Fehler. Er faerbt nicht, aber er wird genannt."""
    ledger = _ledger()
    _t, run = _task_and_run(ledger)
    ledger.transition(run.run_id, S.PLANNING)
    ledger.transition(run.run_id, S.RUNNING)
    ledger.transition(run.run_id, S.WAITING_USER)
    word, reason = H.assess(ledger=ledger)
    require_equal(word, "healthy", "ein wartender Lauf faerbte die Probe")
    require("warten auf dich" in reason, f"die Bitte wird nicht genannt: {reason}")


def t_a_stuck_run_colours_the_probe():
    """„Nicht-terminal und seit ueber einer Stunde ohne Regung." Ein Lauf, den
    niemand als steckend sieht, ist genau der, der niemandem auffaellt."""
    ledger = _ledger()
    _t, run = _task_and_run(ledger)
    ledger.transition(run.run_id, S.PLANNING)
    later = time.time() + 2 * H.STUCK_AFTER
    word, reason = H.assess(now=later, ledger=ledger)
    require_equal(word, "degraded", f"ein steckender Lauf blieb {word}")
    require("haengen" in reason or "Regung" in reason, f"unklarer Grund: {reason}")


def t_an_unreadable_book_is_unknown_never_green():
    """Wenn das Buch nicht lesbar ist, ist das eine Auskunft — keine Beruhigung."""
    class Broken:
        def counts(self, **kw):
            raise RuntimeError("kaputt")
    word, reason = H.assess(ledger=Broken())
    require_equal(word, "unknown", f"ein kaputtes Buch meldete {word}")
    require(word != "healthy", "ein kaputtes Buch war gruen")


def t_a_disabled_runtime_is_unknown_not_healthy():
    saved = os.environ.get("SOLVIO_AGENT_RUNTIME")
    os.environ["SOLVIO_AGENT_RUNTIME"] = "off"
    try:
        word, reason = H.assess(ledger=_ledger())
        require_equal(word, "unknown", "abgeschaltet meldete sich als gesund")
        require("abgeschaltet" in reason, f"unklarer Grund: {reason}")
    finally:
        if saved is None:
            os.environ.pop("SOLVIO_AGENT_RUNTIME", None)
        else:
            os.environ["SOLVIO_AGENT_RUNTIME"] = saved


def t_the_probe_is_registered_with_a_human_label():
    from solvio.control_center.probes import build
    probes = build(dispatcher=None, settings=None, approver=None)
    found = [p for p in probes if p.key == "agent_runtime"]
    require(found, "die Probe ist nicht registriert")
    require_equal(found[0].label, "Auftraege", f"falsches Label: {found[0].label}")


def t_the_component_has_a_name_in_every_label_dictionary():
    """Zwei Namen fuer dasselbe Ding waeren eine zweite Wahrheit im Kleinen."""
    from solvio.capabilities.doctor import _ALIASES, _LABELS as CL
    from solvio.control_center.activity import _COMPONENTS
    from solvio.doctor.supervisor import _LABELS as SL
    for name, mapping in (("Kontrollzentrum", _COMPONENTS), ("Ueberwachung", SL),
                          ("Arzt-Faehigkeit", CL)):
        require("agent_runtime" in mapping, f"{name} kennt die Komponente nicht")
    require_equal(len({_COMPONENTS["agent_runtime"], SL["agent_runtime"],
                       CL["agent_runtime"]}), 1,
                  "die drei Woerterbuecher nennen sie verschieden")
    require_equal(_ALIASES.get("auftraege"), "agent_runtime",
                  "der gesprochene Name fuehrt nicht zur Komponente")


# =====================================================================
# Die Chronik
# =====================================================================

def t_the_chronicle_reader_is_a_projection_and_writes_nothing():
    ledger = _ledger()
    _t, run = _task_and_run(ledger)
    ledger.transition(run.run_id, S.PLANNING)
    before = len(ledger.events_for_run(run.run_id))

    events = ACT._from_agent_runs(ledger, since=0.0)
    require(events, "die Chronik sah nichts")
    require_equal(len(ledger.events_for_run(run.run_id)), before,
                  "der Leser hat geschrieben")
    require(all(e.kind == "auftrag" for e in events), "falsche Ereignisart")
    require(all(e.task_id.startswith("ar-") for e in events), "kein Laufbezug")


def t_the_chronicle_shows_less_than_its_source_never_more():
    """Eine Projektion erfindet keine Zeile, die im Buch nicht steht."""
    ledger = _ledger()
    _t, run = _task_and_run(ledger)
    ledger.transition(run.run_id, S.PLANNING)
    in_book = len(ledger.recent_events(limit=200))
    in_chronicle = len(ACT._from_agent_runs(ledger, since=0.0))
    require(in_chronicle <= in_book,
            f"die Chronik zeigt mehr als das Buch: {in_chronicle} > {in_book}")


def t_an_unreadable_ledger_does_not_break_the_chronicle():
    class Broken:
        def recent_events(self, **kw):
            raise RuntimeError("kaputt")
    require_equal(ACT._from_agent_runs(Broken(), since=0.0), [],
                  "ein kaputtes Buch riss die Chronik mit")


# =====================================================================
# Der Arzt
# =====================================================================

def t_the_doctor_has_exactly_one_playbook_and_it_only_reconciles():
    from solvio.doctor.playbooks import for_component
    books = for_component("agent_runtime")
    require_equal([b.key for b in books], ["agent_reconcile"],
                  f"mehr als ein Vorgehen: {[b.key for b in books]}")


def t_the_playbook_never_cancels_an_active_run():
    """Abbruch ist eine Nutzerhandlung. Ein Playbook, das einen laufenden
    Auftrag beendete, waere eine Nutzerhandlung ohne Nutzer — die Mutation,
    die `cancel` hineinschreibt, muss hier scheitern."""
    import ast
    path = os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                        "doctor", "playbooks.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_agent_reconcile")
    called = {n.func.attr for n in ast.walk(func)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for forbidden in ("cancel", "approve", "execute", "resume", "transition"):
        require(forbidden not in called,
                f"das Vorgehen des Arztes ruft {forbidden}")
    require("reconcile" in called, "das Vorgehen gleicht gar nicht ab")


def t_the_playbook_is_a_retry_not_a_restart():
    """Der Abgleich startet keinen Prozess neu — er liest und raeumt auf."""
    from solvio.doctor.diagnosis import RepairClass
    from solvio.doctor.playbooks import get
    book = get("agent_reconcile")
    require(book is not None, "das Vorgehen fehlt")
    require_equal(book.repair, RepairClass.RETRY, f"falsche Klasse: {book.repair}")


# =====================================================================
# Die fuenf Routen
# =====================================================================

class _FakeOrchestrator:
    def __init__(self, ledger) -> None:
        self.ledger = ledger
        self.cancelled: list[str] = []
        self.resumed: list[str] = []

    async def cancel(self, run_id):
        self.cancelled.append(run_id)
        return True

    async def resume(self, run_id):
        self.resumed.append(run_id)
        return True


def _app(ledger):
    from aiohttp import web
    app = web.Application()
    return E.attach(app, _FakeOrchestrator(ledger))


def t_every_route_is_path_exact_and_there_are_exactly_five():
    """Eine Route, die `/{aktion}` entgegennaehme, waere eine Stelle, an der ein
    Tippfehler zu einer anderen Handlung wird."""
    app = _app(_ledger())
    # aiohttp haengt an jedes GET automatisch ein HEAD. Das ist die Bibliothek,
    # keine zusaetzliche Flaeche — gezaehlt werden die Routen, die SOLVIO
    # ausdruecklich anlegt.
    paths = sorted((r.resource.canonical, r.method) for r in app.router.routes()
                   if r.method != "HEAD")
    require_equal(len(paths), 5, f"nicht fuenf Routen: {paths}")
    expected = sorted([
        ("/v1/agent/runs", "GET"), ("/v1/agent/runs/{run_id}", "GET"),
        ("/v1/agent/running", "GET"),
        ("/v1/agent/runs/{run_id}/cancel", "POST"),
        ("/v1/agent/runs/{run_id}/resume", "POST")])
    require_equal(paths, expected, f"andere Routen: {paths}")


def t_the_run_view_shows_operational_truth_and_no_raw_material():
    ledger = _ledger()
    _t, run = _task_and_run(ledger)
    view = E._run_view(ledger.get_run(run.run_id))
    forbidden = ("prompt", "transcript", "raw", "reasoning", "stdout", "stderr",
                 "token", "auth", "secret", "cot")
    leaked = [k for k in view if any(word in k.lower() for word in forbidden)]
    require_equal(leaked, [], f"die Sicht traegt Rohfelder: {leaked}")
    for key in ("id", "zustand", "ergebnis", "wartet_auf"):
        require(key in view, f"das Feld {key} fehlt der Sicht")


def t_the_states_are_rendered_in_german_not_as_identifiers():
    """Ein Mensch liest „wartet auf deine Freigabe", nicht `WAITING_APPROVAL`."""
    ledger = _ledger()
    _t, run = _task_and_run(ledger)
    ledger.transition(run.run_id, S.PLANNING)
    ledger.transition(run.run_id, S.RUNNING)
    ledger.transition(run.run_id, S.WAITING_APPROVAL)
    view = E._run_view(ledger.get_run(run.run_id))
    require_equal(view["zustand"], "wartet auf deine Freigabe",
                  f"nicht uebersetzt: {view['zustand']}")
    require_equal(view["zustand_code"], "WAITING_APPROVAL",
                  "der Maschinencode fehlt daneben")


def t_every_state_and_failure_category_has_a_german_word():
    """Ein Zustand ohne Wort rendert als Bezeichner — und genau der faellt
    niemandem auf, weil er selten vorkommt."""
    missing = [state for state in S.ALL_STATES if state not in E._WORDS]
    require_equal(missing, [], f"Zustaende ohne Wort: {missing}")
    missing = [c for c in S.FAILURE_CATEGORIES if c not in E._REASONS]
    require_equal(missing, [], f"Fehlerkategorien ohne Wort: {missing}")


def t_an_open_boundary_is_visible_in_the_view():
    ledger = _ledger()
    _t, run = _task_and_run(ledger)
    ledger.transition(run.run_id, S.PLANNING)
    ledger.transition(run.run_id, S.RUNNING)
    boundary = B.policy_refusal("purchase_place", "as-1")
    ledger.set_run_fields(run.run_id, boundary=boundary.as_dict())
    ledger.transition(run.run_id, S.WAITING_USER)
    view = E._run_view(ledger.get_run(run.run_id))
    require(view["wartet_auf"], "die offene Grenze ist unsichtbar")
    require("iPhone" in view["wartet_auf"]["handlung"], "die Handlung fehlt")


def t_the_routes_exist_even_before_the_runtime_does():
    """Live gefunden: der Freigabe-Gateway startet rund VIER SEKUNDEN vor dem
    Orchestrator. Ein Attach, der das Objekt einmal einsammelt, bekam immer
    `None` und haengte gar keine Route an — `/v1/agent/runs` antwortete mit 404
    statt 401, und die ganze Nexus-Sicht war tot.

    Die Routen existieren deshalb IMMER. Ob dahinter etwas laeuft, entscheidet
    sich beim Zugriff: 503, nicht 404.
    """
    from aiohttp import web
    app = E.attach(web.Application())          # ohne Laufzeit
    paths = sorted({r.resource.canonical for r in app.router.routes()})
    require_equal(len(paths), 5, f"ohne Laufzeit fehlen Routen: {paths}")
    require(all(p.startswith("/v1/agent") for p in paths), f"falsche Pfade: {paths}")


def t_the_runtime_is_read_at_request_time_not_at_attach_time():
    from aiohttp import web
    holder = {"orch": None}
    app = E.attach(web.Application(), provider=lambda: holder["orch"])

    class FakeRequest:
        def __init__(self, application): self.app = application

    require(E._orchestrator(FakeRequest(app)) is None,
            "ohne Laufzeit wurde eine geliefert")
    holder["orch"] = _FakeOrchestrator(_ledger())      # kommt SPAETER
    require(E._orchestrator(FakeRequest(app)) is not None,
            "die spaeter gestartete Laufzeit wird nicht gesehen")


def t_the_approver_attaches_the_routes_unconditionally():
    """Die Mutation, die den Attach wieder an ein vorhandenes Objekt bindet,
    muss gefangen werden."""
    import ast
    path = os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                        "capabilities", "approver_runtime.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "_attach_agent_endpoint")
    # Kein fruehes `return` mehr, das den Attach ueberspringt.
    early = [n for n in func.body if isinstance(n, ast.If)
             and any(isinstance(x, ast.Return) for x in ast.walk(n))]
    require_equal(early, [], "der Attach wird wieder uebersprungen")
    body = ast.dump(func)
    require("provider" in body, "der Attach reicht keinen Provider durch")


def t_the_endpoint_uses_the_same_device_check_as_the_approval_path():
    """Bewusst der Aufruf der bestehenden Funktion und keine Kopie: eine zweite
    Fassung derselben Pruefung waere die Stelle, an der spaeter eine der beiden
    nachgeschaerft wird und die andere nicht."""
    import ast
    path = os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                        "agent_runtime", "endpoint.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_owner_device")
    imported = [n.module for n in ast.walk(func) if isinstance(n, ast.ImportFrom)]
    require("solvio.security.mobile_approval.gateway" in imported,
            f"der Endpunkt prueft selbst statt zu fragen: {imported}")


def t_a_run_identifier_is_a_reference_not_an_authorisation():
    """Jede Route prueft die Transportkennung zuerst. Eine `ar-`-Kennung allein
    oeffnet nichts."""
    import ast
    path = os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                        "agent_runtime", "endpoint.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    attach = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "attach")
    handlers = [n for n in attach.body
                if isinstance(n, ast.AsyncFunctionDef) and n.name != "guard"]
    require(len(handlers) == 5, f"nicht fuenf Handler: {[h.name for h in handlers]}")
    for handler in handlers:
        body = ast.dump(handler)
        require("guard" in body, f"{handler.name} prueft die Kennung nicht")
        require("401" in body, f"{handler.name} antwortet nicht mit 401")


def t_the_planner_is_wired_to_the_broker_that_actually_exists():
    """Live gefunden, nicht im Test: der Broker haengt am SERVER, nicht am
    Dispatcher.

    Die erste Verdrahtung las `dispatcher.broker` — das ist immer `None`, und
    der Planer war damit stumm: kein Lease, keine Zuordnung im Broker-Buch, und
    jeder Lauf endete ehrlich, aber nutzlos an `plan_invalid`. Ein Fehler, den
    keine Suite sehen konnte, weil er in der Verdrahtung lag und nicht im Modul.
    """
    import ast
    path = os.path.join(os.path.dirname(__file__), "..", "src", "solvio",
                        "realtime", "core_server.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "Planner"]
    require(calls, "der Planer wird gar nicht mehr gebaut")
    for call in calls:
        broker = [k.value for k in call.keywords if k.arg == "broker"]
        require(broker, "der Planer bekommt keinen Broker")
        source = ast.dump(broker[0])
        require("dispatcher" not in source,
                "der Planer liest den Broker wieder vom Dispatcher — dort ist keiner")
        require("broker" in source, "die Broker-Referenz fehlt")


def t_the_planner_defaults_to_the_broker_transport():
    """Ohne Transport war jeder Planungsaufruf `planner_transport_missing`."""
    from solvio.agent_runtime import planner as PL
    require(PL.Planner()._transport is PL.broker_transport,
            "der Planer hat keinen Standard-Transport")
    require(PL.Planner(transport=lambda *a, **k: None)._transport
            is not PL.broker_transport,
            "ein eigener Transport wird ignoriert")


def t_the_planner_never_reuses_a_rotated_broker_token():
    """Live gefunden: der Broker rotiert den Token bei Lease-Null.

    Der erste Planaufruf ging durch, die Nachplanung bekam `401` — weil der
    Planer den Token gemerkt hatte. Das ist kein Broker-Fehler, sondern seine
    Zusage („kein Zugang ueber den Auftrag hinaus"). Der Planer holt sich
    deshalb je Aufruf einen frischen.
    """
    from solvio.agent_runtime import planner as PL

    class RotatingBroker:
        def __init__(self):
            self.generation = 0
        def register_principal(self, name):
            self.generation += 1
            return f"sk-solvio-broker-gen{self.generation}"

    broker = RotatingBroker()
    planner = PL.Planner(broker=broker)
    first = planner.ensure_principal()
    second = planner.ensure_principal()
    require(first != second,
            f"der Planer benutzt einen rotierten Token weiter: {first}")
    require_equal(broker.generation, 2, "es wurde nicht je Aufruf gepraegt")


# =====================================================================
# Der Nutzer spricht mit SOLVIO, nicht mit dessen Fachleuten
#
# Produktkorrektur, ausdruecklich gefordert: es darf KEIN Stichwort noetig
# sein, um einen Auftrag auszuloesen. „Kannst du mal schauen, warum das nicht
# geht?" muss dieselbe Behandlung bekommen wie „Recherchier das bitte" — die
# semantische Aufgabe ist dieselbe.
#
# Was diese Tests koennen und was nicht: die Auswahl trifft das Sprachmodell,
# und ob es sie richtig trifft, zeigt erst die Live-Abnahme. Pruefbar ist aber
# das, was VOR dem Modell liegt und deterministisch ist — dass keine Stelle im
# Code eine Aeusserung nach ihrem Wortlaut anders behandelt, und dass die
# Anweisung an das Modell keine Ausloeseliste ist.
# =====================================================================

#: Zehn Aeusserungen, fuenf Absichten, jede zweimal verschieden gesagt. Keine
#: davon enthaelt ein Routing-Kommando.
NATUERLICHE_AEUSSERUNGEN = (
    "Kannst du mal schauen, warum das nicht geht?",
    "Findest du raus, woran das liegt?",
    "Ich verstehe nicht, warum das passiert.",
    "Pruef das mal gruendlich.",
    "Warum funktioniert mein Nuki nicht mehr?",
    "Ich moechte naechstes Wochenende mit der Familie irgendwo hin.",
    "Mach mir daraus eine funktionierende Loesung.",
    "Schau mal, warum dieser Fehler auftritt und behebe ihn.",
    "Ich brauche fuer das Projekt eine bessere Loesung.",
    "Finde heraus, welcher Flug fuer uns am besten ist.",
)


def t_no_utterance_is_routed_by_the_words_it_happens_to_contain():
    """Kein Codepfad darf eine Aeusserung nach ihrem Wortlaut sortieren.

    Der Weg zum Auftrag laeuft ueber Werkzeugauswahl des Modells, nicht ueber
    Mustervergleich im Code. Dieser Test sucht die Gegenprobe: gibt es
    irgendwo eine Stichwortliste, die ueber Agentenlaufzeit entscheidet?
    """
    import ast

    verdaechtig = ("agent_task", "agent_run", "researcher", "investigator",
                   "builder", "deep_research", "solvio_task", "cognition")
    ausloeser = ("recherche", "researcher", "codex", "diagnostician",
                 "bauauftrag", "deep research", "agent",
                 # Cognitive Router V1: die acht Wege sind Namen, keine
                 # Ausloeser. `if "kurzrecherche" in text:` waere Keyword-
                 # Routing mit neuem Vokabular.
                 "kurzrecherche", "nachdenken", "klaerung", "fachbot",
                 "diagnose", "auftrag", "kein_auftrag", "auftrag_recherche",
                 "auftrag_bau", "solvio_task")
    treffer: list[str] = []

    wurzel = os.path.join(os.path.dirname(__file__), "..", "src", "solvio")
    for ordner, _dirs, dateien in os.walk(wurzel):
        if "__pycache__" in ordner:
            continue
        for name in dateien:
            if not name.endswith(".py"):
                continue
            pfad = os.path.join(ordner, name)
            with open(pfad, encoding="utf-8") as handle:
                quelle = handle.read()
            if not any(v in quelle for v in verdaechtig):
                continue
            baum = ast.parse(quelle, filename=pfad)
            for knoten in ast.walk(baum):
                # `"recherche" in text` — ein Wortvergleich, der etwas entscheidet.
                if isinstance(knoten, ast.Compare) and any(
                        isinstance(op, ast.In) for op in knoten.ops):
                    links = knoten.left
                    if isinstance(links, ast.Constant) and isinstance(links.value, str):
                        if links.value.lower() in ausloeser:
                            treffer.append(f"{name}:{knoten.lineno}: {links.value!r}")
    require_equal(treffer, [],
                  f"Ausloesewoerter entscheiden ueber Agentenlaufzeit: {treffer}")


def t_the_instruction_names_the_kind_of_goal_not_a_list_of_phrases():
    """Die Anweisung an das Modell darf keine Ausloeseliste sein.

    Eine Liste von Formulierungen waere Keyword-Routing mit anderen Mitteln:
    sie trifft genau die Saetze, die dort stehen, und keinen anderen.
    """
    from solvio.realtime.core_server import TOOL_INSTRUCTIONS

    require("agent_task_research" in TOOL_INSTRUCTIONS,
            "die Anweisung erwaehnt die Agentenlaufzeit nicht — dann waehlt das "
            "Modell sie nur, wenn jemand sehr deutlich danach fragt")
    require("agent_task_build" in TOOL_INSTRUCTIONS, "der Bau-Weg fehlt")
    require("von SELBST" in TOOL_INSTRUCTIONS,
            "es steht nicht da, dass SOLVIO selbst entscheiden soll")
    require("kein bestimmtes Wort" in TOOL_INSTRUCTIONS,
            "es steht nicht da, dass kein Stichwort noetig ist")
    # Und die Gegenrichtung: keine unnoetigen Auftraege.
    require("nicht fuer" in TOOL_INSTRUCTIONS and "einfache Frage" in TOOL_INSTRUCTIONS,
            "die Abgrenzung nach unten fehlt — dann wird jede Frage ein Auftrag")


def t_every_natural_utterance_reaches_the_model_unchanged():
    """Keine der zehn Aeusserungen wird unterwegs abgefangen oder umgeschrieben.

    Gepruefte Stellen: die Stoppwort-Erkennung, die schon einmal harmlose Saetze
    verschluckt hat (das Wort „aus" in „licht aus"), und jede andere Funktion
    im Sprachweg, die eine Aeusserung als Ganzes bewertet.
    """
    from solvio.realtime import core_server as CS

    stopp = getattr(CS, "_is_stop_utterance", None)
    if stopp is not None:
        gefangen = [a for a in NATUERLICHE_AEUSSERUNGEN if stopp(a)]
        require_equal(gefangen, [], f"als Stoppwort gelesen: {gefangen}")


def t_the_same_intent_said_differently_is_not_treated_differently():
    """Fuenf Absichten, je zweimal gesagt — die Paare duerfen sich nicht trennen.

    Das ist der Kern der Anforderung: „Findest du raus, woran das liegt?" und
    „Pruef das mal gruendlich." meinen dasselbe. Wenn eine Stelle im Code sie
    unterschiedlich behandelt, liegt es an ihren Woertern — und genau das soll
    es nicht geben.
    """
    from solvio.realtime import core_server as CS

    paare = (
        ("Kannst du mal schauen, warum das nicht geht?", "Findest du raus, woran das liegt?"),
        ("Ich verstehe nicht, warum das passiert.", "Pruef das mal gruendlich."),
        ("Recherchier das bitte.", "Findest du das fuer mich heraus?"),
        ("Mach mir daraus eine funktionierende Loesung.",
         "Schau mal, warum dieser Fehler auftritt und behebe ihn."),
        ("Finde heraus, welcher Flug fuer uns am besten ist.",
         "Ich moechte naechstes Wochenende mit der Familie irgendwo hin."),
    )
    stopp = getattr(CS, "_is_stop_utterance", None)
    for eins, zwei in paare:
        if stopp is not None:
            require_equal(stopp(eins), stopp(zwei),
                          f"unterschiedlich behandelt: {eins!r} vs {zwei!r}")


def t_the_repository_field_asks_for_a_path_not_for_a_name():
    """Live gefunden: das Feld hiess „Optional: welches Projekt", und das Modell
    trug das blosse Wort „Projekt" aus dem gesprochenen Satz ein. Daran zerbrach
    jeder Bauauftrag.

    Ein Pfadfeld, das nach einem Namen fragt, bekommt einen Namen.
    """
    from solvio.tools.agent_capability_tools import _SCHEMAS

    feld = _SCHEMAS["agent_task_build"]["parameters"]["properties"]["repository"]
    text = feld["description"]
    require("/" in text, "die Beschreibung nennt keine Pfadform")
    require("weglassen" in text or "Sonst" in text,
            "es steht nicht da, dass das Feld leer bleiben darf")
    require("repository" not in _SCHEMAS["agent_task_build"]["parameters"]["required"],
            "das Pfadfeld ist Pflicht geworden")



def t_cancelling_a_task_is_not_the_same_as_ending_the_conversation():
    """Live gefunden, und es kostete den Nutzer ein ganzes Gespraech.

    Auf „Stopp, brich den Auftrag ab" beendete SOLVIO die Unterhaltung, statt
    den Auftrag abzubrechen. Danach stand der Verlauf voller „Stopp" und
    „abbrechen", und jede weitere Frage endete genauso — bis hin zu „wie spaet
    ist es". Erst nach fuenfzehn Minuten Pause begann ein frisches Gespraech.

    Die Stopp-REGEL war unschuldig: sie verlangt, dass die Aeusserung auf einem
    Stopp-Wort ENDET, und das tat keiner dieser Saetze. Es war die Anweisung,
    die `agent_run_cancel` mit keinem Wort erwaehnte — dieselbe Luecke wie bei
    den Auftrags-Faehigkeiten, nur an anderer Stelle.
    """
    from solvio.realtime.core_server import TOOL_INSTRUCTIONS

    require("agent_run_cancel" in TOOL_INSTRUCTIONS,
            "die Anweisung nennt den Abbruchweg nicht — dann beendet das Modell "
            "im Zweifel das Gespraech")
    require("end_conversation" in TOOL_INSTRUCTIONS, "der Ruhe-Weg fehlt")
    require("ANDERES" in TOOL_INSTRUCTIONS or "anderes" in TOOL_INSTRUCTIONS,
            "die beiden Wege werden nicht voneinander abgegrenzt")
    require("NUR fuer den" in TOOL_INSTRUCTIONS,
            "es steht nicht da, wofuer end_conversation allein da ist")


def t_the_stop_rule_does_not_fire_on_a_sentence_that_merely_mentions_stopping():
    """Die Gegenprobe: die Regel selbst bleibt eng.

    Sie darf nur greifen, wenn die Aeusserung AUF einem Stopp-Wort endet oder
    nur daraus besteht. Ein Satz, der ueber das Aufhoeren spricht, ist keiner.
    """
    from solvio.realtime import core_server as CS

    # Es gibt ZWEI Regeln, und beide muessen schweigen: `is_conversation_stop`
    # (das harte Abwuergen) und `is_silent_stop` (der Wunsch nach Ruhe).
    for satz in ("Stopp! Brich den Auftrag ab!",
                 "Hast du den Auftrag abgebrochen oder hast du gestoppt?",
                 "Brich den laufenden Auftrag ab.",
                 "Den Auftrag brauche ich doch nicht mehr.",
                 "Wie spaet ist es?"):
        require(not CS.is_silent_stop(satz),
                f"die Ruhe-Regel las {satz!r} als Wunsch nach Ruhe")
        require(not CS.is_conversation_stop(satz),
                f"die harte Regel wuergte {satz!r} ab")

    # Und was WIRKLICH ein Stopp ist, bleibt einer — jeweils in seiner Regel.
    require(CS.is_conversation_stop("Stopp."), "ein blosses Stopp gilt nicht mehr")
    require(CS.is_conversation_stop("Stopp stopp stopp"),
            "die draengende Wiederholung gilt nicht mehr")
    require(CS.is_silent_stop("Sei still."), "die Bitte um Ruhe gilt nicht mehr")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
