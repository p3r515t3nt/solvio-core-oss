"""Warum ein Passwort nie ins Gedaechtnis rutscht — und wohin es stattdessen geht.

Sagt jemand „merk dir, mein Passwort ist Hund1234", darf SOLVIO daraus keinen
Gedaechtnissatz machen. Er soll etwas anderes tun: **das gehoert in den Tresor.**

Was hier neu ist, ist nicht die Erkennung. Die gab es schon
(`solvio.memory.intent.looks_like_secret`) — deutschsprachig, kontextuell und
robust gegen die Wortzerlegung der Spracherkennung. Neu ist, dass sie an ALLEN
dauerhaften Schreibwegen sitzt statt nur an zweien. Vorher galt:

| Speicher | vorher |
|---|---|
| kanonisches Gedaechtnis (ausdrueckliches Merken) | geprueft |
| Adaptive-Kandidaten | geprueft |
| Gedaechtnis ueber `memory_correct` | **ungeprueft** |
| bestaetigter Kandidat | **ungeprueft** |
| Gespraechsverlauf (90 Tage Klartext) | **ungeprueft** |
| Proaktiver Eingang | **ungeprueft** |
| Obsidian-Projektion | nur Formregeln, schwaecher |

Der Verlauf ist der unangenehmste Eintrag in dieser Tabelle: ein gesprochenes
Passwort lag dort neunzig Tage im Klartext, unabhaengig davon, ob das Gedaechtnis
es abgelehnt hat.

**Zwei Antworten, nicht eine.** Ein Speicher, dessen Zweck eine Aussage ist
(Gedaechtnis, Wissen, Eingang), VERWEIGERT — dort waere ein halber Satz
schlimmer als keiner. Ein Speicher, dessen Zweck ein Verlauf ist (Gespraech),
REDIGIERT — den ganzen Redebeitrag zu verwerfen wuerde die Gespraechshoheit
brechen, und der Core besitzt das Gespraech.

**Und ehrlich zur Reichweite.** Eine Erkennung kennt nur, was jemand
aufgeschrieben hat. `looks_like_secret` findet benannte Zugangsdaten mit Wert
und bekannte Schluesselformen. Ein Satz wie „die Zahl ist 4711" ohne jeden
Begriff findet sie nicht — und das ist keine Luecke dieses Moduls, sondern die
Grenze jeder Mustererkennung. Deshalb ist der Tresor der eigentliche Weg und
diese Datei nur der Zaun daneben.
"""
from __future__ import annotations

from solvio.logging_setup import get_logger

log = get_logger("vault")

#: Was SOLVIO sagt, statt zu speichern. Produktsprache, kein Fehlercode.
TRESOR_HINWEIS = ("Das gehoert in den Tresor, nicht ins Gedaechtnis. "
                  "Leg es im iPhone unter System → Tresor ab.")

#: Was im Gespraechsverlauf stehen bleibt, wo ein Zugangsdatum stand. Der
#: Redebeitrag verschwindet nicht — sein Wert schon.
TRANSCRIPT_MARKER = "[Zugangsdaten — nicht gespeichert]"


class CredentialRefused(ValueError):
    """Dieser Inhalt sieht wie ein Zugangsdatum aus und wird nicht abgelegt.

    Traegt einen kategorischen Grund und NIE den Text, der die Ablehnung
    ausgeloest hat. Diese Ausnahme kann bis in ein Werkzeugergebnis und damit
    ins Modell laufen (`tools/dispatcher.py` stellt `str(exc)` hinein).
    """

    def __init__(self, reason: str, where: str = "") -> None:
        super().__init__(f"credential_refused:{reason}")
        self.reason = reason
        self.where = where
        self.human_message = TRESOR_HINWEIS


def is_credential(text: str) -> bool:
    """Der EINE Praedikat im Haus. Bewusst keine zweite Musterliste.

    Der Import liegt in der Funktion, weil `solvio.memory` diesen Zaun an seinen
    eigenen Schreibwegen benutzt — ein Import auf Modulebene waere ein Zyklus.
    """
    from solvio.memory.intent import looks_like_secret
    return bool(text) and looks_like_secret(text)


def reason_for(text: str) -> str:
    from solvio.memory.intent import credential_reason
    return credential_reason(text or "")


def refuse_if_credential(text: str, *, where: str) -> None:
    """Der Zaun fuer Speicher, deren Zweck eine Aussage ist. Wirft oder schweigt."""
    if not text or not is_credential(text):
        return
    reason = reason_for(text)
    # Nur die Tatsache und der Ort. Kein Ausschnitt, keine Laenge, kein
    # Anfangsbuchstabe — solche „harmlosen" Auskuenfte sind der uebliche Weg,
    # auf dem ein Geheimnis doch noch in ein Protokoll rutscht.
    log.warning("vault.firewall_refused", where=where, reason=reason)
    raise CredentialRefused(reason, where)


def redact_if_credential(text: str, *, where: str) -> str:
    """Der Zaun fuer Speicher, deren Zweck ein Verlauf ist. Gibt Ersatz zurueck."""
    if not text or not is_credential(text):
        return text
    log.warning("vault.firewall_redacted", where=where, reason=reason_for(text))
    return TRANSCRIPT_MARKER


def any_credential(*texts: str) -> str:
    """Der erste Text, der wie ein Zugangsdatum aussieht — oder leer.

    Fuer Speicher mit mehreren Feldern (Eingang, Aufgabe), damit der Aufrufer
    nicht selbst eine Schleife baut und dabei ein Feld vergisst.
    """
    for text in texts:
        if text and is_credential(text):
            return text
    return ""
