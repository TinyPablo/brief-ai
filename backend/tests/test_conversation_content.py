from conversation_content import provider_messages
import pytest


@pytest.mark.parametrize('provider', ['anthropic', 'openai', 'google', 'local'])
def test_follow_up_keeps_assistant_turn_separate(provider):
    turns = [{'role': role, 'content': text} for role, text in
             [('user', 'A'), ('assistant', 'B'), ('user', 'C')]]
    messages = provider_messages(turns, provider)
    assert [m['role'] for m in messages] == ['user', 'model' if provider == 'google' else 'assistant', 'user']
    if provider == 'google':
        assert [m['parts'][0]['text'] for m in messages] == ['A', 'B', 'C']
    elif provider == 'local':
        assert [m['content'] for m in messages] == ['A', 'B', 'C']
    else:
        assert [m['content'][0]['text'] for m in messages] == ['A', 'B', 'C']


@pytest.mark.parametrize('provider', ['anthropic', 'openai', 'google'])
def test_previous_image_stays_with_its_original_user_message(provider):
    turns = [{'role': 'user', 'content': 'A', 'images': [{'raw': b'image', 'media_type': 'image/png'}]},
             {'role': 'assistant', 'content': 'B'}, {'role': 'user', 'content': 'C'}]
    messages = provider_messages(turns, provider)
    key = 'parts' if provider == 'google' else 'content'
    assert len(messages[0][key]) == 2
    assert len(messages[1][key]) == len(messages[2][key]) == 1
    if provider == 'google':
        assert messages[0][key][0]['inline_data']['data'] == b'image'
    elif provider == 'anthropic':
        assert messages[0][key][0]['source']['data'] == 'aW1hZ2U='
    else:
        assert messages[0][key][0]['image_url']['url'] == 'data:image/png;base64,aW1hZ2U='


def test_text_only_model_rejects_images_instead_of_silently_discarding_them():
    with pytest.raises(ValueError, match='text only'):
        provider_messages([{'role': 'user', 'content': 'A', 'images': [{'raw': b'x', 'media_type': 'image/png'}]}], 'local')


@pytest.mark.parametrize('provider', ['anthropic', 'openai', 'google'])
def test_image_only_message_has_no_empty_text_block(provider):
    messages = provider_messages([{'role': 'user', 'content': '',
                                  'images': [{'raw': b'image', 'media_type': 'image/png'}]}], provider)
    parts = messages[0]['parts' if provider == 'google' else 'content']
    assert len(parts) == 1
    assert parts[0].get('type') != 'text' and 'text' not in parts[0]
