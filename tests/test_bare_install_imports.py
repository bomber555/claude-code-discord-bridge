"""A Discord-only install has no extras, so optional packages must not break startup."""

from __future__ import annotations

import subprocess
import sys


def test_main_imports_without_pyjwt() -> None:
    code = "import sys; sys.modules['jwt'] = None; import claude_discord.main"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_entra_stays_off_without_pyjwt() -> None:
    code = (
        "import sys; sys.modules['jwt'] = None\n"
        "from claude_discord.ext.entra_auth import EntraTokenVerifier as V\n"
        "env = {'CCDB_INGEST_ENTRA_TENANT_ID': 't', 'CCDB_INGEST_ENTRA_CLIENT_ID': 'c',"
        " 'CCDB_INGEST_ENTRA_ALLOWED_OIDS': 'o'}\n"
        "assert V.from_env(env) is None\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
