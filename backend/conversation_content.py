"""Translate stored turns into provider-specific multimodal messages."""

import base64


def provider_messages(turns, provider):
    messages = []
    for turn in turns:
        role, text = turn['role'], turn['content']
        parts = []
        for image in turn.get('images', []):
            raw = image['raw']
            encoded = base64.b64encode(raw).decode('ascii')
            if provider == 'anthropic':
                parts.append({'type': 'image', 'source': {
                    'type': 'base64', 'media_type': image['media_type'], 'data': encoded,
                }})
            elif provider == 'openai':
                parts.append({'type': 'image_url', 'image_url': {
                    'url': f"data:{image['media_type']};base64,{encoded}",
                }})
            elif provider == 'google':
                parts.append({'inline_data': {'mime_type': image['media_type'], 'data': raw}})
            else:
                raise ValueError('This model supports text only.')
        if provider == 'google':
            if text or not parts:
                parts.append({'text': text})
            messages.append({'role': 'model' if role == 'assistant' else 'user', 'parts': parts})
        elif provider in ('anthropic', 'openai'):
            if text or not parts:
                parts.append({'type': 'text', 'text': text})
            messages.append({'role': role, 'content': parts})
        else:
            messages.append({'role': role, 'content': text})
    return messages
