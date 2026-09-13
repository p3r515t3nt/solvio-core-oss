"""`/v1/agent/*` — fuenf Routen, mehr iPhone-Flaeche definiert V1 nicht.

Es entsteht hier **kein neuer Vertrauensanker**. Dieselbe TLS-Verbindung mit
gepinntem Zertifikat, dieselbe Geraeteregistrierung, dieselbe Transportkennung
wie beim Freigabeweg — und dieselbe scharfe Trennung:

* **Transportkennung darf lesen.** Sie beweist, dass die Anfrage vom
  registrierten Geraet des Besitzers kommt.
* **Transportkennung darf niemals freigeben.** Eine Freigabe braucht Face ID und
  eine frische App-Attest-Aussage. Daran aendert dieses Modul nichts, und es
  bietet auch keinen Weg dorthin an.

`cancel` und `resume` sind SOLVIO-INTERNE Buchhaltung — dieselbe Linie wie
`run_now` im Kontrollzentrum. Sie brechen einen Lauf ab oder nehmen ihn wieder
auf; sie fuehren keine Handlung nach aussen aus. Was ein Lauf danach TUT, wird
beim Ausfuehren erneut bewertet und landet, wenn es schreibt, wie immer als
Freigabe auf dem iPhone.

**Eine `ar-`-Kennung ist ein Verweis, kein Berechtigungsnachweis.** Keine Route
hier akzeptiert sie als Autoritaet: ohne Transportkennung des registrierten
Geraets gibt es 401, mit ihr gibt es genau die Buchhaltung dieses Nutzers.

Was NICHT hinausgeht: Gedankengaenge, Prompts, Rohausgaben, Geheimnisse,
Anmeldungen. Die Antworten tragen Betriebswahrheit — was laeuft, welcher
Spezialist, welcher Schritt, worauf gewartet wird, was herauskam, was
fehlschlug.
"""
from __future__ import annotations

from typing import Any

from aiohttp import web

from solvio.logging_setup import get_logger

log = get_logger("agent_runtime")

PREFIX = "/v1/agent"


def _err(status: int, code: str) -> web.Response:
    return web.json_response({"error": code}, status=status)


async def _owner_device(request: web.Request) -> str | None:
    """Dieselbe Geraetepruefung wie der Freigabeweg — nicht eine zweite.

    Bewusst der Aufruf der bestehenden Funktion und keine Kopie: eine zweite
    Fassung derselben Pruefung waere genau die Stelle, an der spaeter eine der
    beiden nachgeschaerft wird und die andere nicht.
    """
    from solvio.security.mobile_approval.gateway import _authed_device
    return await _authed_device(request)


def _orchestrator(request: web.Request):
    """Die Laufzeit — zur ANFRAGEZEIT gelesen, nicht beim Anhaengen.

    Live gefunden: der Freigabe-Gateway startet rund vier Sekunden vor dem
    Orchestrator. Ein Attach, der das Objekt einmal einsammelt, bekam deshalb
    immer `None`, haengte gar keine Route an — und `/v1/agent/runs` antwortete
    mit 404 statt mit 401. Die Routen existieren jetzt immer; ob dahinter etwas
    laeuft, entscheidet sich beim Zugriff.
    """
    provider = request.app.get("agent_runtime_provider")
    if callable(provider):
        return provider()
    return request.app.get("agent_runtime")


def _run_view(run, boundary=None) -> dict:
    """Die sichere Betriebssicht auf einen Lauf. Deutsch und ohne Rohkennungen."""
    from solvio.agent_runtime.boundaries import UserBoundary

    parsed = boundary if boundary is not None else UserBoundary.from_json(run.boundary)
    return {
        "id": run.run_id,
        "aufgabe": run.task_id,
        "zustand": _WORDS.get(run.state, run.state),
        "zustand_code": run.state,
        "begonnen": run.started_at,
        "beendet": run.finished_at,
        "ergebnis": run.result_summary,
        "grund": _REASONS.get(run.failure_category, run.failure_category),
        "spezialisten": run.specialist_count,
        "arbeitsergebnis": run.branch_ref,
        "wartet_auf": parsed.as_dict() if parsed else None,
    }


#: Zustaende in Worte. Ein Mensch liest „wartet auf deine Freigabe", nicht
#: `WAITING_APPROVAL`.
_WORDS = {
    "CREATED": "angenommen", "PLANNING": "plant", "RUNNING": "arbeitet",
    "WAITING_SPECIALIST": "wartet auf einen Spezialisten",
    "WAITING_CAPABILITY": "fuehrt etwas aus",
    "WAITING_APPROVAL": "wartet auf deine Freigabe",
    "WAITING_USER": "wartet auf dich",
    "VERIFYING": "prueft das Ergebnis",
    "SUCCEEDED": "fertig", "FAILED": "fehlgeschlagen",
    "CANCELLED": "abgebrochen", "INTERRUPTED": "unterbrochen",
}

