"""The keyless tour: real routes, real engine, no model. Asserts on structured outcomes
-- the completed order and the two evidence kinds -- never on narration prose."""

from __future__ import annotations

from scripts.tour import main, run_tour


def test_run_tour_places_an_order_and_records_both_evidence_kinds(tmp_path):
    result = run_tour(str(tmp_path / "tour.db"))
    assert result.ok, result.steps
    assert result.order_number
    assert result.refused_unapproved_apply is True
    assert set(result.evidence_kinds) == {"activity_log", "kernel_receipt"}


def test_run_tour_fails_if_the_unapproved_apply_were_to_succeed(tmp_path, monkeypatch):
    """A synthetic check that the tour's own success condition would flip if the
    refusal did not fire: monkeypatch `EngineMerchant.apply_change` to always succeed
    (simulating a missing approval gate) and confirm `run_tour` reports failure."""

    from engine_backend.merchant import EngineMerchant

    async def _always_succeeds(self, session, change_id):
        from engine_backend import staging

        change = await staging.load(self.store, change_id)
        return change

    monkeypatch.setattr(EngineMerchant, "apply_change", _always_succeeds)
    result = run_tour(str(tmp_path / "tour-broken.db"))
    assert result.ok is False
    assert result.refused_unapproved_apply is False


def test_main_exits_zero_with_no_api_key_set(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    code = main(["--db", str(tmp_path / "tour-cli.db")])
    assert code == 0
