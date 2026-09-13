"""Wer fragen darf, wie lange, und wie viel — die Registratur des Brokers.

Drei Verneinungen tragen diesen Milestone, und diese Datei haelt die zweite und
die dritte:

1. **Kein Anbieterzugang.** Der Wert im Kaefig ist ein undurchsichtiger
   Broker-Token; gegen `api.openai.com` bekommt er `401`. (Das entsteht
   dadurch, dass `upstream.py` den echten Schluessel setzt, nicht hier.)
2. **Kein Zugang ohne laufenden Auftrag.** Ohne offenes, nicht abgelaufenes
   Lease antwortet der Broker `403`.
3. **Kein Zugang ueber den Auftrag hinaus.** Faellt die Zahl lebender Leases
   eines Auftraggebers auf null, wird sein Token **neu gepraegt**; der alte ist
   ab da `401`.

Erst die dritte trennt „verschoben" von „strukturell unnoetig". Ohne sie bliebe
ein Wert im Kaefig liegen, der den Auftrag ueberlebt, fuer den er gepraegt
wurde.

**Die Generation zaehlt je Auftraggeber, nie global.** Der Doktor provisioniert
im `hermes_restart` **nur** Deep neu; ein globaler Zaehler wuerde dabei die drei
Bot-Token entwerten, die niemand nachpraegt — und die Bots wuerden ab da
schweigend `401` kassieren.

**Die Tokenkappe belastet vor.** `usage` erreicht den Broker erst im letzten
Stromereignis und bei abgeklemmter Verbindung nie. Eine Kappe, die auf
Beobachtetes wartet, waere mit einem weggelassenen
`stream_options.include_usage` oder einem abgebrochenen Strom auf null zu
setzen. Deshalb wird aus dem ohnehin gepufferten Rumpf geschaetzt und sofort
gebucht; ein spaeter eintreffendes `usage` **ersetzt** die Schaetzung — nach
oben wie nach unten. Zurueckgebucht wird nie.
"""
from __future__ import annotations

import hmac
import secrets
import time
from dataclasses import dataclass, field

from solvio.logging_setup import get_logger

log = get_logger("broker")

DOCUMENT_PRINCIPAL = "document-ask"

#: Das Praefix macht einen Fund im Kaefig sofort lesbar: das ist ein
#: Broker-Token, kein Anbieterschluessel. Der Rumpf ist `token_hex(24)`.
TOKEN_PREFIX = "sk-solvio-broker-"
TOKEN_BYTES = 24


def mint_token() -> str:
    """Ein frischer, undurchsichtiger Broker-Token. Nur im Speicher."""
    return TOKEN_PREFIX + secrets.token_hex(TOKEN_BYTES)


@dataclass(frozen=True)
class Caps:
    """Der Rand eines Auftraggebers.

    Die Vorgaben sind bewusst grosszuegig und werden in der Live-Abnahme auf
    das **Gemessene** nach unten gezogen. Eine Kappe, die nie gemessen wurde,
    ist eine Behauptung.
    """

    max_leases: int = 4
    max_inflight: int = 6
    requests_per_day: int = 5_000
    tokens_per_day: int = 5_000_000

    #: **Welche Modelle dieser Auftraggeber ueberhaupt anfordern darf.**
    #:
    #: Bis Cognitive Router V1 gab es genau ein Modell und deshalb genau eine
    #: globale Liste. Sobald ein zweites Modell existiert, ist eine globale
    #: Liste keine Grenze mehr, sondern eine Einladung: sie haette `gpt-5.4`
    #: auch dem Hermes-Kaefig gegeben. Die Vorgabe ist deshalb das kleine
    #: Modell — wer das grosse braucht, bekommt es ausdruecklich, und das sind
    #: genau die zwei Core-gehaltenen Eskalations-Auftraggeber.
    allowed_models: frozenset[str] = frozenset({"gpt-5.4-mini"})

    #: **Welche anbieterseitigen Werkzeuge dieser Auftraggeber ueberhaupt
    #: nennen darf.** Anbieterseitig heisst: der Anbieter fuehrt es selbst aus
    #: (`web_search`, `code_interpreter`, `file_search`, `computer_use`,
    #: `image_generation`, `mcp`, ...) — nicht `function`, das bleibt
    #: klientenseitig und damit immer erlaubt. Die Vorgabe ist die LEERE
    #: Menge: ein neues anbieterseitiges Werkzeug ist eine bewusste
    #: Politikaenderung je Auftraggeber, nie eine Voreinstellung. Bestehende
    #: Auftraggeber, die nie eines nannten, sind davon unberuehrt.
    allowed_provider_tools: frozenset[str] = frozenset()


