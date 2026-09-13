"""Der Planer schlaegt vor. Eine deterministische Core-Policy entscheidet.

Das ist dasselbe Muster wie bei Adaptive Memory, und es ist der Grund, warum
hier ein Modell ueberhaupt vorkommen darf: **die Ausgabe des Planers ist ein
Vorschlag, kein Befehl.** Zwischen dem Modell und jeder Wirkung liegt
`validate()` — und was dort nicht durchkommt, existiert nicht.

Was der Planer strukturell NICHT kann, weil das Plan-Schema die Felder nicht
hat und die Validierung sie nicht liest:

* eine Freigabe erteilen oder ein Risiko herabstufen,
* `TrustContext`, Herkunft (`origin`) oder Provenienz setzen,
* eine SecretRef-Autoritaet oder eine Zahlungsautoritaet erzeugen,
* eine Faehigkeit erfinden oder eine gesperrte nennen,
* ein Budget lockern.

**Die Aufrufinvariante, exakt:** ein gemaklerter Modellaufruf je
PLANUNGSEREIGNIS. Planungsereignisse eines Laufs sind genau eines (PLANNING)
plus hoechstens zwei REPLANNING. Ein REPLANNING wird ausschliesslich von zwei
benannten Orchestrator-Ereignissen ausgeloest — Schrittfehlschlag und
Pruefbefund —, **nie von Planerausgabe selbst**. Es gibt keine versteckte
Planerschleife. Ein schema-ungueltiges Ergebnis bekommt je Ereignis hoechstens
EINE Nachfrage; dann `FAILED (plan_invalid)`. Harte Obergrenze damit: sechs
Aufrufe je Lauf, jeder einzeln geleast, budgetiert und im Broker-Buch dem
Principal zuordenbar.

Der Weg zum Modell laeuft ueber den Provider Broker — nicht, weil der Core
keinen Schluessel haette (er hat ihn; er betreibt den Broker), sondern weil
dort die Frage „was hat ein Tag gekostet" schon beantwortet wird: Lease je
Aufruf, Kappen je Principal, eine Buchzeile je Anfrage.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from solvio.agent_runtime import authority, budget as BU
from solvio.agent_runtime.store import STEP_KINDS
from solvio.logging_setup import get_logger

log = get_logger("agent_runtime")

#: Der eigene Auftraggeber der Agentenlaufzeit im Broker.
BROKER_PRINCIPAL = "agent-runtime"

#: Das Modell, das der Planer heute benutzt. Es steht auf der globalen
#: Modell-Allowlist des Brokers; welches der Planer wirklich braucht, wird in
#: der Abnahme gemessen und danach hier gezogen.
PLANNER_MODEL = "gpt-5.4-mini"

#: Wie lange ein Lease offen bleibt. Der Planer ist ein einzelner Aufruf, kein
#: Gespraech — ein langes Fenster waere ein offenes Fenster.
LEASE_SECONDS = 120.0

#: Schrittarten, die ein PLAN vorschlagen darf. Bewusst kleiner als die
#: Schrittarten des Ledgers: `harvest` und `summary` erzeugt der Orchestrator
#: selbst, und `user_boundary` entsteht aus einer Lage, nicht aus einem Wunsch.
PLANNABLE_KINDS = frozenset({"specialist", "capability", "verify",
                             "knowledge_proposal"})

MAX_PLAN_STEPS = 12
MAX_TEXT = 600


class PlanInvalid(ValueError):
    """Der Vorschlag hat die Policy nicht ueberstanden."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"plan_invalid:{reason}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class PlannedStep:
    """Ein validierter Schritt. Alles daran ist geprueft, nichts uebernommen."""

    kind: str
    #: Bei `specialist`: der Profilschluessel. Sonst leer.
    profile: str = ""
    #: Bei `capability`: der Faehigkeitsname (bereits gegen die Sperrliste geprueft).
    capability: str = ""
    #: Der Auftragstext an den Spezialisten bzw. die Argumente der Faehigkeit.
    instruction: str = ""
    arguments: dict = field(default_factory=dict)
    #: Darf der Lauf ohne diesen Schritt zu Ende gehen?
    optional: bool = False


@dataclass(frozen=True)
class Plan:
    goal: str
    steps: tuple[PlannedStep, ...]
    note: str = ""


