"""Der CLI-Kanarienvogel — der Dauertest, der `--bare` beim Wort nimmt.

Die ganze V0.6-Sicherheitsaussage haengt an einer gemessenen Eigenschaft eines
**fremden** Programms: `claude --bare` liest weder Schluesselbund noch OAuth
und sendet ausschliesslich die Anmeldung, die man ihm gibt. Gemessen am
2026-09-02 an CLI 2.1.222 — und die CLI aktualisiert sich selbst.

Eine Zusicherung, die diese Eigenschaft nur EINMAL geprueft hat, ist deshalb
keine Zusicherung, sondern ein Datum. Der Kanarienvogel wiederholt die Messung
bei jedem Gate-Lauf gegen einen lokalen Lauscher mit einem Wegwerf-Wert:

* Im Draht steht AUSSCHLIESSLICH der Wegwerf-Wert.
* Kein zweiter Anmeldekopf, kein OAuth, kein `sk-ant-oat`.
* Ohne Wert faellt die CLI geschlossen aus, statt auf das Abo zurueckzufallen.

**Rot heisst gesperrt, nicht gewarnt** (Vertrag §5). `verdict()` gibt genau
das zurueck, was `ClaudeWriterBuilder.blocked_reason()` daraus macht.

Er laeuft ohne echte Anmeldung und ohne Netz nach draussen: der Lauscher
bindet auf der Rueckschleife, und alles, was der Kanarienvogel prueft, sieht er
an seinen eigenen Anfragen.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer

from solvio.logging_setup import get_logger

log = get_logger("autopilot")

#: Der Wegwerf-Wert. Er sieht aus wie eine Anmeldung und ist keine — er oeffnet
#: nichts ausser diesem Lauscher.
PROBE_TOKEN = "solvio-canary-not-a-real-credential-0001"

#: Formen, die eine ECHTE Anmeldung haette. Taucht eine davon im Draht auf,
#: ist der Kanarienvogel rot — gleich, wie sie dorthin kam.
LEAK_MARKERS = ("sk-ant-", "sk-ant-oat", "oauth", "Bearer sk-", "claude.ai")

#: Koepfe, in denen eine Anmeldung reisen kann.
AUTH_HEADERS = ("authorization", "x-api-key", "proxy-authorization", "cookie")

CANARY_TIMEOUT = 90.0


@dataclass
class CanaryResult:
    """Was der Lauf gesehen hat. Kein Urteil ohne Beleg."""

    ok: bool
    reason: str = ""
    requests: int = 0
    #: Die beobachteten Anmeldewerte, maskiert. Nie der Rohwert.
    seen_auth: tuple[str, ...] = ()
    detail: str = ""
    fields: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "reason": self.reason, "requests": self.requests,
                "seen_auth": list(self.seen_auth), "detail": self.detail[:400]}


class _Listener(BaseHTTPRequestHandler):
    """Gibt sich als Anbieter aus und schreibt jede Anfrage mit."""

    seen: list[dict[str, str]] = []

    def log_message(self, *args) -> None:      # noqa: D102 - kein stderr-Rauschen
        pass

    def do_POST(self) -> None:                 # noqa: N802 - BaseHTTPRequestHandler
        laenge = int(self.headers.get("Content-Length", 0) or 0)
        try:
            self.rfile.read(laenge)
        except Exception:                      # noqa: BLE001
            pass
        eintrag = {"path": self.path}
        for name, wert in self.headers.items():
            if name.lower() in AUTH_HEADERS:
                eintrag[name.lower()] = wert
        type(self).seen.append(eintrag)
        antwort = json.dumps({
            "id": "msg_canary", "type": "message", "role": "assistant",
            "model": "canary",
            "content": [{"type": "text", "text": "KANARIENVOGEL"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(antwort)))
        self.end_headers()
        self.wfile.write(antwort)


def _mask(wert: str) -> str:
    """Ein Anmeldewert wird nie roh gemeldet — auch kein gefundener."""
    wert = (wert or "").strip()
    if not wert:
        return ""
    if wert.endswith(PROBE_TOKEN) or wert == PROBE_TOKEN:
        return "PROBE_TOKEN"
    return f"{wert[:8]}…(len={len(wert)})"


def _claude_binary() -> str:
    from solvio.specialists.launcher import LauncherError, resolve
    try:
        return resolve("claude")
    except LauncherError:
        return ""


def run(*, timeout: float = CANARY_TIMEOUT) -> CanaryResult:
    """Ein Lauf. Ohne CLI ist er **nicht gruen** — er ist ergebnislos.

    Die Unterscheidung ist wichtig: „keine CLI" heisst nicht „sicher", es
    heisst „ungemessen". Der Aufrufer entscheidet, ob ihn das sperrt; hier wird
    nichts beschoenigt.
    """
    binaer = _claude_binary()
    if not binaer:
        return CanaryResult(False, "cli_missing",
                            detail="claude nicht auffindbar — ungemessen, "
                                   "nicht sicher")

    _Listener.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Listener)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    arbeit = tempfile.mkdtemp(prefix="solvio-canary-")
    try:
        umgebung = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": os.environ.get("HOME", arbeit),
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
            "ANTHROPIC_API_KEY": PROBE_TOKEN,
        }
        argv = [binaer, "--bare", "-p", "--no-session-persistence",
                "--model", "claude-sonnet-5", "Sag KANARIENVOGEL."]
        try:
            proc = subprocess.run(argv, cwd=arbeit, env=umgebung,
                                  stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True,
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return CanaryResult(False, "cli_timeout",
                                requests=len(_Listener.seen))
        except OSError as exc:
            return CanaryResult(False, "cli_unstartable",
                                detail=type(exc).__name__)

        gesehen = list(_Listener.seen)
        werte: list[str] = []
        for eintrag in gesehen:
            for name in AUTH_HEADERS:
                if eintrag.get(name):
                    werte.append(eintrag[name])

        if not gesehen:
            # Kein Verkehr am Lauscher: entweder ist `--bare` kaputt, oder das
            # CLI ist woanders hingegangen. Beides ist rot.
            return CanaryResult(
                False, "no_traffic", requests=0,
                detail=(proc.stdout or proc.stderr or "")[:300])

        maskiert = tuple(sorted({_mask(w) for w in werte}))
        fremd = [w for w in werte
                 if PROBE_TOKEN not in w]
        if fremd:
            return CanaryResult(False, "foreign_credential",
                                requests=len(gesehen), seen_auth=maskiert,
                                detail="ein Wert im Draht war nicht der "
                                       "Wegwerf-Wert")
        verdaechtig = [m for m in LEAK_MARKERS
                       for w in werte if m.lower() in w.lower()]
        if verdaechtig:
            return CanaryResult(False, "credential_shape",
                                requests=len(gesehen), seen_auth=maskiert,
                                detail=f"Form einer echten Anmeldung: "
                                       f"{sorted(set(verdaechtig))}")
        return CanaryResult(True, "", requests=len(gesehen),
                            seen_auth=maskiert)
    finally:
        server.shutdown()
        server.server_close()
        import shutil
        shutil.rmtree(arbeit, ignore_errors=True)


def verdict(result: CanaryResult) -> str:
    """Leer heisst frei. Sonst der Sperrgrund, wortgleich fuer den Adapter."""
    if result.ok:
        return ""
    return f"cli_canary_failed:{result.reason}"


if __name__ == "__main__":                     # pragma: no cover - Werkzeug
    ergebnis = run()
    print(json.dumps(ergebnis.as_dict(), indent=2, ensure_ascii=False))
    sys.exit(0 if ergebnis.ok else 1)
