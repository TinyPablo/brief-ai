"""compose_prompt is the single place the three stored parts are joined.

Everything it does must be reversible by inspection, because the opposite -
a composed blob in the `prompt` column - is exactly what the split fixed.
"""

from app import BRIEF_PREFIX, compose_prompt

CONTEXT = "now: 14th September 2026, 11:20\nuser is currently in Bielsko-Biała"


def test_bare_prompt_passes_through_untouched():
    assert compose_prompt("jak to działa", "", False) == "jak to działa"
    assert compose_prompt("jak to działa", None, False) == "jak to działa"


def test_context_goes_above_the_prompt_with_a_blank_line():
    assert compose_prompt("jak to działa", CONTEXT, False) == CONTEXT + "\n\njak to działa"


def test_brief_prefixes_the_prompt_not_the_context():
    composed = compose_prompt("jak to działa", CONTEXT, True)
    assert composed == CONTEXT + "\n\n" + BRIEF_PREFIX + "jak to działa"
    assert not composed.startswith(BRIEF_PREFIX)


def test_brief_alone_still_prefixes():
    assert compose_prompt("jak to działa", "", True) == BRIEF_PREFIX + "jak to działa"


def test_surrounding_whitespace_in_context_is_dropped():
    """The blank-line separator is ours to place; a stray trailing newline in
    the context would otherwise turn it into a triple break."""
    assert compose_prompt("x", "  " + CONTEXT + "\n\n  ", False) == CONTEXT + "\n\nx"


def test_prompt_whitespace_is_left_alone():
    """The endpoint strips the prompt before storing it; compose_prompt must
    not strip again, or a deliberate trailing newline inside a code block
    would be silently rewritten."""
    assert compose_prompt("line\n\n", "", False) == "line\n\n"
