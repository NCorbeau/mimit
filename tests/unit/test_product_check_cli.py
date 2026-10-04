"""The operator command reports a safe failure without leaking runtime secrets."""

from uuid import UUID, uuid4

import pytest

from mimit.products import check


def test_configuration_or_database_exception_is_redacted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def broken(consumable_id: UUID, household_id: UUID | None) -> int:
        raise RuntimeError("postgres://secret:password@internal/db?token=private")

    monkeypatch.setattr(check, "_run", broken)
    assert check.main([str(uuid4())]) == 2
    captured = capsys.readouterr()
    assert "configuration_or_runtime_error" in captured.out
    assert "password" not in captured.out and "private" not in captured.out
    assert not captured.err


def test_optional_ownership_scope_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    item_id, household_id = uuid4(), uuid4()

    async def run(consumable_id: UUID, household: UUID | None) -> int:
        assert consumable_id == item_id and household == household_id
        return 0

    monkeypatch.setattr(check, "_run", run)
    assert check.main([str(item_id), "--household-id", str(household_id)]) == 0
