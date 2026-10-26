"""Validated, versioned chart data shared by every conversation provider."""

import json
import math

MAX_CHARTS = 3
MAX_CHART_BYTES = 32_000
MAX_POINTS = 400


def _text(value, field, limit=160, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f'{field} must be text of at most {limit} characters.')
    return value.strip()


def _number(value):
    return type(value) in (int, float) and abs(value) <= 1e15 and math.isfinite(value)


def validate_chart(chart):
    if not isinstance(chart, dict):
        raise ValueError('Chart must be an object.')
    fields = {'version', 'type', 'title', 'x_label', 'y_label', 'source', 'assumptions', 'series'}
    if set(chart) != fields or chart['version'] != 1 or type(chart['version']) is not int:
        raise ValueError('Use the complete version 1 chart format.')
    if chart['type'] not in ('bar', 'line', 'scatter'):
        raise ValueError('Chart type must be bar, line or scatter.')
    result = {key: chart[key] for key in ('version', 'type')}
    for key in ('title', 'x_label', 'y_label'):
        result[key] = _text(chart[key], key)
    if chart['source'] not in ('conversation', 'example'):
        raise ValueError('Source must be conversation or example.')
    result['source'] = chart['source']
    result['assumptions'] = _text(chart['assumptions'], 'assumptions', 1000, empty=True)
    if chart['source'] == 'example' and not result['assumptions']:
        raise ValueError('Example charts must describe their assumptions.')
    series = chart['series']
    if not isinstance(series, list) or not 1 <= len(series) <= 4:
        raise ValueError('Use between one and four series.')
    result['series'] = []
    count, names, x_kind = 0, set(), None
    for item in series:
        if not isinstance(item, dict) or set(item) != {'name', 'points'}:
            raise ValueError('Each series needs a name and points.')
        name = _text(item['name'], 'series name', 80)
        if name in names:
            raise ValueError('Series names must be unique.')
        names.add(name)
        if not isinstance(item['points'], list) or not item['points']:
            raise ValueError('Every series must contain points.')
        points, seen = [], set()
        for point in item['points']:
            if not isinstance(point, dict) or set(point) != {'x', 'y'} or not _number(point['y']):
                raise ValueError('Each point needs x and a finite numeric y.')
            x = point['x']
            if chart['type'] == 'bar':
                x = _text(x, 'category', 80)
            elif chart['type'] == 'scatter':
                if not _number(x):
                    raise ValueError('Scatter x values must be finite numbers.')
            elif isinstance(x, str):
                x = _text(x, 'x value', 80)
            elif not _number(x):
                raise ValueError('Line x values must be categories or finite numbers.')
            kind = 'category' if isinstance(x, str) else 'number'
            if x_kind is not None and x_kind != kind:
                raise ValueError('All series must use the same kind of x values.')
            x_kind = kind
            if chart['type'] != 'scatter' and x in seen:
                raise ValueError('Bar and line series cannot repeat x values.')
            seen.add(x)
            points.append({'x': x, 'y': point['y']})
            count += 1
            if count > MAX_POINTS:
                raise ValueError(f'Use at most {MAX_POINTS} points per chart.')
        result['series'].append({'name': name, 'points': points})
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_CHART_BYTES:
        raise ValueError('Chart data is too large.')
    return result


def validate_charts(charts):
    if not isinstance(charts, list) or len(charts) > MAX_CHARTS:
        raise ValueError(f'Use at most {MAX_CHARTS} charts per answer.')
    return [validate_chart(chart) for chart in charts]


def chart_context(charts):
    if not charts:
        return ''
    return '\n\nSaved chart data:\n' + json.dumps(charts, ensure_ascii=False, separators=(',', ':'))
