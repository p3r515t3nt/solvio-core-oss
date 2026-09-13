"""Der Sprachweg des iPhones — auf dem Weg, der schon vertraut ist.

Es entsteht hier **kein neuer Vertrauensanker und kein neuer Port**. Dieselbe
TLS-Verbindung mit gepinntem Zertifikat, dieselbe Geraeteregistrierung, dieselbe
Transportkennung wie beim Freigabeweg und beim Kontrollzentrum. Angehaengt wird
an die bereits gebaute Anwendung, statt den Freigabe-Gateway zu veraendern — das
ist genau das Vorgehen, das das Kontrollzentrum freigegeben hat
(`src/solvio/control_center/routes.py`), und aus demselben Grund: der Gateway
ist heikel genug, er soll so bleiben, wie er freigegeben wurde.

**Warum nicht der Satellitenport 8766?** Der ist Klartext — `satellite_auth.py`
sagt das selbst: „Not TLS, not a PKI, not device attestation." Ein Telefon
traegt sein Mikrofon durch die Wohnung; rohes Sprachaudio unverschluesselt zu
senden waere ein Rueckschritt hinter das, was der Freigabeweg laengst kann. Dazu
kaeme ein zweites Geheimnis, das irgendwie auf das Telefon muesste — die
Kopplungsnutzlast ist eingefroren, und den Nutzer einen Schluessel abtippen zu
lassen ist ausgeschlossen. Und die Satellitenkennung kennt keine Sperre: ein
verlorenes Telefon waere nur durch Editieren einer Datei und einen Neustart zu
entziehen. `verify_transport_cred` prueft die Sperrliste bei jedem Aufruf.

**Was die Transportkennung beweist und was nicht.** Sie beweist, dass die
Verbindung vom registrierten, attestierten, nicht gesperrten Geraet des
Besitzers kommt. Sie beweist **keine Freigabe**. Wer spricht, hat damit nichts
genehmigt: eine folgenreiche Handlung geht weiterhin als Freigabe auf das iPhone
und braucht Face ID und eine frische App-Attest-Aussage. Dieses Modul kennt den
Freigabepfad gar nicht und bietet keinen Weg dorthin an.

Dass eine Transportkennung damit auch ein *Gespraech* eroeffnen darf — und ein
Gespraech harmlose Faehigkeiten ausloesen kann — ist eine Ausweitung dessen, was
sie bisher durfte. Sie ist aufgeschrieben, nicht nebenbei passiert:
`docs/decisions/ADR-0018-iphone-ist-sprachendpunkt-nicht-autoritaet.md`.

**Was hier NICHT noch einmal gebaut wird.** Die Sitzung ist die freigegebene
`Session`, die Nachrichtenschleife ist `pump_endpoint` — beides aus
`realtime/core_server.py`, beides dasselbe, was der Satellit benutzt.
Gespraechsbesitz, Barge-in, Vorlauf, Werkzeugschleife, Wiederaufbau und Fristen
bleiben unveraendert. Was ein zweiter Endpunkt brauchte, war erstaunlich wenig,
weil `Session` von ihrem Transport nur eine einzige Methode benutzt (`ws.send`).
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

from aiohttp import WSMsgType, web

from solvio import voice_session_proof as VSP
from solvio.logging_setup import get_logger

log = get_logger("voice_endpoint")

PATH = "/v1/voice"

#: Groesse eines einzelnen Rahmens. 20 ms PCM16 bei 16 kHz sind 640 Byte; das
#: hier ist grosszuegig fuer eine Sammelsendung und trotzdem weit von allem
#: entfernt, was nach Missbrauch aussieht.
#:
#: Die Grenze MUSS hier stehen: `client_max_size` der Anwendung begrenzt
#: HTTP-Koerper, nicht WebSocket-Rahmen, und aiohttp erlaubt sonst 4 MiB. Ein
#: 4-MiB-Rahmen waere ein synchroner Resample-Lauf ueber zwei Millionen Samples
#: auf demselben Eventloop, der die Freigaben bedient.
MAX_FRAME = 64 * 1024

#: Wie oft die Berechtigung waehrend eines laufenden Gespraechs neu geprueft
#: wird.
#:
#: `verify_transport_cred` laeuft sonst genau einmal, beim Verbindungsaufbau —
#: ein gesperrtes Geraet behielte seine offene Sitzung, und eine Sperre ist
#: ausdruecklich als endgueltig gemeint. Die Pruefung ist ein Datenbankblick,
#: kein Netzverkehr; alle 20 Sekunden kostet sie nichts und schliesst genau
#: diese Luecke.
RECHECK_SECONDS = 20.0

#: Rueckfall, wenn der Server die Einstellung nicht kennt. Der Satellit
#: behaelt in jedem Fall seinen eigenen Wert.
#:
#: Der Weg dahin ging ueber `high` (staendig unterbrochen, sobald ein
#: Fernseher lief), `low` (von 19 Anlaeufen kamen 5 durch) und `medium`.
#: Zurueck bei `high`, und das ist kein Kreis: die Ruecksicht war noetig,
#: solange das Telefon ALLES weiterschickte, was sein Mikrofon hoerte.
#: Inzwischen haelt es Raumgeraeusch selbst zurueck und unterbricht selbst,
#: sobald wirklich jemand redet. Was `eagerness` jetzt noch bestimmt, ist vor
#: allem, wie lange SOLVIO wartet, bevor er glaubt, dass der Mensch fertig
#: ist — und das Warten war das, was sich langsam anfuehlte.
PHONE_EAGERNESS = "high"


class _EndpointSocket:
    """Die eine Methode, die `Session` von einem Transport braucht.

    `Session` ruft ausschliesslich `await self.ws.send(...)` auf — nachgezaehlt
    sieben Stellen, alle `send`, einmal mit JSON als Zeichenkette und einmal mit
    PCM16 als Bytes. Genau das bildet dieser Adapter ab. Eine Fassade mit mehr
    Methoden waere eine Einladung, spaeter mehr zu benutzen.

    Ein Sendeversuch auf eine schon geschlossene Verbindung ist hier kein
    Fehler: die Sitzung raeumt beim Schliessen noch auf, und ein `session_end`
    ins Leere darf den Abbau nicht kippen.
    """

    __slots__ = ("_ws", "_closed")

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self._ws = ws
        self._closed = False

    async def send(self, payload: Any) -> None:
        if self._closed or self._ws.closed:
            return
        try:
            if isinstance(payload, (bytes, bytearray, memoryview)):
                await self._ws.send_bytes(bytes(payload))
            else:
                await self._ws.send_str(str(payload))
        except (ConnectionResetError, RuntimeError) as exc:
            self._closed = True
            log.info("voice_endpoint.send_after_close", kind=type(exc).__name__)


async def _owner_device(request: web.Request) -> str | None:
    """Dieselbe Pruefung wie beim Freigabeweg — nicht eine eigene.

    Bewusst der Aufruf der bestehenden Funktion und keine Kopie: eine zweite
    Fassung derselben Pruefung waere genau die Stelle, an der spaeter eine der
    beiden nachgeschaerft wird und die andere nicht. Sie prueft Geraetestatus,
    Attestierung, Sperre, Umgebung und die Kennung in konstanter Zeit.
    """
    from solvio.security.mobile_approval.gateway import _authed_device
    return await _authed_device(request)


def principal_for(device_id: str) -> str:
    """Wer spricht — abgeleitet aus der bewiesenen Geraetekennung.

    Das ist **nicht** der Principal, unter dem eine Freigabe abgelegt wird: den
    setzt `approval_gateway` unveraendert auf den Besitzer, unabhaengig davon,
    wer gefragt hat. Dieser Name landet in `voice_trust` (bewiesener Anrufer
    ja/nein), im Protokoll und in der duennen Zusammenfassung einer Anfrage.

    Das Praefix ist Absicht: eine Telefonsitzung soll im Journal nie mit einer
    Satellitensitzung zu verwechseln sein.
    """
    short = "".join(ch for ch in (device_id or "") if ch.isalnum() or ch in "._-")[:12]
    return f"iphone-{short}" if short else "iphone"


def attach(app: web.Application, server: Any) -> web.Application:
    """Haengt den Sprachweg an die bestehende Anwendung.

    `server` ist der laufende `CoreServer`. Er wird nicht veraendert; gelesen
    werden nur sein Sitzungsriegel und die Sitzungsfabrik.
    """
    app["voice_core_server"] = server
    app.router.add_get(PATH, _handle)
    log.info("voice_endpoint.attached", path=PATH)
    return app


def _long_capability_notice(sess: Any, server: Any, ws: web.WebSocketResponse):
    """Sagt dem Endpunkt Bescheid, wenn eine LANGE Faehigkeit begonnen hat.

    „Lang" ist keine Schaetzung und kein Zeitgeber: es ist die
    Ausfuehrungsklasse `DEEP` aus dem Faehigkeitsvertrag — dieselbe Angabe, die
    auch bedeutet, dass so eine Aufgabe nicht im Sprach-Turn zu Ende laeuft.
    Genau einmal je Sitzung; eine zweite Recherche im selben Gespraech ist keine
    neue Nachricht wert.

    Die Nachricht ist AUSKUNFT. Sie traegt keinen Zustand des Cores, keine
    Kennung und keinen Fachbegriff — die App macht daraus einen Satz in
    Produktsprache.
    """
    sent = False

    async def notice(capability: str) -> None:
        nonlocal sent
        if sent:
            return
        dispatcher = getattr(server, "dispatcher", None)
        router = getattr(dispatcher, "capabilities", None)
        spec = router.spec(capability) if router is not None else None
        if spec is None or getattr(spec.execution_class, "value", "") != "deep":
            return
        sent = True
        try:
            await ws.send_str(json.dumps({"type": "notice", "kind": "deep_work"}))
        except Exception as exc:  # noqa: BLE001
            log.info("voice_endpoint.notice_failed", kind=type(exc).__name__)
        log.info("voice_endpoint.deep_work", session_id=sess.session_id,
                 capability=capability)

    return notice


async def _frames(ws: web.WebSocketResponse):
    """Die aiohttp-Nachrichten als das, was `pump_endpoint` erwartet.

    Genau zwei Sorten kommen durch: `bytes` fuer Audio und `str` fuer JSON.
    Alles andere — Schliessen, Fehler, Ping — beendet die Schleife, statt
    stillschweigend etwas anderes zu bedeuten.
    """
    async for msg in ws:
        if msg.type == WSMsgType.BINARY:
            yield msg.data
        elif msg.type == WSMsgType.TEXT:
            yield msg.data
        elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSING,
                          WSMsgType.CLOSED):
            return


async def _revocation_watch(request: web.Request, device_id: str,
                            ws: web.WebSocketResponse, session_id: str) -> None:
    """Bleibt dieses Geraet berechtigt? Solange das Gespraech laeuft, immer wieder.

    Eine Sperre soll sofort wirken und nicht erst, wenn der Mensch von selbst
    auflegt. Faellt die Pruefung, wird die Verbindung geschlossen — die Sitzung
    raeumt daraufhin im `finally` des Handlers ab.
    """
    try:
        while not ws.closed:
            await asyncio.sleep(RECHECK_SECONDS)
            if ws.closed:
                return
            if await _owner_device(request) is None:
                log.warning("voice_endpoint.revoked_mid_session",
                            device=device_id[:12], session_id=session_id)
                await ws.close(code=4401, reason="unauthorized")
                return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - die Wache darf das Gespraech nicht kippen
        log.info("voice_endpoint.recheck_failed", kind=type(exc).__name__)



async def _session_proof(request: web.Request, ws: web.WebSocketResponse,
                         device_id: str) -> bool:
    """Fordert den App-Attest-Sitzungsbeweis an und prueft ihn.

    Kein neuer Endpunkt, kein zweites Geheimnis: die Nonce geht ueber die
    bereits offene, TLS-gepinnte Verbindung, und geprueft wird mit dem
    eingefrorenen Verifizierer gegen den eingeschriebenen Schluessel.

    Ein Telefon, das den Rahmen nicht kennt, antwortet nicht — nach drei
    Sekunden laeuft das Gespraech ohne den Beweis weiter. Deshalb wartet der
    Core hier, bevor irgendetwas anderes passiert: eine spaeter eintreffende
    Antwort duerfte die Herkunft eines schon begonnenen Turns nicht mehr
    aendern.
    """
    control_plane = request.app.get("control_plane")
    if control_plane is None:
        return False
    core_id = str(getattr(control_plane, "core_instance_id", "") or "")
    if not core_id:
        return False
    nonces: VSP.SessionNonces = request.app.setdefault(
        "voice_session_nonces", VSP.SessionNonces())
    nonce = nonces.issue(device_id)
    try:
        await ws.send_str(json.dumps({"type": "session_challenge",
                                      "protocol_version": VSP.BINDING_PROTOCOL_VERSION,
                                      "core_instance_id": core_id,
                                      "session_nonce": nonce}))
        message = await asyncio.wait_for(ws.receive(), timeout=VSP.PROOF_TIMEOUT)
    except (asyncio.TimeoutError, ConnectionResetError, RuntimeError):
        log.info("voice_endpoint.proof_absent", device=device_id[:12],
                 reason="no_answer")
        return False
    if message.type is not web.WSMsgType.TEXT:
        log.info("voice_endpoint.proof_absent", device=device_id[:12],
                 reason="wrong_frame")
        return False
    try:
        payload = json.loads(message.data)
        assertion = base64.b64decode(str(payload.get("assertion", "")), validate=True)
    except Exception:  # noqa: BLE001 - eine unlesbare Antwort ist kein Beweis
        log.info("voice_endpoint.proof_absent", device=device_id[:12],
                 reason="unreadable")
        return False
    if str(payload.get("type", "")) != "session_assertion":
        return False
    # Die Nonce zaehlt genau einmal, und nur fuer das Geraet, das sie bekam.
    if not nonces.consume(str(payload.get("session_nonce", "")), device_id):
        log.info("voice_endpoint.proof_rejected", device=device_id[:12],
                 kind="nonce")
        return False
    ok = await VSP.verify_session_proof(
        control_plane, device_id=device_id, core_instance_id=core_id,
        session_nonce=nonce, assertion=assertion)
    log.info("voice_endpoint.session_proof", device=device_id[:12], proven=ok)
    return ok


async def _handle(request: web.Request) -> web.WebSocketResponse:
    """Eine Sprachverbindung vom registrierten iPhone.

    Reihenfolge mit Absicht: **erst** die Geraetepruefung, dann der
    Protokollwechsel. Ein nicht registriertes Geraet bekommt eine gewoehnliche
    HTTP-Absage und nie einen offenen Socket — dieselbe Haltung wie beim
    Satelliten, wo vor der Authentifizierung nichts Teures passiert.
    """
    peer = request.remote or "?"
    device_id = await _owner_device(request)
    if device_id is None:
        log.warning("voice_endpoint.rejected", peer=peer, reason="unauthorized")
        return web.json_response({"error": "unauthorized"}, status=401)

    server = request.app.get("voice_core_server")
    if server is None:
        log.error("voice_endpoint.no_server", peer=peer)
        return web.json_response({"error": "voice_unavailable"}, status=503)

    # Ein Gespraech zur Zeit — derselbe prozessweite Riegel wie fuer den
    # Satelliten, und aus demselben Grund: es gibt EINE Anbietersitzung und EIN
    # Gespraech. Wer zu spaet kommt, bekommt eine Auskunft und keinen rohen
    # Abbruch; die App sagt es dem Menschen so, wie es ist.
    if getattr(server, "_busy", False):
        log.info("voice_endpoint.busy", peer=peer, device=device_id[:12])
        return web.json_response({"error": "voice_busy"}, status=409)

    ws = web.WebSocketResponse(max_msg_size=MAX_FRAME, heartbeat=20.0)
    await ws.prepare(request)

    t_connected = time.monotonic()
    server._busy = True
    from solvio.realtime.core_server import Session, describe_exception, pump_endpoint

    sess = Session(server, _EndpointSocket(ws))
    sess.t_connected = t_connected
    sess.t_auth = time.monotonic()
    # Die Geraetepruefung hat diese Kennung bewiesen. Ab hier traegt die
    # Sitzung sie, damit eine Faehigkeit weiss, WER fragt — genauso wie beim
    # Satelliten, nur mit einem Namen, den man davon unterscheiden kann.
    sess.satellite_id = principal_for(device_id)
    # Ein Telefon hoert seinen Raum mit, ein Satellit steht darin. `high` laesst
    # SOLVIO auf dem Telefon bei jedem Geraeusch anhalten — gemessen mit einem
    # leise laufenden Fernseher daneben. `low` heisst nicht taub: die
    # Erkennung bleibt semantisch, sie ist nur zurueckhaltender damit, ein
    # Geraeusch fuer eine Ansprache zu halten.
    sess.eagerness = getattr(server, "phone_eagerness", PHONE_EAGERNESS)
    # Das Telefon hat einen Bildschirm. Was im Hintergrund entstanden ist,
    # steht dort im Tab „Hinweise" mit Zaehler — es muss nicht zu Beginn eines
    # Gespraechs vorgelesen werden, das der Mensch gerade mit einer Frage
    # eroeffnet hat.
    sess.mention_proactive = False
    # DIE HERKUNFTSKLASSE. Sie steht hier und nur hier, nach der bewiesenen
    # Geraetepruefung — sie ist Transportwahrheit, nie eine Angabe des Modells.
    #
    # Und sie ist nicht mehr nur fuers Protokoll: Adaptive Memory lernt
    # ausschliesslich aus dieser Klasse. Ein Telefon ist entsperrt, registriert,
    # attestiert und jemand haelt es in der Hand — ein Raummikrofon hoert
    # jeden, der zufaellig im Zimmer redet, den Fernseher eingeschlossen.
    # Belegt ist auch hier das GERAET und nicht die STIMME; der Unterschied ist
    # die Absicht, mit der eine Sitzung geoeffnet wird.
    sess.channel = "voice_iphone"
    # DER SITZUNGSBEWEIS. Er entscheidet, ob diese Sitzung die reduzierte
    # iPhone-Zeile der Freigabematrix traegt oder die vorsichtige Raum-Zeile.
    #
    # Fail-DOWN, nicht fail-closed: eine App, die nicht antwortet, bekommt ein
    # ganz gewoehnliches Gespraech — nur eben mit dem Face-ID-Verhalten, das sie
    # heute schon hat. Niemand verliert etwas, das er hatte.
    sess.interactive_proof = await _session_proof(request, ws, device_id)
    # Lange Faehigkeiten sichtbar machen — als AUSKUNFT, nicht als Zustand des
    # Cores. Kein Zeitgeber, keine Schaetzung.
    sess.on_capability_started = _long_capability_notice(sess, server, ws)
    log.info("voice_endpoint.connected", peer=peer, device=device_id[:12],
             principal=sess.satellite_id, session_id=sess.session_id)

    watch = asyncio.create_task(_revocation_watch(request, device_id, ws,
                                                  sess.session_id))
    try:
        await pump_endpoint(sess, _frames(ws))
    except Exception as exc:  # noqa: BLE001 - eine Sprachverbindung darf den Core nie kippen
        log.error("voice_endpoint.error", session_id=sess.session_id,
                  stage=sess.open_stage, **describe_exception(exc))
    finally:
        watch.cancel()
        await sess.close(reason="disconnect")
        server._busy = False
        if not ws.closed:
            await ws.close()
        log.info("voice_endpoint.disconnected", session_id=sess.session_id,
                 seconds=int(time.monotonic() - t_connected))
    return ws
