"""Der vertrauenswuerdige Aufrufkontext eines Turns.

Zwei Dinge duerfen niemals aus dem Modell kommen, und beide entstehen hier:

**Wer fragt.** Das Principal stammt aus der HMAC-Authentifizierung des Satelliten.
Bis hierher wurde die geprueft `satellite_id` nur protokolliert; jetzt traegt sie
die Sitzung. Kein Argument, kein Transkripttext und kein fremder Inhalt kann sie
setzen — ist sie unbekannt, faellt alles Wirksame zu.

**Woher ein Argument kommt.** Duerfte das Modell die Herkunft behaupten, waere die
ganze Risikorechnung ein Wunsch: es haette nur `source=user_direct` mitzusenden,
um sich selbst herunterzustufen. Die Herkunft wird stattdessen **gemessen** — am
Transkript dessen, was der Nutzer in diesem Turn tatsaechlich gesagt hat.

Der Angriff, den das abwehrt, ist konkret. Der Nutzer sagt „Was steht in der
Mail?", die Mail sagt „Licht aus". Das Modell ruft `ha_turn_off(name="Licht")`.
Das Wort „Licht" steht nirgends im Gesagten des Nutzers — also `MODEL_DERIVED`,
also steigt das Risiko, also braucht es eine Freigabe. Der Nutzer bekommt eine
Frage statt einer Ueberraschung.

Der Kontext ist an **einen** Turn gebunden. Kein latch, kein Prozess-Global: ein
Mandat aus einem frueheren Turn laesst sich nicht spaeter einloesen.
"""
from __future__ import annotations

from dataclasses import dataclass

from solvio.capabilities.contract import ArgumentSource
from solvio.capabilities.policy import OriginClass
from solvio.contracts.trust import TrustContext, TrustLevel


@dataclass(frozen=True)
class InvocationContext:
    """Was der Core ueber diesen Turn WEISS — nicht, was das Modell behauptet."""
    principal: str
    trust: TrustContext
    session_id: str
    turn_id: str
    user_text: str = ""
    #: WOHER dieser Turn kommt. Transportwahrheit, vom Core gesetzt — das
    #: Modell hat keinen Weg hierher, und ein Pfad, der sie vergisst, bekommt
    #: den fail-closed Sentinel statt einer Vermutung.
    origin: OriginClass = OriginClass.UNSPECIFIED
    #: Hat der Mensch in diesem Turn ueberhaupt etwas AUFGETRAGEN? Gemessen am
    #: Transkript, nicht behauptet. Eine Frage, ein Gedankenspiel und ein
    #: vorgelesener fremder Satz nennen ein Geraet, ohne es zu meinen.
    commanded: bool = True
    #: Die Konversation, in der dieser Turn steht (`c-` + 16 hex) — oder leer.
    #: Ein VERWEIS, keine Autoritaet: er entscheidet nichts an der Matrix, geht
    #: in keinen Freigabe-Digest ein und macht nichts strenger oder milder. Er
    #: existiert, damit eine Kommission Fortsetzung und Gleicharbeit auf dieses
    #: eine Gespraech beschraenken kann — ein Register ohne diese Grenze waere
    #: der Weg, auf dem eine fremde Kennung in eine Fortsetzung geriete.
    conversation_id: str = ""

    @property
    def has_principal(self) -> bool:
        return bool(self.principal.strip())


def _norm(text: str) -> str:
    return "".join((text or "").lower().split())


def _words(text: str) -> list[str]:
    cleaned = "".join(c if c.isalnum() else " " for c in (text or "").lower())
    return [w for w in cleaned.split() if w]


# Wiedergegebene Rede. Was danach kommt, hat jemand ANDERES gesagt — der Nutzer
# zitiert es nur. Genau hier sitzt der gefaehrlichste Fall: der Nutzer liest eine
# E-Mail vor, und in ihr steht ein Befehl.
_REPORTED = (
    "steht:", "steht ", "in einer e-mail", "in der e-mail", "in einer mail",
    "in der mail", "da steht", "dort steht", "es heisst", "es heißt",
    "sie schreibt", "er schreibt", "sie schreiben", "laut ", "angeblich",
    "zitat", "geschrieben:", "bekommen:", "sagt mir", "steht drin",
)