#: Vorgaben je Auftraggeberform, **auf die Live-Messung heruntergezogen**.
#:
#: Der Entwurf schlug grosszuegige 5 Mio. bzw. 1 Mio. Token am Tag vor und sagte
#: ausdruecklich dazu, dass die Abnahme sie auf die Wirklichkeit ziehen soll.
#: Gemessen wurde: eine Deep-Aufgabe = 3 physische Anfragen, zusammen rund
#: 30 000 Token; eine Botfrage zwischen 2 800 und 31 300 Token (der
#: Projektkenner mit voller Mappe ist der teure Fall).
#:
#: 2 Mio. tragen damit rund 65 Deep-Aufgaben am Tag, 500 000 rund 16 teure oder
#: 170 guenstige Botfragen — beides weit ueber allem, was ein echter Tag hier
#: gesehen hat, und eine Groessenordnung enger als die Vorgabe. Die Kappe
#: begrenzt den Schaden eines kompromittierten Kaefigs, nicht den Nutzer.
DEEP_CAPS = Caps(max_leases=4, max_inflight=6,
                 requests_per_day=5_000, tokens_per_day=2_000_000)
BOT_CAPS = Caps(max_leases=2, max_inflight=6,
                requests_per_day=5_000, tokens_per_day=500_000)

#: Der Planer der Agentenlaufzeit. Bewusst die engsten Kappen im Haus: er macht
#: hoechstens SECHS Aufrufe je Lauf (drei Planungsereignisse, je hoechstens eine
#: Nachfrage), und ein Lauf laeuft nicht in Serie. Startwerte mit
#: Kalibrierungsauftrag — eine Kappe, die nie gemessen wurde, ist eine
#: Behauptung.
AGENT_CAPS = Caps(max_leases=2, max_inflight=2,
                  requests_per_day=300, tokens_per_day=200_000)


#: Der kognitive Router. Eine Einschaetzung je Kommission, und eine Kommission
#: entsteht nur, wenn das Sprachmodell `solvio_task` ruft — nicht je Turn. 500
#: Anfragen sind weit mehr Kommissionen, als ein gesprochener Tag hier je
#: gesehen hat; 300 000 Token tragen sie bei den angehefteten 600
#: Ausgabe-Token je Aufruf mit Abstand. Startwerte mit Kalibrierungsauftrag.
COGNITION_CAPS = Caps(max_leases=2, max_inflight=2,
                      requests_per_day=500, tokens_per_day=300_000)

#: Die zwei Eskalations-Auftraggeber — die EINZIGEN, die `gpt-5.4` anfordern
#: duerfen. Ihre Kappen sind die harte Obergrenze der Tagesausgabe fuer das
#: teure Modell: nicht eine Absicht, sondern eine Zahl. `max_inflight=1` ist
#: ausdruecklich Teil davon — zwei Bahnen, die im selben Augenblick
#: eskalieren wollen, reihen sich; die zweite nimmt den gekappten Rueckfall.
#:
#: Sie borgen NIE bei den Mini-Auftraggebern. Ist die Kappe erschoepft, laeuft
#: das Ereignis so aus, wie es ohne Eskalation ausgelaufen waere — nie
#: schlechter als der Stand vor diesem Milestone.
COGNITION_ESCALATION_CAPS = Caps(max_leases=2, max_inflight=1,
                                 requests_per_day=40, tokens_per_day=200_000,
                                 allowed_models=frozenset({"gpt-5.4"}))
