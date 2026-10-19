import errno
import json
import pytest
import live_mirror


def test_failed_transfer_keeps_old_snapshot_and_cleans_temp(tmp_path,monkeypatch):
    source=tmp_path/'source';target=tmp_path/'target'
    source.write_bytes(b'new complete snapshot');target.write_bytes(b'old valid snapshot')
    original=live_mirror.os.fsync
    monkeypatch.setattr(live_mirror.os,'fsync',lambda fd:(_ for _ in ()).throw(OSError(errno.EIO,'simulated transfer error')))
    with pytest.raises(OSError):live_mirror.atomic_copy(source,target)
    assert target.read_bytes()==b'old valid snapshot'
    assert not list(tmp_path.glob('.*.tmp'))
    monkeypatch.setattr(live_mirror.os,'fsync',original)
    live_mirror.atomic_copy(source,target)
    assert target.read_bytes()==source.read_bytes()


def test_mirror_oserror_is_diagnostic_not_worker_failure(tmp_path,monkeypatch):
    job=tmp_path/'job';requests=job/'trial/requests';requests.mkdir(parents=True)
    (requests/'usage.csv').write_text('original')
    output=tmp_path/'output';output.mkdir();windows=tmp_path/'windows';windows.mkdir()
    def fail(*a):raise OSError(errno.EACCES,'simulated lock','usage.csv')
    monkeypatch.setattr(live_mirror,'atomic_copy',fail)
    live_mirror.mirror_progress(job,output,windows)
    event=json.loads((output/'mirror_errors.jsonl').read_text())
    assert event['errno']==errno.EACCES
    assert event['filename']=='usage.csv'
    assert event['action']=='keep_supervising_original_pier_process'
