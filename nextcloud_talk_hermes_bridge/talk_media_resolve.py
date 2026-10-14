"""Global media cache and local/sudo reads are disabled in this deployment."""
def resolve_talk_media_path(*args, **kwargs):
    raise PermissionError('Local media resolution is disabled')
def describe_talk_image_for_vision(*args, **kwargs):
    raise PermissionError('Use delegated user API access for media')
