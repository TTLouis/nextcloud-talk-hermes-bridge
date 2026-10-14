"""Local Nextcloud data/database resolution is intentionally disabled.

Media is handled using the authenticated conversation user's Nextcloud APIs.
"""
def resolve_share_path(*args, **kwargs):
    raise PermissionError('Local share/database resolution is disabled')
def resolve_user_file_path(*args, **kwargs):
    raise PermissionError('Fixed-user local file resolution is disabled')
def transcribe_from_talk_params(*args, **kwargs):
    raise PermissionError('Use delegated user API access for media')
