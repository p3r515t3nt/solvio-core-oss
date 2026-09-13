"""Wie ein Spezialist gestartet wird — und was er dabei ausdruecklich NICHT bekommt.

Zwei gemessene Tatsachen haben diese Datei erzwungen.

**Erstens:** Hermes kann die Spezialisten gar nicht starten. Sein Seatbelt-Profil
erlaubt `process-exec` nur unterhalb des uv-Python und des Gefaengnisses, sein
`PATH` ist `/usr/bin:/bin`, und `~/.claude` wie `~/.codex` liegen ausserhalb jedes
erlaubten `file-read*`-Pfades. Claude Code oder Codex „durch Hermes" laufen zu
lassen hiesse, genau diese Isolation aufzubrechen. Also startet sie der Core —
und das Modell im Gefaengnis bekommt davon nichts, nicht einmal einen Pfad.

**Zweitens:** beide Werkzeuge koennen auf zwei Arten bezahlen. Mit der Sitzung
des Abonnements — oder, wenn ein API-Schluessel in der Umgebung steht, damit. Das
ist der eigentliche Grund fuer die Sperrliste unten: sie ist keine Vorsichtsgeste,
sondern der Mechanismus, der „keine ueberraschende Abrechnung" **erzwingt**
statt ihn zu versprechen. Ist der Schluessel nicht da, kann er nicht benutzt
werden — auch nicht, wenn das Kontingent des Abonnements erschoepft ist. Dann
scheitert der Aufruf, und das ist die gewollte Antwort.

Der Rest folgt daraus: feste Programmpfade statt Suche im `PATH`, eine Argumentliste
statt einer Kommandozeile, die Frage ueber `stdin` statt ueber `argv`, feste
Fristen, feste Ausgabegrenzen. Es gibt keinen Weg, hier ein beliebiges Kommando
unterzubringen — nicht weil es verboten waere, sondern weil keine Stelle existiert,
an der ein Kommando entstehen koennte.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from dataclasses import dataclass, field

from solvio.logging_setup import get_logger

log = get_logger("specialists")

#: Was ein Spezialist an Umgebung bekommt. Fuenf Namen, wie beim Gefaengnis auch.
#: `HOME` muss dabei sein: dort liegt die Sitzung des Abonnements, die das
#: Werkzeug selbst liest. SOLVIO liest sie nie.
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM", "SHELL",
                 "USER", "LOGNAME")

#: Die Sperrliste, die aus einer Zusage einen Mechanismus macht.
#:
#: Steht einer dieser Namen in der Kindumgebung, kann das Werkzeug auf
#: Abrechnung nach Verbrauch ausweichen — lautlos, und genau dann, wenn das
#: Kontingent des Abonnements zu Ende ist. Also steht keiner drin. Der Core
#: selbst braucht `OPENAI_API_KEY` fuer den Sprachweg; hier wird er entfernt.
DENIED_ENV = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "CLAUDE_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORGANIZATION",
    "CODEX_API_KEY", "AZURE_OPENAI_API_KEY",
    # Nichts aus SOLVIO selbst hat in einem Spezialisten etwas zu suchen.
    "HOME_ASSISTANT_TOKEN", "HOME_ASSISTANT_URL",
    "GOOGLE_CALENDAR_CLIENT_SECRET", "GOOGLE_CALENDAR_REFRESH_TOKEN",
    "GOOGLE_CALENDAR_CLIENT_ID", "SOLVIO_DEEP_JAIL", "SOLVIO_DEEP_STATE_DIR",
)

#: Wie viel Text ein Spezialist zurueckgeben darf. Ein Modell, das zehn Megabyte
#: liefert, hat nicht mehr gesagt — es hat nur mehr Kontext verbraucht.
MAX_OUTPUT = 60_000

#: Muster, die nach Geheimnis aussehen. Die Ausgabe eines fremden Werkzeugs ist
#: fremder Text; er koennte einen Schluessel enthalten, weil das Werkzeug ihn in
#: einer Fehlermeldung nennt. Was hier durchrutscht, steht danach im Journal.
_SECRET_SHAPES = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\b(?:oauth|bearer|access|refresh)[_-]?token\b\s*[:=]\s*\S+",
               re.IGNORECASE),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
)

MASK = "<entfernt>"


class LauncherError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Invocation:
    """Ein fester Aufruf. Alles daran ist vorher bekannt."""

    #: Der absolute Pfad des Programms. Kein Name, der im `PATH` gesucht wird —
    #: sonst entscheidet die Umgebung, welches Programm laeuft.
    executable: str
    #: Die Argumente NACH dem Programmnamen. Eine Liste, nie eine Zeichenkette:
    #: es gibt damit keine Shell, die etwas interpretieren koennte.
    argv: tuple[str, ...]
    timeout: float
    #: Die Frage geht ueber `stdin`. In `argv` waere sie in jeder Prozessliste
    #: des Rechners sichtbar.
    prompt_via_stdin: bool = True
    cwd: str = "/"


@dataclass
class Outcome:
    """Was zurueckkam — Text, kein Vertrauen."""

    ok: bool
    text: str = ""
    reason: str = ""
    exit_code: int | None = None
    elapsed: float = 0.0
    truncated: bool = False
    stderr_note: str = ""


def redact(text: str) -> str:
    """Entfernt, was nach Anmeldedaten aussieht — bevor es irgendwo landet."""
    cleaned = text or ""
    for shape in _SECRET_SHAPES:
        cleaned = shape.sub(MASK, cleaned)
    return cleaned


def child_environment() -> dict[str, str]:
    """Die Umgebung eines Spezialisten: Erlaubnisliste minus Sperrliste.

    Die Reihenfolge ist wichtig und absichtlich doppelt gesichert: erst wird nur
    uebernommen, was auf der Erlaubnisliste steht, danach wird trotzdem noch
    einmal gegen die Sperrliste geprueft. Die zweite Pruefung ist theoretisch
    ueberfluessig — bis jemand der Erlaubnisliste einen Namen hinzufuegt.
    """
    env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
    for name in DENIED_ENV:
        env.pop(name, None)
    env["PATH"] = _child_path(env.get("PATH", ""))
    leaking = [name for name in env if name in DENIED_ENV]
    if leaking:
        raise LauncherError("environment_leak", f"{len(leaking)} names")
    return env


def brokered_environment(*, base_url: str, token: str,
                         tmpdir: str = "") -> dict[str, str]:
    """Die Umgebung des **gemakelten** Claude-Builders (V0.6).

    Sie ist `child_environment()` plus genau zwei Namen — und beide stehen auf
    der Sperrliste. Das ist kein Widerspruch, sondern der Punkt: die Sperrliste
    verhindert, dass die Umgebung des Cores durchsickert. Was hier gesetzt
    wird, kommt NICHT aus `os.environ`, sondern aus dem Aufruf:

    * `ANTHROPIC_BASE_URL` — die Rueckschleife zum eigenen Broker. Der Kaefig
      laesst ohnehin kein anderes Ziel zu; diese Zeile sagt der CLI nur, wohin.
    * `ANTHROPIC_API_KEY` — das **Broker-Token**, kurzlebig und leasegebunden.
      Ohne offenes Lease oeffnet es nichts, und nach dem Lease-Schluss ist es
      `401`.

    Fail-closed an beiden Werten: eine Basis, die nicht auf die Rueckschleife
    zeigt, und ein leeres Token sind Fehler, keine Vorgaben. Sonst waere die
    stille Fehlbedienung genau die, bei der das CLI doch zum echten Anbieter
    spricht.
    """
    if not token:
        raise LauncherError("brokered_env_incomplete", "kein Broker-Token")
    if not (base_url.startswith("http://127.0.0.1:")
            or base_url.startswith("http://localhost:")):
        raise LauncherError("brokered_env_not_loopback", base_url[:40])
    env = child_environment()
    env["ANTHROPIC_BASE_URL"] = base_url
    env["ANTHROPIC_API_KEY"] = token
    if tmpdir:
        # Das Laufzeitverzeichnis der CLI **in den Kaefig** holen.
        #
        # Live gemessen am 2026-09-02: ohne das scheitert der Start mit
        # `EEXIST: mkdir '/tmp/claude-501'`. Die CLI legt dort ein
        # Verzeichnis JE BENUTZERKENNUNG an — geteilt mit den interaktiven
        # Sitzungen des Eigentuemers. Es dem Kaefig zu oeffnen waere die
        # bequeme Loesung und die falsche: ein modellgesteuerter Schreiber
        # koennte dort Dateien hinterlegen, die ein anderer Claude-Prozess
        # liest.
        #
        # `CLAUDE_CODE_TMPDIR` verlegt es. Das ist eine VERENGUNG, keine
        # Ausnahme: der Builder bekommt sein eigenes Laufzeitverzeichnis
        # innerhalb der Grenzen, die er ohnehin hat.
        env["TMPDIR"] = tmpdir
        env["CLAUDE_CODE_TMPDIR"] = tmpdir
    return env


def _child_path(inherited: str) -> str:
    """Der Suchpfad des Kindes — um `STANDARD_BINARIES` ergaenzt.

    `resolve()` findet ein Werkzeug auch dann, wenn `PATH` es nicht kennt. Das
    genuegt fuer das Werkzeug selbst, aber nicht fuer das, was es STARTET.

    Live gemessen: `codex` ist ein Node-Skript mit `#!/usr/bin/env node`. Unter
    launchd ist `PATH` `/usr/bin:/bin:/usr/sbin:/sbin`, Homebrew fehlt darin,
    und jeder Bauauftrag scheiterte mit `env: node: No such file or directory` —
    dreimal hintereinander, bis die Grenze erreicht war. Aus einer Shell
    gestartet lief derselbe Code, weil dort ein anderer `PATH` vererbt wird.

    Dasselbe Muster wie damals beim Fachteam, eine Ebene tiefer: dort war das
    Werkzeug nicht auffindbar, hier ist es der Interpreter, den das Werkzeug
    braucht.

    Die Ergaenzung ist ANGEHAENGT, nicht vorangestellt: ein vererbter `PATH`
    behaelt seinen Vorrang, und die Liste steht als Code, nicht als Umgebung —
    ein Suchpfad, den ein Aufrufer setzen kann, waere ein Weg, ein
    untergeschobenes Werkzeug ausfuehren zu lassen.
    """
    teile = [p for p in (inherited or "").split(os.pathsep) if p]
    for folder in STANDARD_BINARIES:
        if folder not in teile and os.path.isdir(folder):
            teile.append(folder)

    # Die eine Ausnahme von der Anhaenge-Regel: die gepruefte git-Binary muss
    # VORNE stehen (P0.2).
    #
    # `/usr/bin/git` ist auf macOS kein git, sondern der xcselect-Weiterleiter.
    # Im Kaefig darf er das Entwicklerverzeichnis nicht lesen, haelt die
    # Werkzeuge fuer nicht installiert und oeffnet den GUI-Installer — gemessen
    # am 2026-09-02, neun Kernel-Verweigerungen und fuenf nutzlose
    # Installationen. Stuende das Verzeichnis hinten, gaebe `/usr/bin` weiterhin
    # den Weiterleiter zurueck.
    #
    # Das widerspricht der Regel oben nicht: dieser Pfad kommt nicht aus der
    # Umgebung, sondern aus `git_binary.resolve()` — und der hat ihn vorher
    # geprueft (Eigentuemer, Rechte, Signatur, Probelauf).
    try:
        from solvio import git_binary as _GB
        vertraut = os.path.dirname(_GB.resolve())
    except Exception:                                    # noqa: BLE001
        vertraut = ""
    if vertraut and os.path.isdir(vertraut):
        teile = [vertraut] + [p for p in teile if p != vertraut]

    return os.pathsep.join(teile) or os.pathsep.join(STANDARD_BINARIES)


#: Wo Werkzeuge liegen, wenn PATH sie nicht kennt.
#:
#: Unter launchd ist PATH `/usr/bin:/bin:/usr/sbin:/sbin` — Homebrew fehlt
#: darin. Das Fachteam war im Dienstbetrieb damit vollstaendig tot und lief nur,
#: wenn SOLVIO aus einer Shell gestartet wurde. Aufgefallen ist das dem Arzt.
#:
#: Die Liste steht als Code hier und kommt ausdruecklich NICHT aus der Umgebung.
#: Ein Suchpfad, den ein Aufrufer setzen kann, waere ein Weg, SOLVIO ein
#: untergeschobenes `claude` ausfuehren zu lassen — dieselbe Ueberlegung wie bei
#: den Vorgehen des Arztes: Code, nicht Text.
STANDARD_BINARIES = ("/opt/homebrew/bin", "/usr/local/bin",
                     os.path.expanduser("~/.local/bin"))


def resolve(program: str) -> str:
    """Der absolute Pfad eines Werkzeugs, einmal aufgeloest und geprueft."""
    found = shutil.which(program)
    if not found:
        for folder in STANDARD_BINARIES:
            candidate = os.path.join(folder, program)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                found = candidate
                break
    if not found:
        raise LauncherError("not_installed", program)
    if not os.path.isabs(found):
        raise LauncherError("not_absolute", program)
    if not os.access(found, os.X_OK):
        raise LauncherError("not_executable", program)
    return found


async def run(invocation: Invocation, prompt: str) -> Outcome:
    """Fuehrt einen festen Aufruf aus. Kein Shell, kein Kommando aus Text."""
    import time
    started = time.monotonic()
    try:
        env = child_environment()
    except LauncherError as exc:
        return Outcome(False, reason=exc.reason)

    try:
        process = await asyncio.create_subprocess_exec(
            invocation.executable, *invocation.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env, cwd=invocation.cwd, start_new_session=True)
    except (OSError, ValueError) as exc:
        log.warning("specialist.spawn_failed", kind=type(exc).__name__)
        return Outcome(False, reason="spawn_failed")

    payload = (prompt or "").encode("utf-8") if invocation.prompt_via_stdin else b""
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(payload), timeout=invocation.timeout)
    except asyncio.TimeoutError:
        # Die ganze Prozessgruppe, nicht nur das Kind: ein CLI startet gern
        # Helfer, und ein verwaister Helfer haelt Kontingent und Speicher fest.
        _terminate(process)
        return Outcome(False, reason="timeout",
                       elapsed=time.monotonic() - started)
    except Exception as exc:  # noqa: BLE001
        _terminate(process)
        log.warning("specialist.communicate_failed", kind=type(exc).__name__)
        return Outcome(False, reason="communication_failed")

    text = redact(stdout.decode("utf-8", "replace"))
    truncated = len(text) > MAX_OUTPUT
    note = redact(stderr.decode("utf-8", "replace")).strip()
    return Outcome(
        ok=process.returncode == 0,
        text=text[:MAX_OUTPUT],
        reason="" if process.returncode == 0 else "nonzero_exit",
        exit_code=process.returncode,
        elapsed=time.monotonic() - started,
        truncated=truncated,
        # Nur der Anfang, und schon entschaerft. `stderr` ist die Stelle, an der
        # ein CLI seine Konfiguration ausplaudert.
        stderr_note=note[:400])


def _terminate(process) -> None:
    import contextlib
    import signal
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        process.kill()