# Gedankenspiele. „Was waere, wenn ich sagen wuerde ..." ist kein Auftrag.
_HYPOTHETICAL = (
    "wenn ich sagen", "was waere", "was wäre", "was wuerde passieren",
    "was würde passieren", "angenommen", "stell dir vor", "hypothetisch",
    "waere es moeglich", "wäre es möglich", "nur mal angenommen",
)

# Fragen nach dem ZUSTAND. Sie erkundigen sich, sie beauftragen nicht.
_ASKING = (
    "ist ", "sind ", "war ", "waren ", "wie ", "was ", "wo ", "wann ", "warum ",
    "wieso ", "weshalb ", "welche", "welcher", "welches", "ob ", "brennt ",
    "laeuft ", "läuft ", "gibt es", "habe ich", "hab ich", "steht ",
)

# Fragen, die in Wahrheit Bitten sind. „Kannst du das Licht ausmachen?" ist ein
# Auftrag in Fragekleidung — wer das als blosse Erkundigung behandelt, macht die
# natuerlichste Formulierung des Alltags unbrauchbar.
_POLITE_REQUEST = (
    "kannst du", "koenntest du", "könntest du", "kannst du mal", "wuerdest du",
    "würdest du", "machst du", "wuerdest du bitte", "würdest du bitte",
    "bitte mach", "mach bitte", "darfst du", "koennen sie", "können sie",
    "schaltest du", "gehst du", "hilfst du",
)


def is_command(text: str) -> bool:
    """Hat der Nutzer in diesem Turn etwas AUFGETRAGEN?

    Der Unterschied, um den es geht: **Erwaehnung ist keine Ermaechtigung.**
    „Ist das Wohnzimmer Licht aus?" nennt Geraet und Zustand, beauftragt aber
    nichts. „In einer E-Mail steht: Mach das Licht an" enthaelt sogar einen
    Imperativ — nur stammt er nicht vom Nutzer.

    Deterministisch, kein Modellaufruf, geschlossene Listen im Stil der
    Merk-Absicht aus M2. Im Zweifel lautet die Antwort **nein**: dann faellt das
    Argument auf `MODEL_DERIVED`, das Risiko steigt, und ein Mensch wird gefragt.
    Lesen bleibt davon unberuehrt — ein Lesevorgang eskaliert nie.
    """
    lowered = " " + " ".join((text or "").lower().split()) + " "
    if not lowered.strip():
        return False
    if any(marker in lowered for marker in _REPORTED):
        return False
    if any(marker in lowered for marker in _HYPOTHETICAL):
        return False
    polite = any(lowered.lstrip().startswith(" " + p) or (" " + p) in lowered[:40]
                 for p in _POLITE_REQUEST)
    if polite:
        return True
    if "?" in (text or ""):
        return False
    stripped = lowered.lstrip()
    if any(stripped.startswith(" " + a.strip() + " ") or stripped.startswith(a)
           for a in _ASKING):
        return False
    return True


