from copy import deepcopy

import pytest

from charts import chart_context, validate_chart, validate_charts
from conversation_content import provider_messages


def sample_chart(kind='bar'):
    return dict(version=1, type=kind, title='Monthly expenses', x_label='Month', y_label='PLN',
                source='conversation', assumptions='',
                series=[dict(name='Expenses', points=[dict(x='January' if kind != 'scatter' else 1, y=1200)])])


@pytest.mark.parametrize('kind', ['bar', 'line', 'scatter'])
def test_supported_charts_round_trip(kind):
    chart = sample_chart(kind)
    original = deepcopy(chart)
    assert validate_chart(chart) == chart == original


@pytest.mark.parametrize('value', [True, None, '1200', float('nan'), float('inf'), 1e16, 10**1000])
def test_invalid_coordinates(value):
    chart = sample_chart()
    chart['series'][0]['points'][0]['y'] = value
    with pytest.raises(ValueError):
        validate_chart(chart)


def test_examples_require_assumptions():
    chart = sample_chart()
    chart['source'] = 'example'
    with pytest.raises(ValueError, match='assumptions'):
        validate_chart(chart)
    chart['assumptions'] = 'Illustrative monthly spending, not actual records.'
    assert validate_chart(chart)['source'] == 'example'


def test_limits_and_untrusted_code_are_rejected():
    chart = sample_chart()
    with pytest.raises(ValueError):
        validate_chart({**chart, 'javascript': 'alert(1)'})
    with pytest.raises(ValueError):
        validate_charts([chart] * 4)
    chart['series'][0]['points'] = [dict(x=str(i), y=i) for i in range(401)]
    with pytest.raises(ValueError):
        validate_chart(chart)


def test_mixed_line_axes_and_duplicate_categories_are_rejected():
    chart = sample_chart('line')
    chart['series'][0]['points'].append(dict(x=2, y=1500))
    with pytest.raises(ValueError, match='same kind'):
        validate_chart(chart)
    chart['series'][0]['points'][1]['x'] = 'January'
    with pytest.raises(ValueError, match='repeat'):
        validate_chart(chart)


@pytest.mark.parametrize('provider', ['anthropic', 'openai', 'google', 'local'])
def test_follow_up_receives_chart_data_with_original_assistant(provider):
    chart = sample_chart()
    turns = [dict(role='assistant', content='Here is the comparison.', charts=[chart]),
             dict(role='user', content='What about January?')]
    messages = provider_messages(turns, provider)
    if provider == 'local':
        text = messages[0]['content']
    else:
        text = messages[0]['parts' if provider == 'google' else 'content'][0]['text']
    assert chart_context([chart]) in text
    assert '1200' in text and 'January' in text
