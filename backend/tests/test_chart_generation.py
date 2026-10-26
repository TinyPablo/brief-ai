import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from google.genai import types

from chart_generation import (CHART_SCHEMA, ChartSession, anthropic_generate,
                              extract_local_charts, google_generate, openai_generate)
from test_charts import sample_chart

RATES = {'model': {'input': 1, 'output': 2}, '__default__': {'input': 1, 'output': 2}}


def test_tool_dispatch_validates_and_deduplicates():
    session = ChartSession(RATES)
    assert not session.execute('shell', {})['ok']
    assert not session.execute('create_chart', '{bad json')['ok']
    assert session.execute('create_chart', json.dumps(sample_chart()))['ok']
    assert session.execute('create_chart', sample_chart())['already_saved']
    assert len(session.charts) == 1


def test_local_blocks_are_extracted_and_truncation_is_reported():
    text = 'Comparison:\n```brief-chart\n'+json.dumps(sample_chart())+'\n```\nLower in March.'
    answer, charts = extract_local_charts(text)
    assert len(charts) == 1 and 'Lower in March.' in answer and 'brief-chart' not in answer
    answer, charts = extract_local_charts('Text\n```brief-chart\n{"version":')
    assert not charts and 'could not be created' in answer and '{' not in answer
    assert extract_local_charts('Just Markdown.') == ('Just Markdown.', [])


def anthropic_reply(calls=True, chart=None):
    return NS(model='model', usage=NS(input_tokens=10, output_tokens=5),
              stop_reason='tool_use' if calls else 'end_turn',
              content=[NS(type='thinking', thinking='private', signature='signature'),
                       NS(type='tool_use', id='tool-1', name='create_chart', input=chart or sample_chart())]
              if calls else [NS(type='text', text='Explanation.')])


def test_anthropic_continuation_preserves_thinking_and_accounts_all_rounds():
    initial = anthropic_reply()
    requests = []
    replies = iter([initial, anthropic_reply(False)])
    def send(kwargs):
        requests.append({**kwargs, 'messages': list(kwargs['messages'])})
        return next(replies)
    result = anthropic_generate(send, dict(model='model', messages=[dict(role='user', content='Compare')],
                                          system='System', max_tokens=100), RATES)
    assert len(result['charts']) == 1 and result['answer'] == 'Explanation.'
    assert result['input_tokens'] == 20 and result['output_tokens'] == 10
    assert result['cost_usd'] == pytest.approx(40/1_000_000)
    assert requests[1]['messages'][-2]['content'] is initial.content
    assert requests[1]['messages'][-1]['content'][0]['tool_use_id'] == 'tool-1'
    assert requests[1]['max_tokens'] == 95


def test_bad_chart_can_be_corrected_without_unbounded_calls():
    requests = []
    responses = iter([anthropic_reply(chart={'bad': 'data'}), anthropic_reply(), anthropic_reply(False)])
    def send(kwargs):
        requests.append({**kwargs, 'messages': list(kwargs['messages'])})
        return next(responses)
    result = anthropic_generate(send, dict(model='model', messages=[], system='System', max_tokens=100), RATES)
    assert len(requests) == 3 and requests[-1]['tool_choice'] == {'type': 'none'}
    assert requests[1]['messages'][-1]['content'][0]['is_error']
    assert len(result['charts']) == 1 and 'invalid' not in result['answer']


def test_plain_anthropic_answer_does_not_start_a_second_call():
    send = Mock(return_value=anthropic_reply(False))
    result = anthropic_generate(send, dict(model='model', messages=[], system='System', max_tokens=100), RATES)
    assert send.call_count == 1 and result['charts'] == []


def test_google_continuation_keeps_signature_and_disables_sdk_execution():
    call_part = types.Part(function_call=types.FunctionCall(id='call-1', name='create_chart', args=sample_chart()),
                           thought_signature=b'signature')
    content = types.Content(role='model', parts=[call_part])
    usage = types.GenerateContentResponseUsageMetadata(prompt_token_count=10, candidates_token_count=5, thoughts_token_count=2)
    first = types.GenerateContentResponse(candidates=[types.Candidate(content=content, finish_reason='STOP')], usage_metadata=usage)
    final = types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role='model', parts=[types.Part(text='Explanation.')]), finish_reason='STOP')], usage_metadata=usage)
    client = NS(models=NS(generate_content=Mock(side_effect=[first, final])))
    result = google_generate(client, types, 'model', [], dict(system_instruction='System', max_output_tokens=100), RATES)
    assert len(result['charts']) == 1 and result['output_tokens'] == 14
    second = client.models.generate_content.call_args_list[1].kwargs
    assert second['contents'][-2].parts[0].thought_signature == b'signature'
    assert second['contents'][-1].parts[0].function_response.id == 'call-1'
    assert second['config'].automatic_function_calling.disable
    assert second['config'].max_output_tokens == 93


def test_openai_uses_stateless_responses_and_preserves_reasoning_items():
    reasoning = NS(type='reasoning', encrypted_content='encrypted')
    tool = NS(type='function_call', name='create_chart', call_id='call-1', arguments=json.dumps(sample_chart()))
    def reply(output, text):
        return NS(model='model', usage=NS(input_tokens=10, output_tokens=5), status='completed', output=output, output_text=text)
    requests = []
    responses = iter([reply([reasoning, tool], ''), reply([], 'Explanation.')])
    def create(**kwargs):
        requests.append({**kwargs, 'input': list(kwargs['input'])})
        return next(responses)
    result = openai_generate(NS(responses=NS(create=create)), 'model',
                             [dict(role='user', content=[dict(type='text', text='Compare'),
                              dict(type='image_url', image_url={'url':'data:image/png;base64,eA=='})])],
                             'System', 100, {'effort':'low'}, RATES)
    assert result['answer'] == 'Explanation.' and len(result['charts']) == 1
    assert requests[0]['store'] is False
    assert requests[0]['tools'][0]['parameters'] == CHART_SCHEMA
    assert requests[0]['input'][0]['content'][1]['type'] == 'input_image'
    assert requests[1]['input'][-3] is reasoning
    assert requests[1]['input'][-1]['call_id'] == 'call-1'
    assert result['input_tokens'] == 20 and result['output_tokens'] == 10


def test_refusal_clears_anthropic_chart_content():
    refusal = anthropic_reply(False)
    refusal.stop_reason = 'refusal'
    replies = iter([anthropic_reply(), refusal])
    result = anthropic_generate(lambda kwargs:next(replies), dict(model='model', messages=[], system='System', max_tokens=100), RATES)
    assert result['charts'] == [] and 'declined' in result['answer']
