"""Ein sehr schmaler Griff in den laufenden Core — fuer den Besitzer, nicht fuers Modell.

Der Anlass ist eine Altlast, die ein echter Portal-Lauf sichtbar gemacht hat: der
Freigabeweg lebt im Core-Prozess, weil die bestaetigte Entscheidung dort im
Arbeitsspeicher liegt. Wer eine freigabepflichtige Faehigkeit ausfuehren will,
muss also **im Core** sein. Ein Abnahmeskript daneben konnte das nicht — es hat
stattdessen den Core angehalten und selbst Port 8770 belegt.

Das ist die falsche Richtung. Zwischen zwei Laeufen lag dann gar kein Gateway,
und die iPhone-App, die nur beim Nachfragen etwas erfaehrt, fand nichts. Vier
gescheiterte Abnahmeversuche gingen darauf zurueck.

Also bekommt der Core einen Griff statt eines Konkurrenten. Bewusst so klein wie
moeglich:

* **Ein Unix-Socket**, kein Port. Ein lauschender Port ist fuer jeden lokalen
  Prozess erreichbar und kennt seinen Anrufer nicht; ein Unix-Socket kennt ihn,
  vom Kern, beim `connect`, nicht auf Zuruf. Dieselbe Naht wie zum Portal-
  Arbeiter, dieselbe Pruefung.
* **Zwei Vorgaenge**: Zustand melden, eine benannte Faehigkeit ausfuehren. Kein
  Code, kein Pfad, kein Werkzeugname ausser den registrierten.
* **Keine neue Autoritaet.** Der Anrufer ist der Besitzer des Rechners — er
  koennte ohnehin alles lesen. Was er hier *nicht* bekommt, ist eine Abkuerzung
  an der Freigabe vorbei: eine kritische Faehigkeit endet auch hier bei
  `approval_required` und braucht das iPhone.

Wer das liest und an eine Fernsteuerung denkt: es gibt keinen Weg von hier zu
einem beliebigen Kommando. Die Fähigkeiten sind die, die auch die Stimme hat.
"""
from __future__ import annotations

import asyncio
import os
import socket
import time
from typing import Any

from solvio.capabilities.policy import OriginClass
from solvio.contracts.trust import TrustContext, TrustLevel
from solvio.logging_setup import get_logger
from solvio.portal import protocol as P

log = get_logger("core")

#: Wo der Griff liegt. Im Zustandsverzeichnis des Besitzers, nicht in /tmp.
DEFAULT_SOCKET = os.path.expanduser("~/.solvio/control.sock")

#: Der Principal, unter dem ein oertlicher Aufruf laeuft. Ausdruecklich nicht der
#: eines Satelliten: wer hier fragt, sitzt am Rechner.
CONTROL_PRINCIPAL = "local-control"

HEALTH = "health"
RUN = "run_capability"

#: Ein Broker-Token fuer den Entwicklungs-Autopiloten praegen.
#:
#: Warum es diese Operation gibt: die Broker-Registratur lebt im
#: Core-Prozess, und ihre Tageskappen sind nur dort EINE Wahrheit. Der
#: Autopilot-Treiber laeuft aber bewusst als eigener Prozess — Baulaeufe
#: dauern Stunden und gehoeren nicht in den Core. Ohne diese Naht bliebe nur
#: eine zweite Broker-Instanz mit eigenen Kappen, und zwei Kappen sind keine
#: Kappe.
#:
#: Was hier NICHT herausgeht: ein Anbieterschluessel. Ein Broker-Token oeffnet
#: ohne offenes Lease nichts, gilt nur fuer seinen Auftraggeber und faellt
#: unter dessen Kappen. Der Anrufer ist ueber die Kennung des Sockets als der
#: Besitzer belegt — kein Modell, kein Kaefig und kein Satellit erreicht ihn.
AUTOPILOT_TOKEN = "autopilot_token"
AUTOPILOT_LEASE = "autopilot_lease"

OPERATIONS = frozenset({HEALTH, RUN, AUTOPILOT_TOKEN, AUTOPILOT_LEASE})

