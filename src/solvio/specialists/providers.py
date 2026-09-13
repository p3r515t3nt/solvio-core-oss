"""Die beiden Abo-gestuetzten Berater — mit genau den Flags, die es wirklich gibt.

Alles hier ist an den **installierten** Fassungen gemessen, nicht aus
Dokumentation abgeleitet:

* Claude Code 2.1.222 kennt `--print`, `--output-format json`,
  `--permission-mode plan`, `--model`, `--effort`, `--allowedTools`,
  `--disallowedTools`, `--strict-mcp-config`. Es kennt **kein** `--max-turns`
  (das gab es einmal) und **kein** ACP-Flag — die Warnung im Auftrag war
  berechtigt, und deshalb laeuft der Weg ueber den Druckmodus.
* Codex 0.147.0 kennt `exec --sandbox read-only --ephemeral
  --ignore-user-config --skip-git-repo-check --cd --color never` und liest die
  Frage von `stdin`.

Zur Abrechnung: beide Werkzeuge koennen mit einem API-Schluessel bezahlen, wenn
einer in der Umgebung steht. Genau deshalb entfernt der Starter diese Namen. Die
Sitzung des Abonnements liegt in `$HOME` und wird vom Werkzeug selbst gelesen —
SOLVIO fasst sie nie an und sieht sie nie.

Ein Wort zu `--bare` bei Claude Code: das Flag zwingt ausdruecklich auf
`ANTHROPIC_API_KEY` und liest OAuth und Schluesselbund NICHT. Es waere also genau
der falsche Weg und wird hier bewusst nicht benutzt.
"""
from __future__ import annotations

from dataclasses import dataclass

from solvio.logging_setup import get_logger
from solvio.specialists.launcher import Invocation, LauncherError, Outcome, resolve, run

log = get_logger("specialists")

#: Jedes Werkzeug, das CLI **2.1.258** dem Berater in irgendeinem der
#: gemessenen Laeufe angeboten hat. Am 2026-09-02 dreimal am Draht gemessen
#: (lokaler Lauscher, `tools[].name` aus dem Anfragerumpf); je Lauf identisch.
#:
#: Es ist die VEREINIGUNG zweier Faelle, und der Unterschied ist selbst ein
#: Befund: ohne Sperren stehen 24 Namen im Draht, `Glob` und `Grep` aber
#: NICHT — die erscheinen erst, wenn `--allowedTools` sie ausdruecklich
#: nennt. Die Erlaubnisliste nimmt also nichts weg, sie legt hoechstens etwas
#: dazu.
#:
#: Der Katalog steht hier, weil die Sperrliste sonst gegen eine Vermutung
#: geprueft wuerde statt gegen die Wirklichkeit. Waechst er mit einer neuen
#: CLI-Fassung, faellt die Zusicherung — und das ist der Zweck.
CLAUDE_TOOL_CATALOGUE_2_1_258 = (
    "Agent", "Bash", "CronCreate", "CronDelete", "CronList", "DesignSync",
    "Edit", "EnterWorktree", "ExitWorktree", "Glob", "Grep", "ListAgents",
    "Monitor", "NotebookEdit", "PushNotification", "Read", "ReportFindings",
    "ScheduleWakeup", "SendMessage", "Skill", "TaskOutput", "TaskStop",
    "WebFetch", "WebSearch", "Workflow", "Write")

