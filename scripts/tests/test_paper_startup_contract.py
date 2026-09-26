"""Static contracts for the paper Binance/Bybit startup and CI profile."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_startup_scripts_install_only_the_hash_locked_paper_profile():
    for relative in ("setup.sh", "start.sh", "start.ps1"):
        text = _read(relative)
        assert "requirements.paper.lock" in text
        assert "--require-hashes" in text
        assert "fastapi \"uvicorn[standard]\" websockets requests" not in text
        assert "requirements.lock" not in text


def test_ci_python_install_uses_the_hash_locked_paper_profile():
    text = _read(".github/workflows/ci.yml")
    assert "pip install --require-hashes -r requirements.paper.lock" in text
    assert "pip install -r server/requirements.txt requests pytest" not in text
    assert "requirements.lock" not in text


def test_telegram_scan_rejects_unsupported_dex_before_any_install():
    text = _read(".github/workflows/telegram-push.yml")
    reject = text.index("Reject unsupported DEX opt-in")
    checkout = text.index("actions/checkout")
    setup_python = text.index("actions/setup-python")
    pip_install = text.index("pip install")
    assert reject < checkout < setup_python < pip_install
    assert "DEX scanning is unsupported in this paper workflow" in text
    assert "default: false" in text[text.index("include_dex:") : text.index("include_dex:") + 220]
    assert "requirements.paper.lock" in text
    assert "requirements.lock" not in text
    assert "--venues binance,bybit" in text
    assert "--include-dex" not in text


def test_default_contributor_docs_use_the_paper_lock():
    text = _read("CONTRIBUTING.md")
    assert "--require-hashes -r requirements.paper.lock" in text
    assert "pip install -r requirements.txt" not in text
    assert "pip install -r server/requirements.txt" not in text


def test_default_docs_use_the_paper_lock():
    for relative in (
        "README.md",
        "docs/en/overview.md",
        "docs/zh-CN/overview.md",
        "docs/zh-TW/overview.md",
        "web/src/content/docs/articles/readme.ts",
    ):
        text = _read(relative)
        assert "requirements.paper.lock" in text
        assert "pip install -r requirements.txt" not in text


def test_serverless_workflow_examples_do_not_opt_into_unsupported_dex():
    for relative in (
        "docs/en/serverless-pipeline.md",
        "docs/zh-CN/serverless-pipeline.md",
        "docs/zh-TW/serverless-pipeline.md",
        "web/src/content/docs/articles/serverlessPipeline.ts",
    ):
        text = _read(relative)
        assert '"include_dex": false' in text
        assert '"include_dex": true' not in text


def test_paper_lock_excludes_optional_dex_sdks_and_hashes_every_requirement():
    text = _read("requirements.paper.lock")
    package_names = {
        "lighter-sdk",
        "edgex-python-sdk",
        "dydx-v4-client",
        "hyperliquid-python-sdk",
    }
    lines = text.splitlines()
    assert not any(
        any(line.lower().startswith(f"{name}==") for name in package_names)
        for line in lines
    )
    package_entries = [
        (index, line)
        for index, line in enumerate(lines)
        if line and not line.startswith("#") and not line.startswith(" ") and "==" in line
    ]
    assert package_entries
    for index, line in package_entries:
        block = []
        for candidate in lines[index:]:
            if (
                block
                and candidate
                and not candidate.startswith(" ")
                and not candidate.startswith("#")
                and "==" in candidate
            ):
                break
            block.append(candidate)
        assert any("--hash=sha256:" in candidate for candidate in block), line


def test_live_execution_gate_is_not_replaced_by_startup_configuration():
    text = _read("scripts/routes/positions.py") if (ROOT / "scripts/routes/positions.py").exists() else _read("server/routes/positions.py")
    assert 'EXECUTION_VENUES = frozenset({"binance", "bybit"})' in text
    assert "live_trading_enabled" in text
    assert "FARB_LIVE" not in _read("start.sh")
    assert "FARB_LIVE" not in _read("start.ps1")
