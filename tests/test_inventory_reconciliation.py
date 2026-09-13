"""Baum↔Inventur-Abgleich — die DEBT-0130-Doktrin als Test.

`broker.sqlite3` verschwand stumm aus jedem Sicherungssatz, weil es entstand,
ohne dass die Inventur davon erfuhr. Die Inventur hat die ausdrueckliche
Regel: „Eine Sicherung, die ihre Luecken verschweigt, ist die gefaehrlichste
Art von gruen." Dieser Test macht die Regel mechanisch — in zwei Richtungen:

* **Code → Inventur:** jeder `~/.solvio*`-Pfad, den der Quelltext nennt,
  muss klassifiziert sein — als `Item`, als `EXCLUDED`-Zeile oder als hier
  BEGRUENDET gefuehrter Betriebszustand. Ein neuer Store faellt damit beim
  ersten Gate auf, nicht beim Restore.
* **Lebender Baum → Inventur:** jeder Eintrag auf oberster Ebene von
  `~/.solvio` muss dieselbe Entscheidung tragen. Was dort liegt und in
  keiner Liste steht, ist eine stumme Entscheidung — genau die Krankheit.

Wird dieser Test rot, ist die Antwort NIE, ihn aufzuweichen: der neue Pfad
bekommt ein `Item` (er ist Bestand), eine `EXCLUDED`-Zeile (er ist bewusst
keiner — mit Grund und Wiederherstellungsweg) oder, nur fuer Betriebszustand
der Werkzeuge selbst, eine begruendete Zeile in `OPERATIONAL` hier.

ASSERTION POLICY: `require*` aus `tests/_guard.py` sind Funktionsaufrufe und
ueberleben `python -O`.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from _guard import enforce_assertions, require, require_equal  # noqa: E402

from solvio.storage import inventory  # noqa: E402

enforce_assertions()

_SRC_ROOT = os.path.join(os.path.dirname(__file__), "..", "src", "solvio")

#: Betriebszustand der Werkzeuge selbst — bewusst WEDER Item NOCH EXCLUDED im
#: Register: das Register beantwortet „was gehoert zum Bestand", diese Liste
#: beantwortet „was ist Maschinerie". Jede Zeile traegt ihren Grund; eine
#: Zeile ohne Grund waere dieselbe stumme Entscheidung, gegen die der Test da
#: ist.
OPERATIONAL: dict[str, str] = {
    "~/.solvio/storage":
        "Betriebszustand des Sicherungswerkzeugs selbst (state.json, lock, "
        "log) — zirkulaer, Vertrag Offsite V1 §19.",
    "~/.solvio/storage.json":
        "Konfiguration des Sicherungswerkzeugs (Volume-UUID) — zirkulaer wie "
        "storage/; nach Plattenverlust wird neu eingerichtet.",
    "~/.solvio/offsite":
        "Arbeitsverzeichnis des Offsite-Werkzeugs; sein state.json ist "
        "zirkulaer (Vertrag §19). Der Umschlag DARIN ist eigener Bestand "
        "(Item offsite-identity-envelope).",
    "~/.solvio/offsite-b0":
        "B0-Arbeitsverzeichnis (Owner-Probe-Reports). Die Beweise sind im "
        "Repository archiviert (docs/design/offsite-encrypted-backup-v1/b0/"
        "reports/), der Umschlag liegt kanonisch unter ~/.solvio/offsite.",
    "~/.solvio/control.sock":
        "Laufzeit-Socket des Cores. Existiert nur, solange der Prozess lebt.",
    "~/.solvio/autopilot.lock":
        "flock des Autopilot-Treibers. Er verhindert, dass zwei Treiber "
        "denselben Arbeitsbereich bearbeiten; nach einem Neustart ist er "
        "bedeutungslos. Das BUCH daneben ist eigener Bestand (Item autopilot).",
    "~/.solvio-approvals":
        "Standardpfad von identity.DEFAULT_STATE_DIR — traegt eine ANDERE "
        "Kryptoidentitaet und ein aelteres Schema; ausdruecklich nicht der "
        "Bestand (siehe FALLBACK-Kommentar in inventory.py).",
}

#: Begleitdateien und Fluechtiges, das keine eigene Entscheidung braucht.
_IGNORED_SUFFIXES = ("-wal", "-shm", "-journal", ".lock", ".log", ".tmp",
                     ".pid")
_IGNORED_NAMES = {".DS_Store"}


def _norm(path: str) -> str:
    return path.rstrip("/.")


def _expand(path: str) -> str:
    return os.path.expanduser(path)


def _mentions_as_whole(what: str, probe: str) -> bool:
    """Nennt die Registerzeile den Pfad als GANZES — nicht als Praefix eines
    tieferen Pfads? Nach dem Treffer darf hoechstens ein einzelner Schraeg-
    strich und dann eine Nicht-Pfad-Fortsetzung kommen (Ende, Komma,
    Leerzeichen, Klammer)."""
    boundary = ("", " ", ",", ")", "}")
    for match in re.finditer(re.escape(probe), what):
        rest = what[match.end():]
        if rest.startswith("/"):
            rest = rest[1:]
        if rest[:1] in boundary:
            return True
    return False


def _covered(path: str) -> str | None:
    """Wie der Pfad klassifiziert ist — oder None, wenn er es nicht ist."""
    p = _expand(_norm(path))

    for item in inventory.items():
        src = _expand(item.source)
        if p == src:
            return f"Item {item.name}"
        if p.startswith(src + os.sep):
            return f"unter Item {item.name}"
        if src.startswith(p + os.sep):
            return f"Elternverzeichnis von Item {item.name}"

    # EXCLUDED-Zeilen sind Prosa („a, b, c"); geprueft wird deshalb, ob der
    # Pfad selbst oder eines seiner Elternverzeichnisse woertlich in einer
    # Zeile vorkommt — in ~-Form, wie das Register schreibt, und nur an einer
    # PFADGRENZE: „~/.solvio" in „~/.solvio/payment-sandbox.env" zaehlt NICHT
    # als Ausschluss von ~/.solvio (die erste Fassung dieses Tests machte
    # genau den Fehler, und die Mutation „Item entfernt" blieb gruen).
    probe = _norm(path)
    while probe and probe not in ("~", os.sep):
        for ex in inventory.EXCLUDED:
            if _mentions_as_whole(ex.what, probe):
                return f"EXCLUDED ({probe})"
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent

    for op, _reason in OPERATIONAL.items():
        op_n = _norm(op)
        if _norm(path) == op_n or _norm(path).startswith(op_n + "/"):
            return "OPERATIONAL"
    return None


def _code_referenced_paths() -> set[str]:
    pattern = re.compile(r"~/\.solvio[A-Za-z0-9._/@-]*")
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(_SRC_ROOT):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                for match in pattern.findall(fh.read()):
                    normed = _norm(match)
                    if normed:
                        found.add(normed)
    return found


def t_every_code_referenced_state_path_is_classified() -> None:
    paths = _code_referenced_paths()
    require(len(paths) >= 20,
            f"der Code-Scan fand nur {len(paths)} Pfade — der Scanner selbst "
            "ist kaputt, nicht der Bestand")
    unclassified = sorted(p for p in paths if _covered(p) is None)
    require_equal(unclassified, [],
                  "Pfade im Quelltext ohne Inventur-Entscheidung (Item ODER "
                  "EXCLUDED ODER begruendet OPERATIONAL) — die DEBT-0130-"
                  f"Doktrin: {unclassified}")


def t_every_live_toplevel_entry_is_classified() -> None:
    root = _expand("~/.solvio")
    if not os.path.isdir(root):
        return  # ein leerer Baum ist ein leerer Befund, kein roter
    unclassified: list[str] = []
    for name in sorted(os.listdir(root)):
        if name in _IGNORED_NAMES or name.endswith(_IGNORED_SUFFIXES):
            continue
        if _covered(f"~/.solvio/{name}") is None:
            unclassified.append(name)
    require_equal(unclassified, [],
                  "Eintraege in ~/.solvio ohne Inventur-Entscheidung — jeder "
                  "braucht Item, EXCLUDED-Zeile oder begruendetes "
                  f"OPERATIONAL: {unclassified}")


def t_the_excluded_register_carries_reason_and_recovery() -> None:
    for ex in inventory.EXCLUDED:
        require(ex.reason.strip(),
                f"EXCLUDED ohne Grund: {ex.what!r} — der Grund IST die "
                "Entscheidung")
        require(ex.recovery.strip(),
                f"EXCLUDED ohne Wiederherstellungsweg: {ex.what!r}")


def t_items_do_not_collide() -> None:
    names = [i.name for i in inventory.items()]
    require_equal(len(names), len(set(names)), "doppelter Item-Name")
    dests = [i.dest for i in inventory.items()]
    require_equal(len(dests), len(set(dests)), "doppeltes Item-Ziel")
    sources = [_expand(i.source) for i in inventory.items()]
    require_equal(len(sources), len(set(sources)), "doppelte Item-Quelle")


def t_the_offsite_v1_additions_are_present() -> None:
    """B1-Zusicherung: die vier neuen Bestandszeilen und die vier neuen
    EXCLUDED-Entscheidungen aus dem Architekturvertrag (§3/§19) existieren."""
    by_name = {i.name: i for i in inventory.items()}
    for name in ("agent-run-artifacts", "offsite-ledger",
                 "offsite-identity-envelope", "payment-config"):
        require(name in by_name, f"Item fehlt: {name}")
    require(by_name["offsite-identity-envelope"].secret_class
            == inventory.CLASS_BACKUP,
            "der Umschlag ist Klasse A — ohne Passphrase wertlos, deshalb "
            "sicherbar")
    joined = "\n".join(ex.what for ex in inventory.EXCLUDED)
    for needle in ("payment-sandbox.env", "~/.solvio-hermes/api_key",
                   "~/.solvio/memory/backups", "postgresql@16",
                   "agent_workspaces"):
        require(needle in joined, f"EXCLUDED-Entscheidung fehlt: {needle}")


if __name__ == "__main__":
    from _harness import run_module
    raise SystemExit(run_module(globals(), __name__))
