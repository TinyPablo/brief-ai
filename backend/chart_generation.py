"""Bounded chart tool execution with provider-specific message continuations."""

import json
import re

from charts import MAX_CHARTS, validate_chart

CHART_INSTRUCTIONS = """
You have an internal create_chart tool. Use it autonomously when a chart materially
helps compare quantities, show trends, explain functions or reveal relationships.
Use bar for category comparisons, line for ordered trends/functions, scatter for
numeric relationships. Plain text is enough for simple answers; respect requests
for text only. Answer in the user's language, including chart titles and labels.
Use supplied conversation data, or clearly label illustrative data as source=example
and explain assumptions. Never invent missing factual measurements or claim current
internet data. You have no browsing tool. Include axis units, meaningful series names
and a concise explanation in Markdown. Do not describe your tool implementation.
Call create_chart once per chart, at most three charts, four series and 400 total
points per chart. Every point has x and numeric y; use string x for bar, numeric x
for scatter, consistently string or numeric x for line. Do not send code, formulas,
HTML, URLs or executable expressions. Correct invalid arguments when the tool
returns an error. After success, explain the result without recreating the chart.
""".strip()


def _object(properties):
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


CHART_SCHEMA = _object(dict(
    version=dict(type='integer', enum=[1]),
    type=dict(type='string', enum=['bar', 'line', 'scatter']),
    title=dict(type='string', maxLength=160),
    x_label=dict(type='string', maxLength=160, description='X quantity or category, including units.'),
    y_label=dict(type='string', maxLength=160, description='Y quantity, including units.'),
    source=dict(type='string', enum=['conversation', 'example']),
    assumptions=dict(type='string', maxLength=1000, description='Required explanation for examples; otherwise empty or data caveats.'),
    series=dict(type='array', minItems=1, maxItems=4, items=_object(dict(
        name=dict(type='string', maxLength=80),
        points=dict(type='array', minItems=1, maxItems=400, items=_object(dict(
            x=dict(anyOf=[dict(type='string', maxLength=80), dict(type='number')]),
            y=dict(type='number'),
        ))),
    ))),
))
TOOL_DESCRIPTION = 'Create a saved interactive chart displayed with this answer. Supply concrete data, not code.'


class ChartSession:
    def __init__(self, rates):
        self.charts, self.text = [], []
        self.input_tokens = self.output_tokens = 0
        self.cost_usd = 0
        self.rates = rates
        self.invalid = False

    def account(self, model, input_tokens, output_tokens):
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        rate = self.rates.get(model, self.rates.get('__default__', {}))
        self.cost_usd += (input_tokens * rate.get('input', 0) + output_tokens * rate.get('output', 0)) / 1_000_000

    def execute(self, name, arguments):
        try:
            if name != 'create_chart':
                raise ValueError('Only create_chart is available.')
            if isinstance(arguments, str):
                if len(arguments.encode()) > 64_000:
                    raise ValueError('Chart arguments are too large.')
                arguments = json.loads(arguments)
            chart = validate_chart(arguments)
            if chart in self.charts:
                return dict(ok=True, chart_index=self.charts.index(chart), already_saved=True)
            if len(self.charts) >= MAX_CHARTS:
                raise ValueError('The three-chart limit has been reached.')
            self.charts.append(chart)
            self.invalid = False
            return dict(ok=True, chart_index=len(self.charts)-1, title=chart['title'])
        except (ValueError, TypeError, OverflowError, RecursionError):
            self.invalid = True
            return dict(ok=False, error='Invalid chart. Check required fields, coordinate types, unique categories, example assumptions and size limits.')

    def result(self, model, stop_reason):
        answer = '\n\n'.join(t.strip() for t in self.text if t and t.strip())
        if not answer:
            answer = '\n\n'.join(chart['title'] for chart in self.charts) or '_No response._'
        if self.invalid:
            answer += '\n\n_One chart could not be created because its data was invalid._'
        return dict(answer=answer, charts=self.charts, input_tokens=self.input_tokens,
                    output_tokens=self.output_tokens, cost_usd=self.cost_usd,
                    served=model, stop_reason=stop_reason)


def anthropic_generate(send, kwargs, rates):
    session = ChartSession(rates)
    kwargs = {**kwargs, 'messages': list(kwargs['messages']),
              'system': kwargs['system'] + '\n\n' + CHART_INSTRUCTIONS,
              'tools': [dict(name='create_chart', description=TOOL_DESCRIPTION, input_schema=CHART_SCHEMA)]}
    limit = kwargs['max_tokens']
    for step in range(3):
        kwargs['max_tokens'] = max(1, limit-session.output_tokens)
        kwargs['tool_choice'] = {'type': 'none' if step == 2 or len(session.charts) >= MAX_CHARTS else 'auto'}
        response = send(kwargs)
        served = getattr(response, 'model', kwargs['model'])
        session.account(served, response.usage.input_tokens, response.usage.output_tokens)
        session.text.extend(block.text for block in response.content if block.type == 'text')
        calls = [block for block in response.content if block.type == 'tool_use']
        if response.stop_reason == 'refusal':
            session.text = ['_The model declined to answer this request._']
            session.charts = []
            break
        if response.stop_reason == 'max_tokens':
            break
        if not calls:
            break
        results = []
        for call in calls:
            result = session.execute(call.name, call.input)
            results.append(dict(type='tool_result', tool_use_id=call.id,
                                content=json.dumps(result), is_error=not result['ok']))
        if session.output_tokens >= limit:
            break
        # Preserve thinking blocks and signatures verbatim through the tool round trip.
        kwargs['messages'].extend([dict(role='assistant', content=response.content),
                                   dict(role='user', content=results)])
    return session.result(served, response.stop_reason)


