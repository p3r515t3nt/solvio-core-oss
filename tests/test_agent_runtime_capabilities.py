"""Die Anbindung an den Router — und die Grenzen, die ein Lauf nicht adressiert.

Vier Fragen, jede mit einer eigenen Antwort im Code:

1. **Stempelt die Laufzeit ehrlich?** Herkunft immer `BACKGROUND_AUTOMATION`,
   Principal `agent:<id>`, Provenienz je Argument — und Spezialistenausgabe
   IMMER `UNTRUSTED_CONTENT`.
2. **Kann ein Lauf seine eigene Familie rufen?** Nein: `agent_task_*`,
   `agent_run_*` und `deep_*` stehen auf der Sperrliste VOR dem Router, und die
   Erzeugungs-Handler verweigern zusaetzlich jede nicht-interaktive Herkunft.
   Zwei unabhaengige Schranken.
3. **Gibt es einen Schreibpfad in Gedaechtnis oder Wissen?** Nein, und zwar
   nicht gefiltert, sondern **nicht verdrahtet** — per AST festgenagelt.
4. **Bleibt der Rollback moeglich?** Kein Kernmodul importiert
   `solvio.agent_runtime`; ohne den Attach-Aufruf gibt es die Faehigkeiten
   schlicht nicht.
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))
from _guard import enforce_assertions, require, require_equal  # noqa: E402
enforce_assertions()

_TMP = tempfile.mkdtemp(prefix="solvio-caps-")
os.environ["SOLVIO_STATE_DIR"] = _TMP
os.environ["SOLVIO_AGENT_RUNS_DB"] = os.path.join(_TMP, "agent_runs.sqlite3")

from solvio.agent_runtime import authority as A  # noqa: E402
from solvio.capabilities import agent as CAP  # noqa: E402
from solvio.capabilities.policy import ACTION_CLASS, ActionClass, OriginClass  # noqa: E402
from solvio.security.mobile_approval.execution import READ_ONLY  # noqa: E402

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RUNTIME_DIR = os.path.join(REPO, "src", "solvio", "agent_runtime")
SRC = os.path.join(REPO, "src")


def _modules(folder: str) -> list[str]:
    out = []
    for base, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        out += [os.path.join(base, f) for f in sorted(files) if f.endswith(".py")]
    return out


def _imports(path: str) -> list[tuple[int, str]]:
    tree = ast.parse(open(path, encoding="utf-8").read())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
    return found


# =====================================================================
# 1 — die Sperrliste steht VOR dem Router
# =====================================================================

def t_every_blocked_family_is_refused_before_the_router():
    families = {
        "memory_forget": "memory", "memory_purge": "memory",
        "secret_use": "secret", "secret_store": "secret",
        "background_create": "background", "background_run_now": "background",
        "proactive_mark_read": "proactive",
        # Die ganze eigene Familie, nicht zwei Zweige davon: ein kuenftiges
        # `agent_policy_set` fiele sonst durch beide Praefixe.
        "agent_task_research": "agent", "agent_task_build": "agent",
        "agent_run_cancel": "agent", "agent_run_resume": "agent",
        "agent_policy_set": "agent", "agent_budget_set": "agent",
        "deep_research": "deep", "deep_cancel": "deep",
    }
    for name, family in families.items():
        reason = A.is_blocked(name)
        require(reason, f"{name} war nicht gesperrt")
        require(family in reason, f"{name}: falscher Grund {reason}")


def t_the_recursion_block_cannot_be_shortened_without_being_caught():
    """Die Mutation, die die Sperrliste um `agent_task_build` kuerzt, muss
    gefangen werden — sonst gebaert ein Lauf einen Lauf."""
    for name in ("agent_task_research", "agent_task_build",
                 "agent_run_status", "agent_run_cancel", "agent_run_resume"):
        require(A.is_blocked(name), f"{name} ist aus der Sperrliste gefallen")
    require("agent_" in A.BLOCKED_PREFIXES,
            "das Praefix der eigenen Familie fehlt oder wurde verengt")
    # Und ein Name, der nur zufaellig so anfaengt, bleibt erlaubt — eine
    # Sperrliste, die `agentur_termin` faengt, ist zu grob und wird umgangen.
    require_equal(A.is_blocked("agentur_termin"), "",
                  "die Sperrliste ist zu grob geworden")


def t_money_is_blocked_except_the_named_preparation_seam():
    for name in ("purchase_place", "payment_execute", "payment_refund",
                 "purchase_confirm"):
        require(A.is_blocked(name), f"{name} war erreichbar")
    require_equal(A.is_blocked(A.PAYMENT_PREPARE), "",
                  "der Vorschlagsweg wurde mitgesperrt")


def t_a_malformed_or_unknown_name_is_refused_not_guessed():
    for name in ("", "  ", "AGENT_TASK_BUILD", "../etc/passwd", "a", "x" * 200):
        require(A.is_blocked(name), f"'{name[:20]}' passierte die Namenspruefung")


def t_the_guard_raises_instead_of_returning_a_falsy_value():
    """Fail-closed: ein Aufrufer, der den Rueckgabewert ignoriert, soll nicht
    versehentlich weiterlaufen."""
    try:
        A.guard("memory_forget")
    except A.CapabilityBlocked as exc:
        require(exc.name == "memory_forget", "der Name fehlt im Fehler")
        require(exc.reason, "der Grund fehlt im Fehler")
    else:
        require(False, "guard liess einen gesperrten Namen durch")


# =====================================================================
# 2 — die Erzeugungs-Handler verweigern jede nicht-interaktive Herkunft
# =====================================================================

def t_the_creation_origins_exclude_background_and_untrusted():
    require(OriginClass.BACKGROUND_AUTOMATION not in CAP.CREATION_ORIGINS,
            "ein Hintergrundlauf darf Auftraege anlegen — das ist die Rekursion")
    require(OriginClass.EXTERNAL_UNTRUSTED not in CAP.CREATION_ORIGINS,
            "Fremdinhalt darf Auftraege anlegen")
    require(OriginClass.UNSPECIFIED not in CAP.CREATION_ORIGINS,
            "eine unbekannte Herkunft darf Auftraege anlegen")
    require_equal(sorted(o.value for o in CAP.CREATION_ORIGINS),
                  ["local_owner", "room_voice", "trusted_interactive_app"],
                  "die Herkunftsliste hat sich verschoben")


def t_outside_a_router_call_the_origin_is_unspecified_and_therefore_refused():
    """Fail-closed: ohne Router-Kontext gibt es keine Herkunft, und keine
    Herkunft ist keine Erlaubnis."""
    require_equal(CAP.current_origin(), OriginClass.UNSPECIFIED,
                  "ausserhalb eines Aufrufs entstand eine Herkunft aus dem Nichts")


def t_the_origin_comes_from_the_context_not_from_an_argument():
    """Ein Modell darf die Herkunft weder setzen noch faelschen — sie steht in
    keinem Eingabeschema."""
    for name, spec in CAP.SPECS.items():
        properties = set((spec.input_schema.get("properties") or {}))
        forbidden = {"origin", "trust", "principal", "approval", "commanded"}
        require_equal(sorted(properties & forbidden), [],
                      f"{name} nimmt ein Autoritaetsfeld entgegen")


# =====================================================================
# Die Politik-Einstufung
# =====================================================================

def t_creating_autonomous_work_is_a_write_class():
    """Eine READ_ONLY-Einstufung entwaffnete beide Schranken: `authority_refusal`
    laesst Lesendes aus jeder Herkunft passieren, und selbst
    `EXTERNAL_UNTRUSTED × READ_ONLY` ist in der Matrix DIREKT."""
    require_equal(ACTION_CLASS["agent_task_research"], ActionClass.NORMAL_WRITE,
                  "die Recherche wurde zur Leseklasse")
    require_equal(ACTION_CLASS["agent_task_build"], ActionClass.NORMAL_WRITE,
                  "der Bauauftrag wurde zur Leseklasse")
    require_equal(ACTION_CLASS["agent_run_resume"], ActionClass.NORMAL_WRITE,
                  "die Wiederaufnahme wurde zur Leseklasse")
    # Und die Gegenrichtung: Lesen steht NICHT in der Registry. Es wird aus der
    # serverseitigen Spec abgeleitet — zwei getrennte Wahrheiten ueber dieselbe
    # Faehigkeit laufen frueher oder spaeter auseinander.
    for name in ("agent_run_status", "agent_run_cancel"):
        require(name not in ACTION_CLASS,
                f"{name} steht als Leseklasse in der Registry statt in der Spec")
        require_equal(CAP.SPECS[name].semantics, READ_ONLY,
                      f"{name} ist in der Spec nicht lesend")


def t_every_agent_capability_has_an_approval_label():
    """Ohne Label rendert die Freigabefrage als nackter Faehigkeitsname — und
    ein Mensch bestaetigt etwas, das er nicht gelesen hat."""
    from solvio.capabilities.approval_gateway import ACTION_LABELS
    CAP.register(_FakeRouter(), CAP.AgentCapabilities(None))
    for name in ("agent_task_research", "agent_task_build", "agent_run_resume"):
        require(name in ACTION_LABELS, f"{name} hat keinen Freigabetext")
        headline, fields = ACTION_LABELS[name]
        require(headline and not headline.startswith("agent_"),
                f"{name}: die Ueberschrift ist ein Bezeichner")


class _FakeRouter:
    def __init__(self) -> None:
        self.registered = []

    def register(self, spec, handler):
        self.registered.append(spec.name)


# =====================================================================
# 3 — kein Schreibpfad in Gedaechtnis oder Wissen
# =====================================================================

def t_the_runtime_imports_neither_memory_nor_knowledge():
    """Nicht gefiltert, sondern nicht verdrahtet. Das ist der Unterschied
    zwischen „wir passen auf" und „es geht nicht"."""
    offenders = []
    for path in _modules(RUNTIME_DIR):
        for line, module in _imports(path):
            if module.startswith(("solvio.memory", "solvio.knowledge")):
                offenders.append(f"{os.path.basename(path)}:{line} {module}")
    require_equal(offenders, [],
                  f"die Agentenlaufzeit importiert Gedaechtnis/Wissen: {offenders}")


def t_the_runtime_names_no_memory_or_knowledge_capability():
    """Auch nicht als Zeichenkette: ein Name, den der Planer nennen koennte,
    waere ein Weg, den die Sperrliste zwar faengt — aber der gar nicht erst
    entstehen soll."""
    for path in _modules(RUNTIME_DIR):
        source = open(path, encoding="utf-8").read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value.strip()
                # `knowledge_proposal` ist die SCHRITTART des Vorschlagswegs und
                # gehoert hierher. Ein Faehigkeitsname der beiden Familien nicht.
                if value in ("knowledge_proposal",):
                    continue
                if value.startswith(("memory_", "knowledge_")) and len(value) > 8:
                    require(False, f"{os.path.basename(path)} nennt {value}")


def t_a_knowledge_proposal_is_an_artifact_not_a_write():
    """Ein Vorschlag erzeugt eine Datei und eine Meldung. Der Wissens-Compiler
    liest weiterhin nur `active_records()` — es gibt keinen Aufrufpfad hinein."""
    source = open(os.path.join(RUNTIME_DIR, "orchestrator.py"), encoding="utf-8").read()
    tree = ast.parse(source)
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_proposal_step")
    body = ast.dump(func)
    require("add_artifact" in body, "der Vorschlag erzeugt kein Artefakt")
    # Geprueft wird der AUFRUF, nicht das Wort: `knowledge_proposal` ist die
    # Schrittart und soll dastehen. Was nicht dastehen darf, ist ein Aufruf in
    # den Wissens- oder Gedaechtnispfad.
    called = {n.func.attr for n in ast.walk(func)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    for forbidden in ("remember", "promote", "compile", "learn", "write_record",
                      "put_record", "add_record"):
        require(forbidden not in called,
                f"der Vorschlagsschritt ruft {forbidden}")


# =====================================================================
# 4 — Rollback und die eine Router-Stelle
# =====================================================================

def t_no_core_module_imports_the_agent_runtime():
    """Die Rollback-Zusage: `SOLVIO_AGENT_RUNTIME=off` (oder ein Revert der
    Attach-Commits) entfernt Faehigkeiten, Endpunkt, Probe und Takt
    vollstaendig. Ein Import auf Modulebene irgendwo im Kern wuerde das
    unmoeglich machen."""
    offenders = []
    for path in _modules(SRC):
        if path.startswith(RUNTIME_DIR):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        # NUR Importe auf Modulebene: `registry.attach_agent_runtime` und der
        # Start in `core_server.serve()` importieren absichtlich INNERHALB einer
        # Funktion — genau das macht den Rollback moeglich, weil der Import erst
        # beim Anhaengen passiert und ohne ihn nie.
        for node in tree.body:
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            for module in mods:
                if module.startswith("solvio.agent_runtime"):
                    offenders.append(f"{os.path.relpath(path, REPO)}:{node.lineno}")
    require_equal(offenders, [],
                  f"ein Kernmodul importiert die Laufzeit auf Modulebene: {offenders}")


def t_origin_is_stamped_in_exactly_one_module():
    """Eine zweite Stelle waere eine zweite Meinung darueber, was eine Herkunft
    ist — und die erste, die jemand vergisst nachzuschaerfen."""
    # Gemeint ist der STEMPEL, nicht das Wort: `create_task(origin=...)` traegt
    # die Herkunft der ERZEUGUNG in das Buch und ist etwas anderes als die
    # Herkunft, mit der gehandelt wird. Geprueft wird deshalb, wo `OriginClass`
    # ueberhaupt vorkommt — dort faellt die Entscheidung.
    offenders = []
    for path in _modules(RUNTIME_DIR):
        source = open(path, encoding="utf-8").read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and \
                    isinstance(node.value, ast.Name) and \
                    node.value.id == "OriginClass":
                offenders.append(f"{os.path.basename(path)}:{node.lineno}")
    require_equal(sorted(set(o.split(":")[0] for o in offenders)), ["steps.py"],
                  f"`OriginClass` steht ausserhalb von steps.py: {offenders}")


def t_the_runtime_calls_router_execute_in_exactly_one_module():
    # `connection.execute(...)` im Buch ist SQL und hat mit dem Router nichts zu
    # tun. Gezaehlt wird nur ein `execute` auf einem Empfaenger, der `router`
    # heisst — das ist die Naht, um die es geht.
    offenders = []
    for path in _modules(RUNTIME_DIR):
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "execute"):
                continue
            receiver = node.func.value
            name = receiver.id if isinstance(receiver, ast.Name) else (
                receiver.attr if isinstance(receiver, ast.Attribute) else "")
            if "router" in name.lower():
                offenders.append(os.path.basename(path))
    require_equal(sorted(set(offenders)), ["steps.py"],
                  f"`router.execute` steht ausserhalb von steps.py: {set(offenders)}")


def t_the_frozen_security_package_was_not_touched():
    """`src/solvio/security/` wurde von DIESEM Milestone nicht angefasst.

    **Beide Enden sind gepinnt**, und das ist der Punkt. Frueher lief der
    Vergleich von `c96d10e` bis `HEAD` — damit war die Zusicherung keine Aussage
    ueber Agent Runtime V1 mehr, sondern ein mitwachsender Waechter ueber alles,
    was danach kommt. Als DEBT-0155 den eingefrorenen Speicher bewusst und
    aufgezeichnet aenderte (ADR-0032), waere sie damit fuer immer rot geworden —
    fuer eine Aenderung, die diesen Milestone nie beruehrt hat.

    Der laufende Freeze-Waechter ist ein anderer und steht woanders: vier
    Zusicherungen gegen die jeweils geltende Baseline
    (`approval-security-v1-audit-truth`). Diese hier sagt etwas ueber die
    Geschichte, und Geschichte aendert sich nicht.
    """
    import subprocess
    # c96d10e = die Kredentialgrenze, d33890b = der Merge von Agent Runtime V1.
    out = subprocess.run(["/usr/bin/git", "diff", "--name-only", "c96d10e", "d33890b"],
                         cwd=REPO, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise __import__("unittest").SkipTest("braucht das git-Repository")
    touched = [line for line in out.stdout.splitlines()
               if line.startswith("src/solvio/security/")]
    require_equal(touched, [], f"der eingefrorene Baum wurde angefasst: {touched}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
