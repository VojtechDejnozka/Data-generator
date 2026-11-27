"""Built-in demo preset: retail chain with a late-delivering supplier."""
from __future__ import annotations

from ..contracts.io import ScenarioInput

RETAIL_DEMO = ScenarioInput(
    company=("Dobrý Košík a.s. – regionální síť supermarketů v Česku (potraviny a drogerie). Prodejny o ploše "
             "500–1 500 m², věrnostní program, objednávky zboží řídí prodejny periodicky u 7 dodavatelů."),
    environment=("Česká republika, konkurenční trh potravinového retailu. Sezónnost: Vánoce, léto (nápoje), "
                 "týdenní cyklus s víkendovou špičkou. Prodejny mají čítače návštěvnosti u vstupu."),
    story=("Vedení vidí, že tržby sítě od podzimu klesají, a podezírá pokles poptávky. Ve skutečnosti poptávka "
           "(návštěvnost, počet zákazníků, zájem o produkty) zůstává stabilní. Jeden dodavatel – mlékárna – začal "
           "od konce léta pravidelně dodávat pozdě a zpoždění se postupně prodlužuje. To vede k výpadkům zásob jeho "
           "produktů, zákazníci nemohou nakoupit (část odchází bez nákupu) a roste počet stížností na nedostupnost "
           "zboží. Ostatní dodavatelé fungují normálně. Do dat patří i sezónnost a matoucí události."),
    entity_counts={"stores": 10, "products": 240, "suppliers": 7, "customers": 1500},
    period_start="2025-01-01",
    period_end="2026-03-31",
    volume="medium",
    story_intensity=1.0,
    seed=42,
    preset="retail_supplier_delay",
)

PRESETS = {
    "retail_supplier_delay": {
        "id": "retail_supplier_delay",
        "title": "Síť prodejen – zpožďující se dodavatel",
        "description": "Klesající tržby při stabilní poptávce; příčinou je dodavatel, který dodává stále později.",
        "input": RETAIL_DEMO.model_dump(),
    }
}
