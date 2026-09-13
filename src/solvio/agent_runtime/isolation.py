"""Die Grenze eines schreibenden Spezialisten ist ein Betriebssystem-Sandkasten,
kein Politik-Flag.

Gemessen, nicht angenommen: Codex bringt seinen eigenen OS-Sandkasten mit
(`--sandbox workspace-write`). **Claude Code hat keinen.** `--permission-mode`
und Werkzeuglisten sind Politik — und ein Builder, der Tests ausfuehrt, fuehrt
beliebigen Code aus. Fuer den Berater hat die Briefing-Architektur schon
festgehalten, dass „seine Gutmuetigkeit" als Grenze nicht akzeptabel ist; fuer
einen schreibenden, testausfuehrenden Agenten gilt das erst recht. DEBT-0128
war genau diese Gestalt: unsandkastiger Unterprozess mit Schluesselreichweite.

Also bekommt der Claude-Builder ein SOLVIO-eigenes Seatbelt-Profil, nach dem
zweifach erprobten Muster aus `deep/isolation.py` und `bots/runner.py`.

Drei Dinge daran sind Entscheidungen und keine Vorsicht:

**1. Ein Profil ist eine Erlaubnisliste.** `~/solvio-core`, `~/.solvio*`, der
Tresor, die Freigaben, `~/.codex` und `~/.ssh` stehen NICHT unter einem
`deny` — sie stehen gar nicht drin. Nichts unter `/Users` ist erlaubt ausser
dem Arbeitsbereich, dem Scratch und dem EIGENEN Werkzeugzustand des CLIs. Ein
`(subpath "{home}")` waere die eine Zeile, die alles davon auf einmal
aufmachte; ein Test verlangt, dass sie fehlt.

**2. Kein Rueckschleifen-Ziel.** Der Kaefig von Hermes darf genau einen
Loopback-Port erreichen (den Broker). Ein Builder braucht keinen — die Zeile
fehlt hier, und damit sind Core (8766), Freigabeweg (8770) und Broker (8792)
nicht erreichbar. Die bekannte Seatbelt-Einschraenkung bleibt und wird wie in
`deep/isolation.py` benannt: das `*` in `(remote tcp "*:443")` ist das
**Wirtsfeld**, nicht der Port — `127.0.0.1:443` und `:80` sind also erreichbar.
Auf 443/80 bindet der Core nichts.

**3. Die Kredentialgrenze ist die ACL des Schluesselbund-Eintrags, nicht dieses
Profil.** Am laufenden System gemessen (2026-08-29): die wiederverwendbare
Claude-Sitzung liegt AUSSCHLIESSLICH im macOS-Schluesselbund (Eintrag
`Claude Code-credentials` in `login.keychain-db`); `~/.claude.json` traegt unter
`oauthAccount` nur Konto-Metadaten (Schluesselinventar geprueft: keine
Token-Felder), und `~/.claude/` haelt Verlauf, Sitzungen und Einstellungen. Der
lesbare `~/.claude*`-Zustand ist damit kredentialfrei — deshalb darf er im
Profil stehen. Das CLI selbst muss sich anmelden koennen und braucht dafuer
`mach-lookup`; die Grenze zu seinen Werkzeug-Kindern ist die **binaergebundene
ACL** des Eintrags: ein `bash`-Kind ist ein anderes Binary. Das ist ein
BINAERES B2-Gate — gelingt einem Kind der Lesezugriff still, ist der
Claude-Builder BLOCKIERT und der Codex-only-Rueckfall greift. Es wird nicht
weggeredet, und das Gate wird nicht aufgeweicht, damit es besteht.

Der `(deny process-exec)` auf `/usr/bin/security` weiter unten ist
ausdruecklich **zusaetzlich** und nicht der Mechanismus: er macht die Absicht
lesbar und kostet nichts. Wer ihn fuer die Grenze haelt, hat die ACL nicht
verstanden.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import time
from dataclasses import dataclass

from solvio.logging_setup import get_logger

log = get_logger("agent_runtime")

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

PROFILE_NAME = "builder.sandbox.sb"

#: Werkzeugketten, die ein Builder ausfuehren darf. Bewusst als Liste im Code —
#: ein Suchpfad aus der Umgebung waere ein Weg, dem Builder ein untergeschobenes
#: Programm unterzujubeln (dieselbe Ueberlegung wie bei `STANDARD_BINARIES`).
TOOLCHAIN_ROOTS = ("/usr", "/bin", "/opt/homebrew", "/usr/local")


def trusted_git_root() -> str:
    """Die Wurzel der gepruefte git-Binary — oder leer, wenn es keine gibt.

    Ohne sie ist der Kaefig fuer git blind: `/usr/bin/git` ist auf macOS der
    xcselect-Weiterleiter, und wenn er das Entwicklerverzeichnis nicht lesen
    darf, haelt er die Werkzeuge fuer nicht installiert und oeffnet den
    GUI-Installer. Gemessen am 2026-09-02: neun Kernel-Verweigerungen auf
    `libxcrun.dylib` aus genau diesem Profil, und fuenf Installationen, die
    nichts geholfen haben, weil nie etwas gefehlt hat.

    Es ist keine Erweiterung der Rechte: `/usr` ist ohnehin lesbar und
    ausfuehrbar, der Weiterleiter also erreichbar. Diese Zeile macht nur den
    Weg gangbar, den er ohnehin nehmen wollte.
    """
    try:
        from solvio import git_binary as _GB
        return toolchain_root(_GB.resolve())
    except Exception:                                    # noqa: BLE001
        return ""

#: Pfade, die im gerenderten Profil NICHT vorkommen duerfen. Sie stehen hier
#: nur, damit ein Test ihre Abwesenheit pruefen kann — das Profil selbst ist
#: eine Erlaubnisliste und nennt sie nie.
SEALED_PATHS = (
    "~/solvio-core",
    "~/.solvio",
    "~/.solvio-vault",
    "~/.solvio-portal",
    "~/.solvio-approvals",
    "~/.solvio-approvals-production",
    "~/.codex",
    "~/.ssh",
    ".env",
)

#: Rueckschleifen-Ports, die aus dem Builder-Kaefig unerreichbar sein muessen.
FORBIDDEN_LOOPBACK_PORTS = (8766, 8770, 8791, 8792, 8123)


class BuilderJailUnavailable(RuntimeError):
    """Kein Sandkasten, kein Builder. Es gibt keinen Halbzustand."""


_PROFILE = """(version 1)
(deny default)

