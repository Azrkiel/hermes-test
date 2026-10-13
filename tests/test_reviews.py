"""Review-layer tests use real Git repositories, without launching agent CLIs."""
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge_core import Bridge, BridgeError, database, load, save


@pytest.fixture
def built(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args, cwd=repo):
        return subprocess.check_output(['git', '-C', str(cwd), *args], stderr=subprocess.STDOUT, text=True).strip()
    git('init')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.invalid')
    (repo / 'base.txt').write_text('base\n')
    (repo / '.gitignore').write_text('ignored.txt\n')
    git('add', '.')
    git('commit', '-m', 'base')
    config = tmp_path / 'agents.json'
    config.write_text(json.dumps({'agents': {
        'claude': {'family': 'claude', 'build': [sys.executable, '-c', 'pass']},
        'codex': {'family': 'codex', 'review': [sys.executable, '-c', 'pass']},
        'alias': {'family': 'claude', 'review': [sys.executable, '-c', 'pass']},
    }}))
    bridge = Bridge(roots=[str(tmp_path)], agents_file=str(config))
    bridge._spawn = lambda *args: None
    task = bridge.dispatch_task(str(repo), 'build')
    work = Path(task['worktree'])
    (work / 'committed.txt').write_text('committed\n')
    git('add', 'committed.txt', cwd=work)
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'builder', cwd=work)
    (work / 'base.txt').write_text('unstaged\n')
    (work / 'untracked.txt').write_text('new\n')
    (work / 'ignored.txt').write_text('excluded\n')
    with database(repo) as db:
        task = load(db, task['task_id'])
        task['status'] = 'done'
        save(db, task)
    return bridge, repo, task, git


def review_module():
    assert importlib.util.find_spec('bridge_review') is not None, 'review layer is missing'
    return importlib.import_module('bridge_review')


def test_review_creates_independent_clean_full_snapshot_without_staging(built):
    mod = review_module()
    bridge, repo, task, git = built
    before = git('diff', '--cached', cwd=task['worktree'])
    reviewed = mod.review_task(bridge, str(repo), task['task_id'])
    snapshot = Path(reviewed['worktree'])
    assert reviewed['status'] == 'queued'
    assert reviewed['kind'] == 'review'
    assert (snapshot / '.git').is_dir()
    assert git('rev-parse', '--git-common-dir', cwd=snapshot) == '.git'
    assert git('status', '--porcelain', cwd=snapshot) == ''
    assert (snapshot / 'base.txt').read_text() == 'unstaged\n'
    assert (snapshot / 'committed.txt').read_text() == 'committed\n'
    assert (snapshot / 'untracked.txt').read_text() == 'new\n'
    assert not (snapshot / 'ignored.txt').exists()
    assert not (snapshot / '.bridge').exists()
    assert before == git('diff', '--cached', cwd=task['worktree'])
    with database(repo) as db:
        builder = load(db, task['task_id'])
    assert builder['review_task_id'] == reviewed['task_id']
    assert builder['review_status'] == 'queued'


def test_finish_review_accepts_final_verdict_and_attaches_status(built):
    mod = review_module()
    bridge, repo, task, git = built
    review = mod.review_task(bridge, repo, task['task_id'])
    (Path(review['task_dir']) / 'output.log').write_text('Everything checked.\nVERDICT: APPROVE\n')
    assert hasattr(mod, 'finish_review'), 'review finalization is missing'
    mod.finish_review(repo, review['task_id'], 0, 'done')
    with database(repo) as db:
        result = load(db, review['task_id'])
        builder = load(db, task['task_id'])
    assert result['status'] == 'done'
    assert result['verdict'] == 'APPROVE'
    assert (Path(review['task_dir']) / 'review.json').exists()
    enriched = mod.attach_review(bridge, repo, builder)
    assert enriched['review_status'] == 'approved'
    assert enriched['review']['verdict'] == 'APPROVE'
    (Path(task['worktree']) / 'untracked.txt').write_text('changed after review')
    enriched = mod.attach_review(bridge, repo, builder)
    assert enriched['review_status'] == 'invalidated'
    assert enriched['review']['verdict'] is None