AGENT_ESCALATION_CAPS = Caps(max_leases=2, max_inflight=1,
                             requests_per_day=20, tokens_per_day=150_000,
                             allowed_models=frozenset({"gpt-5.4"}))

#: Der Technical Lead des Entwicklungs-Autopiloten. Er urteilt einmal je Runde,
#: und eine Runde dauert Minuten bis Stunden — 200 Anfragen sind weit mehr
#: Runden, als ein Tag hier je gesehen hat. Die Kappe ist bewusst enger als
#: DEEP_CAPS: der Lead liest Delta und Evidence, nicht Transkripte.
#:
#: Warum er ueberhaupt hier steht statt an einem CLI: Builder-Verfuegbarkeit
#: und Technical-Lead-Verfuegbarkeit duerfen nicht gemeinsam ausfallen. Der
#: Builder haengt an einem Abo-CLI, der Lead am Broker — zwei Anbieter, zwei
#: Kontingente. Startwerte mit Kalibrierungsauftrag.
AUTOPILOT_LEAD_CAPS = Caps(max_leases=2, max_inflight=2,
                           requests_per_day=200, tokens_per_day=400_000)

#: Die Eskalationsstufe des Leads — der dritte und letzte Auftraggeber, der
#: `gpt-5.4` anfordern darf. Enger als beide anderen: eine Architektur- oder
#: Root-Cause-Entscheidung ist selten, und wenn sie oft noetig waere, ist nicht
#: die Kappe das Problem.
AUTOPILOT_LEAD_ESCALATION_CAPS = Caps(max_leases=1, max_inflight=1,
                                      requests_per_day=15, tokens_per_day=120_000,
                                      allowed_models=frozenset({"gpt-5.4"}))

#: Der schreibende Claude-Builder (Development Autopilot V0.6). Er spricht die
#: **Anthropic**-Flaeche, nicht die OpenAI-Flaeche — die Modellnamen sind
#: deshalb andere, und ein Auftraggeber, der einen OpenAI-Namen nennt, faellt
#: am Modelltor.
#:
#: Die Kappen sind bewusst grosszuegiger als beim Lead und bewusst nicht
#: unbegrenzt: eine Bauphase ist ein langes Gespraech mit vielen Werkzeugrunden
#: (die gemessene Codex-Phase lief 30 Minuten), aber ein Builder, der in einer
#: Nacht das ganze Abo-Kontingent verbrennt, hat den Eigentuemer aus seinem
#: eigenen Werkzeug verdraengt. Die Zahlen sind ein Startwert MIT
#: Messauftrag — die Live-Abnahme zieht sie auf das Gemessene.
AUTOPILOT_WRITER_CLAUDE_CAPS = Caps(
    max_leases=2, max_inflight=2, requests_per_day=2_000,
    tokens_per_day=8_000_000,
    allowed_models=frozenset({"claude-sonnet-5"}))

#: Die Eskalationsstufe des Schreibers. Wie ueberall im Haus gilt: **der
#: Auftraggeber ist der Zugang, der Modellname keine Berechtigung.** Wer
#: `claude-opus-5` bauen lassen will, braucht diesen Token — und den haelt
#: ausschliesslich Core-Code.
AUTOPILOT_WRITER_CLAUDE_ESCALATION_CAPS = Caps(
    max_leases=1, max_inflight=1, requests_per_day=400,
    tokens_per_day=3_000_000,
    allowed_models=frozenset({"claude-opus-5"}))