;; --- Prozess -----------------------------------------------------------
;; `process-exec` NUR auf der Werkzeugkette, ausdruecklich NICHT auf dem
;; Arbeitsbereich: Tests laufen als `python <skript>` — das braucht Lesen am
;; Skript und Ausfuehren am Interpreter, nicht Ausfuehren an der Datei. Damit
;; kann ein Builder kein Programm bauen und starten, das er selbst geschrieben
;; hat. Braucht ein Repo das wirklich, ist das eine eigene, aufgezeichnete
;; Entscheidung — kein stilles Aufweichen hier.
(allow process-fork)
(allow process-exec
{toolchain_exec})
(allow signal (target self))
(allow sysctl-read)
;; Das CLI muss sich bei seinem eigenen Anbieter anmelden koennen. Die Grenze zu
;; seinen Werkzeug-Kindern ist die binaergebundene ACL des Schluesselbund-
;; Eintrags, nicht diese Zeile — siehe Modul-Docstring.
(allow mach-lookup)
(allow ipc-posix-shm-read-data (ipc-posix-name "apple.shm.notification_center"))

;; Zweites Schloss, ausdruecklich NICHT der Mechanismus: das Werkzeug, mit dem
;; man einen Schluesselbund-Eintrag von der Kommandozeile holt, ist hier nicht
;; ausfuehrbar. Der Mechanismus bleibt die ACL.
(deny process-exec (literal "/usr/bin/security"))