_REASONS = {
    "plan_invalid": "kein brauchbarer Plan",
    "specialist_unavailable": "kein Spezialist erreichbar",
    "specialist_failed": "der Spezialist kam nicht durch",
    "quota": "das Kontingent ist erschoepft",
    "capability_failed": "eine Handlung ist fehlgeschlagen",
    "approval_denied": "du hast es abgelehnt",
    "approval_expired": "die Freigabefrage ist verfallen",
    "policy_denied": "aus dem Hintergrund nicht erlaubt",
    "recovery_required": "der Ausgang ist ungewiss — bitte pruefen",
    "budget_exhausted": "die Grenze war erreicht",
    "loop_detected": "es drehte sich im Kreis",
    "timeout": "es hat zu lange gedauert",
    "interrupted": "ein Neustart kam dazwischen",
    "workspace_conflict": "die Arbeitskopie liess sich nicht anlegen",
    "cancelled_by_user": "du hast abgebrochen",
    "no_result": "es kam kein Arbeitsergebnis heraus",
    "planner_invalid_step": "ich habe denselben unvollstaendigen Schritt "
                            "zweimal geplant",
}


def attach(app: web.Application, orchestrator: Any = None, *,
           provider: Any = None) -> web.Application:
    """Haengt die fuenf Routen an die bestehende Anwendung.

    `provider` ist eine Funktion, die die Laufzeit bei Bedarf liefert. Sie ist
    der normale Weg: der Gateway steht frueher als der Orchestrator, und eine
    Route, die es erst danach gaebe, gaebe es nie.
    """
    app["agent_runtime"] = orchestrator
    app["agent_runtime_provider"] = provider

    async def guard(request: web.Request):
        return await _owner_device(request)

    # -- Lesen ---------------------------------------------------------

    async def runs(request: web.Request) -> web.Response:
        if await guard(request) is None:
            return _err(401, "unauthorized")
        orch = _orchestrator(request)
        if orch is None:
            return _err(503, "agent_runtime_disabled")
        return web.json_response(
            {"laeufe": [_run_view(r) for r in orch.ledger.recent_runs(limit=25)]})

    async def run_detail(request: web.Request) -> web.Response:
        if await guard(request) is None:
            return _err(401, "unauthorized")
        orch = _orchestrator(request)
        if orch is None:
            return _err(503, "agent_runtime_disabled")
        run_id = request.match_info["run_id"]
        run = orch.ledger.get_run(run_id)
        if run is None:
            return _err(404, "unknown_run")
        view = _run_view(run)
        view["schritte"] = [
            {"folge": s.seq, "art": s.kind, "zustand": s.state,
             "spezialist": s.specialist_profile, "faehigkeit": s.capability,
             "zusammenfassung": s.summary,
             # Verweise, nie Material.
             "artefakte": s.artifact_refs, "commits": s.commit_ref}
            for s in orch.ledger.steps_for_run(run_id)]
        view["verlauf"] = [
            {"zeit": e.at, "art": e.kind, "text": e.summary}
            for e in orch.ledger.events_for_run(run_id, limit=60)]
        return web.json_response(view)

    async def running(request: web.Request) -> web.Response:
        if await guard(request) is None:
            return _err(401, "unauthorized")
        orch = _orchestrator(request)
        if orch is None:
            return _err(503, "agent_runtime_disabled")
        return web.json_response(
            {"laeufe": [_run_view(r) for r in orch.ledger.open_runs()]})

    # -- Handeln: SOLVIO-interne Buchhaltung, nie Aussenwirkung ---------

    async def cancel(request: web.Request) -> web.Response:
        if await guard(request) is None:
            return _err(401, "unauthorized")
        orch = _orchestrator(request)
        if orch is None:
            return _err(503, "agent_runtime_disabled")
        run_id = request.match_info["run_id"]
        if not await orch.cancel(run_id):
            return _err(409, "not_cancellable")
        log.info("agent_runtime.cancelled_by_user", run_id=run_id)
        return web.json_response({"id": run_id, "zustand": "abgebrochen"})

    async def resume(request: web.Request) -> web.Response:
        if await guard(request) is None:
            return _err(401, "unauthorized")
        orch = _orchestrator(request)
        if orch is None:
            return _err(503, "agent_runtime_disabled")
        run_id = request.match_info["run_id"]
        if not await orch.resume(run_id):
            return _err(409, "not_waiting")
        log.info("agent_runtime.resumed_by_user", run_id=run_id)
        return web.json_response({"id": run_id, "zustand": "arbeitet"})

    # Pfad-exakt: keine Praefixroute, kein Platzhalter fuer die Aktion. Eine
    # Route, die `/{aktion}` entgegennaehme, waere eine Stelle, an der ein
    # Tippfehler zu einer anderen Handlung wird.
    app.add_routes([
        web.get(f"{PREFIX}/runs", runs),
        web.get(f"{PREFIX}/runs/{{run_id}}", run_detail),
        web.get(f"{PREFIX}/running", running),
        web.post(f"{PREFIX}/runs/{{run_id}}/cancel", cancel),
        web.post(f"{PREFIX}/runs/{{run_id}}/resume", resume),
    ])
    log.info("agent_runtime.endpoint_attached", prefix=PREFIX, routes=5)
    return app
