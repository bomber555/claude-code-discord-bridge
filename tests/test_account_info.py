"""Tests for account_info — reads the signed-in Claude account for display.

Pure-logic module: the on-disk files are provided as tmp_path fixtures so no
real credentials are ever touched.
"""

from __future__ import annotations

import json
from pathlib import Path

from claude_code_core.account_info import (
    AccountInfo,
    describe_auth,
    read_account_info,
)
from claude_code_core.api_provider import uses_cloud_backend


def _write_pair(
    tmp_path: Path,
    *,
    oauth_account: dict | None = None,
    credentials: dict | None = None,
) -> tuple[Path, Path]:
    claude_json = tmp_path / ".claude.json"
    creds = tmp_path / ".credentials.json"
    if oauth_account is not None:
        claude_json.write_text(json.dumps({"oauthAccount": oauth_account}), encoding="utf-8")
    if credentials is not None:
        creds.write_text(json.dumps(credentials), encoding="utf-8")
    return claude_json, creds


# ---------------------------------------------------------------------------
# uses_cloud_backend
# ---------------------------------------------------------------------------


def test_cloud_backend_detection() -> None:
    assert uses_cloud_backend({"CLAUDE_CODE_USE_BEDROCK": "1"}) is True
    assert uses_cloud_backend({"CLAUDE_CODE_USE_VERTEX": "true"}) is True
    assert uses_cloud_backend({"CLAUDE_CODE_USE_FOUNDRY": "yes"}) is True
    assert uses_cloud_backend({}) is False
    assert uses_cloud_backend({"CLAUDE_CODE_USE_BEDROCK": "0"}) is False


# ---------------------------------------------------------------------------
# read_account_info
# ---------------------------------------------------------------------------


def test_reads_email_and_subscription(tmp_path: Path) -> None:
    claude_json, creds = _write_pair(
        tmp_path,
        oauth_account={
            "emailAddress": "someone@example.com",
            "billingType": "stripe_subscription",
            "organizationType": "claude_max",
        },
        credentials={"claudeAiOauth": {"subscriptionType": "max"}},
    )
    info = read_account_info(claude_json_path=claude_json, credentials_path=creds)
    assert info == AccountInfo(
        email="someone@example.com",
        subscription_type="max",
        billing_type="stripe_subscription",
        organization_type="claude_max",
    )


def test_returns_none_when_both_files_missing(tmp_path: Path) -> None:
    info = read_account_info(
        claude_json_path=tmp_path / "nope.json",
        credentials_path=tmp_path / "also-nope.json",
    )
    assert info is None


def test_tolerates_malformed_json(tmp_path: Path) -> None:
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{not json", encoding="utf-8")
    creds = tmp_path / ".credentials.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"subscriptionType": "pro"}}), encoding="utf-8")

    info = read_account_info(claude_json_path=claude_json, credentials_path=creds)
    assert info is not None
    assert info.email is None
    assert info.subscription_type == "pro"


def test_ignores_non_dict_oauth_account(tmp_path: Path) -> None:
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({"oauthAccount": "nope"}), encoding="utf-8")
    info = read_account_info(
        claude_json_path=claude_json,
        credentials_path=tmp_path / "missing.json",
    )
    assert info is None


# ---------------------------------------------------------------------------
# describe_auth
# ---------------------------------------------------------------------------


def test_describe_auth_subscription_with_email() -> None:
    account = AccountInfo(
        email="someone@example.com",
        subscription_type="max",
        billing_type="stripe_subscription",
        organization_type="claude_max",
    )
    assert describe_auth({}, account) == "Max subscription (someone@example.com)"


def test_describe_auth_subscription_without_email() -> None:
    account = AccountInfo(
        email=None,
        subscription_type="pro",
        billing_type=None,
        organization_type=None,
    )
    assert describe_auth({}, account) == "Pro subscription"


def test_describe_auth_unknown_plan_keeps_raw_value() -> None:
    account = AccountInfo(
        email=None,
        subscription_type="enterprise_beta",
        billing_type=None,
        organization_type=None,
    )
    assert describe_auth({}, account) == "enterprise_beta subscription"


def test_describe_auth_api_key_wins_over_subscription() -> None:
    """An API key in the subprocess env means metered billing, not the plan."""
    account = AccountInfo(
        email="someone@example.com",
        subscription_type="max",
        billing_type="stripe_subscription",
        organization_type="claude_max",
    )
    env = {"ANTHROPIC_API_KEY": "sk-ant-xxx"}
    assert describe_auth(env, account) == "API key (metered)"
    assert describe_auth({"ANTHROPIC_AUTH_TOKEN": "tok"}, account) == "API key (metered)"


def test_describe_auth_blank_api_key_is_not_a_key() -> None:
    account = AccountInfo(
        email=None,
        subscription_type="max",
        billing_type=None,
        organization_type=None,
    )
    assert describe_auth({"ANTHROPIC_API_KEY": "   "}, account) == "Max subscription"


def test_describe_auth_none_for_cloud_backends() -> None:
    """Bedrock/Vertex/Foundry authenticate with cloud credentials, not an account."""
    account = AccountInfo(
        email="someone@example.com",
        subscription_type="max",
        billing_type=None,
        organization_type=None,
    )
    assert describe_auth({"CLAUDE_CODE_USE_BEDROCK": "1"}, account) is None
    assert describe_auth({"CLAUDE_CODE_USE_VERTEX": "1"}, account) is None
    assert describe_auth({"CLAUDE_CODE_USE_FOUNDRY": "1"}, account) is None


def test_describe_auth_email_only_account() -> None:
    account = AccountInfo(
        email="someone@example.com",
        subscription_type=None,
        billing_type=None,
        organization_type=None,
    )
    assert describe_auth({}, account) == "Claude account (someone@example.com)"


def test_describe_auth_none_without_account() -> None:
    assert describe_auth({}, None) is None
