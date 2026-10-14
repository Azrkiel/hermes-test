"""Scoped power requests: fake only the native execution-state boundary."""

import sys
import threading

import pytest

import bridge_power as power


class ExecutionState:
    def __init__(self, *results):
        self.results = iter(results)
        self.calls = []

    def __call__(self, flags):
        self.calls.append((flags, threading.get_ident()))
        return next(self.results)


def test_windows_request_restores_previous_execution_state(monkeypatch):
    previous = 0x80000002
    native = ExecutionState(previous, 0x80000001)
    monkeypatch.setattr(power.sys, "platform", "win32")
    monkeypatch.setattr(power, "_load_execution_state", lambda: native)

    with power.keep_awake() as state:
        assert state == {"supported": True, "active": True}
        assert native.calls == [(0x80000001, threading.get_ident())]

    assert native.calls == [
        (0x80000001, threading.get_ident()),
        (previous, threading.get_ident()),
    ]
    assert state["active"] is False


def test_non_windows_reports_no_sleep_prevention(monkeypatch):
    monkeypatch.setattr(power.sys, "platform", "linux")

    def forbidden_native_load():
        raise AssertionError("non-Windows must not load kernel32")

    monkeypatch.setattr(power, "_load_execution_state", forbidden_native_load)
    with power.keep_awake() as state:
        assert state == {"supported": False, "active": False}
    assert state == {"supported": False, "active": False}


def test_windows_refusal_fails_before_entering_scope(monkeypatch):
    native = ExecutionState(0, 0x80000000)
    monkeypatch.setattr(power.sys, "platform", "win32")
    monkeypatch.setattr(power, "_load_execution_state", lambda: native)

    with pytest.raises(RuntimeError, match="SetThreadExecutionState.*refused"):
        with power.keep_awake():
            pytest.fail("a refused request must not run the protected body")
    assert native.calls == [(0x80000001, threading.get_ident())]


@pytest.mark.parametrize("body_raises", [False, True])
def test_windows_restoration_refusal_is_visible(monkeypatch, body_raises):
    native = ExecutionState(0x80000000, 0)
    monkeypatch.setattr(power.sys, "platform", "win32")
    monkeypatch.setattr(power, "_load_execution_state", lambda: native)
    original = ValueError("body failed")

    with pytest.raises(RuntimeError, match="SetThreadExecutionState.*restor") as caught:
        with power.keep_awake() as state:
            if body_raises:
                raise original

    assert state["active"] is True  # Restoration failed: never claim release.
    assert native.calls == [
        (0x80000001, threading.get_ident()),
        (0x80000000, threading.get_ident()),
    ]
    if body_raises:
        assert caught.value.__context__ is original


def test_cross_thread_exit_is_rejected_without_changing_other_thread(monkeypatch):
    native = ExecutionState(0x80000000, 0x80000001)
    monkeypatch.setattr(power.sys, "platform", "win32")
    monkeypatch.setattr(power, "_load_execution_state", lambda: native)
    scope = power.keep_awake()
    state = scope.__enter__()
    failures = []

    def exit_on_other_thread():
        try:
            scope.__exit__(None, None, None)
        except RuntimeError as error:
            failures.append(error)

    thread = threading.Thread(target=exit_on_other_thread)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(failures) == 1, "cross-thread exit must not silently restore the wrong thread"
    assert "same thread" in str(failures[0])
    assert native.calls == [(0x80000001, threading.get_ident())]
    assert state["active"] is True


def test_body_exception_restores_state_and_propagates_unchanged(monkeypatch):
    native = ExecutionState(0x80000000, 0x80000001)
    monkeypatch.setattr(power.sys, "platform", "win32")
    monkeypatch.setattr(power, "_load_execution_state", lambda: native)
    original = KeyboardInterrupt("interrupted")

    with pytest.raises(KeyboardInterrupt) as caught:
        with power.keep_awake() as state:
            raise original

    assert caught.value is original
    assert state["active"] is False
    assert native.calls == [
        (0x80000001, threading.get_ident()),
        (0x80000000, threading.get_ident()),
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows API only")
@pytest.mark.parametrize("body_raises", [False, True])
def test_real_windows_request_is_acquired_and_restored(body_raises):
    native = power._load_execution_state()
    continuous = power.ES_CONTINUOUS
    required = continuous | power.ES_SYSTEM_REQUIRED
    original = native(continuous)
    assert original != 0, "Windows refused the test's thread-only baseline request"
    acquired = []
    try:
        def exercise_scope():
            with power.keep_awake() as state:
                assert state == {"supported": True, "active": True}
                held = native(required)
                acquired.append(held)
                assert held == required, f"request not active: {held:#x}"
                if body_raises:
                    raise ValueError("native scope interrupted")
            return state

        if body_raises:
            with pytest.raises(ValueError, match="native scope interrupted"):
                exercise_scope()
        else:
            assert exercise_scope()["active"] is False

        restored = native(continuous)
        assert restored == continuous, f"prior state not restored: {restored:#x}"
        print(f"native proof: acquired={acquired[-1]:#x}, restored={restored:#x}")
    finally:
        assert native(original) != 0, "Windows refused restoration of the original test-thread state"
