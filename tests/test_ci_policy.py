"""The experiment's CI validates code; it is not a release authority."""
import copy
import json
import re
from pathlib import Path

import pytest


def workflow():
    path = Path(__file__).resolve().parents[1] / '.github' / 'workflows' / 'ci.yml'
    assert path.exists(), 'read-only qualification workflow is not implemented'
    return json.loads(path.read_text(encoding='utf-8'))


def assert_validation_only(data):
    assert data['permissions'] == {'contents': 'read'}
    assert not {'pull_request_target', 'workflow_run'} & set(data['on'])
    assert 'secrets.' not in json.dumps(data).lower()
    for job in data['jobs'].values():
        assert 'environment' not in job
        assert job.get('permissions', {'contents': 'read'}) == {'contents': 'read'}
        assert 0 < job['timeout-minutes'] <= 35
        for step in job['steps']:
            action = step.get('uses')
            if action:
                owner, separator, commit = action.partition('@')
                assert owner in {'actions/checkout', 'actions/setup-python',
                                 'actions/upload-artifact'}
                assert separator and re.fullmatch(r'[0-9a-f]{40}', commit)
            command = step.get('run', '')
            assert not re.search(
                r'(?i)(?:\bgh\s+(?:release|api|pr)|\bgit\s+push|\btwine\s+upload|'
                r'\bnpm\s+publish|\bdocker\s+push|\buv\s+publish)', command)
            if str(action or '').startswith('actions/checkout@'):
                assert step.get('with', {}).get('persist-credentials') is False


def test_ci_cannot_publish_or_use_production_secrets():
    assert_validation_only(workflow())


@pytest.mark.parametrize('step', [
    {'run': 'gh release create v1 artifact.whl'},
    {'run': 'git push origin HEAD:main'},
    {'run': 'python -m twine upload dist/*'},
    {'uses': 'softprops/action-gh-release@v2'},
])
def test_ci_guard_rejects_release_or_push_steps(step):
    data = copy.deepcopy(workflow())
    next(iter(data['jobs'].values()))['steps'].append(step)
    with pytest.raises(AssertionError):
        assert_validation_only(data)
