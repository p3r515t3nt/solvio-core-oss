"""Die Pipeline — Turn hinein, Entscheidung hinaus, nie im Antwortpfad.

Diese Datei ist die Verdrahtung. Alles Wesentliche wurde vorher entschieden:
`policy.py` sagt WAS, `candidates.py` haelt WO, `extractor.py` schlaegt VOR.
Hier steht nur, in welcher Reihenfolge das passiert — und der eine Riegel, der
zaehlt.

**DER RIEGEL.** Es gibt in diesem Modul genau eine Stelle, die kanonisch
schreibt, und sie schreibt ausschliesslich `SourceType.SOLVIO_INFERENCE` mit
`TrustLevel.AGENT_GENERATED`. Das ist keine Konvention, sondern eine Zusicherung
mit Test und Mutationsprobe: die Automatik erreicht nie `user_direct`, auch
dann nicht, wenn saemtliche Evidenz aus Nutzeraeusserungen besteht. Ein
Agenten-Durchlauf hebt den Taint nicht auf — das steht seit Langem in
`TRUST_BOUNDARY.md` §7 und gilt hier woertlich.

**NIE IM ANTWORTPFAD.** `observe_turn()` legt den Turn in eine Warteschlange
und kehrt sofort zurueck. Der Aufrufer ist der Reader, eine synchrone Funktion,
die nicht blockieren darf. Faellt der Extraktor aus, haengt die Stimme nicht;
sie merkt es nicht einmal.

**WARUM V1 NICHT SPRICHT.** Die Architektur (§8.4) sieht fuer eine Rueckfrage
zwei Wege vor: gesprochen im unmittelbaren Kontext der Aeusserung — „sonst
still ins WISSEN-Postfach". Der erste Weg ist hier nicht baubar, und zwar aus
demselben Grund, der die Pipeline sicher macht: die Extraktion laeuft
ASYNCHRON nach Turn-Finalisierung. Wenn ihr Ergebnis vorliegt, spricht SOLVIO
laengst. Die Frage in einen SPAETEREN Turn zu schieben waere eine Aenderung an
der Gespraechshoheit — ein eigener Milestone, nicht ein Nebenprodukt.

V1 nimmt deshalb ausdruecklich den zweiten Weg: ASK_PENDING und CONTESTED
warten still im WISSEN-Postfach und werden ueber den Freigabeweg mit Face ID
entschieden. `confirm_candidate(via=...)` ist auf diesen Weg vorbereitet und
kennt keinen Modell-Aufrufer.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from solvio.contracts.memory import (MemoryRecord, MemoryType, ProvenanceEntry,
                                     Relation, Sensitivity)
from solvio.contracts.trust import SourceType, TrustLevel
from solvio.logging_setup import get_logger
from solvio.memory.adaptive import candidates as C
from solvio.memory.adaptive import lifecycle as L
from solvio.memory.adaptive import policy as P
from solvio.memory.adaptive.candidates import (Candidate, CandidateStore,
                                               Evidence)
from solvio.memory.adaptive.extractor import Extractor, NullExtractor
from solvio.memory.intent import looks_like_secret

log = get_logger("adaptive")

#: Wie viele Turns gleichzeitig warten duerfen. Laeuft die Schlange voll, wird
#: der aelteste verworfen: Lernen ist nachrangig, und eine wachsende Schlange
#: waere ein Speicherleck mit persoenlichem Inhalt darin.
QUEUE_LIMIT = 32

@dataclass
class TurnOutcome:
    """Was ein Turn bewirkt hat. Zahlen und Codes — nie der Satz."""

    proposals: int = 0
    adopted: int = 0
    asked: int = 0
    contested: int = 0
    gathered: int = 0
    reinforced: int = 0
    discarded: int = 0
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"proposals": self.proposals, "adopted": self.adopted,
                "asked": self.asked, "contested": self.contested,
                "gathered": self.gathered, "reinforced": self.reinforced,
                "discarded": self.discarded,
                "reasons": sorted(set(self.reasons))}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AdaptiveMemory:
    """Der Dienst. Haelt Kandidaten, Policy und den Weg ins Gedaechtnis zusammen."""

    def __init__(self, service: Any, store: CandidateStore, *,
                 extractor: Extractor | None = None, enabled: bool = True) -> None:
        self.service = service                 # MemoryService (kanonischer Weg)
        self.candidates = store
        self.extractor: Extractor = extractor or NullExtractor()
        self.enabled = enabled
        self._queue: asyncio.Queue[tuple[P.OwnerTurn, str]] = asyncio.Queue(
            maxsize=QUEUE_LIMIT)
        self._worker: asyncio.Task | None = None
        #: Welche Erinnerungen dieses Gespraech beruehrt hat. Ein Korrekturziel
        #: MUSS hier stehen — sonst koennte ein Modell eine Korrektur auf einen
        #: Eintrag erfinden, den das Gespraech nie sah.
        self.used_memories: dict[str, set[str]] = {}
        self.dropped_turns = 0
        self.last_outcome: TurnOutcome | None = None

    # ================================================================ Betrieb
    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._loop())

    async def close(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.candidates.close()

    def note_used(self, conversation_id: str, memory_ids) -> None:
        """Diese Erinnerungen wurden in diesem Gespraech in den Kontext gereicht."""
        bucket = self.used_memories.setdefault(conversation_id or "", set())
        bucket.update(i for i in memory_ids if i)

    # ============================================================ Eingang
    def observe_turn(self, turn: P.OwnerTurn, *, context: str = "") -> bool:
        """Einen finalisierten Owner-Turn zum Lernen anbieten. NICHT blockierend.

        Gibt zurueck, ob der Turn angenommen wurde. Der Aufrufer im Reader
        ignoriert das Ergebnis: Lernen ist nachrangig und darf die Sprachschicht
        nie aufhalten.
        """
        if not self.enabled:
            log.info("adaptive.disabled", channel=turn.channel)
            return False
        eligible, reason = turn.is_eligible()
        if not eligible:
            # `speaker_unverified` ist der haeufigste Fall im Alltag (jeder
            # Satz am Satelliten) und kein Zwischenfall. Er wird deshalb
            # ruhiger protokolliert als eine fremde Quelle.
            log.info("adaptive.turn_rejected", reason=reason,
                     channel=turn.channel,
                     source_class=turn.source_class.value)
            return False
        try:
            self._queue.put_nowait((turn, context))
        except asyncio.QueueFull:
            self.dropped_turns += 1
            log.info("adaptive.queue_full", dropped=self.dropped_turns)
            return False
        self.start()
        return True

    async def _loop(self) -> None:
        while True:
            turn, context = await self._queue.get()
            try:
                await self.process(turn, context=context)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - Lernen wirft nie nach oben
                log.error("adaptive.turn_failed", kind=type(exc).__name__,
                          detail=str(exc)[:200])
            finally:
                self._queue.task_done()

    # ============================================================ Verarbeitung
    async def process(self, turn: P.OwnerTurn, *, context: str = "",
                      now: datetime | None = None) -> TurnOutcome:
        """Ein Turn, vollstaendig. Auch der Einstieg fuer Tests."""
        moment = now or _now()
        outcome = TurnOutcome()
        eligible, reason = turn.is_eligible()
        if not eligible:
            outcome.reasons.append(reason)
            return self._done(outcome, turn)

        # DER SECRET-SCAN VOR DEM ANBIETER, NICHT DANACH.
        #
        # Bis hierher stand er in `_handle()` — also NACH `extractor.propose()`,
        # und das heisst: nach einem POST des rohen Redebeitrags (bis 4000
        # Zeichen) an `api.openai.com`. Der deterministische Zaun sah ein
        # gesprochenes Passwort damit erst, nachdem es das Haus verlassen hatte.
        # Kein Kandidat zu erzeugen war richtig und trotzdem zu spaet.
        #
        # Der Scan in `_handle()` bleibt stehen: er prueft zusaetzlich den
        # VORSCHLAG, den das Modell formuliert hat. Zwei Zaeune an zwei Stellen,
        # weil sie zwei verschiedene Texte pruefen.
        if looks_like_secret(turn.text):
            outcome.reasons.append("secret_shaped_turn")
            log.info("adaptive.turn_withheld", reason="secret_shaped",
                     channel=turn.channel)
            return self._done(outcome, turn)

        try:
            result = await self.extractor.propose(turn, context=context)
        except Exception as exc:  # noqa: BLE001 - der Anbieter darf ausfallen
            # Der Ausfall wird HIER gefangen und nicht erst in der Schleife:
            # `process()` ist auch der Einstieg fuer Tests und fuer einen
            # spaeteren Nachlauf, und beide duerfen nichts umwerfen.
            log.info("adaptive.extractor_unavailable", kind=type(exc).__name__)
            outcome.reasons.append("extractor_failed")
            return self._done(outcome, turn)
        outcome.proposals = len(result.proposals)
        if result.rejected:
            outcome.reasons.append("malformed_proposals")
        if not result.proposals:
            outcome.reasons.append(result.reason or "no_proposals")
            return self._done(outcome, turn)

        for proposal in result.proposals:
            await self._handle(proposal, turn, outcome, moment)

        return self._done(outcome, turn)

    def _done(self, outcome: TurnOutcome, turn: P.OwnerTurn) -> TurnOutcome:
        """Der EINZIGE Ausgang aus `process()` — und er protokolliert immer.

        Vorher gab es vier Ausgaenge, und drei davon schwiegen. Bei der ersten
        Live-Abnahme lief ein Turn sauber durch, es passierte nichts, und im
        Log stand kein Hinweis darauf, wo er geblieben war. Ein Pfad, der ueber
        Gedaechtnis entscheidet, darf nicht unbemerkt enden.
        """
        self.last_outcome = outcome
        log.info("adaptive.turn_processed", channel=turn.channel,
                 source_class=turn.source_class.value, **outcome.as_dict())
        return outcome

    async def _handle(self, proposal: P.Proposal, turn: P.OwnerTurn,
                      outcome: TurnOutcome, moment: datetime) -> None:
        dedup_key = self._dedup_key(proposal.statement)

        # Der Secret-Scan zuerst und ohne Rueckstand: bei einem Treffer entsteht
        # nicht einmal ein Kandidat, und es wird nur gezaehlt.
        secret = looks_like_secret(proposal.statement) or looks_like_secret(turn.text)
        suppressed = await self.candidates.is_suppressed(dedup_key)
        conflict = await self._find_conflict(proposal, turn)

        decision = P.decide(proposal, turn,
                            evidence_conversations=0, suppressed=suppressed,
                            secret_hit=secret, conflict=conflict)

        # Wissen wir das schon KANONISCH? Dann entsteht kein zweiter Record und
        # kein zweiter Kandidat, sondern die Kette des bestehenden waechst.
        #
        # Ohne diesen Zweig fehlte dem Entwurf sein Herzstueck: `reinforce()`
        # war gebaut und wurde nie gerufen. Gemessen hat es zwei Faelle
        # zerlegt — nach einem Neustart entstand ein zweiter Record fuer
        # denselben Satz, und die dritte Wiederholung legte einen zweiten
        # Kandidaten an, weil der erste terminal war.
        if decision.action in (P.ADOPT, P.GATHER, P.ASK):
            if await self._reinforce_existing(proposal, turn, dedup_key,
                                              outcome, moment):
                return

        # Braucht die Entscheidung Evidenz, muss der Kandidat erst existieren.
        if decision.action in (P.GATHER, P.ADOPT, P.ASK, P.CONTEST):
            cand = await self._observe(proposal, turn, dedup_key, decision, moment)
            if cand is None:
                outcome.discarded += 1
                outcome.reasons.append("candidate_budget")
                return
            if proposal.kind == P.INFERRED:
                # Jetzt erst steht die echte Evidenzlage fest — neu entscheiden.
                decision = P.decide(
                    proposal, turn,
                    evidence_conversations=cand.independent_conversations(),
                    suppressed=suppressed, secret_hit=secret, conflict=conflict)
        else:
            cand = None

        outcome.reasons.append(decision.reason)

        if decision.action == P.DISCARD:
            outcome.discarded += 1
            log.info("adaptive.discarded", reason=decision.reason,
                     marker=decision.marker)
            return

        if cand is None:
            return

        if decision.action == P.ADOPT:
            memory_id = await self._adopt(cand, proposal, turn, decision, moment)
            if memory_id:
                outcome.adopted += 1
            return

        if decision.action == P.ASK:
            await self.candidates.transition(cand.id, C.ASK_PENDING,
                                             reason=decision.ask_reason, now=moment)
            outcome.asked += 1
            return

        if decision.action == P.CONTEST:
            await self.candidates.transition(
                cand.id, C.CONTESTED, reason=decision.ask_reason,
                contested_memory_id=decision.contested_memory_id, now=moment)
            outcome.contested += 1
            return

        outcome.gathered += 1

    async def _canonical_twin(self, statement: str):
        """Gibt es diesen Gedanken schon als aktuelle Wahrheit?

        Verglichen wird ueber denselben Dedup-Schluessel, den der
        ausdrueckliche Weg benutzt — nicht ueber Aehnlichkeit. Ein falsch
        erkanntes Duplikat verliert Wissen; ein uebersehenes kostet einen
        zweiten Eintrag. Diese Asymmetrie steht schon in `service._dedup_key`
        und gilt hier genauso.
        """
        semantic = getattr(self.service, "semantic", None)
        if semantic is None:
            return None
        try:
            hits = await semantic.hybrid_recall(statement, limit=8)
        except Exception:  # noqa: BLE001
            return None
        wanted = self._dedup_key(statement)
        for record in hits:
            if record.superseded_by is None and \
                    self._dedup_key(record.content) == wanted:
                return record
        return None

    async def _reinforce_existing(self, proposal: P.Proposal, turn: P.OwnerTurn,
                                  dedup_key: str, outcome: TurnOutcome,
                                  moment: datetime) -> bool:
        """Schon bekannt? Dann Evidenz anhaengen statt neu anlegen.

        Gegen einen `user_direct`-Record wird NICHT verstaerkt: seine Herkunft
        um eine Maschinenbeobachtung zu ergaenzen hiesse, sie zu verwaessern —
        und an der Herkunft haengt die Autoritaetsachse. Er ist dann einfach
        schon wahr, und der Vorschlag faellt weg.
        """
        twin = await self._canonical_twin(proposal.statement)
        if twin is None:
            return False
        open_cand = await self.candidates.open_by_key(dedup_key)
        if open_cand is not None:
            # Ein offener Kandidat zu etwas, das laengst kanonisch ist: das ist
            # der Absturz-Fall der Zustandsmaschine. Er wird geschlossen.
            try:
                await self.candidates.transition(open_cand.id, C.ADOPTED,
                                                 memory_id=twin.id, now=moment)
            except C.CandidateError:
                pass
        if not L.is_machine_learned(twin):
            outcome.discarded += 1
            outcome.reasons.append("already_user_truth")
            return True
        try:
            await self.service.semantic.memory.reinforce(twin.id, ProvenanceEntry(
                source_type=SourceType.SOLVIO_INFERENCE,
                source=(f"conversation:{turn.conversation_id}"
                        if turn.conversation_id else "voice_turn"),
                trust_level=TrustLevel.AGENT_GENERATED, at=moment,
                note=f"{proposal.kind}: {proposal.statement}"[:C.MAX_NOTE]))
            outcome.reinforced += 1
            outcome.reasons.append("reinforced")
        except (ValueError, Exception) as exc:  # noqa: BLE001
            outcome.reasons.append("reinforce_failed")
            log.info("adaptive.reinforce_failed", kind=type(exc).__name__)
        return True

    # ------------------------------------------------------------ Kandidaten
    @staticmethod
    def _dedup_key(text: str) -> str:
        """Derselbe Schluessel wie im kanonischen Dienst — bewusst nicht neu erfunden."""
        from solvio.memory.service import _dedup_key as canonical
        return canonical(text)

    async def _observe(self, proposal: P.Proposal, turn: P.OwnerTurn,
                       dedup_key: str, decision: P.Decision,
                       moment: datetime) -> Candidate | None:
        evidence = Evidence(
            at=moment.astimezone(timezone.utc).isoformat(),
            kind=L.STATED.rstrip(":") if proposal.kind == P.STATED
            else L.OBSERVED.rstrip(":"),
            conversation_id=turn.conversation_id,
            message_id=turn.message_id,
            note=f"{proposal.kind}: {proposal.statement}"[:C.MAX_NOTE])
        cand = Candidate(
            id="", statement=proposal.statement, dedup_key=dedup_key,
            kind=proposal.kind, memory_type=proposal.memory_type.value,
            subject=proposal.subject or self._subject(proposal.statement),
            sensitivity=decision.sensitivity.value,
            valid_until=proposal.valid_until)
        try:
            return await self.candidates.observe(cand, evidence, now=moment)
        except C.CandidateError:
            return None

    @staticmethod
    def _subject(text: str) -> str:
        from solvio.memory.service import _subject_of
        return _subject_of(text)

    # ------------------------------------------------------------ Widerspruch
    async def _find_conflict(self, proposal: P.Proposal,
                             turn: P.OwnerTurn) -> dict[str, Any] | None:
        """Widerspricht dieser Vorschlag einer AKTIVEN kanonischen Wahrheit?

        Nicht jede Differenz ist ein Widerspruch — „mag Kaffee" und „mag Tee"
        koexistieren. Verlangt wird ein Abloesungsmarker im Turntext PLUS ein
        aktiver Record zum selben Subjekt. Im Zweifel: kein Widerspruch. Das ist
        der sichere Fehler, denn ein falsch erkannter Widerspruch loescht eine
        fremde Wahrheit.
        """
        if not P.has_supersession_marker(turn.text):
            return None
        semantic = getattr(self.service, "semantic", None)
        if semantic is None:
            return None
        try:
            hits = await semantic.hybrid_recall(proposal.statement, limit=8)
        except Exception:  # noqa: BLE001
            return None
        # WELCHER Eintrag gemeint ist, entscheidet NICHT diese Datei. Es
        # entscheidet `MemoryService._supersede_target` — derselbe Mechanismus,
        # den der ausdrueckliche Korrekturweg seit M2 benutzt, mit gemessenen
        # Schwellen und eingebautem Ambiguitaetsschutz.
        #
        # Uebernommen statt neu erfunden, und der Grund steht im Repository
        # schon aufgeschrieben: Wortueberlappung allein findet umformulierte
        # Aussagen nicht („Der Vertrag endet erst im April" teilt mit „der
        # Vertrag laeuft bis Ende Maerz" nur 0.125), und ohne die zweite,
        # semantische Stufe entsteht ein ZWEITER aktiver Fakt daneben.
        #
        # Genau das ist hier im Probelauf passiert: „Inzwischen moechte ich
        # lieber mehrere Vorschlaege" liess den gelernten Eintrag „moechte eine
        # klare Empfehlung" stehen und stellte sich daneben — zwei aktive,
        # unvereinbare Wahrheiten, die es laut Architektur nicht geben darf.
        try:
            scores = dict(await semantic.semantic_hits(proposal.statement, k=32))
        except Exception:  # noqa: BLE001 - ohne Semantik bleibt Stufe 1
            scores = {}
        best, why = self.service._supersede_target(proposal.statement, hits, scores)
        if best is None:
            if why == "ambiguous":
                # Es gibt etwas, aber unklar was. Der Aufloeser raet nicht —
                # richtig so. FRUEHER hat diese Datei sein `None` wie „kein
                # Widerspruch" gelesen und einen Paralleleintrag angelegt.
                #
                # Live gemessen: „Meine Lieblingsfarbe ist inzwischen eher
                # Bernstein" traf auf zwei Kandidaten — die Lieblingsfarbe
                # (0.553) und das alte Testwort „Bernstein84" (0.473). Abstand
                # 0.08, also unklar. Ergebnis war ein zweiter aktiver Eintrag
                # NEBEN dem ausdruecklichen „Petrol": zwei unvereinbare
                # Wahrheiten gleichzeitig, die es laut Architektur nicht gibt.
                #
                # Der Mensch hat mit dem Abloesungsmarker gesagt, dass etwas
                # ERSETZT werden soll. Wenn unklar ist WAS, ist die Antwort
                # eine Frage — nicht ein zweiter Eintrag.
                return {"ambiguous": True, "memory_id": "", "user_direct": False,
                        "lifecycle": "", "why": why}
            return None
        return {"memory_id": best.id,
                "user_direct": not L.is_machine_learned(best),
                "lifecycle": L.lifecycle_of(best),
                "why": why}

    # ------------------------------------------------------------- Adoption
    async def _adopt(self, cand: Candidate, proposal: P.Proposal,
                     turn: P.OwnerTurn, decision: P.Decision,
                     moment: datetime) -> str:
        """Der EINZIGE automatische Weg ins kanonische Gedaechtnis.

        Er schreibt ausschliesslich `SOLVIO_INFERENCE`/`AGENT_GENERATED`. Wer
        diese beiden Zeilen aendert, bricht die Kerninvariante — und die
        Mutationsprobe faellt.
        """
        semantic = getattr(self.service, "semantic", None)
        if semantic is None:
            return ""

        record = self._build(cand, proposal, turn, decision, moment)

        # Ein bestehender LEARNED-Record, dem eine direkte Nutzeraussage
        # widerspricht, wird abgeloest — der einzige automatische Supersede.
        target = decision.contested_memory_id
        try:
            if target:
                existing = await semantic.get(target)
                if existing is None or not L.is_machine_learned(existing):
                    # Fail-closed: gegen einen Nutzer-Record supersediert die
                    # Automatik nie. Das duerfte die Policy schon verhindert
                    # haben; hier steht der zweite Riegel.
                    log.error("adaptive.refused_supersede_user_direct")
                    return ""
                record.supersedes = target
                record.relations = [Relation(kind="contradicts", target_id=target)]
                new = await semantic.supersede(target, record)
                memory_id = new.id
            else:
                memory_id = await semantic.remember(record)
        except Exception as exc:  # noqa: BLE001
            log.error("adaptive.adopt_failed", kind=type(exc).__name__)
            return ""

        # ERST kanonisch schreiben, DANN den Kandidaten schliessen. Ein Absturz
        # dazwischen hinterlaesst einen adoptierten Record und einen offenen
        # Kandidaten — der naechste Lauf erkennt die Dublette und schliesst ihn.
        await self.candidates.transition(cand.id, C.ADOPTED,
                                         memory_id=memory_id, now=moment)
        log.info("adaptive.adopted", candidate=cand.id[:8], memory=memory_id[:8],
                 reason=decision.reason, kind=proposal.kind,
                 memory_type=proposal.memory_type.value)
        return memory_id

    def _build(self, cand: Candidate, proposal: P.Proposal, turn: P.OwnerTurn,
               decision: P.Decision, moment: datetime) -> MemoryRecord:
        """Ein gelernter Record. `user_direct` kommt hier nicht vor — nirgends."""
        chain = [
            ProvenanceEntry(
                source_type=SourceType.SOLVIO_INFERENCE,
                source=(f"conversation:{e.conversation_id}"
                        if e.conversation_id else "voice_turn"),
                trust_level=TrustLevel.AGENT_GENERATED,
                at=datetime.fromisoformat(e.at) if e.at else moment,
                note=e.note[:C.MAX_NOTE])
            for e in cand.evidence]
        valid_until = None
        if cand.valid_until:
            try:
                valid_until = datetime.fromisoformat(
                    cand.valid_until.replace("Z", "+00:00"))
            except ValueError:
                valid_until = None
        confidence = 0.75 if proposal.kind == P.STATED else 0.6
        return MemoryRecord(
            id="", memory_type=proposal.memory_type, content=proposal.statement,
            subject=cand.subject, source=f"adaptive:{cand.id[:8]}",
            # ---- Die Kerninvariante. Zwei Zeilen, kein Sonderfall. ----
            source_type=SourceType.SOLVIO_INFERENCE,
            trust_level=TrustLevel.AGENT_GENERATED,
            # -----------------------------------------------------------
            created_at=moment, updated_at=moment, valid_until=valid_until,
            sensitivity=decision.sensitivity, confidence=confidence,
            importance=0.5, provenance=chain, tags=["learned", proposal.kind],
            metadata={"adaptive": True, "candidate_id": cand.id,
                      "conversation_id": turn.conversation_id,
                      "evidence_conversations": cand.independent_conversations(),
                      # Ausdruecklich FALSCH und ausdruecklich vorhanden: ohne
                      # dieses Feld koennte spaeter jemand aus seiner Abwesenheit
                      # das Falsche schliessen.
                      "explicit_intent": False})

    # ======================================================== Nutzerentscheide
    async def confirm_candidate(self, candidate_id: str, *,
                                now: datetime | None = None,
                                via: str = "device") -> str:
        """Der Mensch bestaetigt — ein AUTORITAETSWECHSEL, kein Feldwert.

        Es entsteht ein NEUER Record mit `user_direct`, weil die Bestaetigung
        selbst eine Nutzeraussage ist. Widersprach der Kandidat einem aktiven
        Record, loest der neue ihn ab.

        Aufrufer sind ausschliesslich Nutzerwege: die deterministisch erkannte
        gesprochene Zustimmung und die per Face ID freigegebene
        iPhone-Entscheidung. Ein Modell hat hier keinen Aufrufer.
        """
        moment = now or _now()
        cand = await self.candidates.get(candidate_id)
        if cand is None:
            return ""
        if cand.state in C.TERMINAL_STATES:
            log.info("adaptive.already_decided", state=cand.state)
            return ""
        semantic = getattr(self.service, "semantic", None)
        if semantic is None:
            return ""

        try:
            sensitivity = Sensitivity(cand.sensitivity)
        except ValueError:
            sensitivity = Sensitivity.SENSITIVE
        try:
            memory_type = MemoryType(cand.memory_type)
        except ValueError:
            memory_type = MemoryType.PREFERENCE

        record = MemoryRecord(
            id="", memory_type=memory_type, content=cand.statement,
            subject=cand.subject, source=f"confirmation:{via}",
            source_type=SourceType.USER_DIRECT,
            trust_level=TrustLevel.USER_DIRECT,
            created_at=moment, updated_at=moment,
            sensitivity=sensitivity, confidence=1.0, importance=0.7,
            provenance=[ProvenanceEntry(
                source_type=SourceType.USER_DIRECT,
                source=f"confirmation:{via}",
                trust_level=TrustLevel.USER_DIRECT, at=moment,
                note=f"{L.CONFIRMED_NOTE} {cand.statement}"[:C.MAX_NOTE])],
            tags=["confirmed"],
            metadata={"adaptive": True, "candidate_id": cand.id,
                      "confirmed_via": via, "explicit_intent": False})

        target = cand.contested_memory_id
        try:
            if target:
                record.supersedes = target
                new = await semantic.supersede(target, record)
                memory_id = new.id
            else:
                memory_id = await semantic.remember(record)
        except Exception as exc:  # noqa: BLE001
            log.error("adaptive.confirm_failed", kind=type(exc).__name__)
            return ""
        await self.candidates.transition(candidate_id, C.ADOPTED,
                                         memory_id=memory_id, now=moment)
        # Eine Bestaetigung hebt eine fruehere Unterdrueckung auf: der Mensch
        # hat es sich anders ueberlegt, und das ist sein gutes Recht.
        await self.candidates.unsuppress(cand.dedup_key)
        log.info("adaptive.confirmed", candidate=candidate_id[:8],
                 memory=memory_id[:8], via=via)
        return memory_id

    async def decline_candidate(self, candidate_id: str, *,
                                now: datetime | None = None) -> bool:
        """Nein heisst nein — und es haelt.

        Ohne die Unterdrueckung formte die naechste Wiederholung denselben
        Kandidaten neu, und SOLVIO fragte erneut, was der Mensch gerade
        beantwortet hat.
        """
        moment = now or _now()
        cand = await self.candidates.get(candidate_id)
        if cand is None or cand.state in C.TERMINAL_STATES:
            return False
        await self.candidates.transition(candidate_id, C.DECLINED, now=moment)
        await self.candidates.suppress(cand.dedup_key,
                                       reason=C.SUPPRESS_DECLINED, now=moment)
        log.info("adaptive.declined", candidate=candidate_id[:8])
        return True

    async def note_forgotten(self, memory_id: str, *, content: str = "",
                             now: datetime | None = None) -> bool:
        """Nach einem `forget()`: den Gedanken nicht wieder vorschlagen.

        Sonst waere „vergiss das" eine Bitte, die beim naechsten Vorkommen
        wieder zur Frage wird — und Vergessen waere keines.
        """
        moment = now or _now()
        text = content
        if not text:
            semantic = getattr(self.service, "semantic", None)
            if semantic is not None:
                try:
                    record = await semantic.get(memory_id)
                    text = record.content if record else ""
                except Exception:  # noqa: BLE001
                    text = ""
        if not text:
            return False
        await self.candidates.suppress(self._dedup_key(text),
                                       reason=C.SUPPRESS_FORGOTTEN, now=moment)
        return True

    # ============================================================ Wartung
    async def maintenance(self, *, now: datetime | None = None) -> dict[str, int]:
        """Fristen laufen lassen. Ruft der Hintergrund, nicht der Antwortpfad."""
        return await self.candidates.expire_due(now=now)

    async def stats(self) -> dict[str, Any]:
        data = await self.candidates.stats()
        data["dropped_turns"] = self.dropped_turns
        data["extractor"] = getattr(self.extractor, "name", "unknown")
        return data
