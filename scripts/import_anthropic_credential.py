#!/usr/bin/env python3
"""Der sichere Weg, die Anthropic-Anmeldung in den Tresor zu bringen.

**Der Wert wird an keiner Stelle sichtbar.** Nicht als Argument, nicht in der
Umgebung, nicht in der Schalenhistorie, nicht im Protokoll, nicht in einem
Handoff und nicht in einem Chat. Er wird verdeckt eingetippt und geht direkt in
den Tresor.

Warum das ein eigenes Skript ist und kein Chat-Schritt: ein Token, das durch
ein Modell reist — auch nur als Werkzeugergebnis —, ist ein Token, das in einem
Transkript steht. Der Eigentuemer fuehrt das hier in SEINEM Terminal aus; das
Modell sieht davon hoechstens „stored = true".

    python3 scripts/import_anthropic_credential.py --kind subscription_oauth

Danach steht im Tresor `secret://anthropic/subscription-token`, gebunden an
genau einen Executor (`anthropic_broker`) und genau ein Ziel
(`api.anthropic.com`). Der schreibende Claude-Builder ist dort ausdruecklich
nicht gebunden — er sieht nur ein Broker-Token.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from solvio.provider_broker import anthropic as AN   # noqa: E402
from solvio.secret_vault import admin as VA          # noqa: E402
from solvio.secret_vault import policy as VP         # noqa: E402

#: Nur diese beiden Formen. `kind` wird NICHT aus dem Wert geraten — ein
#: Rateschritt hier waere ein 401 beim Anbieter, den niemand erklaeren kann.
KINDS = {
    AN.KIND_OAUTH: VP.SecretKind.OAUTH_REFRESH_TOKEN,
    AN.KIND_API_KEY: VP.SecretKind.API_KEY,
}


def fingerprint(value: str) -> str:
    """Ein Abdruck, kein Wert.

    SHA-256 ueber den Wert, auf zwoelf Hex-Zeichen gekuerzt. Er reicht, um zwei
    Importe zu unterscheiden und einen versehentlichen Doppelimport zu
    erkennen — und er reicht nicht, um den Wert zu rekonstruieren.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def read_secret(prompt: str) -> str:
    """Verdeckt, und nur von einem echten Terminal.

    Ohne TTY wird abgebrochen statt von der Standardeingabe gelesen: eine
    Pipeline waere genau der Weg, auf dem der Wert doch in einer Historie,
    einem Skript oder einem Protokoll landet.
    """
    if not sys.stdin.isatty():
        raise SystemExit("FEHLER: kein Terminal. Der Wert wird ausschliesslich "
                         "verdeckt eingetippt, nie aus einer Pipeline gelesen.")
    wert = getpass.getpass(prompt)
    zweit = getpass.getpass("Zur Sicherheit noch einmal: ")
    if wert != zweit:
        raise SystemExit("FEHLER: die beiden Eingaben sind verschieden. "
                         "Nichts gespeichert.")
    return wert.strip()


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="import_anthropic_credential",
        description="Legt die Anthropic-Anmeldung verdeckt im Tresor ab.")
    parser.add_argument("--kind", choices=sorted(KINDS), required=True,
                        help="subscription_oauth (aus `claude setup-token`) "
                             "oder api_key (Anthropic Console)")
    parser.add_argument("--replace", action="store_true",
                        help="einen vorhandenen Eintrag ersetzen")
    args = parser.parse_args()

    # Der Wert steht bewusst NICHT in `args`: argparse-Werte landen in `argv`,
    # und `argv` steht in `ps`.
    wert = read_secret(f"Anthropic-{args.kind} (verdeckt, kein Echo): ")
    if not wert:
        raise SystemExit("FEHLER: leere Eingabe. Nichts gespeichert.")

    nutzlast = json.dumps({"kind": args.kind, "token": wert},
                          separators=(",", ":")).encode("utf-8")
    abdruck = fingerprint(wert)
    del wert

    try:
        policy = VA.add(
            secret_ref=AN.SECRET_REF,
            kind=KINDS[args.kind],
            plaintext=nutzlast,
            allowed_capabilities=[AN.CAPABILITY],
            allowed_targets=[AN.UPSTREAM_ORIGIN],
            allowed_executors=[VP.ExecutorId.ANTHROPIC_BROKER],
            display_name="Anthropic (Claude Writer)",
            service_label=AN.UPSTREAM_HOST,
            note="Development Autopilot V0.6 — nur der Broker-Ausgang leiht "
                 "diesen Wert. Der Builder sieht ihn nie.",
            # Ein unbeaufsichtigter Bauablauf ist genau der Zweck; eine
            # frische Nutzerentscheidung je Anfrage waere das Gegenteil von
            # 24/7. Die Grenze ist die Executor-Bindung, nicht ein Prompt.
            allow_background=True,
            requires_user_presence=False,
            replace=args.replace)
    except VA.AdminError as exc:
        if str(exc) == "secret_already_exists":
            raise SystemExit(
                "FEHLER: es liegt bereits eine Anmeldung unter "
                f"{AN.SECRET_REF}. Mit --replace ersetzen, wenn das gewollt "
                "ist.") from None
        raise SystemExit(f"FEHLER: {exc}") from None
    finally:
        del nutzlast

    print("stored      = true")
    print(f"secret_ref  = {AN.SECRET_REF}")
    print(f"kind        = {args.kind}")
    print(f"version     = {policy.version}")
    print(f"executor    = {VP.ExecutorId.ANTHROPIC_BROKER.value}")
    print(f"target      = {AN.UPSTREAM_ORIGIN}")
    print(f"fingerprint = {abdruck}")
    print("value       = NEVER")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
