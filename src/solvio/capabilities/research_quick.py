"""Kurzrecherche als Faehigkeit -- eine aktuelle Frage, im selben Gespraechs-Turn.

Der Hermes-Deep-Pfad (`deep_research`) ist gruendlich und langsam: er wartet
`FIRST_WAIT` Sekunden und meldet sich sonst spaeter. Fuer "wer ist gerade
Bundeskanzler" ist das die falsche Antwort -- nicht falsch im Ergebnis, falsch
im Takt. Diese Faehigkeit nimmt einen anderen Weg: die NATIVE Websuche des
Anbieters fuehrt aus, in EINEM Anfrage-Antwort-Paar, und SOLVIO behaelt genau
die vier Dinge, die es bei jeder anderen Faehigkeit auch behaelt -- Auftrag
(eigener Auftraggeber, eigenes Lease), Kappe (eigene Caps, eigenes Broker-Tor
fuer das anbieterseitige Werkzeug), Vertrauen (`content_trust: untrusted_web`,
dieselbe Entwaffnungshuelle wie beim Browser) und Wahrheit (Quellen kommen
AUSSCHLIESSLICH aus den `url_citation`-Annotationen des Anbieters, nie aus
geratenen URLs).

**Der Aufrufweg folgt woertlich `cognition/assessor.py`.** Ein frischer Token
je Aufruf (`register_principal` praegt neu), ein eigenes Lease, ein POST auf
die Broker-Rueckschleife, `close_lease` in einem `finally`, das nie wirft. Kein
Hermes, keine Agentenlaufzeit, keine Hintergrundaufgabe: `execution_class` ist
FAST, `executor` ist `inline`, und der ganze Aufruf laeuft im Turn.

**Der Nebenbefund aus der Live-Abnahme (2026-09-02):** das Modell schreibt eine
Quelle ZWEIMAL -- strukturiert in den `url_citation`-Annotationen UND als
Markdown-Zitat mitten im Antworttext ("... Merz. ([bundesregierung.de]"
"(https://...))"). Genau das zweite darf nicht vorgelesen werden. Der
Antworttext wird deshalb von eingebetteten Markdown-Zitaten UND von jeder
verbliebenen nackten URL befreit, bevor er als `answer` zurueckgeht -- die
Quellen stehen strukturiert daneben, in `sources[]`.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from solvio.capabilities.browser import CONTENT_TRUST
from solvio.capabilities.contract import (
    CapabilityDeclined, CapabilitySpec, ExecutionClass, ExecutorUnavailable,
)
from solvio.contracts.untrusted import neutralize
from solvio.logging_setup import get_logger
from solvio.provider_broker.service import RESEARCH_QUICK_PRINCIPAL
from solvio.security.mobile_approval.execution import READ_ONLY
from solvio.tools.base import RiskLevel

log = get_logger("research_quick")

#: Das einzige Modell, das dieser Auftraggeber anfordern darf
#: (`RESEARCH_QUICK_CAPS.allowed_models`, `provider_broker/session.py`) --
#: hier referenziert, nicht abgeschrieben, wie ueberall im Haus.
MODEL = "gpt-5.4-mini"

#: Was in `sources[].domain`-Nachbarschaft und im Ergebnis steht -- eine
#: Beschriftung, kein Zugangsname.
PROVIDER = "openai"

#: Frist des einzelnen POSTs auf die Broker-Rueckschleife. Gemessen: 4,36 s
#: fuer eine echte Anfrage. Grosszuegig genug fuer eine schwankende
#: Websuche, eng genug, dass der Turn nicht steht.
REQUEST_TIMEOUT = 20.0

#: Lease-Dauer je Aufruf. Deckt den POST plus etwas Spielraum -- eine
#: Kurzrecherche lebt keine Minuten.
LEASE_SECONDS = 30.0

#: Angeheftete Ausgabekappe. Ohne sie veranschlagt der Broker pauschal 4096
#: Token; eine kurze Antwort braucht das nicht.
MAX_OUTPUT_TOKENS = 800

MIN_QUESTION = 3
MAX_QUESTION = 800

SPECS: dict[str, CapabilitySpec] = {
    "research_quick": CapabilitySpec(
        name="research_quick", version=1, execution_class=ExecutionClass.FAST,
        base_risk=RiskLevel.HARMLESS, semantics=READ_ONLY,
        input_schema={"type": "object", "properties": {
            "question": {"type": "string"}}, "required": ["question"]},
        executor="inline", timeout=REQUEST_TIMEOUT + 5.0,
        description="Beantwortet eine aktuelle Faktenfrage sofort, im selben "
                    "Gespraech, mit der nativen Websuche des Anbieters."),
}

# -- Markdown-Zitate und nackte URLs entfernen -------------------------------

#: `([Titel](https://...))` -- die vollstaendige, in Klammern eingefasste
#: Form, wie sie in der Live-Abnahme im Antworttext auftauchte.
_MD_CITATION = re.compile(r"\(\[[^\]\n]{0,200}\]\(https?://[^\s()]+\)\)")
#: Dieselbe Form ohne die aeussere Klammer.
_MD_LINK = re.compile(r"\[[^\]\n]{0,200}\]\(https?://[^\s()]+\)")
#: Was danach noch an nackter URL uebrig sein koennte -- der Sicherheitsnetz-
#: Fall, nicht der erwartete.
_BARE_URL = re.compile(r"https?://\S+")


def _strip_embedded_urls(text: str) -> str:
    """Nimmt dem Sprechtext jede URL -- eingebettet als Markdown oder nackt."""
    cleaned = _MD_CITATION.sub("", text)
    cleaned = _MD_LINK.sub("", cleaned)
    cleaned = _BARE_URL.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def _domain(url: str) -> str:
    """Aus der URL ABGELEITET, nie geraten. Ein Parsefehler heisst: keine."""
    try:
        return urlparse(url).netloc
    except ValueError:
        return ""


def _extract_answer_and_citations(data: Any) -> tuple[str, list[dict[str, str]]]:
    """Der Sprechtext und die Quellen -- AUSSCHLIESSLICH aus `url_citation`.

    Andere `output`-Eintraege (`web_search_call` und Aehnliches) tragen
    keinen Sprechtext und werden uebersprungen. Liefert der Anbieter keine
    Annotation, bleibt die Quellenliste leer -- es wird NIE eine URL aus dem
    Text herauskonstruiert.
    """
    text = ""
    citations: list[dict[str, str]] = []
    if not isinstance(data, dict):
        return text, citations
    for item in data.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind not in (None, "message"):
            continue
        for part in item.get("content", []) or []:
            if not isinstance(part, dict):
                continue
            part_text = part.get("text")
            if isinstance(part_text, str) and part_text.strip() and not text:
                text = part_text
            for annotation in part.get("annotations", []) or []:
                if not isinstance(annotation, dict):
                    continue
                if annotation.get("type") != "url_citation":
                    continue
                url = str(annotation.get("url") or "").strip()
                if not url:
                    continue
                citations.append({"title": str(annotation.get("title") or "").strip(),
                                  "url": url})
    return text, citations


def _dedupe_sources(citations: list[dict[str, str]]) -> list[dict[str, str]]:
    """Dieselbe URL zweimal ist ein Fund des Anbieters, kein zweiter Beleg."""
    merged: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for citation in citations:
        url = citation["url"]
        if url not in merged:
            merged[url] = {"title": citation.get("title", ""), "url": url,
                           "domain": _domain(url)}
            order.append(url)
        elif not merged[url]["title"] and citation.get("title"):
            merged[url]["title"] = citation["title"]
    return [merged[url] for url in order]


# -- Der Transport: woertlich der Weg aus cognition/assessor.py -------------

def _payload(question: str) -> dict[str, Any]:
    return {
        "model": MODEL,
        "input": [{"role": "user", "content": question}],
        "tools": [{"type": "web_search", "search_context_size": "low"}],
        # NICHT "minimal" -- die Anbieterdoku schliesst das fuer diesen Pfad
        # ausdruecklich aus (Live-Abnahme 2026-09-02).
        "reasoning": {"effort": "low"},
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }


def _denied_reason(body: str) -> str:
    """`{"error": {"type": "solvio_broker", "code": "<grund>"}}` -- oder nichts.

    Dieselbe Lesart wie in `cognition/assessor.py`: der Grund des Brokers wird
    GELESEN, nicht geraten.
    """
    try:
        parsed = json.loads(body or "")
    except ValueError:
        return ""
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if not isinstance(error, dict):
        return ""
    return str(error.get("code", "") or "")


async def _post_broker(payload: dict, *, token: str, port: int = 0,
                       timeout: float = REQUEST_TIMEOUT) -> dict:
    """POST an den Broker. Ein Anbieterfehler ist keine Schema-Frage.

    Woertlich derselbe Weg wie `cognition.assessor.broker_transport` -- nur
    dass hier der GANZE gepufferte Rumpf zurueckgeht, weil die Quellen aus
    `output[].content[].annotations` stammen und nicht nur aus dem Text.
    """
    import aiohttp

    from solvio.provider_broker.service import configured_port

    chosen = int(port) or configured_port()
    url = f"http://127.0.0.1:{chosen}/v1/responses"
    headers = {"Authorization": f"Bearer {token}",
               "Content-Type": "application/json"}
    limit = aiohttp.ClientTimeout(total=float(timeout))
    try:
        async with aiohttp.ClientSession(timeout=limit) as session:
            async with session.post(url, json=payload, headers=headers) as response:
                body = await response.text()
                if response.status != 200:
                    reason = _denied_reason(body) or f"broker_{response.status}"
                    log.warning("research_quick.rejected", status=response.status,
                               reason=reason)
                    return {"ok": False, "reason": reason}
                try:
                    data = json.loads(body)
                except ValueError:
                    return {"ok": False, "reason": "broker_unreadable"}
    except aiohttp.ClientError as exc:
        return {"ok": False, "reason": f"broker_unreachable:{type(exc).__name__}"}
    except TimeoutError:
        return {"ok": False, "reason": "broker_timeout"}
    return {"ok": True, "data": data}


async def _call(question: str, *, broker: Any, transport: Any, port: int) -> dict:
    """Genau ein gemaklerter Aufruf, mit eigenem Lease im `finally`.

    Woertlich der Weg aus `cognition.assessor.Assessor.call`: frischer Token
    je Aufruf, eigenes Lease, POST, `close_lease` in einem `finally`, das nie
    wirft.
    """
    if broker is None:
        return {"ok": False, "reason": "broker_absent"}

    token = broker.register_principal(RESEARCH_QUICK_PRINCIPAL)
    lease_id = ""
    try:
        lease_id = broker.open_lease(RESEARCH_QUICK_PRINCIPAL, question[:80],
                                     deadline=time.time() + LEASE_SECONDS)
    except Exception as exc:  # noqa: BLE001 - eine Kappe ist kein Absturz
        log.info("research_quick.lease_refused", kind=type(exc).__name__)
        return {"ok": False, "reason": getattr(exc, "reason", "lease_refused")}
    try:
        return await transport(_payload(question), token=token, port=port)
    except Exception as exc:  # noqa: BLE001 - ein Fehlschlag ist keine Erlaubnis
        log.warning("research_quick.call_failed", kind=type(exc).__name__)
        return {"ok": False, "reason": "research_quick_failed"}
    finally:
        # Steht in einem `finally` und darf deshalb nie werfen -- dieselbe
        # Regel wie beim Broker selbst.
        if lease_id:
            try:
                broker.close_lease(lease_id)
            except Exception as exc:  # noqa: BLE001 - nie den Core stoeren
                log.info("research_quick.lease_close_failed",
                         kind=type(exc).__name__)


class ResearchQuickCapabilities:
    """Der Handler. Der Broker wird SPAET gelesen, nie beim Anhaengen gemerkt.

    Derselbe Grund wie beim kognitiven Router: der Broker haengt am Server,
    nicht am Dispatcher, und entsteht NACH dieser Registrierung. Ein einmal
    gemerktes `None` waere fuer die gesamte Laufzeit `broker_absent`.
    """

    def __init__(self, dispatcher: Any, *, transport: Any = None,
                port: int = 0) -> None:
        self.dispatcher = dispatcher
        self._transport = transport if transport is not None else _post_broker
        self._port = port

    @property
    def broker(self) -> Any:
        return getattr(self.dispatcher, "provider_broker", None)

    async def research(self, arguments: dict[str, Any]) -> dict[str, Any]:
        question = str(arguments.get("question", "") or "").strip()
        if len(question) < MIN_QUESTION:
            raise CapabilityDeclined("question_too_short",
                                     "Wonach genau soll ich kurz suchen?")
        if len(question) > MAX_QUESTION:
            raise CapabilityDeclined("question_too_long",
                                     "Das ist zu lang -- bitte kuerzer fassen.")

        outcome = await _call(question, broker=self.broker,
                              transport=self._transport, port=self._port)
        if not outcome.get("ok"):
            reason = str(outcome.get("reason") or "research_quick_failed")
            error = ExecutorUnavailable(reason)
            # `CapabilityDeclined` traegt seinen maschinenlesbaren Grund seit
            # jeher als Attribut; `ExecutorUnavailable` ist im gemeinsamen
            # Vertrag absichtlich duenner. Dieser Pfad braucht den Namen aber
            # fuer die kurze, ehrliche Sprachmeldung und darf ihn nicht aus
            # Freitext zurueckraten.
            error.reason = reason
            raise error

        text, citations = _extract_answer_and_citations(outcome.get("data"))
        if not text.strip():
            raise CapabilityDeclined("no_answer",
                                     "Dazu habe ich gerade keine Antwort bekommen.")

        answer = neutralize(_strip_embedded_urls(text))
        sources = _dedupe_sources(citations)
        for source in sources:
            source["title"] = neutralize(source["title"], limit=300)

        return {
            "answer": answer,
            "sources": sources,
            "provider": PROVIDER,
            "model": MODEL,
            "searched_at": datetime.now(timezone.utc).isoformat(),
            "content_trust": CONTENT_TRUST,
        }


def register(router: Any, capabilities: ResearchQuickCapabilities) -> list[str]:
    handlers = {"research_quick": capabilities.research}
    for name, handler in handlers.items():
        router.register(SPECS[name], handler)
    return sorted(handlers)
