import hashlib
import os
from pathlib import Path
DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp/app-data"))
RULE_VERSION = "2026.11.18.2"
MAX_BYTES = 2_000_000


def digest_of(text):
    return hashlib.sha256((RULE_VERSION + "\n" + text).encode()).hexdigest()