#: Das Schema, das dem Modell mitgegeben wird. Es hat bewusst KEINE Felder fuer
#: Risiko, Freigabe, Herkunft oder Vertrauen — ein Feld, das der
#: Vertragsschicht aehnlich saehe, wuerde frueher oder spaeter mit ihr
#: verwechselt.
PLAN_SCHEMA = {
    "type": "object",
    "required": ["schritte"],
    "properties": {
        "schritte": {
            "type": "array",
            "maxItems": MAX_PLAN_STEPS,
            "items": {
                "type": "object",
                "required": ["art"],
                "properties": {
                    "art": {"type": "string", "enum": sorted(PLANNABLE_KINDS)},
                    "profil": {"type": "string"},
                    "faehigkeit": {"type": "string"},
                    "auftrag": {"type": "string"},
                    "argumente": {"type": "object"},
                    "verzichtbar": {"type": "boolean"},
                },
            },
        },
        "hinweis": {"type": "string"},
    },
}


def validate(raw: object, *, scope: str, allowed_profiles: set[str],
             known_capabilities: set[str], goal: str) -> Plan:
    """Die deterministische Core-Policy. Sie glaubt nichts und prueft alles.

    Jede Ablehnung nennt einen Grund aus einer kleinen, festen Liste — damit
    ein Fehlschlag im Ledger eine Kategorie hat und nicht einen Satz.
    """
    if not isinstance(raw, dict):
        raise PlanInvalid("not_an_object")
    steps_raw = raw.get("schritte")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise PlanInvalid("no_steps")
    if len(steps_raw) > MAX_PLAN_STEPS:
        raise PlanInvalid("too_many_steps", str(len(steps_raw)))

    steps: list[PlannedStep] = []
    for index, entry in enumerate(steps_raw):
        if not isinstance(entry, dict):
            raise PlanInvalid("step_not_an_object", str(index))
        kind = str(entry.get("art", "")).strip()
        if kind not in PLANNABLE_KINDS:
            raise PlanInvalid("unknown_step_kind", kind)
        if kind not in STEP_KINDS:      # doppelt geprueft, absichtlich
            raise PlanInvalid("unknown_step_kind", kind)

        profile = str(entry.get("profil", "")).strip()
        capability = str(entry.get("faehigkeit", "")).strip()

        if kind == "specialist":
            if profile not in allowed_profiles:
                # Ein Profil, das es nicht gibt — oder eines, das gemessen
                # blockiert ist (der Claude-Builder). Beides ist dieselbe
                # Ablehnung: der Planer waehlt nicht, was verfuegbar ist.
                raise PlanInvalid("unknown_profile", profile)
            # Ein Bau-Schritt in einem Rechercheauftrag ist strukturell
            # unmoeglich — geprueft am SCOPE der Aufgabe, nicht an einer
            # Absichtserklaerung des Modells.
            from solvio.agent_runtime import specialists as SP
            if SP.profile(profile).mode == SP.BUILDER and scope != "build":
                raise PlanInvalid("builder_in_research_scope", profile)
        elif kind == "capability":
            reason = authority.is_blocked(capability)
            if reason:
                raise PlanInvalid("blocked_capability", f"{capability}:{reason}")
            if known_capabilities and capability not in known_capabilities:
                raise PlanInvalid("unknown_capability", capability)

        arguments = entry.get("argumente") or {}
        if not isinstance(arguments, dict):
            raise PlanInvalid("arguments_not_an_object", str(index))
        # Ein Argument, das wie ein Autoritaetsfeld heisst, wird nicht
        # stillschweigend ignoriert — es ist ein Ablehnungsgrund. Wer so etwas
        # vorschlaegt, hat den Vertrag missverstanden, und das soll auffallen.
        for forbidden in ("trust", "origin", "provenance", "approval",
                          "approval_request_id", "execution_id", "principal",
                          "commanded", "user_authorized", "risk"):
            if forbidden in arguments:
                raise PlanInvalid("authority_field_in_arguments", forbidden)

        steps.append(PlannedStep(
            kind=kind, profile=profile, capability=capability,
            instruction=str(entry.get("auftrag", ""))[:MAX_TEXT],
            arguments=arguments,
            optional=bool(entry.get("verzichtbar", False))))

    return Plan(goal=goal, steps=tuple(steps),
                note=str(raw.get("hinweis", ""))[:MAX_TEXT])


# =====================================================================
# Der gemaklerte Aufruf
# =====================================================================

def _tier_for(event_ordinal: int, attempt: int) -> str:
    """E4 und E5 — der Ereigniskatalog, an genau einer Stelle gefuehrt.

    Er wohnt in `solvio.cognition.policy`, weil dort alle fuenf
    Eskalationsereignisse zusammenstehen; er wird INNERHALB der Funktion
    importiert, damit die Laufzeit ohne den Router lauffaehig bleibt. Fehlt das
    Paket, plant sie klein — also genau so wie vor diesem Milestone.
    """
    try:
        from solvio.cognition.policy import planning_tier
        tier, _event = planning_tier(event_ordinal, attempt)
        return tier.value
    except Exception:  # noqa: BLE001 - ohne Router bleibt es beim kleinen Modell
        return "mini"