#: Die Kurzrecherche im selben Gespraechs-Turn (`capabilities/research_quick.py`).
#: Ihr einziger Zweck ist, EINE aktuelle Frage waehrend des Gespraechs zu
#: beantworten — die native Websuche des Anbieters fuehrt aus, SOLVIO haelt
#: Auftrag, Kappe, Vertrauen und Wahrheit. `web_search` ist das EINZIGE
#: anbieterseitige Werkzeug, das dieser Auftraggeber nennen darf; jedes
#: andere (code_interpreter, file_search, computer_use, image_generation,
#: mcp, ...) faellt am Broker-Tor mit `provider_tool_not_allowed`. Startwerte
#: mit Kalibrierungsauftrag, gemessen an der Live-Abnahme vom 2026-09-02
#: (eine echte Anfrage, HTTP 200 in 4,36 s).
RESEARCH_QUICK_CAPS = Caps(max_leases=2, max_inflight=2,
                           requests_per_day=300, tokens_per_day=1_500_000,
                           allowed_models=frozenset({"gpt-5.4-mini"}),
                           allowed_provider_tools=frozenset({"web_search"}))

# Native Dokumentanalyse. Keine anbieterseitigen Werkzeuge: `input_file` ist
# ein Inhaltsteil der Responses-Anfrage, kein Werkzeug und kein Files-Endpunkt.
DOCUMENT_CAPS = Caps(max_leases=2, max_inflight=2,
                     requests_per_day=200, tokens_per_day=2_000_000,
                     allowed_models=frozenset({"gpt-5.4-mini"}),
                     allowed_provider_tools=frozenset())

#: Der Adaptiv-Extraktor (`memory/adaptive/extractor.py`). Er stellt hoechstens
#: EINEN Vorschlag je finalisiertem Nutzerturn und braucht deshalb nie mehr als
#: ein Lease und einen gleichzeitigen Aufruf. `gpt-4.1-mini` ist das einzige
#: erlaubte Modell — dieselbe Enge wie bei jedem anderen Auftraggeber im Haus:
#: der Auftraggeber ist der Zugang, der Modellname keine Berechtigung. Startwert
#: mit Kalibrierungsauftrag.
ADAPTIVE_EXTRACTOR_CAPS = Caps(max_leases=1, max_inflight=1,
                               requests_per_day=400, tokens_per_day=200_000,
                               allowed_models=frozenset({"gpt-4.1-mini"}))