def test_finish_does_not_approve_after_stop_request(built):
    mod = review_module()
    bridge, repo, task, git = built
    review = mod.review_task(bridge, repo, task['task_id'])
    (Path(review['task_dir']) / 'output.log').write_text('VERDICT: APPROVE\n')
    with database(repo) as db:
        current = load(db, review['task_id'])
        current['stop_request'] = 'cancelled'
        save(db, current)
    result = mod.finish_review(repo, review['task_id'], 0, 'done')
    assert result['status'] == 'cancelled'
    assert result['verdict'] is None


@pytest.mark.parametrize('output', [
    '', 'VERDICT: APPROVE\ncontinued text', 'prefix VERDICT: APPROVE',
    'VERDICT: APPROVE\nVERDICT: APPROVE',
    'VERDICT: CHANGES_REQUESTED\nVERDICT: APPROVE',
    ' VERDICT: APPROVE', 'VERDICT: APPROVE ', 'x' * (1024 * 1024) + '\nVERDICT: APPROVE',
] , ids=['missing', 'trailing-text', 'prefixed', 'duplicate', 'conflicting', 'leading-space', 'trailing-space', 'oversized'])
def test_ambiguous_or_unbounded_results_fail_closed(built, output):
    mod = review_module()
    bridge, repo, task, git = built
    review = mod.review_task(bridge, repo, task['task_id'])
    (Path(review['task_dir']) / 'output.log').write_text(output)
    result = mod.finish_review(repo, review['task_id'], 0, 'done')
    assert result['status'] == 'failed'
    assert result['verdict'] is None


@pytest.mark.parametrize('mutation', ['tracked', 'untracked', 'ignored', 'source'])
def test_mutations_invalidate_a_review(built, mutation):
    mod = review_module()
    bridge, repo, task, git = built
    review = mod.review_task(bridge, repo, task['task_id'])
    (Path(review['task_dir']) / 'output.log').write_text('VERDICT: APPROVE\n')
    if mutation == 'source':
        (Path(task['worktree']) / 'base.txt').write_text('source mutated')
    else:
        name = {'tracked': 'base.txt', 'untracked': 'extra.txt', 'ignored': 'ignored.txt'}[mutation]
        (Path(review['worktree']) / name).write_text('reviewer mutated')
    result = mod.finish_review(repo, review['task_id'], 0, 'done')
    assert result['status'] == 'failed'
    assert result['verdict'] is None


def test_result_file_avoids_duplicate_cli_transcript(built):
    mod = review_module()
    bridge, repo, task, git = built
    config = json.loads(bridge.agents_file.read_text())
    config['agents']['codex']['result_file'] = True
    bridge.agents_file.write_text(json.dumps(config))
    review = mod.review_task(bridge, repo, task['task_id'])
    (Path(review['task_dir']) / 'output.log').write_text('VERDICT: APPROVE\nVERDICT: APPROVE\n')
    (Path(review['task_dir']) / 'result.txt').write_text('Findings.\nVERDICT: CHANGES_REQUESTED\n')
    result = mod.finish_review(repo, review['task_id'], 0, 'done')
    assert result['status'] == 'done'
    assert result['verdict'] == 'CHANGES_REQUESTED'


def test_validation_refuses_same_family_active_and_truncated(built, monkeypatch):
    mod = review_module()
    bridge, repo, task, git = built
    with pytest.raises(BridgeError, match='different'):
        mod.review_task(bridge, repo, task['task_id'], agent='alias')
    monkeypatch.setattr(bridge, 'get_diff', lambda *args, **kw: {'truncated': True})
    with pytest.raises(BridgeError, match='size'):
        mod.review_task(bridge, repo, task['task_id'])
    monkeypatch.undo()
    mod.review_task(bridge, repo, task['task_id'])
    with pytest.raises(BridgeError, match='active'):
        mod.review_task(bridge, repo, task['task_id'])


def test_reparse_directory_is_not_followed(built):
    mod = review_module()
    bridge, repo, task, git = built
    outside = repo.parent / 'outside'
    outside.mkdir()
    (outside / 'secret.txt').write_text('must not read')
    junction = Path(task['worktree']) / 'junction'
    if sys.platform == 'win32':
        subprocess.check_call(['cmd', '/c', 'mklink', '/J', str(junction), str(outside)], stdout=subprocess.DEVNULL)
    else:
        junction.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(BridgeError, match='symlink/reparse'):
            mod.review_task(bridge, repo, task['task_id'])
    finally:
        if sys.platform == 'win32':
            junction.rmdir()
        else:
            junction.unlink()