;; --- Pfadaufloesung; als Liste, nicht global (sonst Metadatenleck) ------
(allow file-read-metadata
  (literal "/") (literal "/etc") (literal "/var") (literal "/tmp")
  (subpath "/usr") (subpath "/System") (subpath "/bin") (subpath "/dev")
  (subpath "/private/etc") (subpath "/private/var/db") (subpath "/private/var/run")
{metadata_paths})

;; --- Lesen -------------------------------------------------------------
;; Nichts unter /Users ausser Arbeitsbereich, Scratch und dem EIGENEN
;; Werkzeugzustand des CLIs. Kein `{home}`-Subpath — das ist die eine Zeile,
;; die den Tresor, die Freigaben, `~/.codex`, `~/.ssh` und jede `.env` auf
;; einmal aufmachte.
(allow file-read*
  (literal "/")                          ;; dyld liest das Wurzelverzeichnis selbst
  (subpath "/usr") (subpath "/System") (subpath "/bin")
  (subpath "/private/var/db")
  (subpath "/private/etc/ssl")
  (literal "/private/etc/hosts") (literal "/private/etc/resolv.conf")
  ;; `/bin/sh` liest beim Start `/private/var/select/sh`. Ohne diese Zeile
  ;; scheitert jede Shell mit „Operation not permitted" — eine Meldung, die
  ;; nach einer Rechtefrage aussieht und eine Pfadfrage ist.
  (subpath "/private/var/select")
  (literal "/dev/null") (literal "/dev/zero")
  (literal "/dev/random") (literal "/dev/urandom")
  (literal "/dev/dtracehelper")
  (literal "/dev/tty")
{read_paths})

;; --- Schreiben: Arbeitsbereich, Scratch, eigener Werkzeugzustand -------
(allow file-write*
{write_paths})
(allow file-write-data
  (literal "/dev/null") (literal "/dev/dtracehelper")
  (literal "/dev/stdout") (literal "/dev/stderr") (literal "/dev/tty"))