class CapExceeded(RuntimeError):
    """Eine Kappe steht im Weg. Traegt den Grund, der ins Buch gehoert."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Lease:
    """Ein Zeitfenster fuer genau einen Auftrag."""

    lease_id: str
    principal: str
    ref: str
    deadline: float

    def expired(self, now: float) -> bool:
        return now >= self.deadline


@dataclass
class Principal:
    """Ein Auftraggeber: sein Token, seine Generation, seine Buchhaltung."""

    name: str
    caps: Caps
    token: str = ""
    generation: int = 0
    leases: dict[str, Lease] = field(default_factory=dict)
    inflight: int = 0
    #: UTC-Tag, auf den sich `requests_today`/`tokens_today` beziehen.
    day: str = ""
    requests_today: int = 0
    tokens_today: int = 0

    def live_leases(self, now: float) -> int:
        return sum(1 for lease in self.leases.values() if not lease.expired(now))


def utc_day(now: float) -> str:
    """Der UTC-Tag als Zeichenkette. Die Kappe laeuft auf UTC, nicht auf der
    Ortszeit — sonst verschoebe eine Zeitumstellung die Tagesgrenze."""
    return time.strftime("%Y-%m-%d", time.gmtime(now))


class Registry:
    """Die Registratur. Nur im Speicher — ein Core-Neustart entwertet alles.

    Das ist Absicht und keine Nachlaessigkeit: ein Token, der einen Neustart
    ueberlebt, ist genau der wiederverwendbare Zugang, den dieser Milestone
    abschafft.
    """

    def __init__(self) -> None:
        self._principals: dict[str, Principal] = {}
        self._by_token: dict[str, str] = {}

    # ---- Steuerebene ---------------------------------------------------

    def register(self, name: str, *, caps: Caps | None = None) -> str:
        """Praegt einen frischen Token fuer DIESEN Namen und liefert ihn zurueck.

        Der alte Token dieses Namens verliert damit seine Gueltigkeit; die Token
        aller anderen Namen bleiben unberuehrt.
        """
        existing = self._principals.get(name)
        chosen = caps or (existing.caps if existing else _default_caps(name))
        if existing is None:
            existing = Principal(name=name, caps=chosen)
            self._principals[name] = existing
        else:
            existing.caps = chosen
        if existing.token:
            self._by_token.pop(existing.token, None)
        existing.token = mint_token()
        existing.generation += 1
        self._by_token[existing.token] = name
        log.info("broker.principal_registered", principal=name,
                 generation=existing.generation)
        return existing.token

    def principal(self, name: str) -> Principal | None:
        return self._principals.get(name)

    def names(self) -> list[str]:
        return sorted(self._principals)

    def resolve(self, presented: str) -> Principal | None:
        """Token → Auftraggeber, in konstanter Zeit verglichen.

        Der Wortlaut zaehlt: verglichen wird gegen JEDEN eingetragenen Token mit
        `hmac.compare_digest`, und es wird nicht frueh abgebrochen. Ein
        Woerterbuchtreffer waere schneller — und seine Laufzeit haengt am Inhalt
        des Geheimnisses.
        """
        if not presented:
            return None
        found: Principal | None = None
        for token, name in self._by_token.items():
            if hmac.compare_digest(token, presented):
                found = self._principals.get(name)
        return found

    # ---- Leases --------------------------------------------------------

    def open_lease(self, name: str, ref: str, *, deadline: float,
                   now: float) -> str:
        principal = self._principals.get(name)
        if principal is None:
            raise CapExceeded("lease_absent")
        self._expire(principal, now)
        if principal.live_leases(now) >= principal.caps.max_leases:
            raise CapExceeded("rate_capped")
        lease_id = "lease-" + secrets.token_hex(8)
        principal.leases[lease_id] = Lease(lease_id=lease_id, principal=name,
                                           ref=ref, deadline=deadline)
        log.info("broker.lease_opened", principal=name, ref=ref,
                 live=principal.live_leases(now))
        return lease_id

    def close_lease(self, lease_id: str, *, now: float) -> str:
        """Schliesst ein Lease und meldet den Auftraggeber, wenn er auf null faellt.

        Liefert den Namen des Auftraggebers zurueck, wenn dieser Schluss der
        letzte war — dann rotiert der Broker. Sonst leere Zeichenkette.
        """
        for principal in self._principals.values():
            if lease_id in principal.leases:
                principal.leases.pop(lease_id, None)
                self._expire(principal, now)
                remaining = principal.live_leases(now)
                log.info("broker.lease_closed", principal=principal.name,
                         live=remaining)
                return principal.name if remaining == 0 else ""
        return ""

    def has_live_lease(self, name: str, *, now: float) -> bool:
        principal = self._principals.get(name)
        if principal is None:
            return False
        self._expire(principal, now)
        return principal.live_leases(now) > 0

    def lease_ref(self, name: str, *, now: float) -> tuple[str, str]:
        """Irgendein lebendes Lease dieses Auftraggebers, fuer die Buchzeile.

        Auf dem Draht liegt nur der Token — eine Lease-Kennung kann nicht
        mitreisen, ohne den angehefteten Hermes zu patchen. Die Zuordnung im
        Buch ist deshalb „eines der offenen", nicht „genau dieses". Das steht
        so auch im Bedrohungsmodell.
        """
        principal = self._principals.get(name)
        if principal is None:
            return "", ""
        for lease in principal.leases.values():
            if not lease.expired(now):
                return lease.lease_id, lease.ref
        return "", ""

    # ---- Kappen --------------------------------------------------------

    def admit(self, principal: Principal, *, now: float) -> None:
        """Tor 3: die bereits gebuchte Summe gegen die Kappen."""
        self._roll_day(principal, now)
        if principal.inflight >= principal.caps.max_inflight:
            raise CapExceeded("rate_capped")
        if principal.requests_today >= principal.caps.requests_per_day:
            raise CapExceeded("rate_capped")
        if principal.tokens_today >= principal.caps.tokens_per_day:
            raise CapExceeded("token_capped")

    def precharge(self, principal: Principal, *, estimate: int,
                  now: float) -> None:
        """Tor 6: die Schaetzung gegen die Tokenkappe — und dann gebucht.

        Reisst `gebucht + Schaetzung` die Kappe, wird **ohne** Anruf nach
        draussen abgelehnt.
        """
        self._roll_day(principal, now)
        if principal.tokens_today + estimate > principal.caps.tokens_per_day:
            raise CapExceeded("token_capped")
        principal.tokens_today += estimate
        principal.requests_today += 1

    def replace_estimate(self, principal: Principal, *, estimate: int,
                         reported: int, now: float) -> None:
        """Das gemeldete `usage` ersetzt die Schaetzung — nach oben wie unten.

        **Ersetzt**, nicht addiert, und nie zurueckgebucht: bleibt `usage` aus,
        bleibt die Schaetzung stehen. Genau das ist der Unterschied zwischen
        einer Kappe und einer Statistik.
        """
        self._roll_day(principal, now)
        principal.tokens_today = max(0, principal.tokens_today - estimate + reported)

    def seed_day(self, principal: Principal, *, requests: int, tokens: int,
                 now: float) -> None:
        """Uebernimmt den heutigen Verbrauch aus dem Buch nach einem Neustart."""
        principal.day = utc_day(now)
        principal.requests_today = max(0, int(requests))
        principal.tokens_today = max(0, int(tokens))

    # ---- Innenleben ----------------------------------------------------

    def _roll_day(self, principal: Principal, now: float) -> None:
        today = utc_day(now)
        if principal.day != today:
            principal.day = today
            principal.requests_today = 0
            principal.tokens_today = 0

    def _expire(self, principal: Principal, now: float) -> None:
        stale = [key for key, lease in principal.leases.items() if lease.expired(now)]
        for key in stale:
            principal.leases.pop(key, None)
        if stale:
            log.info("broker.lease_expired", principal=principal.name,
                     count=len(stale))


def _default_caps(name: str) -> Caps:
    # Die Namen stehen hier als Zeichenketten und nicht als Konstanten aus
    # `service.py`: dieses Modul kennt den Dienst nicht, und das soll so
    # bleiben.
    if name.startswith("bot:"):
        return BOT_CAPS
    if name == "agent-runtime":
        return AGENT_CAPS
    if name == "agent-runtime-escalation":
        return AGENT_ESCALATION_CAPS
    if name == "cognitive-router":
        return COGNITION_CAPS
    if name == "cognitive-router-escalation":
        return COGNITION_ESCALATION_CAPS
    if name == "autopilot-lead":
        return AUTOPILOT_LEAD_CAPS
    if name == "autopilot-lead-escalation":
        return AUTOPILOT_LEAD_ESCALATION_CAPS
    if name == "autopilot-writer-claude":
        return AUTOPILOT_WRITER_CLAUDE_CAPS
    if name == "autopilot-writer-claude-escalation":
        return AUTOPILOT_WRITER_CLAUDE_ESCALATION_CAPS
    if name == "adaptive-extractor":
        return ADAPTIVE_EXTRACTOR_CAPS
    if name == "research-quick":
        return RESEARCH_QUICK_CAPS
    return DEEP_CAPS
