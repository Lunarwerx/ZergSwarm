"""Word pools the fixtures draw names from (shared so every generator samples the same list)."""
from __future__ import annotations

WORDS = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec "
    "romeo sierra tango uniform victor whiskey xray yankee zulu amber basil cedar dune ember fjord grove harbor "
    "iris jade karst lagoon marsh nectar ochre pine quartz reef slate tundra umber vale willow yarrow zenith"
).split()

LAYER_POOL = ["Working", "Episodic", "Semantic", "Procedural", "Forgetting", "Reflex", "Archive", "Ledger", "Scratch", "Oracle"]