;; --- Netz: HTTPS, HTTP, DNS. KEIN Rueckschleifen-Ziel. ----------------
;; Der Kaefig von Hermes darf genau einen Loopback-Port (den Broker). Ein
;; Builder braucht keinen — die Zeile fehlt hier. Zur Ehrlichkeit: das `*` in
;; `(remote tcp "*:443")` ist das WIRTSFELD, nicht der Port; 127.0.0.1:443 und
;; :80 sind damit erreichbar. Auf 443/80 bindet der Core nichts.
{network}
(allow system-socket)
"""

#: Der Netzblock des gewoehnlichen Builders: hinaus ins Netz, kein Loopback.
#: Codex braucht ihn nicht (sein eigener Sandkasten pinnt `network_access` auf
#: `false`), aber ein Builder, der Pakete zieht, braucht ihn.
_NETWORK_EGRESS = """(allow network-outbound
  (remote tcp "*:443")
  (remote tcp "*:80")
  (remote tcp "*:53")
  (remote udp "*:53")
  (path "/private/var/run/mDNSResponder"))"""

#: Der Netzblock des **gemakelten** Builders (Development Autopilot V0.6):
#: GENAU ein Ziel, die Rueckschleife zum eigenen Broker. Kein 443, kein 80,
#: kein DNS, kein mDNS.
#:
#: Gemessen am 2026-09-02 mit `claude --bare`: ein voller Turn kommt damit
#: durch. Ebenso gemessen, was NICHT geht — `api.anthropic.com` (kein DNS),
#: `claude.ai`, jeder andere Loopback-Port (auch der Core auf 8766), und
#: `/usr/bin/security` (exec verweigert, die Zeile steht ohnehin oben).
#:
#: **Syntaxfalle, teuer gelernt:** `sandbox-exec` weist `(remote tcp
#: "127.0.0.1:8792")` mit „host must be * or localhost" ab. Es muss
#: `localhost` heissen.
_NETWORK_BROKER_ONLY = """(allow network-outbound
  (remote tcp "localhost:{broker_port}"))"""


def toolchain_root(program: str) -> str:
    """Das Verzeichnis, das ein Werkzeug wirklich braucht — mit der uv-Falle.

    Uebernommen aus `deep/isolation.interpreter_root`, weil es genau die Stelle
    ist, an der ein Profil STILL scheitert: `venv/bin/python` zeigt bei uv auf
    einen Alias ohne Patchnummer, waehrend `realpath` beim konkreten Bau landet.
    Erlaubt man nur den aufgeloesten Pfad, verweigert der Kern schon das
    `execvp` — mit einer Meldung, die nach einem Rechteproblem aussieht und ein
    Pfadproblem ist.
    """
    resolved = os.path.realpath(program)
    parts = resolved.split(os.sep)
    if "uv" in parts:
        index = parts.index("uv")
        if len(parts) > index + 1 and parts[index + 1] == "python":
            return os.sep.join(parts[:index + 2])
    return os.path.dirname(os.path.dirname(resolved))


def _subpaths(paths) -> str:
    return "\n".join(f'  (subpath "{os.path.realpath(os.path.expanduser(p))}")'
                     for p in paths if p)


def render_profile(*, workspace: str, scratch: str, tool_state: tuple[str, ...] = (),
                   toolchain: tuple[str, ...] = TOOLCHAIN_ROOTS,
                   broker_port: int = 0) -> str:
    """Baut das Builder-Profil fuer genau diesen Arbeitsbereich.

    Alle Pfade laufen durch `realpath` — ein Symlink, der aus dem Arbeitsbereich
    heraus zeigt, wuerde sonst mitgenehmigt.

    `broker_port > 0` schaltet auf den **gemakelten** Netzblock um: genau die
    Rueckschleife zu diesem Port, sonst nichts. Das ist keine zweite
    Sandkasten-Architektur, sondern eine Zeile — alles andere am Profil bleibt
    Wort fuer Wort dasselbe, `/usr/bin/security` eingeschlossen.
    """
    workspace = os.path.realpath(os.path.expanduser(workspace))
    scratch = os.path.realpath(os.path.expanduser(scratch))
    kette = tuple(toolchain)
    wurzel = trusted_git_root()
    if wurzel and wurzel not in kette:
        kette += (wurzel,)
    existing_toolchain = tuple(p for p in kette if os.path.isdir(p))
    owned = (workspace, scratch) + tuple(
        os.path.realpath(os.path.expanduser(p)) for p in tool_state)

    read = existing_toolchain + owned
    if broker_port:
        if not 1 <= int(broker_port) <= 65535:
            raise ValueError("broker port out of range")
        network = _NETWORK_BROKER_ONLY.format(broker_port=int(broker_port))
    else:
        network = _NETWORK_EGRESS
    return _PROFILE.format(
        toolchain_exec=_subpaths(existing_toolchain),
        metadata_paths=_subpaths(read),
        read_paths=_subpaths(read),
        write_paths=_subpaths(owned),
        network=network,
        home=os.path.expanduser("~"))


def claude_tool_state() -> tuple[str, ...]:
    """Der eigene Zustand des Claude-CLIs — Verlauf, Sitzungen, Einstellungen.

    Gemessen kredentialfrei: die wiederverwendbare Sitzung liegt im
    Schluesselbund, `~/.claude.json` traegt nur Konto-Metadaten. Deshalb darf
    er gelesen und geschrieben werden; er ist Werkzeugzustand, keine Anmeldung.
    """
    home = os.path.expanduser("~")
    return tuple(p for p in (os.path.join(home, ".claude"),) if os.path.isdir(p))


def sealed_violations(profile: str) -> list[str]:
    """Welche versiegelten Pfade das gerenderte Profil doch nennt.

    Der Vergleich laeuft ueber `realpath`, weil das Profil aufgeloeste Pfade
    traegt — ein Test, der `~/.solvio` als Text sucht, faende nichts und waere
    still gruen.
    """
    # Nur die REGELN, nicht die Erklaerungen. Das Profil sagt in einem
    # Kommentar ausdruecklich, dass `.env` und `~/.codex` nicht vorkommen
    # duerfen — eine Textsuche ueber die Rohfassung schlaegt also ausgerechnet
    # an der Zeile an, die es richtig macht, und erzieht dazu, weniger zu
    # erklaeren. Dieselbe Lehre wie beim Quellscan des Fachteams.
    rules = "\n".join(line for line in profile.splitlines()
                      if not line.lstrip().startswith(";"))
    hits = []
    home = os.path.expanduser("~")
    for sealed in SEALED_PATHS:
        if sealed == ".env":
            if ".env" in rules:
                hits.append(sealed)
            continue
        resolved = os.path.realpath(os.path.expanduser(sealed))
        # `~/.solvio` ist ein Praefix von `~/.solvio-vault`: geprueft wird auf
        # Pfadgrenze, sonst meldet der Test den Nachbarn statt des Treffers.
        for line in rules.splitlines():
            if f'"{resolved}"' in line or f'"{resolved}/' in line:
                hits.append(sealed)
                break
    # Und die eine Zeile, die alles auf einmal aufmachte.
    if f'(subpath "{home}")' in rules:
        hits.append("~")
    return sorted(set(hits))


@dataclass
class BuilderProcess:
    process: object
    profile_path: str
    pgid: int
    started_at: float
    executable: str


def available() -> bool:
    return os.path.exists(SANDBOX_EXEC)


async def launch(argv: list[str], *, workspace: str, scratch: str,
                 env: dict[str, str], tool_state: tuple[str, ...] = (),
                 timeout: float = 900.0,
                 broker_port: int = 0) -> tuple[object, BuilderProcess]:
    """Startet ein Kind unter dem Builder-Profil. Fail-closed an jeder Stelle.

    Kein „notfalls eben ohne Sandkasten": fehlt `sandbox-exec`, gibt es keinen
    Builder. Ein Lauf, der dann bei `specialist_unavailable` stehen bleibt, ist
    die ehrlichere Lage als ein schreibender Agent ohne Kernel-Grenze.
    """
    if not available():
        raise BuilderJailUnavailable("sandbox-exec missing")
    if not argv or (shutil.which(argv[0]) is None and not os.path.exists(argv[0])):
        raise BuilderJailUnavailable("builder binary missing")

    os.makedirs(scratch, mode=0o700, exist_ok=True)
    profile_path = os.path.join(scratch, PROFILE_NAME)
    body = render_profile(workspace=workspace, scratch=scratch,
                          tool_state=tool_state, broker_port=broker_port)
    violations = sealed_violations(body)
    if violations:
        # Kein `assert`: unter `python -O` waere die Pruefung weg — und das
        # Siegel genau dort offen, wo es am meisten schadet.
        raise BuilderJailUnavailable(f"profile names sealed paths: {violations}")
    with open(profile_path, "w", encoding="utf-8") as handle:
        handle.write(body)
    os.chmod(profile_path, 0o600)

    process = await asyncio.create_subprocess_exec(
        SANDBOX_EXEC, "-f", profile_path, *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=workspace, env=env, start_new_session=True)
    try:
        pgid = os.getpgid(process.pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = 0
    log.info("agent_runtime.builder_launched", pid=process.pid, sandbox="seatbelt",
             egress="none" if broker_port else "443/80/53",
             loopback=str(broker_port) if broker_port else "none")
    return process, BuilderProcess(process=process, profile_path=profile_path,
                                   pgid=pgid, started_at=time.time(),
                                   executable=os.path.realpath(argv[0]))


def terminate(process) -> None:
    """Die ganze Prozessgruppe, nicht nur das Kind: ein CLI startet gern Helfer,
    und ein verwaister Helfer haelt Kontingent und Speicher fest."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        process.kill()
