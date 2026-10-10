"""The catalog (MODELS) and the selectable list (MODEL_ORDER) are separate on
purpose: `prompts.model` stores whichever model served an answer, so retired
ids must stay priceable and labellable long after they leave the dropdown.
These tests pin that split down."""

import pytest

from app import (
    DEFAULT_MODEL,
    MODEL_ORDER,
    MODELS,
    PROVIDER_LABELS,
    SELECTABLE_MODELS,
    estimate_pln,
)

RETIRED = set(MODELS) - SELECTABLE_MODELS


def test_every_selectable_model_is_in_the_catalog():
    assert SELECTABLE_MODELS <= set(MODELS)


def test_selectable_list_has_no_duplicates():
    assert len(MODEL_ORDER) == len(SELECTABLE_MODELS)


def test_default_model_is_selectable():
    assert DEFAULT_MODEL in SELECTABLE_MODELS


def test_some_models_are_retired():
    """If this ever empties out, someone deleted history's labels along with
    the dropdown entry - which is exactly what the split exists to prevent."""
    assert RETIRED


@pytest.mark.parametrize("model_id", sorted(MODELS))
def test_every_catalog_entry_can_label_and_price_a_history_row(model_id):
    cfg = MODELS[model_id]
    assert cfg["label"]
    assert cfg["provider"] in PROVIDER_LABELS
    assert cfg["input"] > 0
    assert cfg["output"] > 0


@pytest.mark.parametrize("model_id", MODEL_ORDER)
def test_every_selectable_entry_can_fill_out_api_config(model_id):
    """/api/config reads context_window and a price estimate for each entry in
    MODEL_ORDER; a retired entry never reaches that code path."""
    cfg = MODELS[model_id]
    assert cfg["context_window"] > 0
    assert estimate_pln(cfg) > 0


@pytest.mark.parametrize("model_id", MODEL_ORDER)
def test_reasoning_capable_models_map_both_depth_levels(model_id):
    reasoning = MODELS[model_id].get("reasoning")
    if reasoning is None:
        return  # no adjustable reasoning - the UI hides the control entirely
    assert set(reasoning) == {"low", "max"}
    for depth in ("low", "max"):
        assert reasoning[depth]["effort"]
