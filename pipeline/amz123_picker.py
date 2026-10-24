"""amz123_picker.py — lexicon stub for pipeline.fb_fitness.

The original AMZ123 crawler / Amazon scraper was removed (FB-only build).
Only the regex lexicons that fb_fitness.py imports survive here.
"""
from __future__ import annotations

import re

# ── Visual demonstrability ────────────────────────────────────────────────
VISUAL_HIGH = re.compile(
    r'\b(cream|patches?|tool|device|gadget|massager|curler|cover|spray|trimmer|'
    r'shaver|razor|brush|comb|peeler|chopper|slicer|grater|opener|clipper)\b', re.I)
VISUAL_MED_BEFORE_AFTER = re.compile(
    r'\b(supplement|vitamin|powder|tablet|capsule|tincture|gummies|protein)\b', re.I)
VISUAL_LOW_UTIL = re.compile(
    r'\b(sheet|organizer|rack|mat|pad|bin|drawer|hanger|holder|hook|shelf)\b', re.I)
VISUAL_COMMODITY = re.compile(
    r'\b(paper|soap|detergent|sponge|wipes?|tissue|trash\s+bag|bleach)\b', re.I)

# ── Pain / emotion ────────────────────────────────────────────────────────
PAIN_STRONG = re.compile(
    r'\b(relief|treatment|removal|fix|repair|cover|hide|conceal|stop\s+\w+|'
    r'cure|soothe|heal|reduce\s+\w+)\b', re.I)
DESIRE_VANITY = re.compile(
    r'\b(enhancement|anti[-\s]?wrinkle|slimming|lifting|firming|whitening|brightening|'
    r'pheromone|seductive|glow|luminous|youth|tighten|sculpt)\b', re.I)
WELLNESS_2026 = re.compile(
    r'\b(collagen|peptide|magnesium|cortisol|lymphatic|nmn|creatine|taurine|'
    r'colostrum|probiotic|electrolyte|adaptogen|nootropic|ashwagandha)\b', re.I)
HOBBY_LIFESTYLE = re.compile(
    r'\b(yoga|gaming|pilates|hiking|camping|fishing|knitting|crochet|painting|drawing)\b', re.I)

# ── Audience targetability ────────────────────────────────────────────────
TGT_BEAUTY_WOMEN = re.compile(
    r'\b(serum|cream|mask|brow|lash|hair|nail|polish|lipstick|foundation|'
    r'shapewear|leggings|bra|skincare|wrinkle|menopause|hormone)\b', re.I)
TGT_HOME = re.compile(
    r'\b(kitchen|organizer|storage|hanger|drawer|laundry|cleaning|vacuum|'
    r'curtain|rug|pillow|blanket|towel|cookware|grill|oven|fryer)\b', re.I)
TGT_PET = re.compile(
    r'\b(dog|cat|pet|puppy|kitten|aquarium|bird|hamster|rabbit|reptile|leash|'
    r'collar|harness|litter|treat\s+dog|treat\s+cat)\b', re.I)
TGT_MEN_GROOMING = re.compile(
    r'\b(beard|mens?\s+(grooming|skincare|hair)|trimmer|aftershave|cologne|'
    r'testosterone|prostate|men.*health)\b', re.I)
TGT_FITNESS = re.compile(
    r'\b(workout|gym|fitness|protein|creatine|dumbbell|kettlebell|yoga\s+mat|'
    r'resistance|hiking|outdoor|camping|cycling|running\s+shoe)\b', re.I)
TGT_ELECTRONICS = re.compile(
    r'\b(charger|cable|adapter|earbud|headphone|monitor|keyboard|mouse|router|'
    r'laptop|tablet|smartwatch|drone|speaker)\b', re.I)

# ── FB ad policy risk ─────────────────────────────────────────────────────
POLICY_MEDICAL = re.compile(
    r'\b(cure|treat(?:ment)?|heal|diabetes|arthritis|eczema|psoriasis|fungus|'
    r'infection|blood\s+pressure|anxiety|depression|adhd|autism|cancer|'
    r'pain\s+relief|pain[-\s]?relief|migraine|edema|varicose|neuropathy)\b', re.I)
POLICY_INGESTIBLE = re.compile(
    r'\b(supplement|vitamin|capsule|tablet|pill|gumm(?:y|ies)|powder|drops?|'
    r'tincture|tea|detox|fat\s+burner|keto|probiotic|collagen|nmn)\b', re.I)
POLICY_BEFORE_AFTER = re.compile(
    r'\b(before\s+and\s+after|before/after|lose\s+\d+|weight\s+loss|slimming|'
    r'anti[-\s]?wrinkle|whitening|hair\s+growth|regrow)\b', re.I)