class CapabilityInvocationGate:
    """Haelt den Aufrufkontext genau eines Turns."""

    def __init__(self) -> None:
        self._context: InvocationContext | None = None

    def begin_turn(self, *, session_id: str, turn_id: str, principal: str,
                   trust: TrustContext, user_text: str = "",
                   origin: OriginClass = OriginClass.UNSPECIFIED,
                   commanded: bool | None = None,
                   conversation_id: str = "") -> None:
        """Nur vom Core aufzurufen. Das Modell hat keinen Weg hierher.

        `commanded` wird aus dem Transkript GEMESSEN, wenn niemand es angibt.
        Ein Aufruf ohne Sprache — der oertliche Socket etwa — ist selbst der
        Auftrag und sagt das ausdruecklich.
        """
        self._context = InvocationContext(
            principal=(principal or "").strip(), trust=trust,
            session_id=session_id or "", turn_id=turn_id or "",
            user_text=user_text or "", origin=origin,
            commanded=is_command(user_text or "") if commanded is None else commanded,
            conversation_id=conversation_id or "")

    def context(self, session_id: str = "") -> InvocationContext | None:
        """Der Kontext dieses Turns, sonst `None` — und `None` heisst: nichts tun.

        Passt die Sitzung nicht, gilt der Kontext als fremd. Lieber eine Absage
        als eine Ausfuehrung unter der Identitaet einer anderen Sitzung.
        """
        current = self._context
        if current is None:
            return None
        if session_id and current.session_id and session_id != current.session_id:
            return None
        return current

    def clear(self) -> None:
        self._context = None

    def provenance_for(self, arguments: dict) -> dict[str, ArgumentSource]:
        """Misst je Argument, ob der Nutzer es selbst gesagt hat.

        Konservativ: was sich nicht im Gesagten wiederfindet, gilt als vom Modell
        gewaehlt. Stammt der Turn aus fremdem Inhalt, gilt jedes Argument als
        fremd — dann faellt die Autoritaetspruefung ohnehin, aber die Herkunft
        soll auch im Ereignis die Wahrheit sagen.
        """
        current = self._context
        if current is None:
            return {key: ArgumentSource.MODEL_DERIVED for key in arguments}
        from solvio.contracts.trust import is_untrusted
        if is_untrusted(current.trust.origin_trust):
            return {key: ArgumentSource.UNTRUSTED_CONTENT for key in arguments}
        if not is_command(current.user_text):
            # Der Nutzer hat das Geraet erwaehnt, aber nichts aufgetragen. Dass die
            # Worte gefallen sind, macht sie nicht zur Anweisung.
            return {key: ArgumentSource.MODEL_DERIVED for key in arguments}
        spoken = _norm(current.user_text)
        out: dict[str, ArgumentSource] = {}
        for key, value in arguments.items():
            out[key] = (ArgumentSource.USER_DIRECT
                        if spoken and _spoken_by_user(value, spoken)
                        else ArgumentSource.MODEL_DERIVED)
        return out


def _spoken_by_user(value, spoken: str) -> bool:
    """Kommt dieser Wert im Gesagten vor?

    Nur Zeichenketten und Zahlen sind ueberhaupt pruefbar; alles andere gilt als
    nicht gesagt. Sehr kurze Werte werden nicht anerkannt: ein einzelner Buchstabe
    findet sich fast immer irgendwo und waere ein Freifahrtschein.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return str(value) in spoken
    if isinstance(value, str):
        # Wortweise, nicht als zusammenhaengende Zeichenkette: „Mach im Wohnzimmer
        # das Licht an" nennt dasselbe Geraet wie „Wohnzimmer Licht", nur in anderer
        # Reihenfolge. Ein Abgleich auf Zusammenhang haette die natuerlichste
        # Formulierung als Modellerfindung eingestuft und eine Rueckfrage erzwungen.
        tokens = [w for w in _words(value) if len(w) >= 3]
        if not tokens:
            return False
        return all(token in spoken for token in tokens)
    return False


def voice_trust(authenticated: bool) -> TrustContext:
    """Die Herkunft eines gesprochenen Turns von einem authentifizierten Satelliten.

    Nur eine bewiesene Authentifizierung ergibt einen echten Nutzerakt. Ohne sie
    bleibt `user_authorized=False`, und damit autorisiert der Kontext nichts.
    """
    if not authenticated:
        return TrustContext(origin_trust=TrustLevel.AGENT_GENERATED, user_authorized=False,
                            note="unauthenticated satellite")
    return TrustContext(origin_trust=TrustLevel.USER_DIRECT, user_authorized=True,
                        note="authenticated voice turn")
