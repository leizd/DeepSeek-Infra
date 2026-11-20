"""Replay the original HTTP boundary that the earlier native-only test contradicted."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

from deepseek_infra.infra.media import library
from tests.test_web_media_routes import _auth_headers, _request, _running_server

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/native_media_selected_normalization_v1.json'


def fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding='utf8'))


def comparator() -> Any:
    spec = importlib.util.spec_from_file_location('selected_json_comparator', ROOT / 'scripts/native_json_comparison.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.json_values_equal


def test_selected_normalization_fixture_is_bound_to_the_original_complete_observation() -> None:
    data = fixture()
    assert data['schemaVersion'] == 1 and data['actualOriginalLoopbackRequests'] == 13
    assert hashlib.sha256((ROOT / 'deepseek_infra/infra/media/library.py').read_bytes()).hexdigest() == data['originalReaderSha256']
    assert len(data['variants']) == 2
    assert sum(len(case['results']) for case in data['variants']) == 13
    for case in data['variants']:
        assert hashlib.sha256(case['document'].encode('utf8')).hexdigest() == case['documentSha256']
        assert case['rawNormalizationAcceptedRows'] == 2 and case['rawNormalizedLaterPath'] == 'C:/x.png'
        assert case['results']['selectedFirst']['status'] == case['results']['emptyFilter']['status'] == 200
        assert case['results']['fullList']['status'] == case['results']['selectedLater']['status'] == 400
        assert case['results']['segmentsLater']['status'] == 400
        assert case['results']['unauthorized']['status'] == 401


def test_original_selected_normalization_replays_all_thirteen_socket_requests(tmp_settings: Path) -> None:
    assert library.store_path().resolve().is_relative_to(tmp_settings.resolve())
    library.media_dir().mkdir(parents=True, exist_ok=True)
    requests = 0
    same = comparator()
    with _running_server() as server:
        for case in fixture()['variants']:
            raw = case['document'].encode('utf8')
            library.store_path().write_bytes(raw)
            for role, expected in case['results'].items():
                status, body, response = _request(server, 'GET', expected['uri'],
                    headers={} if role == 'unauthorized' else _auth_headers())
                assert response.getheader('Content-Type', '').startswith('application/json')
                assert status == expected['status'], (case['name'], role)
                assert same(json.loads(body), expected['body']), (case['name'], role)
                assert library.store_path().read_bytes() == raw
                requests += 1
    assert requests == 13


def test_json_comparison_rejects_boolean_and_large_integer_aliases() -> None:
    same = comparator()
    assert same({'metadata': [1, True, 'λ']}, {'metadata': [1, True, 'λ']})
    assert same({'number': 1.0}, {'number': 1})
    for left, right in [
        ([True, 1, 'λ'], [1, True, 'λ']), ({'x': True}, {'x': 1}),
        ({'x': False}, {'x': 0}), (18446744073709551617, 1.8446744073709552e19),
        ({'x': None}, {}), ('1', 1), (None, False), (float('inf'), float('inf')),
    ]:
        assert not same(left, right)


def test_launcher_timeout_still_waits_for_startup_session_guardians(monkeypatch: Any) -> None:
    from types import SimpleNamespace
    import pytest

    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('selected_read_cleanup', ROOT / 'scripts/verify_native_media_reads.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    observed: list[str] = []
    alive = True

    def members(session: int) -> dict[int, str]:
        nonlocal alive
        assert session == 99101
        observed.append('session')
        if observed.count('session') >= 3:
            alive = False
        return {99102: 'native-guardian-start'} if alive else {}

    def stop_root(_: Any) -> None:
        observed.append('root-timeout')
        raise module.bootstrap.ProbeFailure('DEFAULT_NATIVE_LAUNCHER_DID_NOT_STOP')

    monkeypatch.setattr(module, 'session_identities', members)
    monkeypatch.setattr(module, 'process_identity', lambda _: 'native-guardian-start' if alive else None)
    monkeypatch.setattr(module, 'session_group_alive', lambda _: alive)
    monkeypatch.setattr(module.bootstrap, 'stop', stop_root)
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=module.time.monotonic, sleep=lambda _: None))

    with pytest.raises(module.bootstrap.ProbeFailure, match='DEFAULT_NATIVE_LAUNCHER_DID_NOT_STOP'):
        module.stop(SimpleNamespace(pid=99101), [])
    assert observed.count('session') >= 3 and observed.index('root-timeout') < len(observed) - 1
    assert not alive


def test_cleanup_observation_failure_still_waits_for_captured_children(monkeypatch: Any) -> None:
    from types import SimpleNamespace
    import pytest

    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('selected_read_observer_failure', ROOT / 'scripts/verify_native_media_reads.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    stopped: list[bool] = []
    polls = 0
    alive = True

    def unavailable(_: int) -> dict[int, str]:
        raise OSError('observer unavailable')

    def known_identity(_: int) -> str | None:
        nonlocal polls, alive
        polls += 1
        if polls >= 2:
            alive = False
        return 'captured-guardian-start' if alive else None

    monkeypatch.setattr(module, 'session_identities', unavailable)
    monkeypatch.setattr(module, 'process_identity', known_identity)
    monkeypatch.setattr(module, 'session_group_alive', lambda _: alive)
    monkeypatch.setattr(module.bootstrap, 'stop', lambda _: stopped.append(True))
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=module.time.monotonic, sleep=lambda _: None))
    with pytest.raises(OSError, match='observer unavailable'):
        module.stop(SimpleNamespace(pid=99101), [{'pid': 99102, 'startTicks': 'captured-guardian-start'}])
    assert stopped == [True] and polls >= 2 and not alive
