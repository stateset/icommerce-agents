import socket

import pytest

from scripts import runtime_check


def test_real_socket_pair_wakeup():
    runtime_check.check_asyncio_wakeup()


@pytest.mark.parametrize("failure", ["creation", "send", "receive", "short_send", "wrong_byte"])
def test_blocked_wakeup_fails_with_actionable_error(monkeypatch, failure):
    sockets = []

    class FakeSocket:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

        def setblocking(self, value):
            assert value is False

        def send(self, value):
            assert value == b"\0"
            if failure == "send":
                raise PermissionError(1, "Operation not permitted")
            return 0 if failure == "short_send" else 1

        def recv(self, count):
            assert count == 1
            if failure == "receive":
                raise BlockingIOError("no wakeup byte")
            return b"x" if failure == "wrong_byte" else b"\0"

    def pair():
        if failure == "creation":
            raise PermissionError(1, "Operation not permitted")
        sockets.extend([FakeSocket(), FakeSocket()])
        return tuple(sockets)

    monkeypatch.setattr(socket, "socketpair", pair)
    with pytest.raises(RuntimeError, match="Threaded engine work can finish") as error:
        runtime_check.check_asyncio_wakeup()
    assert isinstance(error.value.__cause__, OSError)
    assert all(sock.closed for sock in sockets)


def test_cli_failure_is_nonzero(monkeypatch, capsys):
    def blocked():
        raise RuntimeError("blocked wakeups")

    monkeypatch.setattr(runtime_check, "check_asyncio_wakeup", blocked)
    assert runtime_check.main() == 1
    assert "blocked wakeups" in capsys.readouterr().err


def test_cli_success(capsys):
    assert runtime_check.main() == 0
    assert "permitted" in capsys.readouterr().out


def test_pytest_startup_refuses_blocked_environment(monkeypatch):
    from conftest import pytest_sessionstart

    def blocked():
        raise RuntimeError("blocked wakeups")

    monkeypatch.setattr(runtime_check, "check_asyncio_wakeup", blocked)
    with pytest.raises(pytest.UsageError, match="blocked wakeups"):
        pytest_sessionstart(None)
