"""Ist das Ziel schon erfuellt? Der Core entscheidet — deterministisch.

Live gefunden, und es war der letzte Release-Blocker von Agent Runtime V1: der
Kundschafter beantwortete die Frage vollstaendig, mit Quellen, in 45 Sekunden.
Danach plante der Planer weiter, weil ihn niemand fragte, ob noch etwas fehlt.
Der naechste Schritt war strukturell ungueltig, zwei Nachplanungen erzeugten
denselben, das Revisionsbudget war auf, und der ganze Lauf endete auf FAILED —
mit einem fertigen Ergebnis im Journal, das den Menschen nie erreichte.

Die Entscheidung hier ist bewusst **kein Modellaufruf**. Ein Modell zu fragen
„hast du dein Ziel erreicht" waere genau die Selbstgenehmigung, die dieses
Projekt an jeder anderen Stelle verbietet — und sie waere teuer, langsam und
unbelegbar. Stattdessen wird das Ziel auf seine **nachpruefbaren Forderungen**
reduziert und die STRUKTURIERTE Ausgabe des Schritts dagegen gehalten.

Die Regel ist absichtlich streng in eine Richtung: sie darf einen erfuellten
Auftrag uebersehen (dann laeuft der Plan wie bisher weiter und kostet ein paar
Schritte), aber sie darf niemals einen unerfuellten als erfuellt ausgeben.
Deshalb:

* **Eine Wirkung ist keine Auskunft.** Nennt das Ziel eine Handlung — schreiben,
  senden, anlegen, buchen —, dann erfuellt ein Rechercheergebnis es nie, egal
  wie gut es ist. `side_effect` schliesst die Abkuerzung aus.
* **Geforderte Belege werden gezaehlt.** „belege das mit zwei verlaesslichen
  Quellen" ist eine pruefbare Zahl, keine Stimmung.
* **Eine leere oder duenne Antwort erfuellt nichts.** Unterhalb von
  `MIN_ANSWER` Zeichen gilt ein Ergebnis als Anfang, nicht als Antwort.

Was hier NICHT steht, ist ebenso Absicht: kein Urteil ueber inhaltliche
Richtigkeit. Ob 40 ct/kWh stimmt, weiss dieser Code nicht und behauptet es
nicht — er sagt nur, dass die Form der Antwort das ist, was das Ziel verlangt
hat. Die Kennzeichnung als `untrusted_executor` bleibt unberuehrt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from solvio.agent_runtime.store import SCOPE_RESEARCH

#: Kuerzer als das ist ein Anfang, keine Antwort. Gemessen am Live-Lauf: die
#: gelungene Strompreis-Antwort hatte 420 Zeichen, ein leeres Ergebnis null.
MIN_ANSWER = 40

#: Zahlwoerter, die vor einer Belegforderung stehen koennen. Bewusst klein —
#: wer sieben Quellen verlangt, bekommt die Pruefung nicht, sondern den
#: normalen Planablauf. Das ist die sichere Richtung.
_NUMBERS = {
    "ein": 1, "eine": 1, "einer": 1, "eins": 1, "1": 1,
    "zwei": 2, "2": 2, "drei": 3, "3": 3, "vier": 4, "4": 4,
    "fuenf": 5, "fünf": 5, "5": 5,
}

#: „belege das mit ZWEI verlaesslichen QUELLEN" — das Zahlwort darf bis zu drei
#: Fuellwoerter von seinem Substantiv entfernt stehen.
_SOURCE_DEMAND = re.compile(
    r"\b(" + "|".join(sorted(_NUMBERS, key=len, reverse=True)) + r")\b"
    r"(?:\s+\w+){0,3}?\s+"
    r"(quellen|quelle|belege|beleg|nachweise|nachweis|referenzen)\b",
    re.IGNORECASE)

#: Verben, die eine WIRKUNG verlangen statt einer Auskunft. Steht eines davon
#: im Ziel, kann kein Rechercheergebnis es abschliessen — dann gibt es keine
#: vorzeitige Vollendung, sondern den normalen Plan.
#:
#: `melden`, `berichten`, `sagen` stehen bewusst NICHT hier: die Meldung an den
#: Menschen erzeugt der Lauf ohnehin selbst, sie ist kein offener Schritt.
_SIDE_EFFECT = re.compile(
    r"\b("
    r"schreib\w*|erstell\w*|leg\w*\s+an|anleg\w*|erzeug\w*|"
    r"send\w*|schick\w*|mail\w*|poste\w*|verschick\w*|"
    r"buch\w*|kauf\w*|bestell\w*|bezahl\w*|ueberweis\w*|überweis\w*|"
    r"aender\w*|änder\w*|loesch\w*|lösch\w*|entfern\w*|"
    r"speicher\w*|hinterleg\w*|trag\w*\s+ein|eintrag\w*|"
    r"installier\w*|starte\w*|schalte\w*|oeffne\w*|öffne\w*|"
    r"commit\w*|merge\w*|push\w*"
    r")\b", re.IGNORECASE)


@dataclass(frozen=True)
class Requirements:
    """Was das Ziel nachpruefbar verlangt. Alles andere steht hier nicht."""

    min_sources: int = 0
    #: Nicht leer = das Ziel verlangt eine Wirkung. Dann nie vorzeitig fertig.
    side_effect: str = ""


@dataclass(frozen=True)
class Verdict:
    """Das Urteil. `reason` ist eine geschlossene Vokabel, kein Satz."""

    satisfied: bool
    reason: str
    findings: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()


def requirements(goal: str) -> Requirements:
    """Reduziert das Ziel auf seine pruefbaren Forderungen."""
    text = goal or ""
    hit = _SOURCE_DEMAND.search(text)
    minimum = _NUMBERS.get(hit.group(1).lower(), 0) if hit else 0
    effect = _SIDE_EFFECT.search(text)
    return Requirements(min_sources=minimum,
                        side_effect=(effect.group(1).lower() if effect else ""))


def answer_of(result) -> str:
    """Die Antwort eines Ergebnisses — Empfehlung, sonst der erste Befund.

    Bewusst NICHT `raw_excerpt`: der Rohauszug ist fuer den Menschen gedacht,
    der nachsehen will, und er gehoert nicht in eine Erfuellungspruefung.
    """
    text = (getattr(result, "recommended_path", "") or "").strip()
    if text:
        return text
    findings = list(getattr(result, "findings", []) or [])
    return (findings[0] or "").strip() if findings else ""


def evaluate(*, goal: str, result, scope: str) -> Verdict:
    """Erfuellt DIESER Schritt das Ziel bereits vollstaendig?

    `satisfied=True` heisst: es gibt nichts mehr zu tun, was das Ziel genannt
    haette. Jede Ablehnung nennt ihren Grund, damit ein uebersehener Erfolg im
    Buch nachvollziehbar ist und nicht als Schweigen erscheint.
    """
    if scope != SCOPE_RESEARCH:
        return Verdict(False, "scope_not_research")
    if not getattr(result, "ok", False):
        return Verdict(False, "step_not_ok")
    if not getattr(result, "usable", False):
        return Verdict(False, "result_not_usable")

    need = requirements(goal)
    if need.side_effect:
        # Ein Ziel, das eine Wirkung verlangt, ist mit einer Auskunft nicht
        # erledigt — auch dann nicht, wenn die Auskunft ausgezeichnet ist.
        return Verdict(False, "goal_demands_action")

    answer = answer_of(result)
    if len(answer) < MIN_ANSWER:
        return Verdict(False, "answer_too_thin")

    sources = tuple(str(s) for s in (getattr(result, "evidence", []) or []) if str(s).strip())
    if len(sources) < need.min_sources:
        return Verdict(False, "not_enough_sources")

    findings = tuple(str(f) for f in (getattr(result, "findings", []) or []) if str(f).strip())
    return Verdict(True, "goal_met", findings=findings or (answer,), sources=sources)


__all__ = ["MIN_ANSWER", "Requirements", "Verdict", "answer_of", "evaluate",
           "requirements"]