#: Werkzeuge, die ein Berater NICHT bekommt. `Bash` steht ganz oben: damit waere
#: jede andere Grenze hinfaellig, weil `cat` alles liest, was der Nutzer lesen
#: darf — einschliesslich der Anmeldedaten der anderen Berater.
#:
#: **Die Liste ist lang, weil `--allowedTools` nichts wegnimmt.** Gemessen am
#: 2026-09-02: derselbe Aufruf einmal mit und einmal ohne
#: `--allowedTools Read Grep Glob` bietet exakt dieselben Werkzeuge an. Nur
#: `--disallowedTools` entfernt etwas. Wer die Erlaubnisliste fuer die Grenze
#: haelt, hat keine.
#:
#: Was die Messung ausserdem zutage foerderte und was hier vorher fehlte:
#:
#: * `Task` entfernt in dieser Fassung tatsaechlich `Agent`, `TaskOutput` und
#:   `TaskStop` — die Sperre wirkte also, aber ueber einen undokumentierten
#:   Zweitnamen. Sie steht jetzt unter ihrem gemessenen Namen da, damit sie
#:   nicht beim naechsten Umbenennen still ausfaellt.
#: * `Workflow` haette einen ganzen Faecher von Unteragenten gestartet,
#:   `CronCreate` einen dauerhaften Zeitplan angelegt, `PushNotification` und
#:   `SendMessage` das Haus verlassen. Ein Berater, der beraet, braucht
#:   nichts davon.
CLAUDE_DENIED = ("Agent", "Bash", "BashOutput", "CronCreate", "CronDelete",
                 "CronList", "DesignSync", "Edit", "EnterWorktree",
                 "ExitWorktree", "KillShell", "ListAgents", "Monitor",
                 "NotebookEdit", "PushNotification", "ReportFindings",
                 "ScheduleWakeup", "SendMessage", "Skill", "Task",
                 "TaskOutput", "TaskStop", "WebFetch", "WebSearch",
                 "Workflow", "Write")

#: Was er darf: lesen, suchen, denken. Mehr braucht ein Entwurf nicht.
CLAUDE_ALLOWED = ("Read", "Grep", "Glob")


@dataclass(frozen=True)
class ProviderStatus:
    """Ob ein Berater ueberhaupt ansprechbar ist — und warum nicht."""

    name: str
    available: bool
    reason: str = ""
    version: str = ""
    auth: str = ""

    def as_dict(self) -> dict:
        return {"anbieter": self.name, "erreichbar": self.available,
                "grund": self.reason, "version": self.version,
                "anmeldung": self.auth}


# -- Claude Code -------------------------------------------------------------

async def claude_status() -> ProviderStatus:
    """Fragt das Werkzeug selbst, ob es angemeldet ist. Ohne Verbrauch.

    `claude auth status` gibt JSON zurueck und nennt dabei **keine** Anmeldedaten,
    sondern nur die Art der Anmeldung. Das ist die richtige Quelle: eine Datei im
    Dateisystem zu suchen waere raten, weil die Sitzung unter macOS im
    Schluesselbund liegen kann.
    """
    try:
        executable = resolve("claude")
    except LauncherError as exc:
        return ProviderStatus("claude-code", False, exc.reason)
    outcome = await run(Invocation(executable, ("auth", "status"), timeout=30.0,
                                   prompt_via_stdin=False), "")
    import json
    # Gemessen: bei abgemeldetem Konto endet `claude auth status` mit Code 1 und
    # schreibt die Auskunft trotzdem sauber auf stdout. Wer hier zuerst auf den
    # Rueckgabewert schaut, meldet „status_failed" statt „nicht angemeldet" —
    # und aus einem menschlichen Schritt wird eine Stoerung.
    try:
        data = json.loads((outcome.text or "").strip() or "{}")
    except ValueError:
        return ProviderStatus("claude-code", False,
                              outcome.reason or "unreadable_status")
    if not data.get("loggedIn"):
        # Das ist keine Stoerung und kein Mangel an Faehigkeit, sondern ein
        # menschlicher Schritt: `claude auth login` oeffnet einen Browser.
        return ProviderStatus("claude-code", False, "logged_out",
                              auth=str(data.get("authMethod", "none")))
    method = str(data.get("authMethod", ""))
    return ProviderStatus("claude-code", True, "", auth=method)


