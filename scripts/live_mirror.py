"""Best-effort progress mirrors; authoritative collector files remain untouched."""
import json
import os
from pathlib import Path
import shutil
import traceback
import uuid
from datetime import datetime, timezone


def record_error(output, exc):
    value={'at_utc':datetime.now(timezone.utc).isoformat(),'error_type':type(exc).__name__,
           'errno':getattr(exc,'errno',None),'message':str(exc),
           'filename':getattr(exc,'filename',None),'filename2':getattr(exc,'filename2',None),
           'traceback':traceback.format_exc(),'action':'keep_supervising_original_pier_process'}
    # This is host-only metadata, never a model prompt or an HTTP header.
    with (Path(output)/'mirror_errors.jsonl').open('a',encoding='utf-8') as handle:
        handle.write(json.dumps(value)+'\n');handle.flush();os.fsync(handle.fileno())


def atomic_copy(source, target):
    """Publish one bounded snapshot; a failed transfer leaves the previous copy intact."""
    source,target=Path(source),Path(target)
    temporary=target.with_name('.'+target.name+'.sync-'+uuid.uuid4().hex+'.tmp')
    try:
        with source.open('rb') as reader, temporary.open('xb') as writer:
            remaining=os.fstat(reader.fileno()).st_size
            while remaining:
                chunk=reader.read(min(1024*1024,remaining))
                if not chunk:break
                writer.write(chunk);remaining-=len(chunk)
            writer.flush();os.fsync(writer.fileno())
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def mirror_progress(job, output, windows):
    """An OS failure in a progress copy must not detach the paid child process."""
    try:
        if not job.exists():return
        for trial in job.iterdir():
            requests=trial/'requests'
            if not trial.is_dir() or not requests.exists():continue
            for name in ('requests.jsonl','usage.csv','checkpoint.json'):
                source=requests/name
                if source.exists():
                    atomic_copy(source,output/name)
                    atomic_copy(source,windows/name)
    except OSError as exc:
        record_error(output,exc)