def _transport_for(tier: str) -> tuple[str, str]:
    """Stufe → (Auftraggeber, Modell).

    Der Auftraggeber ist der Zugang: `gpt-5.4` erreicht nur, wer den Token des
    Eskalations-Auftraggebers haelt, und den haelt ausschliesslich Core-Code.
    Ein Planer, der das grosse Modell bloss NENNT, bekommt vom Modelltor eine
    Absage — der Name ist keine Berechtigung.
    """
    if tier == "large":
        from solvio.provider_broker.proxy import LARGE_MODEL
        from solvio.provider_broker.service import AGENT_ESCALATION_PRINCIPAL
        return AGENT_ESCALATION_PRINCIPAL, LARGE_MODEL
    return BROKER_PRINCIPAL, PLANNER_MODEL

_INSTRUCTION = (
    "Du planst die Arbeit eines Assistenzsystems. Antworte AUSSCHLIESSLICH mit "
    "JSON nach dem gegebenen Schema, ohne Fliesstext davor oder danach.\n"
    "Du entscheidest NICHT ueber Risiko, Freigabe, Herkunft oder Vertrauen — "
    "diese Felder gibt es nicht, und ein Vorschlag, der sie enthaelt, wird "
    "verworfen.\n"
    "Waehle nur Schrittarten aus dem Schema, nur Profile aus der Liste, und "
    "nur Faehigkeiten aus der Liste. Halte den Plan so kurz wie moeglich."
)


def build_request(*, goal: str, scope: str, allowed_profiles: set[str],
                  known_capabilities: set[str], context: str = "",
                  model: str = PLANNER_MODEL) -> dict:
    """Der Rumpf des Broker-Aufrufs. Enthaelt das Ziel — nie ein Geheimnis."""
    catalogue = {
        "profile": sorted(allowed_profiles),
        "faehigkeiten": sorted(known_capabilities)[:120],
        "scope": scope,
    }
    return {
        "model": model,
        "input": [
            {"role": "system", "content": _INSTRUCTION},
            {"role": "user", "content": json.dumps(
                {"ziel": goal[:2000], "kontext": context[:2000],
                 "auswahl": catalogue, "schema": PLAN_SCHEMA},
                ensure_ascii=False)},
        ],
    }


def extract_json(text: str) -> object:
    """Holt das JSON aus der Antwort. Ein Modell rahmt gern.

    Bewusst tolerant beim RAHMEN und streng beim INHALT: ein ```json-Block oder
    ein Satz davor ist kein Sicherheitsproblem, ein erfundenes Feld schon —
    und das faengt `validate()`.
    """
    body = (text or "").strip()
    if body.startswith("```"):
        body = body.split("```", 2)[1] if body.count("```") >= 2 else body
        if body.startswith("json"):
            body = body[4:]
        body = body.strip()
    try:
        return json.loads(body)
    except ValueError:
        start, end = body.find("{"), body.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(body[start:end + 1])
            except ValueError:
                pass
    raise PlanInvalid("not_json")


async def broker_transport(payload: dict, *, token: str, port: int = 0) -> dict:
    """Der EINE Weg des Planers zum Modell — ueber den Provider Broker.

    Nicht, weil der Core keinen Schluessel haette (er hat ihn; er betreibt den
    Broker), sondern weil dort die Frage „was hat ein Tag gekostet" schon
    beantwortet wird: Lease je Aufruf, Kappen je Principal, eine Buchzeile je
    Anfrage. Der Planer traegt einen Broker-Token, keinen Anbieterschluessel —
    und ohne offenes Lease oeffnet der nichts.

    Ein Fehlschlag hier ist ein Anbieterfehler, keine Schema-Frage: er wird
    gemeldet, nicht nachgefragt.
    """
    import aiohttp

    from solvio.provider_broker.service import configured_port

    chosen = int(port) or configured_port()
    url = f"http://127.0.0.1:{chosen}/v1/responses"
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=LEASE_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload, headers=headers) as response:
                body = await response.text()
                if response.status != 200:
                    log.warning("agent_runtime.planner_rejected",
                                status=response.status)
                    return {"ok": False, "reason": f"broker_{response.status}"}
                import json as _json
                try:
                    data = _json.loads(body)
                except ValueError:
                    return {"ok": False, "reason": "broker_unreadable"}
    except aiohttp.ClientError as exc:
        return {"ok": False, "reason": f"broker_unreachable:{type(exc).__name__}"}
    except TimeoutError:
        return {"ok": False, "reason": "broker_timeout"}
    return {"ok": True, "text": response_text(data), "tokens": response_tokens(data)}