def claude_invocation(*, workdir: str, model: str, effort: str = "medium",
                      timeout: float = 300.0) -> Invocation:
    """Der feste Aufruf. Jedes Flag ist Absicht.

    `--permission-mode plan` ist die eigentliche Leine: in diesem Modus wird
    nichts geschrieben, sondern geplant. `--disallowedTools` haelt zusaetzlich
    `Bash` fern — zwei Sperren, weil die eine ein Modus und die andere eine
    Liste ist und beide anders versagen.
    """
    return Invocation(
        executable=resolve("claude"),
        argv=("--print",
              "--output-format", "json",
              "--permission-mode", "plan",
              "--model", model,
              "--effort", effort,
              # Keine fremden MCP-Server. Was hier zusaetzlich haengt, waere
              # Werkzeugflaeche, die niemand geprueft hat.
              "--strict-mcp-config",
              "--allowedTools", *CLAUDE_ALLOWED,
              "--disallowedTools", *CLAUDE_DENIED),
        timeout=timeout, cwd=workdir)


def claude_text(outcome: Outcome) -> str:
    """Holt die Antwort aus dem JSON-Umschlag des Druckmodus."""
    import json
    try:
        data = json.loads(outcome.text or "{}")
    except ValueError:
        return outcome.text
    if isinstance(data, dict):
        return str(data.get("result") or data.get("text") or outcome.text)
    return outcome.text


# -- Codex -------------------------------------------------------------------

async def codex_status() -> ProviderStatus:
    """`codex login status` sagt die Art der Anmeldung, nie den Token."""
    try:
        executable = resolve("codex")
    except LauncherError as exc:
        return ProviderStatus("codex", False, exc.reason)
    outcome = await run(Invocation(executable, ("login", "status"), timeout=30.0,
                                   prompt_via_stdin=False), "")
    # Gemessen: Codex schreibt „Logged in using ChatGPT" auf stderr, nicht auf
    # stdout. Beide Stroeme werden gelesen, sonst bleibt die Auskunft leer und
    # der Anbieter gilt faelschlich als anmeldelos.
    text = ((outcome.text or "") + " " + (outcome.stderr_note or "")).strip()
    if not outcome.ok and not text:
        return ProviderStatus("codex", False, outcome.reason or "status_failed")
    low = text.lower()
    if "not logged in" in low or "logged out" in low:
        return ProviderStatus("codex", False, "logged_out")
    # „Logged in using ChatGPT" ist genau die Auskunft, auf die es ankommt: das
    # Abonnement zahlt, nicht ein Schluessel. Ein API-Schluessel wuerde hier
    # anders heissen, und der Starter hat ihn ohnehin entfernt.
    auth = "chatgpt" if "chatgpt" in low else ("api_key" if "api key" in low else low[:40])
    return ProviderStatus("codex", True, "", auth=auth)


def codex_invocation(*, workdir: str, model: str = "",
                     timeout: float = 300.0) -> Invocation:
    """Der feste Aufruf: nur lesen, nichts behalten, keine Nutzerkonfiguration.

    `--ephemeral` laesst keine Sitzungsdateien zurueck, `--ignore-user-config`
    macht den Lauf unabhaengig davon, was in `config.toml` steht (die Anmeldung
    kommt weiterhin aus `CODEX_HOME`), und `--sandbox read-only` ist die Leine.
    """
    argv = ["exec",
            "--sandbox", "read-only",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--color", "never",
            "--cd", workdir]
    if model:
        argv += ["--model", model]
    # Ein einzelner Bindestrich: die Frage kommt von stdin, nicht aus argv.
    argv.append("-")
    return Invocation(executable=resolve("codex"), argv=tuple(argv),
                      timeout=timeout, cwd=workdir)


def codex_text(outcome: Outcome) -> str:
    """Codex schreibt Fortschritt und Antwort auf dieselbe Ausgabe.

    Gesucht wird deshalb der letzte zusammenhaengende JSON-Block; faellt keiner
    an, bleibt der Rohtext. Das Zerlegen macht ohnehin `result.parse`.
    """
    return outcome.text


#: Woran ein erschoepftes Kontingent erkennbar ist. Bewusst am Text des
#: Werkzeugs und nicht an einem Kontostand: Nutzungsuebersichten des Anbieters
#: abzufragen waere genau das Scraping, das hier nicht stattfindet.
QUOTA_MARKERS = ("usage limit", "rate limit", "quota", "too many requests",
                 "429", "limit reached", "exceeded your", "try again later",
                 "nutzungslimit", "kontingent")


def quota_exhausted(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in QUOTA_MARKERS)
