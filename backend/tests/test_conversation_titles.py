from types import SimpleNamespace

import pytest

from conversation_titles import TITLE_SYSTEM, clean_title, generate_title


@pytest.mark.parametrize('text, expected', [
    ('"Weekend w Krakowie"', 'Weekend w Krakowie'),
    ('  # Plan   na\nweekend. ', 'Plan na weekend'),
    ('', None), ('x' * 101, None), ('_No response._', None),
])
def test_title_cleanup(text, expected):
    assert clean_title(text) == expected


@pytest.mark.parametrize('provider, cheap', [('google', 'gemini-3.1-flash-lite'),
                                            ('anthropic', 'claude-haiku-4-5'), ('openai', 'gpt-5.4-nano')])
def test_title_uses_small_model_and_only_bounded_question_and_answer(provider, cheap):
    captured = {}

    def call(model, prompt, depth, **kwargs):
        captured.update(model=model, prompt=prompt, depth=depth, **kwargs)
        return {'answer': 'Short title'}

    api = SimpleNamespace(MODELS={'source': {'provider': provider}}, DEFAULT_DEPTH='low',
                          call_gemini=call, call_anthropic=call, call_openai=call)
    generate_title(api, 'source', 'q' * 3000, 'a' * 3000)
    assert captured['model'] == cheap
    assert captured['system_prompt'] == TITLE_SYSTEM
    assert captured['depth'] == 'low'
    assert captured['output_limit'] == 512
    assert captured['prompt'].count('q') == 2000
    assert captured['prompt'].count('a') == 2000
    assert 'images' not in captured and 'messages' not in captured