#: Was der Lease-Vorgang tun darf. Zwei Woerter, keine Zeichenkette aus dem
#: Aufruf — sonst waere er eine Fernbedienung fuer die Registratur.
LEASE_ACTIONS = ("open", "close")

#: Wie lange ein Lease des Autopiloten hoechstens offen bleibt. Der Anrufer
#: darf das nicht setzen: eine Frist, die der Bittsteller bestimmt, ist keine.
LEASE_SECONDS = 300.0

#: Genau die zwei Auftraggeber des Autopiloten. Eine Liste, keine Zeichenkette
#: aus dem Aufruf: sonst waere die Operation ein Token-Automat fuer beliebige
#: Namen.
AUTOPILOT_PRINCIPALS = ("autopilot-lead", "autopilot-lead-escalation",
                        # Der schreibende Claude-Builder und seine
                        # Eskalationsstufe (V0.6). Sie sprechen die
                        # Anthropic-Flaeche; ihr Token oeffnet dort nichts
                        # ohne Lease und ausserhalb der Rueckschleife gar
                        # nichts.
                        "autopilot-writer-claude",
                        "autopilot-writer-claude-escalation")

CALL_TIMEOUT = 300.0


class CoreControl:
    """Nimmt oertliche Auftraege an — und nur vom Besitzer."""

    def __init__(self, dispatcher: Any, *, socket_path: str = DEFAULT_SOCKET,
                 owner_uid: int | None = None) -> None:
        self.dispatcher = dispatcher
        self.socket_path = socket_path
        self.owner_uid = os.getuid() if owner_uid is None else owner_uid
        self._server: socket.socket | None = None
        self._task: asyncio.Task | None = None
        self._turn = 0

    async def start(self) -> None:
        directory = os.path.dirname(self.socket_path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        # 0600: nur der Besitzer. Es gibt keine Gruppe, die hier mitreden soll.
        self._server = P.bind_listener(self.socket_path, mode=0o600)
        self._server.setblocking(False)
        self._task = asyncio.create_task(self._serve())
        log.info("core.control_listening", socket=self.socket_path,
                 owner_uid=self.owner_uid)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._server is not None:
            self._server.close()
            self._server = None
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                connection, _ = await loop.sock_accept(self._server)
            except (asyncio.CancelledError, OSError):
                return
            asyncio.create_task(self._converse(connection))

    async def _converse(self, connection: socket.socket) -> None:
        loop = asyncio.get_running_loop()
        try:
            # Die Kennung zuerst, vor dem ersten Byte der Anfrage.
            P.authenticate(connection, allowed_uid=self.owner_uid)
        except P.ProtocolError as exc:
            log.warning("core.control_caller_rejected", detail=str(exc)[:80])
            connection.close()
            return
        connection.setblocking(False)
        try:
            while True:
                message = await loop.run_in_executor(None, P.decode,
                                                     _Blocking(connection))
                reply = await self.handle(message)
                await loop.sock_sendall(connection, P.encode(reply))
        except (P.ProtocolError, OSError):
            pass
        finally:
            connection.close()

    async def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        operation = str(message.get("op", ""))
        if operation not in OPERATIONS:
            return P.failure("unknown_operation", operation[:40])
        try:
            if operation == HEALTH:
                return await self._health()
            if operation == AUTOPILOT_TOKEN:
                return await self._autopilot_token(message)
            if operation == AUTOPILOT_LEASE:
                return await self._autopilot_lease(message)
            return await self._run(message)
        except Exception as exc:  # noqa: BLE001 - ein Auftrag reisst den Core nie mit
            log.error("core.control_failed", op=operation, kind=type(exc).__name__)
            return P.failure("control_failed", type(exc).__name__)

    async def _health(self) -> dict[str, Any]:
        """Was ein Abnahmelauf wissen muss, bevor er ueberhaupt anfaengt.

        Vor allem `approver`: ohne Freigabeweg endet jede kritische Faehigkeit
        bei `approval_required`, und ein Lauf, der das erst nach dem Absenden
        merkt, hat eine Wartende Freigabe erzeugt, die niemand einloest.
        """
        approver = getattr(self.dispatcher, "approver_runtime", None)
        pending: list[Any] = []
        if approver is not None:
            try:
                pending = await approver.approvals.pending()
            except Exception:  # noqa: BLE001 - Zustand melden, nicht scheitern
                pending = []
        return {"ok": True, "pid": os.getpid(),
                "capabilities": self.dispatcher.capabilities.names(),
                "approver": approver is not None,
                "gateway_port": getattr(approver, "port", 0) if approver else 0,
                "pending": len(pending),
                "portal": getattr(self.dispatcher, "portal", None) is not None}

    async def _autopilot_token(self, message: dict[str, Any]) -> dict[str, Any]:
        """Praegt einen Broker-Token fuer EINEN der zwei Autopilot-Auftraggeber.

        Die Namensliste ist geschlossen. Waere der Name frei waehlbar, koennte
        sich der Anrufer den Token des Eskalations-Auftraggebers eines anderen
        Milestones praegen — oder den des Deep-Gateways.
        """
        name = str(message.get("principal", ""))
        if name not in AUTOPILOT_PRINCIPALS:
            return P.failure("unknown_principal", name[:40])
        broker = getattr(self.dispatcher, "provider_broker", None)
        if broker is None:
            return P.failure("broker_unavailable", "")
        try:
            token = broker.register_principal(name)
        except Exception as exc:  # noqa: BLE001 - der Core reisst hier nicht ab
            log.error("core.autopilot_token_failed", kind=type(exc).__name__)
            return P.failure("mint_failed", type(exc).__name__)
        log.info("core.autopilot_token_minted", principal=name)
        return {"ok": True, "principal": name, "token": token}

    async def _autopilot_lease(self, message: dict[str, Any]) -> dict[str, Any]:
        """Oeffnet oder schliesst EIN Lease fuer einen der zwei Auftraggeber.

        Warum es das gibt: ein Broker-Token allein oeffnet nichts — ohne
        offenes Lease antwortet der Broker mit 403 `lease_absent`. Die
        Registratur lebt im Core, der Treiber aber laeuft als eigener Prozess.
        Ohne diesen Vorgang muesste er sich einen ZWEITEN Broker starten, und
        zwei Kappen sind keine Kappe: die Tageskappe des Anbieters waere
        doppelt vergeben.

        Was er ausdruecklich NICHT kann: einen fremden Auftraggeber bedienen
        (die Liste ist dieselbe geschlossene wie beim Token), eine eigene
        Frist setzen, oder ein fremdes Lease schliessen — `close` gibt nur
        weiter, was `open` vorher zurueckgegeben hat, und der Broker prueft
        die Kennung selbst.
        """
        name = str(message.get("principal", ""))
        if name not in AUTOPILOT_PRINCIPALS:
            return P.failure("unknown_principal", name[:40])
        aktion = str(message.get("action", ""))
        if aktion not in LEASE_ACTIONS:
            return P.failure("unknown_lease_action", aktion[:40])
        broker = getattr(self.dispatcher, "provider_broker", None)
        if broker is None:
            return P.failure("broker_unavailable", "")
        try:
            if aktion == "open":
                lease = broker.open_lease(
                    name, ref=f"autopilot:{name}",
                    deadline=time.time() + LEASE_SECONDS)
                log.info("core.autopilot_lease_opened", principal=name)
                return {"ok": True, "principal": name, "lease_id": lease}
            broker.close_lease(str(message.get("lease_id", "")))
            log.info("core.autopilot_lease_closed", principal=name)
            return {"ok": True, "principal": name}
        except Exception as exc:  # noqa: BLE001 - eine Kappe ist kein Absturz
            log.info("core.autopilot_lease_refused", principal=name,
                     action=aktion, kind=type(exc).__name__)
            return P.failure("lease_refused", type(exc).__name__)

    async def _run(self, message: dict[str, Any]) -> dict[str, Any]:
        """Fuehrt eine registrierte Faehigkeit im laufenden Core aus.

        Der ganze Sinn: die Freigabe laeuft ueber den Gateway, den dieser Prozess
        ohnehin bedient. Niemand muss den Core dafuer anhalten, und zwischen zwei
        Aufrufen bleibt das iPhone erreichbar.
        """
        name = str(message.get("capability", ""))
        arguments = message.get("arguments") or {}
        approval = message.get("approval_request_id") or None
        if not isinstance(arguments, dict):
            return P.failure("invalid_arguments")

        gate = self.dispatcher.capability_gate
        self._turn += 1
        # Wer am Rechner sitzt, ist der Besitzer — der Kern hat seine uid beim
        # `connect` bestaetigt. Das ist dieselbe Autoritaet wie eine gesprochene
        # Bitte, und genau wie dort entscheidet ueber eine kritische Aktion
        # weiterhin das iPhone, nicht dieser Socket.
        #
        # Die Notiz sagt ausdruecklich, woher der Turn kommt. `voice_trust()`
        # waere bequemer gewesen, haette aber „authenticated voice turn" ins
        # Journal geschrieben — und ein Journal, das die Herkunft beschoenigt,
        # ist genau dann wertlos, wenn man es braucht.
        gate.begin_turn(session_id="control", turn_id=f"c-{self._turn}",
                        principal=CONTROL_PRINCIPAL,
                        trust=TrustContext(origin_trust=TrustLevel.USER_DIRECT,
                                           user_authorized=True,
                                           note="local control socket, owner uid verified"),
                        user_text=f"Lokaler Aufruf: {name}",
                        # Der Socket beweist eine uid, keinen Menschen: JEDER
                        # Prozess unter diesem Konto erreicht ihn. Deshalb eine
                        # eigene Herkunft, die die iPhone-Zeile NICHT erbt.
                        origin=OriginClass.LOCAL_OWNER,
                        # Ein Aufruf ueber den Socket IST der Auftrag —
                        # hier gibt es keine Aeusserung zu deuten.
                        commanded=True)
        context = gate.context()
        result = await self.dispatcher.capabilities.execute(
            name, arguments, trust=context.trust,
            provenance=gate.provenance_for(arguments),
            principal=context.principal, approval_request_id=approval,
            origin=context.origin, commanded=context.commanded)
        return {"ok": result.succeeded, "outcome": result.outcome.value,
                "reason": result.reason, "message": result.human_message,
                "data": result.data}


class _Blocking:
    """Blockierender Blick auf einen nicht-blockierenden Socket (wie beim Portal)."""

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection

    def recv(self, count: int) -> bytes:
        self._connection.setblocking(True)
        try:
            return self._connection.recv(count)
        finally:
            self._connection.setblocking(False)


class ControlClient:
    """Die andere Seite — fuer Abnahmelaeufe und `solvio`-Werkzeuge."""

    def __init__(self, socket_path: str = DEFAULT_SOCKET) -> None:
        self.socket_path = socket_path

    def available(self) -> bool:
        return os.path.exists(self.socket_path)

    async def call(self, message: dict[str, Any], *,
                   timeout: float = CALL_TIMEOUT) -> dict[str, Any]:
        loop = asyncio.get_running_loop()

        def exchange() -> dict[str, Any]:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(timeout)
            connection.connect(self.socket_path)
            try:
                connection.sendall(P.encode(message))
                return P.decode(connection)
            finally:
                connection.close()

        return await asyncio.wait_for(loop.run_in_executor(None, exchange),
                                      timeout=timeout + 5)

    async def health(self) -> dict[str, Any]:
        return await self.call({"op": HEALTH}, timeout=15)

    async def run(self, capability: str, arguments: dict[str, Any] | None = None, *,
                  approval_request_id: str = "",
                  timeout: float = CALL_TIMEOUT) -> dict[str, Any]:
        return await self.call({"op": RUN, "capability": capability,
                                "arguments": arguments or {},
                                "approval_request_id": approval_request_id},
                               timeout=timeout)
