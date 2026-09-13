"""Der eine Weg, auf dem eine Faehigkeit ausgefuehrt wird.

Reihenfolge ist hier Sicherheit, nicht Geschmack:

    aufloesen -> Argumente pruefen -> Risiko aus Herkunft -> AUTORITAET -> Freigabe
              -> ausfuehren -> Ausgang ehrlich benennen

Autoritaet wird geprueft, BEVOR eine Freigabe angefordert wird. Sonst koennte ein
fremder Text (E-Mail, Webseite) den Nutzer mit Bestaetigungsfragen bombardieren, die
er nie ausgeloest hat — die Ablehnung gehoert vor die Frage, nicht dahinter.

Der Router erzeugt keine Autoritaet und haelt keine. Er ruft den bestehenden
`ApprovalBroker` auf; ohne Broker ist er fail-closed. Das Modell erreicht ihn nur
ueber Argumente — und Argumente sind hier nie Autoritaet.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import inspect
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from solvio.capabilities.contract import (
    AmbiguousExecution, ArgumentSource, CapabilityDeclined, CapabilityError,
    CapabilityRefused, CapabilitySpec, ExecutorUnavailable, authority_refusal,
    effective_risk, requires_approval,
)
from solvio.capabilities.envelope import CapabilityOutcome, CapabilityResult
from solvio.contracts.trust import TrustContext
from solvio.logging_setup import get_logger
from solvio.capabilities import policy as P
from solvio.capabilities import preauth as PA
from solvio.security.approval import ApprovalLimitError, action_digest
from solvio.security.mobile_approval.execution import (
    EXTERNAL_PENDING, MANUAL_RECOVERY_REQUIRED, recovery_decision,
)
from solvio.tools.base import RiskLevel

log = get_logger("capabilities")

_JSON_TYPES = {
    "string": str, "number": (int, float), "integer": int,
    "boolean": bool, "object": dict, "array": list,
}


@dataclass(frozen=True)
class CapabilityEvent:
    """Ein Lebenszyklus-Ereignis. Identitaet ist die Sequenz, nie die Uhrzeit.

    Zeitstempel taugen nicht als Identitaet: sie kollidieren, springen und sind nach
    einem Neustart nicht monoton. Ein HUD, ein Audit und eine Approval-UI brauchen
    eine Ordnung, auf die sie sich verlassen koennen — deshalb zaehlt hier ein
    Zaehler und nicht die Uhr.
    """
    sequence: int
    call_id: str
    capability: str
    phase: str          # requested | rejected | approval_required | started | finished
    outcome: str = ""
    risk: int = 0
    detail: str = ""


def canonical_binding(spec: CapabilitySpec, arguments: Mapping[str, Any]) -> str:
    """Die kanonische Beschreibung dessen, was ausgefuehrt werden soll.

    Genau dieser String wird vom Broker gehasht. Aendert sich ein Argument, aendert
    sich der Digest — und eine erteilte Freigabe passt nicht mehr. Das ist der
    Schutz gegen Drift zwischen „was der Nutzer bestaetigt hat" und „was laeuft".
    """
    payload = {"capability": spec.name, "version": spec.version,
               "arguments": dict(arguments)}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def binding_digest(spec: CapabilitySpec, arguments: Mapping[str, Any]) -> str:
    """Der Autorisierungs-Digest des Aufrufs — ueber die EINGEFRORENE Funktion.

    Kein zweites Digest-Schema: `action_digest` bringt Domain-Separation und die
    kanonische JSON-Darstellung bereits mit.
    """
    return action_digest(tool_id=spec.name, mode=spec.execution_class.value,
                         task=canonical_binding(spec, arguments), workspace="")


@contextlib.contextmanager
def _secret_context(capability: str, origin: P.OriginClass, automation_id: str,
                    *, approval_id: str = "", user_present: bool = False):
    """Bindet Herkunft und Faehigkeit an den laufenden Vorgang.

    Der Geheimnistresor liest das beim Ausleihen eines Wertes. Ohne diese
    Bruecke muesste jede Schicht zwischen Router und Executor eine Herkunft
    durchreichen, mit der sie nichts zu tun hat — und wo sie vergessen wuerde,
    entstuende eine Vermutung statt einer Absage. Der Vorgabewert ausserhalb
    dieses Blocks ist `UNSPECIFIED`, und der Tresor verweigert das.

    Der Import liegt in der Funktion: der Router steht weit unter dem Tresor in
    der Abhaengigkeitsordnung, und ein Import auf Modulebene waere ein Zyklus.
    """
    from solvio.secret_vault import context as SC
    token = SC.bind(SC.UseContext(
        origin=origin, capability=capability, approval_id=approval_id,
        user_present=user_present, automation_id=automation_id))
    try:
        yield
    finally:
        SC.release(token)


class CapabilityRouter:
    """Haelt die Faehigkeiten und fuehrt sie unter dem Vertrag aus."""

    def __init__(self, *, approvals: Any = None, mobile: Any = None,
                 recorder: Callable[[CapabilityEvent], None] | None = None,
                 principal: str = "voice", preauth: Any = None,
                 policy_mode: str = "enforce") -> None:
        self._specs: dict[str, CapabilitySpec] = {}
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        self._approvals = approvals
        # Der produktive Freigabeweg: das registrierte iPhone. Ist er verdrahtet,
        # laeuft JEDE freigabepflichtige Ausfuehrung durch den eingefrorenen
        # Kontrollpfad — samt durable Journal, Einmaligkeit und Recovery-Regeln.
        # Ohne ihn bleibt der bisherige, ebenfalls fail-closed Broker-Pfad.
        self._mobile = mobile
        self._recorder = recorder
        self._principal = principal
        self._sequence = 0
        self._describers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        #: Wie eine Faehigkeit ihre Klasse am AUFGELOESTEN Ziel misst. Ohne
        #: Eintrag gilt die statische Registry — die Verfeinerung ist die
        #: Ausnahme fuer alles, wo der Name nicht genug sagt (ein „Schalter"
        #: kann eine Lampe oder ein Garagentor sein).
        self._classifiers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        #: Prueft eine gebundene Vorab-Autorisierung gegen die anstehende
        #: Wirkung. Nicht verdrahtet heisst: es gibt keine — fail-closed.
        self._preauth = preauth
        #: `shadow` rechnet die V2-Entscheidung, protokolliert sie und laesst
        #: V1 entscheiden. `enforce` laesst V2 entscheiden. Der Schattenlauf
        #: ist die Bedingung des Migrationsplans: erst messen, dann wirken.
        self._policy_mode = policy_mode
        #: Welche Freigabe zu welcher Faehigkeit zuletzt angefragt wurde. Klein,
        #: aber es beantwortet die Frage, die im Vorfall offen war: gibt es zu
        #: dieser Faehigkeit noch eine aeltere, die niemand mehr fortsetzt?
        self._outstanding: dict[str, str] = {}

    # -- Registrierung -------------------------------------------------------
    def register(self, spec: CapabilitySpec,
                 handler: Callable[[dict[str, Any]], Any],
                 *, describe: Callable[[dict[str, Any]], Any] | None = None,
                 classify: Callable[[dict[str, Any]], Any] | None = None) -> None:
        """Meldet eine Faehigkeit an — und optional, wie sie sich erklaert.

        `describe` beantwortet eine Frage, die bei den bisherigen Faehigkeiten
        nicht auftrat: **was genau steht auf dem iPhone?** Bei „Licht an" sind
        die Modellargumente bereits die ganze Wahrheit. Bei einer Anmeldung auf
        einer Webseite sagt das Modell nur „diese Sitzung" — und eine Freigabe
        ueber eine Sitzungskennung waere keine.

        Also darf eine Faehigkeit ihre Argumente vor der Freigabe zu einer
        vollstaendigen Beschreibung ausbauen. Der Digest laeuft dann ueber diese
        Beschreibung, und beim Fortsetzen wird sie ERNEUT gebildet: hat sich die
        Welt zwischenzeitlich geaendert, passt der Digest nicht mehr. Drift ist
        damit dieselbe Pruefung wie bisher, nur mit mehr Wahrheit darin.
        """
        if spec.name in self._specs:
            raise ValueError(f"capability already registered: {spec.name}")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler
        if describe is not None:
            self._describers[spec.name] = describe
        if classify is not None:
            self._classifiers[spec.name] = classify

    def names(self) -> list[str]:
        return sorted(self._specs)

    def spec(self, name: str) -> CapabilitySpec | None:
        return self._specs.get(name)

    # -- Observability -------------------------------------------------------
    def _emit(self, call_id: str, capability: str, phase: str,
              outcome: str = "", risk: int = 0, detail: str = "") -> CapabilityEvent:
        self._sequence += 1
        event = CapabilityEvent(sequence=self._sequence, call_id=call_id,
                                capability=capability, phase=phase, outcome=outcome,
                                risk=risk, detail=detail)
        if self._recorder is not None:
            try:
                self._recorder(event)
            except Exception as exc:  # noqa: BLE001 - Beobachtung darf nie stoeren
                log.error("capability.recorder_failed", kind=type(exc).__name__)
        else:
            log.info("capability." + phase, capability=capability,
                     call_id=call_id, sequence=event.sequence,
                     outcome=outcome, risk=risk)
        return event

    # -- Ausfuehrung ---------------------------------------------------------
    async def execute(self, name: str, arguments: Mapping[str, Any] | None = None, *,
                      trust: TrustContext,
                      provenance: Mapping[str, ArgumentSource] | None = None,
                      approval_request_id: str | None = None,
                      principal: str = "",
                      origin: P.OriginClass = P.OriginClass.UNSPECIFIED,
                      automation_id: str = "", commanded: bool = True,
                      cancel_token: asyncio.Event | None = None) -> CapabilityResult:
        """Fuehrt eine Faehigkeit aus — oder sagt genau, warum nicht."""
        call_id = "c-" + secrets.token_hex(8)
        args = dict(arguments or {})
        spec = self._specs.get(name)
        if spec is None:
            self._emit(call_id, name, "rejected", "rejected_by_policy")
            return CapabilityResult(
                CapabilityOutcome.REJECTED_BY_POLICY, call_id, name,
                reason="unknown_capability",
                human_message="Diese Faehigkeit kenne ich nicht.")

        self._emit(call_id, spec.name, "requested")

        invalid = _validate(spec, args)
        if invalid is not None:
            self._emit(call_id, spec.name, "rejected", "invalid_input", detail=invalid)
            return CapabilityResult(
                CapabilityOutcome.INVALID_INPUT, call_id, spec.name,
                reason=invalid, human_message="Die Angaben passen nicht.")

        risk = effective_risk(spec, provenance)

        # AUTORITAET VOR FREIGABE. Fremder Inhalt bekommt keine Bestaetigungsfrage,
        # er bekommt ein Nein.
        refusal = authority_refusal(spec, trust, risk)
        if refusal is not None:
            self._emit(call_id, spec.name, "rejected", "rejected_by_policy", int(risk),
                       detail=refusal)
            return CapabilityResult(
                CapabilityOutcome.REJECTED_BY_POLICY, call_id, spec.name,
                reason=refusal, human_message=_refusal_message(refusal))

        # -- APPROVAL POLICY V2 -------------------------------------------
        #
        # Hier, und nur hier, faellt die Entscheidung, ob eine Freigabe noetig
        # ist. Vorher steht unveraendert die Autoritaetspruefung: fremder
        # Inhalt bekommt ein Nein, keine Frage. Nachher steht unveraendert der
        # eingefrorene Freigabeweg. V2 aendert das WANN, nicht das WAS.
        try:
            # DER VORGANG GILT AB HIER, NICHT ERST BEIM AUSFUEHREN.
            #
            # Die Klassifikation loest das ZIEL am echten Geraet auf — bei Home
            # Assistant heisst das: Freigabeliste und Registry holen, also den
            # Zugang benutzen. Lag die Bindung erst um `_run`, sah der Tresor
            # hier `unspecified` und verweigerte, und der Router meldete
            # `executor_unavailable`. Live gefunden: `ha_turn_on` fiel durch,
            # `ha_list_devices` nicht — weil nur das erste einen Klassifizierer
            # hat. Ein Fehler, den kein Test sah, weil kein Test mit echtem Home
            # Assistant klassifiziert.
            #
            # Sicherheitsgewinn statt Verlust: die Herkunft gilt jetzt fuer den
            # GANZEN Aufruf, und die Zugriffsspur nennt sie auch fuer die
            # Aufloesung — vorher stand dort nichts.
            with _secret_context(spec.name, origin, automation_id):
                decision = await self._classify_and_decide(
                    spec, args, origin, provenance, automation_id, call_id,
                    commanded)
        except (CapabilityDeclined, CapabilityRefused) as exc:
            # Der Klassifizierer konnte das Ziel nicht eindeutig aufloesen oder
            # verweigert es. Das ist eine Rueckfrage bzw. eine Absage, keine
            # Panne — und ausdruecklich keine Klassifikation ins Blaue. Beides
            # faellt VOR jeder Freigabefrage: der Mensch soll nie etwas
            # bestaetigen, das danach abgelehnt wird.
            return self._declined_result(exc, call_id, spec.name, risk)
        except ExecutorUnavailable as exc:
            self._emit(call_id, spec.name, "rejected", "executor_unavailable", int(risk))
            return CapabilityResult(
                CapabilityOutcome.EXECUTOR_UNAVAILABLE, call_id, spec.name,
                reason="executor_unavailable", detail=f"{type(exc).__name__}",
                human_message="Dafuer ist gerade nichts erreichbar — es ist nichts passiert.")

        if decision.decision is P.Decision.DENY:
            self._emit(call_id, spec.name, "rejected", "rejected_by_policy",
                       int(risk), detail=f"policy_denied:{decision.reason_code}")
            return CapabilityResult(
                CapabilityOutcome.REJECTED_BY_POLICY, call_id, spec.name,
                reason="policy_denied",
                human_message="Das mache ich auf diesem Weg nicht.")

        needs_approval = decision.decision is P.Decision.REQUIRE_FACE_ID
        if self._policy_mode != "enforce":
            # Schattenlauf: gerechnet und protokolliert, aber V1 entscheidet.
            needs_approval = requires_approval(risk)

        if needs_approval:
            if self._mobile is not None:
                return await self._mobile_approval(spec, args, approval_request_id,
                                                   call_id, risk, principal, origin)
            decided = self._check_approval(spec, args, approval_request_id, call_id,
                                           risk, principal)
            if decided is not None:
                return decided

        if cancel_token is not None and cancel_token.is_set():
            self._emit(call_id, spec.name, "finished", "cancelled", int(risk))
            return CapabilityResult(CapabilityOutcome.CANCELLED, call_id, spec.name,
                                    reason="cancelled_before_start",
                                    human_message="Abgebrochen.")

        self._emit(call_id, spec.name, "started", risk=int(risk))
        with _secret_context(spec.name, origin, automation_id):
            return await self._run(spec, args, call_id, risk, cancel_token)

    # -- Approval Policy V2 --------------------------------------------------
    def _declined_result(self, exc, call_id: str, name: str,
                         risk: RiskLevel) -> CapabilityResult:
        """Ein Einwand des Klassifizierers ist ein Ergebnis, keine Ausnahme."""
        if isinstance(exc, CapabilityRefused):
            self._emit(call_id, name, "rejected", "rejected_by_policy", int(risk),
                       detail=exc.reason)
            return CapabilityResult(
                CapabilityOutcome.REJECTED_BY_POLICY, call_id, name, data=exc.data,
                reason=exc.reason,
                human_message=exc.human_message or "Das mache ich nicht.")
        self._emit(call_id, name, "rejected", "invalid_input", int(risk),
                   detail=exc.reason)
        return CapabilityResult(
            CapabilityOutcome.INVALID_INPUT, call_id, name, data=exc.data,
            reason=exc.reason,
            human_message=exc.human_message or "Damit kann ich nichts anfangen.")

    async def _classify(self, spec: CapabilitySpec,
                        args: dict[str, Any]) -> P.Classification:
        """Die Klasse DIESES Aufrufs — am aufgeloesten Ziel gemessen.

        Grundlage ist die serverseitige Registry; wo der Name nicht genug sagt,
        verfeinert die Faehigkeit am echten Geraet. Ein Verfeinerer, der
        unerwartet scheitert, macht die Klasse UNBEKANNT und damit strenger —
        nie lockerer. Fail-closed ist hier keine Redensart, sondern der
        Unterschied zwischen einer Lampe und einer Haustuer.
        """
        base = P.base_class(spec.name, read_only=spec.is_read_only())
        refine = self._classifiers.get(spec.name)
        if refine is None:
            return P.Classification(P.apply_floor(base, spec.base_risk),
                                    reason="registry")
        result = refine(args)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, P.Classification):
            log.error("capability.classifier_invalid", capability=spec.name)
            return P.Classification(P.ActionClass.UNCLASSIFIED, reason="classifier_invalid")
        # Auch eine Verfeinerung darf die Selbsterklaerung der Faehigkeit nicht
        # unterbieten. Sie darf verschaerfen — mehr nicht.
        return P.Classification(P.apply_floor(result.action_class, spec.base_risk),
                                targets=result.targets, reason=result.reason)

    async def _classify_and_decide(self, spec: CapabilitySpec, args: dict[str, Any],
                                   origin: P.OriginClass, provenance,
                                   automation_id: str, call_id: str,
                                   commanded: bool = True) -> P.PolicyOutcome:
        try:
            classification = await self._classify(spec, args)
        except (CapabilityDeclined, CapabilityRefused, ExecutorUnavailable):
            # Ein Einwand oder ein abwesender Executor sind AUSSAGEN und keine
            # Pannen. Sie muessen durchreichen: „ich erreiche Home Assistant
            # nicht" ist ehrlich, „bestaetige mir das bitte mit Face ID" waere
            # es nicht — der Mensch wuerde etwas freigeben, das danach ohnehin
            # nicht laufen kann.
            raise
        except Exception as exc:  # noqa: BLE001 - fail-closed statt raten
            log.error("capability.classifier_failed", capability=spec.name,
                      kind=type(exc).__name__)
            classification = P.Classification(P.ActionClass.UNCLASSIFIED,
                                              reason="classifier_failed")

        preauth_id = ""
        if (automation_id and origin is P.OriginClass.BACKGROUND_AUTOMATION
                and classification.action_class is PA.ELIGIBLE_CLASS):
            preauth_id = await self._verify_preauth(
                automation_id, spec, args, classification, call_id)

        decision = P.decide(origin, classification.action_class,
                            capability=spec.name, provenance=provenance,
                            commanded=commanded, preauthorization_id=preauth_id)
        # Ein Ereignis je Entscheidung — Herkunft, Klasse, Zelle, Grund. Kein
        # Transkript, keine Argumente: was der Mensch bestaetigt, steht dort,
        # wo es hingehoert, naemlich im signierten Freigabetext.
        log.info("capability.policy_decision", capability=spec.name,
                 origin_class=decision.origin.value,
                 action_class=decision.action_class.value,
                 decision=decision.decision.value,
                 reason_code=decision.reason_code, call_id=call_id,
                 preauthorization=decision.preauthorization_id or "",
                 mode=self._policy_mode)
        return decision

    async def _verify_preauth(self, automation_id: str, spec: CapabilitySpec,
                              args: dict[str, Any], classification: P.Classification,
                              call_id: str) -> str:
        """Traegt eine gebundene Erlaubnis genau DIESE anstehende Wirkung?

        Nicht „die Aufgabe existierte" — das ist keine Autorisierung. Geprueft
        wird die Wirkung: Faehigkeit, aufgeloestes Geraet, gemessene Klasse,
        Argumente, Zeitplan. Ohne verdrahteten Pruefer gibt es keine Erlaubnis.
        """
        if self._preauth is None:
            return ""
        try:
            return await self._preauth.verify(
                automation_id=automation_id, capability=spec.name,
                action_class=classification.action_class, targets=classification.targets,
                arguments=args)
        except PA.PreauthorizationError as exc:
            log.info("capability.preauth_rejected", capability=spec.name,
                     automation=automation_id, reason=exc.reason, call_id=call_id)
            return ""
        except Exception as exc:  # noqa: BLE001 - ein Fehler ist keine Erlaubnis
            log.error("capability.preauth_failed", capability=spec.name,
                      kind=type(exc).__name__, call_id=call_id)
            return ""

    # -- Freigabe ueber das registrierte iPhone -------------------------------
    async def _mobile_approval(self, spec: CapabilitySpec, args: dict[str, Any],
                               approval_id: str | None, call_id: str,
                               risk: RiskLevel, principal: str,
                               origin: P.OriginClass = P.OriginClass.UNSPECIFIED
                               ) -> CapabilityResult:
        """Anfordern oder fortsetzen — die Entscheidung faellt auf dem Geraet.

        Der Router fuehrt hier NICHT selbst aus. Er uebergibt den Handler an den
        eingefrorenen Kontrollpfad, weil dort die Dinge liegen, die eine Freigabe
        erst belastbar machen: der durable Anspruch vor dem externen Aufruf, die
        Einmaligkeit der Bestaetigung und die Regel, was nach einem unklaren
        Ausgang passieren darf.
        """
        handler = self._handlers[spec.name]
        try:
            shown = await self._describe(spec, args)
        except CapabilityDeclined as exc:
            # Der Beschreiber sagt ehrlich, dass er die Aktion nicht mehr
            # beschreiben kann — etwa weil die Seite inzwischen eine andere ist.
            # Das ist ein Ergebnis, keine Ausnahme: an dieser Grenze steht das
            # Modell, und dorthin gehoert ein Umschlag, kein Stacktrace.
            self._emit(call_id, spec.name, "rejected", "invalid_input", int(risk),
                       detail=exc.reason)
            return CapabilityResult(
                CapabilityOutcome.INVALID_INPUT, call_id, spec.name, data=exc.data,
                reason=exc.reason,
                human_message=exc.human_message or "Damit kann ich nichts anfangen.")
        except CapabilityRefused as exc:
            self._emit(call_id, spec.name, "rejected", "rejected_by_policy", int(risk),
                       detail=exc.reason)
            return CapabilityResult(
                CapabilityOutcome.REJECTED_BY_POLICY, call_id, spec.name, data=exc.data,
                reason=exc.reason,
                human_message=exc.human_message or "Das mache ich nicht.")
        except ExecutorUnavailable as exc:
            self._emit(call_id, spec.name, "rejected", "executor_unavailable", int(risk))
            return CapabilityResult(
                CapabilityOutcome.EXECUTOR_UNAVAILABLE, call_id, spec.name,
                reason="executor_unavailable", detail=f"{type(exc).__name__}",
                human_message="Dafuer ist gerade nichts erreichbar — es ist nichts passiert.")
        except Exception as exc:  # noqa: BLE001
            self._emit(call_id, spec.name, "rejected", "rejected_by_policy", int(risk),
                       detail=f"describe_failed:{type(exc).__name__}")
            return CapabilityResult(
                CapabilityOutcome.REJECTED_BY_POLICY, call_id, spec.name,
                reason="action_not_describable",
                human_message="Ich kann dir nicht genau sagen, was passieren wuerde "
                              "— deshalb frage ich auch nicht danach.")
        if not approval_id:
            previous = self._outstanding.get(spec.name, "")
            if previous:
                # ERST NACHSEHEN, OB SIE INZWISCHEN FREIGEGEBEN WURDE.
                #
                # Live beobachtet: der Nutzer sagt „trag mir einen Termin ein",
                # bekommt die Freigabe aufs iPhone, bestaetigt sie mit Face ID —
                # und sagt es SOLVIO. Das Modell ruft die Faehigkeit daraufhin
                # erneut auf, ohne eine Kennung nennen zu koennen: der
                # Sprachpfad hat gar keine. Frueher wurde damit genau die eben
                # freigegebene Anfrage verworfen und eine neue erzeugt. Der
                # Termin entstand nie, und der Mensch hatte trotzdem
                # freigegeben.
                #
                # Nachsehen ist sicher, und zwar nicht aus Zutrauen, sondern
                # weil der eingefrorene Pfad es erzwingt: `execute_approved`
                # faellt geschlossen aus, solange die Anfrage nicht APPROVED
                # ist, und danach steht noch das S1-Gate, das eine bestaetigte
                # Entscheidung des Geraets verlangt. `resume` rechnet den Digest
                # ausserdem gegen die JETZIGEN Argumente nach — eine andere
                # Handlung als die bestaetigte laeuft als `approval_drift` ins
                # Leere.
                resumed = await self._resume_if_approved(
                    previous, spec, shown, handler, args, call_id, risk, origin)
                if resumed is not None:
                    return resumed
                # Nicht freigegeben, oder inzwischen eine andere Handlung: dann
                # gilt weiter, was vorher galt. Eine neue Anfrage derselben
                # Faehigkeit macht die vorherige gegenstandslos — zwei
                # gleichnamige Eintraege auf dem Display sind genau die Lage, in
                # der der Mensch die falsche bestaetigt, und das ist ihm nicht
                # vorzuwerfen, sondern uns.
                self._outstanding.pop(spec.name, None)
                await self._abandon(previous, "superseded_by_new_request")
            try:
                new_id = await self._mobile.request(
                    spec, shown, requested_by=principal,
                    origin_label=P.origin_label(origin))
            except Exception as exc:  # noqa: BLE001
                self._emit(call_id, spec.name, "rejected", "rejected_by_policy", int(risk),
                           detail=f"approval_request_failed:{type(exc).__name__}")
                return CapabilityResult(
                    CapabilityOutcome.REJECTED_BY_POLICY, call_id, spec.name,
                    reason="approval_unavailable",
                    human_message="Ich kann dir das gerade nicht zur Freigabe schicken.")
            self._outstanding[spec.name] = new_id
            self._emit(call_id, spec.name, "approval_required", "approval_required",
                       int(risk), detail=new_id)
            return CapabilityResult(
                CapabilityOutcome.APPROVAL_REQUIRED, call_id, spec.name,
                data={"request_id": new_id},
                reason="awaiting_user_approval",
                human_message="Ich habe dir das aufs iPhone geschickt — bitte mit "
                              "Face ID bestaetigen.")

        self._outstanding.pop(spec.name, None)
        self._emit(call_id, spec.name, "started", risk=int(risk))
        try:
            # Die Beschreibung wird hier NEU gebildet. Genau daran scheitert eine
            # Freigabe, deren Welt sich seit der Zustimmung geaendert hat.
            with _secret_context(spec.name, origin, "", approval_id=approval_id,
                                 user_present=True):
                outcome, status = await self._mobile.resume(
                    approval_id, spec, shown, _WithArguments(handler, args),
                    P.origin_label(origin))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {str(exc)[:160]}"
            self._emit(call_id, spec.name, "finished", "capability_failed", int(risk),
                       detail=detail)
            return CapabilityResult(
                CapabilityOutcome.CAPABILITY_FAILED, call_id, spec.name,
                reason="capability_failed", detail=detail,
                human_message="Dabei ging etwas schief.")
        if status == "ok":
            self._emit(call_id, spec.name, "finished", "success", int(risk))
            return CapabilityResult(CapabilityOutcome.SUCCESS, call_id, spec.name,
                                    data=(outcome or {}).get("info"))
        return self._approval_failure(spec, call_id, risk, status)

    #: Ausgaenge, die bedeuten „diese Freigabe traegt die Handlung nicht".
    #: Alles andere ist eine Aussage des eingefrorenen Pfades ueber die Welt und
    #: wird als solche zurueckgegeben, statt sie mit einer neuen Anfrage zu
    #: uebermalen.
    #:
    #: `denied` steht hier ausdruecklich NICHT. Eine Ablehnung ist eine Antwort,
    #: und eine Antwort wird nicht dadurch besser, dass man sie noch einmal
    #: einholt. Bis zur Live-Abnahme von DEBT-0126 (2026-08-30) war sie in
    #: `not_approved` unsichtbar mitenthalten, und die Folge stand auf dem
    #: Display des Eigentuemers: zweimal abgelehnt, zweimal sofort neu gefragt.
    _NOT_USABLE = ("not_approved", "unknown_approval", "approval_drift",
                   "approval_capability_mismatch")

    async def _resume_if_approved(self, approval_id: str, spec: CapabilitySpec,
                                  shown: dict[str, Any], handler: Any,
                                  args: dict[str, Any], call_id: str,
                                  risk: RiskLevel,
                                  origin: P.OriginClass = P.OriginClass.UNSPECIFIED
                                  ) -> "CapabilityResult | None":
        """Setzt eine bereits freigegebene Anfrage fort — oder sagt, dass es keine gibt.

        Gibt `None` zurueck, wenn diese Freigabe die Handlung nicht traegt: dann
        macht der Aufrufer weiter wie bisher und fragt neu an.
        """
        try:
            with _secret_context(spec.name, origin, "", approval_id=approval_id,
                                 user_present=True):
                outcome, status = await self._mobile.resume(
                    approval_id, spec, shown, _WithArguments(handler, args),
                    P.origin_label(origin))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Ein Fehler beim Nachsehen darf den gewohnten Weg nicht verbauen.
            log.info("capability.resume_probe_failed", capability=spec.name,
                     kind=type(exc).__name__)
            return None
        if status in self._NOT_USABLE:
            return None
        self._outstanding.pop(spec.name, None)
        if status == "ok":
            log.info("capability.resumed_after_approval", capability=spec.name,
                     approval_id=approval_id)
            self._emit(call_id, spec.name, "finished", "success", int(risk))
            return CapabilityResult(CapabilityOutcome.SUCCESS, call_id, spec.name,
                                    data=(outcome or {}).get("info"))
        return self._approval_failure(spec, call_id, risk, status)

    def _approval_failure(self, spec: CapabilitySpec, call_id: str, risk: RiskLevel,
                          status: str) -> CapabilityResult:
        """Uebersetzt die Statuscodes des Kontrollpfads — ohne sie zu beschoenigen."""
        if status == "unknown_outcome":
            # Der durable Rand wurde ueberschritten, die Wirkung KANN eingetreten
            # sein. Das ist die eine Aussage, die hier zaehlt.
            self._emit(call_id, spec.name, "finished", "recovery_required", int(risk),
                       detail=status)
            return CapabilityResult(
                CapabilityOutcome.RECOVERY_REQUIRED, call_id, spec.name, reason=status,
                human_message="Ich weiss nicht sicher, ob das durchging — bitte pruef "
                              "es, bevor wir es wiederholen.")
        if status == "failed_safe":
            self._emit(call_id, spec.name, "finished", "capability_failed", int(risk),
                       detail=status)
            return CapabilityResult(
                CapabilityOutcome.CAPABILITY_FAILED, call_id, spec.name, reason=status,
                human_message="Das hat nicht geklappt — es ist nichts passiert.")
        message = {
            "not_approved": "Das ist noch nicht freigegeben.",
            # Kein „noch nicht" und kein Angebot, es noch einmal zu versuchen.
            # Der Satz ist die ganze Handlung: die Anfrage ist damit erledigt,
            # und `_outstanding` ist oben gefallen. Sagt der Mensch von sich aus
            # noch einmal, was er will, entsteht eine NEUE Anfrage — ein Nein
            # sperrt die Faehigkeit nicht, es beendet nur diese eine Frage.
            "denied": "Das hast du abgelehnt. Dabei bleibt es.",
            "approval_drift": "Das ist nicht mehr die Aktion, die du bestaetigt hast.",
            "unknown_approval": "Diese Freigabe kenne ich nicht.",
            "approval_capability_mismatch": "Diese Freigabe gilt einer anderen Aktion.",
        }.get(status, "Das habe ich nicht ausgefuehrt.")
        self._emit(call_id, spec.name, "rejected", "rejected_by_policy", int(risk),
                   detail=status)
        return CapabilityResult(CapabilityOutcome.REJECTED_BY_POLICY, call_id, spec.name,
                                reason=status, human_message=message)

    # -- Freigabe (Broker-Pfad ohne Geraet) -----------------------------------
    def _check_approval(self, spec: CapabilitySpec, args: dict[str, Any],
                        approval_request_id: str | None, call_id: str,
                        risk: RiskLevel, principal: str = "") -> CapabilityResult | None:
        """`None` = freigegeben, weiter. Sonst das Ergebnis, mit dem abgebrochen wird."""
        broker = self._approvals
        if broker is None:
            # Fail-closed. Ohne Freigabekanal wird nichts Wirksames ausgefuehrt.
            self._emit(call_id, spec.name, "rejected", "rejected_by_policy", int(risk),
                       detail="no_approval_channel")
            return CapabilityResult(
                CapabilityOutcome.REJECTED_BY_POLICY, call_id, spec.name,
                reason="no_approval_channel",
                human_message="Dafuer fehlt mir ein Freigabeweg.")

        if not approval_request_id:
            try:
                request = broker.request(
                    principal=(principal or self._principal), tool=spec.name,
                    task=canonical_binding(spec, args), workspace="",
                    mode=spec.execution_class.value)
            except ApprovalLimitError:
                self._emit(call_id, spec.name, "rejected", "rejected_by_policy",
                           int(risk), detail="too_many_pending_approvals")
                return CapabilityResult(
                    CapabilityOutcome.REJECTED_BY_POLICY, call_id, spec.name,
                    reason="too_many_pending_approvals",
                    human_message="Es warten schon zu viele Freigaben.")
            self._emit(call_id, spec.name, "approval_required", "approval_required",
                       int(risk), detail=request.digest)
            # request_id ist ein IDENTIFIER, keine Autoritaet: allein bewirkt er nichts.
            return CapabilityResult(
                CapabilityOutcome.APPROVAL_REQUIRED, call_id, spec.name,
                data={"request_id": request.request_id},
                reason="awaiting_user_approval",
                human_message="Das muss ich mir erst bestaetigen lassen.")

        taken, status = broker.take_approved(request_id=approval_request_id,
                                             tool=spec.name)
        if taken is None:
            self._emit(call_id, spec.name, "rejected", "rejected_by_policy", int(risk),
                       detail=status)
            return CapabilityResult(
                CapabilityOutcome.REJECTED_BY_POLICY, call_id, spec.name,
                reason=f"approval_{status}",
                human_message="Diese Freigabe gilt nicht.")

        # Drift: die Freigabe galt fuer eine bestimmte Aktion. Passt der Aufruf jetzt
        # nicht mehr dazu, ist die Freigabe verbraucht und wertlos — nicht uebertragbar.
        if not secrets.compare_digest(taken.digest, binding_digest(spec, args)):
            self._emit(call_id, spec.name, "rejected", "rejected_by_policy", int(risk),
                       detail="approval_drift")
            return CapabilityResult(
                CapabilityOutcome.REJECTED_BY_POLICY, call_id, spec.name,
                reason="approval_drift",
                human_message="Das ist nicht mehr die Aktion, die du bestaetigt hast.")
        return None

    # -- Lauf ----------------------------------------------------------------
    async def _run(self, spec: CapabilitySpec, args: dict[str, Any], call_id: str,
                   risk: RiskLevel, cancel_token: asyncio.Event | None) -> CapabilityResult:
        handler = self._handlers[spec.name]
        try:
            if cancel_token is not None and spec.cancellable:
                data = await _run_cancellable(handler, args, spec.timeout, cancel_token)
            else:
                data = await asyncio.wait_for(_call(handler, args), timeout=spec.timeout)
        except asyncio.CancelledError:
            # Von aussen abgebrochen: nie schlucken, sonst stirbt die Task-Hierarchie
            # nicht sauber (M0-Lehre).
            self._emit(call_id, spec.name, "finished", "cancelled", int(risk))
            raise
        except _Cancelled:
            self._emit(call_id, spec.name, "finished", "cancelled", int(risk))
            return CapabilityResult(CapabilityOutcome.CANCELLED, call_id, spec.name,
                                    reason="cancelled", human_message="Abgebrochen.")
        except TimeoutError:
            return self._ambiguous(spec, call_id, risk, CapabilityOutcome.TIMEOUT,
                                   "timeout", "Das hat zu lange gedauert.")
        except CapabilityDeclined as exc:
            self._emit(call_id, spec.name, "finished", "invalid_input", int(risk),
                       detail=exc.reason)
            return CapabilityResult(
                CapabilityOutcome.INVALID_INPUT, call_id, spec.name, data=exc.data,
                reason=exc.reason,
                human_message=exc.human_message or "Damit kann ich nichts anfangen.")
        except CapabilityRefused as exc:
            self._emit(call_id, spec.name, "finished", "rejected_by_policy", int(risk),
                       detail=exc.reason)
            return CapabilityResult(
                CapabilityOutcome.REJECTED_BY_POLICY, call_id, spec.name, data=exc.data,
                reason=exc.reason,
                human_message=exc.human_message or "Das mache ich nicht.")
        except ExecutorUnavailable as exc:
            self._emit(call_id, spec.name, "finished", "executor_unavailable",
                       int(risk), detail=f"{type(exc).__name__}: {exc}")
            return CapabilityResult(
                CapabilityOutcome.EXECUTOR_UNAVAILABLE, call_id, spec.name,
                reason="executor_unavailable", detail=f"{type(exc).__name__}: {exc}",
                human_message="Dafuer ist gerade nichts erreichbar — es ist nichts passiert.")
        except AmbiguousExecution as exc:
            # Fallback TIMEOUT, nicht RECOVERY_REQUIRED: „kein bestaetigtes Ergebnis"
            # ist die schwaechere Aussage. Ob daraus „und niemand darf automatisch
            # wiederholen" wird, entscheidet die eingefrorene Regel unten.
            return self._ambiguous(spec, call_id, risk,
                                   CapabilityOutcome.TIMEOUT, "ambiguous_execution",
                                   "Ich habe dafuer keine Bestaetigung bekommen.",
                                   detail=f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - nie den Core crashen
            detail = f"{type(exc).__name__}: {str(exc)[:200]}"
            self._emit(call_id, spec.name, "finished", "capability_failed", int(risk),
                       detail=detail)
            # Die rohe Exception bleibt im Log. Das Modell bekommt einen stabilen Code.
            return CapabilityResult(
                CapabilityOutcome.CAPABILITY_FAILED, call_id, spec.name,
                reason="capability_failed", detail=detail,
                human_message="Dabei ging etwas schief.")

        self._emit(call_id, spec.name, "finished", "success", int(risk))
        return CapabilityResult(CapabilityOutcome.SUCCESS, call_id, spec.name,
                                data=data)

    async def _abandon(self, approval_id: str, reason: str) -> None:
        """Zieht eine Freigabeanfrage zurueck. Fehlschlaege halten nichts auf."""
        abandon = getattr(self._mobile, "abandon", None)
        if abandon is None:
            return
        try:
            await abandon(approval_id, reason=reason)
        except Exception as exc:  # noqa: BLE001 - Aufraeumen darf nie blockieren
            log.info("capability.abandon_failed", kind=type(exc).__name__)

    async def abandon_outstanding(self, name: str = "") -> int:
        """Zieht offene Anfragen zurueck — eine Faehigkeit oder alle.

        Der Aufrufer weiss, wann er aufgibt; der Router weiss, was dann offen
        bleibt. Ohne diese Verbindung ueberlebt eine Anfrage ihren Vorgang.
        """
        names = [name] if name else list(self._outstanding)
        closed = 0
        for entry in names:
            request_id = self._outstanding.pop(entry, "")
            if request_id:
                await self._abandon(request_id, "capability_gave_up")
                closed += 1
        return closed

    async def _describe(self, spec: CapabilitySpec, args: dict[str, Any]) -> dict[str, Any]:
        """Was dem Nutzer gezeigt und damit gebunden wird."""
        describer = self._describers.get(spec.name)
        if describer is None:
            return dict(args)
        described = await _call(describer, dict(args))
        if not isinstance(described, dict):
            raise CapabilityError("describer did not return an argument mapping")
        return described

    def _ambiguous(self, spec: CapabilitySpec, call_id: str, risk: RiskLevel,
                   outcome: CapabilityOutcome, reason: str, message: str,
                   detail: str = "") -> CapabilityResult:
        """Ausgang unbekannt — die eingefrorene Recovery-Regel entscheidet.

        Nicht neu erfunden: `recovery_decision` ist dieselbe Funktion, die der
        Sicherheitspfad benutzt. Verlangt sie manuelles Eingreifen, sagt das Ergebnis
        genau das, statt Erfolg oder Fehlschlag zu behaupten.
        """
        semantics = spec.effective_semantics()
        if recovery_decision(EXTERNAL_PENDING, semantics) == MANUAL_RECOVERY_REQUIRED:
            outcome = CapabilityOutcome.RECOVERY_REQUIRED
            message = ("Ich weiss nicht sicher, ob das durchging — bitte pruef es, "
                       "bevor wir es wiederholen.")
        self._emit(call_id, spec.name, "finished", outcome.value, int(risk),
                   detail=detail or reason)
        return CapabilityResult(outcome, call_id, spec.name, reason=reason,
                                detail=detail, human_message=message)


class _WithArguments:
    """Fuehrt den Handler mit den ECHTEN Argumenten aus, nicht mit der Anzeige.

    Freigegeben wird die Beschreibung; ausgefuehrt wird die Faehigkeit. Beides
    auseinanderzuhalten ist der ganze Zweck: der Nutzer bestaetigt, was er sieht,
    und die Faehigkeit bekommt, was sie braucht.
    """

    def __init__(self, handler, arguments: dict[str, Any]) -> None:
        self._handler = handler
        self._arguments = dict(arguments)

    async def __call__(self, _shown: dict[str, Any]) -> Any:
        return await _call(self._handler, dict(self._arguments))


class _Cancelled(Exception):
    """Intern: der Cancel-Token hat gefeuert (kein asyncio-Abbruch von aussen)."""


async def _call(handler: Callable[[dict[str, Any]], Any], args: dict[str, Any]) -> Any:
    result = handler(args)
    if asyncio.iscoroutine(result):
        return await result
    return result


async def _run_cancellable(handler: Callable[[dict[str, Any]], Any], args: dict[str, Any],
                           timeout: float, cancel_token: asyncio.Event) -> Any:
    """Laeuft, bis fertig, Frist abgelaufen oder Abbruch — was zuerst kommt."""
    work = asyncio.ensure_future(_call(handler, args))
    waiter = asyncio.ensure_future(cancel_token.wait())
    try:
        done, _ = await asyncio.wait({work, waiter}, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return work.result()
        if waiter in done:
            raise _Cancelled()
        raise TimeoutError()
    finally:
        for task in (work, waiter):
            if not task.done():
                task.cancel()


def _refusal_message(reason: str) -> str:
    if reason == "untrusted_origin":
        # Der Kern des Trust-Boundary-Contracts, in einem Satz fuer den Nutzer.
        return ("Das stand in fremdem Inhalt. Ich kann es dir sagen, aber nicht "
                "danach handeln.")
    return "Dafuer fehlt mir deine ausdrueckliche Anweisung."


def _validate(spec: CapabilitySpec, args: dict[str, Any]) -> str | None:
    """Flache Schema-Pruefung. `None` = in Ordnung, sonst der Grund.

    Absichtlich klein: Pflichtfelder, grobe Typen, keine unbekannten Schluessel.
    Unbekannte Schluessel sind hier ein Fehler und keine Grosszuegigkeit — sie waeren
    ein Kanal, an der Beschreibung vorbei etwas mitzugeben.
    """
    properties = (spec.input_schema or {}).get("properties") or {}
    required = (spec.input_schema or {}).get("required") or []
    for key in required:
        if key not in args:
            return f"missing_argument:{key}"
    if properties:
        for key in args:
            if key not in properties:
                return f"unknown_argument:{key}"
            declared = properties[key].get("type")
            expected = _JSON_TYPES.get(declared)
            if expected is None:
                continue
            value = args[key]
            if declared in ("number", "integer") and isinstance(value, bool):
                return f"wrong_type:{key}"   # bool ist in Python ein int, hier nicht
            if not isinstance(value, expected):
                return f"wrong_type:{key}"
    try:
        json.dumps(args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        # Nicht serialisierbar heisst: nicht digestierbar, also nicht bindbar.
        return "unserializable_arguments"
    return None
