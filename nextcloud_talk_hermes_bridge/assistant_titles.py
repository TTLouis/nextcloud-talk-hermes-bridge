"""Consistent visible roles for Talk conversations, independent of model wording."""
import re

MAIN_TITLE = '主助理 · Hermes'

def conversation_title(value, *, main=False):
    if not isinstance(value,str):raise ValueError('Invalid conversation name')
    label=value.strip()
    if not label or len(label)>100 or any(ord(c)<32 for c in label):raise ValueError('Invalid conversation name')
    role='主助理' if main else '主题' if re.match(r'^主题\s*[·｜|:：]',label) else '项目'
    # Strip only leading role labels, never a similar word inside the subject.
    while True:
        trimmed=re.sub(r'^(?:主助理|主对话|个人助理|项目|主题)\s*[·｜|:：]\s*','',label,count=1)
        if trimmed==label:break
        label=trimmed
    if main and label in ('主助理','主对话','个人助理','Hermes AI'):label='Hermes'
    if not label:raise ValueError('A subject is required')
    title=role+' · '+label
    if len(title)>100:raise ValueError('Conversation name is too long')
    return title
