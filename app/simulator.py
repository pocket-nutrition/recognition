"""POC stand-in for the real speech-to-text + ingredient-extraction pipeline.

Returns the same static payload for every audio clip. Replace `static_result`
with the real Whisper + LLM/search pipeline later; the ticket lifecycle in
store.py does not change.
"""
from app.schemas import CaptureResult, ExtractedItem, Candidate


def static_result() -> CaptureResult:
    return CaptureResult(
        transcript="deux oeufs au plat, cent cinquante grammes de riz basmati et une pomme",
        language="fr",
        items=[
            ExtractedItem(
                raw_text="deux oeufs au plat",
                quantity=2,
                unit="piece",
                cooking_method="fried",
                candidates=[
                    Candidate(ingredient_id="oeuf_poule_entier_cuit", name="Egg, whole, cooked", score=0.93),
                    Candidate(ingredient_id="oeuf_poule_entier_cru", name="Egg, whole, raw", score=0.71),
                    Candidate(ingredient_id="oeuf_caille", name="Quail egg", score=0.22),
                ],
            ),
            ExtractedItem(
                raw_text="cent cinquante grammes de riz basmati",
                quantity=150,
                unit="g",
                cooking_method=None,
                candidates=[
                    Candidate(ingredient_id="riz_basmati_cuit", name="Basmati rice, cooked", score=0.88),
                    Candidate(ingredient_id="riz_basmati_cru", name="Basmati rice, raw", score=0.84),
                    Candidate(ingredient_id="riz_blanc_cuit", name="White rice, cooked", score=0.6),
                ],
            ),
            ExtractedItem(
                raw_text="une pomme",
                quantity=1,
                unit="piece",
                cooking_method=None,
                candidates=[
                    Candidate(ingredient_id="pomme_crue", name="Apple, raw", score=0.95),
                    Candidate(ingredient_id="pomme_de_terre_crue", name="Potato, raw", score=0.18),
                ],
            ),
        ],
    )
