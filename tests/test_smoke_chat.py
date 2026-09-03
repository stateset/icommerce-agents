import pytest

from scripts import smoke_chat


def test_build_turns_shopping_is_non_empty():
    turns = smoke_chat.build_turns("shopping")
    assert isinstance(turns, list)
    assert len(turns) >= 4
    assert all(isinstance(t, str) and t for t in turns)


def test_build_turns_merchant_is_non_empty():
    turns = smoke_chat.build_turns("merchant")
    assert isinstance(turns, list)
    assert len(turns) >= 4
    assert all(isinstance(t, str) and t for t in turns)


def test_build_turns_unknown_role_raises():
    with pytest.raises(ValueError):
        smoke_chat.build_turns("nope")


def test_main_with_no_api_key_exits_0_with_a_clear_message(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    exit_code = smoke_chat.main([])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "ANTHROPIC_API_KEY" in captured.out
