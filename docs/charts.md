# Conversation charts

Assistant messages can include up to three version 1 charts alongside Markdown.
Supported types are `bar`, `line` and `scatter`. The application renders data;
model-generated HTML, JavaScript and executable expressions are not accepted.

Each chart contains `version`, `type`, `title`, `x_label`, `y_label`, `source`,
`assumptions` and `series`. Each series contains a unique `name` and a `points`
array of `{ "x": ..., "y": ... }` objects. Axis labels include units where applicable.
Bar x values are categories, scatter x values are numeric, and line x values
are consistently categorical or numeric across the chart. All y values are finite
numbers. Limits are four series, 400 total points and 32 KB per chart.

`source` is `conversation` for supplied data or `example` for illustrative data.
Examples require a visible description of their assumptions. Charts do not fetch
external data. Model-provided source labels describe provenance; they are not an
independent verification of the numbers.

Charts are stored in `conversation_messages.charts` as JSONB. The additive,
repeatable migration defaults existing messages to an empty array. Branches copy
chart data, and public share snapshots retain it with the included messages.
Follow-up requests receive the saved data with the original assistant turn,
including after switching providers. Conversation size limits include chart data.
Editing the text of an assistant message retains its charts; regenerating replaces
the entire answer. Revocation and deletion follow the existing snapshot lifecycle.

## Generation

Cloud conversation requests expose `create_chart` as a native function tool.
Anthropic Messages and Gemini generate-content calls preserve their original
thinking blocks and signatures during continuation. OpenAI uses Responses with
stateless encrypted reasoning continuation and `store=False`. Title generation
and the legacy standalone generation API retain their existing text-only behavior.

Each answer allows at most three provider calls with a shared output-token budget.
The last call disables tools, and invalid arguments return an error to the model
within this bounded loop. Usage and cost include all rounds. Successful chart
results are stored as data; tool transcripts are not persisted. The local text-only
model emits fenced `brief-chart` JSON blocks through the same validator. Invalid or
truncated local blocks produce a visible failure note instead of a broken chart.
The small local model may produce less reliable chart data than cloud models.

Provider protocol references:
[OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling),
[Anthropic tool results](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls),
[Gemini function calling](https://ai.google.dev/gemini-api/docs/function-calling).