def response_text(data: object) -> str:
    """Der Text aus einer Responses-Antwort. Mehrere Formen, eine Antwort.

    Bewusst tolerant: der Anbieter darf `output_text` liefern oder die lange
    Form mit `output[].content[].text`. Was NICHT toleriert wird, ist der
    Inhalt — den prueft `validate()`.
    """
    if not isinstance(data, dict):
        return ""
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        for chunk in item.get("content") or []:
            if isinstance(chunk, dict) and isinstance(chunk.get("text"), str):
                parts.append(chunk["text"])
    if parts:
        return "".join(parts)
    # Chat-Completions-Form, falls der Broker sie einmal durchreicht.
    for choice in data.get("choices") or []:
        message = (choice or {}).get("message") or {}
        if isinstance(message.get("content"), str):
            parts.append(message["content"])
    return "".join(parts)


def response_tokens(data: object) -> int:
    if not isinstance(data, dict):
        return 0
    usage = data.get("usage") or {}
    for key in ("total_tokens", "total_token_count"):
        value = usage.get(key)
        if isinstance(value, int):
            return value
    return int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)


@dataclass
class PlannerCall:
    """Was ein einzelner Aufruf gekostet hat — fuer das Ledger."""

    ok: bool
    reason: str = ""
    tokens: int = 0
    lease_id: str = ""
    elapsed: float = 0.0


