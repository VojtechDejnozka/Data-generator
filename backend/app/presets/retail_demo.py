"""Built-in demo preset: retail chain with a late-delivering supplier."""
from __future__ import annotations

from ..contracts.io import ScenarioInput

RETAIL_DEMO = ScenarioInput(
    story=("Dobrý Košík a.s. je regionální síť asi deseti supermarketů v Česku (potraviny a drogerie, věrnostní "
           "program, čítače návštěvnosti u vstupu, prodejny objednávají zboží periodicky u několika dodavatelů). "
           "Vedení vidí, že tržby sítě od podzimu klesají, a podezírá pokles poptávky. Ve skutečnosti poptávka "
           "(návštěvnost, počet zákazníků, zájem o produkty) zůstává stabilní. Jeden dodavatel – mlékárna – začal "
           "od konce léta pravidelně dodávat pozdě a zpoždění se postupně prodlužuje. To vede k výpadkům zásob jeho "
           "produktů, zákazníci nemohou nakoupit (část odchází bez nákupu) a roste počet stížností na nedostupnost "
           "zboží. Ostatní dodavatelé fungují normálně. Do dat patří i sezónnost (Vánoce, léto) a matoucí události."),
    volume="medium",
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
