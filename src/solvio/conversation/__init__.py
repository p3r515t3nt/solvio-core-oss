"""SOLVIO-eigene Gespraechsverwaltung (M1).

Eine Provider-Sitzung ist Transport. Ein SOLVIO-Gespraech ist ein Produktobjekt, das
den Wechsel des Providers ueberlebt.
"""
from solvio.conversation.store import (  # noqa: F401
    ConversationStore,
    ConversationStoreError,
    DEFAULT_LINGER_SECONDS,
    DEFAULT_CONTEXT_CHARS,
    default_db_path,
    state_dir,
)