def google_generate(client, types, model, messages, config, rates):
    session = ChartSession(rates)
    messages = list(messages)
    config = {**config, 'system_instruction': config['system_instruction']+'\n\n'+CHART_INSTRUCTIONS,
              'tools': [types.Tool(function_declarations=[types.FunctionDeclaration(
                  name='create_chart', description=TOOL_DESCRIPTION, parameters_json_schema=CHART_SCHEMA)])],
              'automatic_function_calling': types.AutomaticFunctionCallingConfig(disable=True)}
    limit = config['max_output_tokens']
    stop = None
    for step in range(3):
        config['max_output_tokens'] = max(1, limit-session.output_tokens)
        config['tool_config'] = types.ToolConfig(function_calling_config=types.FunctionCallingConfig(
            mode='NONE' if step == 2 or len(session.charts) >= MAX_CHARTS else 'AUTO'))
        response = client.models.generate_content(model=model, contents=messages,
                                                  config=types.GenerateContentConfig(**config))
        usage = response.usage_metadata
        session.account(model, getattr(usage, 'prompt_token_count', 0) or 0,
                        (getattr(usage, 'candidates_token_count', 0) or 0)+(getattr(usage, 'thoughts_token_count', 0) or 0))
        candidate = response.candidates[0] if response.candidates else None
        if not candidate or not candidate.content:
            break
        stop = str(candidate.finish_reason)
        parts = candidate.content.parts or []
        session.text.extend(part.text for part in parts if part.text and not part.thought)
        calls = [part.function_call for part in parts if part.function_call]
        if 'MAX_TOKENS' in stop or not calls:
            break
        results = [types.Part(function_response=types.FunctionResponse(
            id=call.id, name=call.name, response=session.execute(call.name, call.args))) for call in calls]
        if session.output_tokens >= limit:
            break
        # Keep the complete model content, including Gemini thought signatures.
        messages.extend([candidate.content, types.Content(role='user', parts=results)])
    return session.result(model, stop)


def openai_generate(client, model, messages, system, limit, reasoning, rates):
    session = ChartSession(rates)
    inputs = []
    for message in messages:
        role = message['role']
        parts = message['content']
        if isinstance(parts, str):
            inputs.append(dict(role=role, content=parts))
            continue
        if role == 'assistant':
            inputs.append(dict(role=role, content='\n'.join(p['text'] for p in parts if p['type'] == 'text')))
        else:
            inputs.append(dict(role=role, content=[dict(type='input_text', text=p['text'])
                          if p['type'] == 'text' else dict(type='input_image', image_url=p['image_url']['url']) for p in parts]))
    kwargs = dict(model=model, instructions=system+'\n\n'+CHART_INSTRUCTIONS,
                  input=inputs, store=False, include=['reasoning.encrypted_content'],
                  tools=[dict(type='function', name='create_chart', description=TOOL_DESCRIPTION,
                              parameters=CHART_SCHEMA, strict=True)])
    if reasoning:
        kwargs['reasoning'] = {'effort': reasoning['effort']}
    for step in range(3):
        kwargs['max_output_tokens'] = max(1, limit-session.output_tokens)
        kwargs['tool_choice'] = 'none' if step == 2 or len(session.charts) >= MAX_CHARTS else 'auto'
        response = client.responses.create(**kwargs)
        served = response.model
        session.account(served, response.usage.input_tokens, response.usage.output_tokens)
        session.text.append(response.output_text)
        calls = [item for item in response.output if item.type == 'function_call']
        if response.status != 'completed' or not calls:
            break
        inputs.extend(response.output)
        inputs.extend(dict(type='function_call_output', call_id=call.call_id,
                           output=json.dumps(session.execute(call.name, call.arguments))) for call in calls)
        if session.output_tokens >= limit:
            break
    return session.result(served, response.status)


LOCAL_CHART_INSTRUCTIONS = CHART_INSTRUCTIONS + """
For this text-only connection, call create_chart by emitting a fenced brief-chart
block containing exactly one JSON chart object. The application executes it and
displays the chart. Continue normal Markdown outside the block. Omit the block
when unnecessary. Example:
```brief-chart
{"version":1,"type":"bar","title":"Expenses","x_label":"Month","y_label":"PLN","source":"conversation","assumptions":"","series":[{"name":"Expenses","points":[{"x":"January","y":1200}]}]}
```
"""


def extract_local_charts(answer):
    session = ChartSession({})
    def replace(match):
        session.execute('create_chart', match.group(1))
        return ''
    text = re.sub(r'(?m)^```brief-chart[^\S\n]*\n([\s\S]*?)^```[^\S\n]*(?:\n|$)', replace, answer)
    # Do not expose a truncated tool payload as a code sample.
    partial = re.search(r'(?m)^```brief-chart\b', text)
    if partial:
        text = text[:partial.start()]
        session.invalid = True
    session.text = [text]
    result = session.result('local', None)
    return result['answer'], result['charts']
