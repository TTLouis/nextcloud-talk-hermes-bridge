"""Deterministic identity/context keys and Linux descriptor-relative workspace IO."""
import hashlib
import os
from pathlib import Path, PurePosixPath
import re

def context_key(uid, room, thread):
    if not isinstance(uid,str) or not uid or not isinstance(room,str) or not re.fullmatch('[a-zA-Z0-9-]{4,64}',room):
        raise ValueError('Invalid identity')
    if type(thread) is not int or not 0 <= thread < 2**63:
        raise ValueError('A valid conversation/task context is required')
    return hashlib.sha256((uid+'\0'+room+'\0'+str(thread)).encode()).hexdigest()[:32]

def relative_path(value):
    if not isinstance(value,str) or not value or len(value)>1024 or '\\' in value or '\0' in value:
        raise ValueError('Invalid path')
    parts=value.split('/')
    if any(p in ('','.','..') for p in parts):
        raise ValueError('Invalid path')
    return parts

def read_workspace(root, relative, limit=20*1024*1024):
    """Never follow a model-controlled symlink, including during replacement races."""
    parts=relative_path(relative)
    current=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            following=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current)
            os.close(current);current=following
        fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=current)
        try:
            import stat
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size>limit:
                raise ValueError('Unsupported or oversized artifact')
            with os.fdopen(fd,'rb',closefd=False) as handle:
                data=handle.read(limit+1)
            if len(data)>limit:raise ValueError('Oversized artifact')
            return data
        finally:os.close(fd)
    finally:os.close(current)

def write_workspace(root, relative, data):
    parts=relative_path(relative)
    current=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            try:os.mkdir(part,0o700,dir_fd=current)
            except FileExistsError:pass
            following=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current)
            os.close(current);current=following
        fd=os.open(parts[-1],os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=current)
        try:
            with os.fdopen(fd,'wb',closefd=False) as handle:handle.write(data)
        finally:os.close(fd)
    finally:os.close(current)

def task_workspace(base, uid, room, thread):
    key=context_key(uid,room,thread)
    project=hashlib.sha256((uid+'\0'+room).encode()).hexdigest()[:24]
    relative='projects/'+project+'/tasks/'+key
    current=os.open(base,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in relative.split('/'):
            try:os.mkdir(part,0o700,dir_fd=current)
            except FileExistsError:pass
            following=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=current)
            os.close(current);current=following
        for part in ['scratch','hermes']:
            try:os.mkdir(part,0o700,dir_fd=current)
            except FileExistsError:pass
    finally:os.close(current)
    root=Path(base)/relative
    return root