class Planner:
    """Ein Aufruf je Planungsereignis, jeder mit eigenem Lease.

    Der Client wird injiziert: der Planer kennt weder `aiohttp` noch die
    Broker-Adresse aus eigener Anschauung, damit ein Test ihn ohne Netz fahren
    kann und die Laufzeit keine zweite Stelle bekommt, an der eine Basis-Adresse
    entsteht.
    """

    def __init__(self, *, broker=None, transport=None, port: int = 0) -> None:
        self.broker = broker
        # Ohne ausdruecklichen Transport der Weg ueber den Broker. Ein Test
        # reicht seinen eigenen herein und kommt damit ohne Netz aus.
        self._transport = transport if transport is not None else broker_transport
        self._port = port
        self._token = ""

    def ensure_principal(self, principal: str = BROKER_PRINCIPAL) -> str:
        """Praegt einen FRISCHEN Token — je Aufruf, nicht je Lebenszeit.

        Live gefunden: der Broker rotiert den Token, sobald das letzte Lease
        eines Auftraggebers schliesst („Schicht 3: kein Zugang ueber den Auftrag
        hinaus"). Ein gecachter Token ist ab dem zweiten Aufruf `401` — und
        genau das ist dem Planer passiert: der erste Plan ging durch, die
        Nachplanung lief in die Ablehnung.

        Der Token wird deshalb NICHT gemerkt. Das ist kein Umweg um die
        Rotation, sondern ihre bestimmungsgemaesse Benutzung: wer einen neuen
        Auftrag hat, holt sich einen neuen Zugang.
        """
        if self.broker is None:
            return ""
        self._token = self.broker.register_principal(principal)
        return self._token

    async def plan(self, *, goal: str, scope: str, allowed_profiles: set[str],
                   known_capabilities: set[str], ledger: BU.BudgetLedger,
                   run_id: str, context: str = "",
                   event_ordinal: int = 0) -> tuple[Plan, PlannerCall]:
        """EIN Planungsereignis: hoechstens zwei Aufrufe, dann ehrlich Schluss.

        Die Nachfrage ist ausdruecklich auf EINE begrenzt und ausdruecklich kein
        Gespraech: derselbe Auftrag, ein zweites Mal, mit dem Hinweis, dass die
        erste Antwort das Schema verfehlt hat.

        `event_ordinal` ist 0 fuer die erste Planung und zaehlt mit jeder
        Nachplanung hoch. Er entscheidet NICHT, wie oft gerufen wird — nur, auf
        welcher Stufe. Die Zahl der Aufrufe bleibt dieselbe wie vorher, und die
        Sechserkappe je Lauf ebenfalls.
        """
        last_reason = ""
        for attempt in (1, 2):
            ledger.check_planner()
            tier = _tier_for(event_ordinal, attempt)
            call = await self._call(goal=goal, scope=scope, run_id=run_id,
                                    allowed_profiles=allowed_profiles,
                                    known_capabilities=known_capabilities,
                                    context=context, repair=attempt == 2,
                                    hint=last_reason, tier=tier)
            ledger.note_planner_call()
            if not call.ok:
                last_reason = call.reason
                # Ein Anbieterfehler ist keine Schema-Frage: er wird nicht
                # „nachgefragt", sondern gemeldet.
                raise PlanInvalid("planner_unavailable", call.reason)
            try:
                plan = validate(call_payload(call), scope=scope,
                                allowed_profiles=allowed_profiles,
                                known_capabilities=known_capabilities, goal=goal)
                return plan, call
            except PlanInvalid as exc:
                last_reason = exc.reason
                log.info("agent_runtime.plan_rejected", run_id=run_id,
                         attempt=attempt, reason=exc.reason)
        raise PlanInvalid(last_reason or "unusable")

    async def _call(self, *, goal: str, scope: str, run_id: str,
                    allowed_profiles: set[str], known_capabilities: set[str],
                    context: str, repair: bool, hint: str,
                    tier: str = "mini") -> PlannerCall:
        """Genau ein gemaklerter Aufruf, mit eigenem Lease im `finally`.

        **Eine Stufe, die nicht durchkommt, kostet keinen zweiten Versuch aus
        dem Budget.** Ist die Eskalationskappe erschoepft oder das grosse
        Modell nicht erreichbar, laeuft DERSELBE Aufruf auf der kleinen Stufe —
        innerhalb desselben Versuchs. Ein gekappter Lauf darf nie oefter
        scheitern als vor diesem Milestone.
        """
        started = time.monotonic()
        principal, model = _transport_for(tier)
        payload = build_request(goal=goal, scope=scope,
                                allowed_profiles=allowed_profiles,
                                known_capabilities=known_capabilities,
                                context=context, model=model)
        if repair:
            payload["input"].append({
                "role": "user",
                "content": (f"Die vorige Antwort war unbrauchbar ({hint}). "
                            "Antworte NUR mit gueltigem JSON nach dem Schema.")})

        token = self.ensure_principal(principal)
        lease_id = ""
        if self.broker is not None:
            try:
                lease_id = self.broker.open_lease(
                    principal, ref=f"plan:{run_id}",
                    deadline=time.time() + LEASE_SECONDS)
            except Exception as exc:  # noqa: BLE001 - eine Kappe ist kein Absturz
                if principal != BROKER_PRINCIPAL:
                    log.info("agent_runtime.escalation_capped", run_id=run_id,
                             kind=type(exc).__name__)
                    return await self._call(
                        goal=goal, scope=scope, run_id=run_id,
                        allowed_profiles=allowed_profiles,
                        known_capabilities=known_capabilities, context=context,
                        repair=repair, hint=hint, tier="mini")
                raise
        try:
            if self._transport is None:
                return PlannerCall(False, reason="planner_transport_missing",
                                   lease_id=lease_id)
            result = await self._transport(payload, token=token, port=self._port)
            if not result.get("ok") and principal != BROKER_PRINCIPAL:
                # Das grosse Modell ging nicht. Der Lauf faellt auf die Stufe
                # zurueck, die es vor diesem Milestone gab — und das Ereignis
                # bleibt sichtbar, weil es protokolliert ist.
                log.info("agent_runtime.escalation_unavailable", run_id=run_id,
                         reason=str(result.get("reason", "")))
                if self.broker is not None and lease_id:
                    self.broker.close_lease(lease_id)
                    lease_id = ""
                return await self._call(
                    goal=goal, scope=scope, run_id=run_id,
                    allowed_profiles=allowed_profiles,
                    known_capabilities=known_capabilities, context=context,
                    repair=repair, hint=hint, tier="mini")
            call = PlannerCall(ok=bool(result.get("ok")),
                               reason=str(result.get("reason", "")),
                               tokens=int(result.get("tokens", 0)),
                               lease_id=lease_id,
                               elapsed=time.monotonic() - started)
            call.text = str(result.get("text", ""))     # type: ignore[attr-defined]
            call.tier = tier                            # type: ignore[attr-defined]
            return call
        except Exception as exc:  # noqa: BLE001
            log.warning("agent_runtime.planner_failed", run_id=run_id,
                        kind=type(exc).__name__)
            return PlannerCall(False, reason="planner_failed", lease_id=lease_id,
                               elapsed=time.monotonic() - started)
        finally:
            # Steht in einem `finally` und darf deshalb nie werfen — dieselbe
            # Regel wie beim Broker selbst.
            if self.broker is not None and lease_id:
                self.broker.close_lease(lease_id)


def call_payload(call: PlannerCall) -> object:
    """Das JSON aus einer Antwort. Getrennt, damit `plan()` testbar bleibt."""
    return extract_json(getattr(call, "text", ""))
