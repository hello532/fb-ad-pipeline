"""text_analyzer.py — extracted from fb-scan-pipeline-v7.py:295-437"""
import re
from collections import Counter, defaultdict
from typing import Dict, List

from core.logger import get_logger
log = get_logger(__name__)

# ── AdTextAnalyzer — 自动关键词提取引擎 ───────────────────────────────────────

class AdTextAnalyzer:
    """v7: 从广告文本自动提取产品词/痛点词/场景词/价值词/钩子/权威信号"""

    PAIN_PATTERNS = [
        r'(?:suffer(?:ing)?|struggling|tired of|sick of|dealing with)\s+(.{5,40})',
        r'(?:pain|ache|soreness|stiffness|inflammation|discomfort|swelling|numbness|tingling)\s+(?:in|from|of|around)\s+(.{3,30})',
        r'(\w+\s+(?:pain|ache|syndrome|disorder|condition|injury|damage|strain|sprain))',
        r'(?:chronic|severe|constant|persistent|debilitating|unbearable|nagging)\s+(\w+(?:\s+\w+)?)',
        r'(?:carpal\s+tunnel|sciatica|plantar\s+fasciitis|fibromyalgia|arthritis|neuropathy|tendinitis|bursitis|herniated\s+disc|pinched\s+nerve|TMJ|rotator\s+cuff|tennis\s+elbow|frozen\s+shoulder|spinal\s+stenosis|degenerative\s+disc|restless\s+leg|varicose\s+veins|edema|migraines?)',
        r'(?:can\'t|cannot|unable to)\s+(sleep|walk|sit|stand|move|bend|lift|exercise|work)',
        r'(?:waking up|wake up)\s+(?:with|in)\s+(.{3,30})',
        r'(?:lost|losing)\s+(?:mobility|flexibility|range of motion|sleep|quality of life)',
    ]

    SCENE_PATTERNS = [
        r'(?:at work|at home|in bed|at the gym|at your desk|in the office|in the car|on the couch)',
        r'(?:sitting all day|standing all day|on your feet|long hours|long drive|road trip)',
        r'(?:during pregnancy|post surgery|postpartum|prenatal|after giving birth)',
        r'(?:after workout|after gym|after exercise|after training|after running|post workout|recovery day)',
        r'(?:nurse|teacher|gamer|athlete|runner|golfer|driver|office worker|desk worker|remote worker|trucker|hairstylist|bartender|warehouse worker)',
        r'(?:morning routine|evening routine|daily use|on the go|before bed|while watching TV|while sleeping)',
        r'(?:weekend warrior|CrossFit|marathon|hiking|yoga|pilates|weightlifting)',
        r'(?:elderly|senior|aging|over \d+|retirement|grandparent)',
        r'(?:work from home|WFH|home office|cubicle|commute)',
    ]

    VALUE_PATTERNS = [
        r'(?:clinically\s+(?:proven|tested|validated|studied))',
        r'(?:doctor|physician|dermatologist|PT|physical therapist|chiropractor|neurologist|orthopedic|surgeon)\s+(?:recommended|approved|designed|developed|created)',
        r'(?:FDA\s+(?:cleared|approved|registered)|CE\s+(?:marked|certified)|ISO\s+certified|UL\s+listed)',
        r'(?:money[\s-]?back|satisfaction)\s+guarantee',
        r'(?:free shipping|free returns?|risk[\s-]?free|no[\s-]?risk)',
        r'(?:patent(?:ed)?|proprietary|breakthrough|revolutionary|first[\s-]?of[\s-]?its[\s-]?kind)',
        r'(\d+)\s*(?:day|week|minute|second)s?\s+(?:results?|relief|transformation|difference)',
        r'(?:lifetime|limited lifetime)\s+warranty',
        r'(?:made in (?:USA|America|Japan|Germany))',
        r'(?:premium|medical[\s-]?grade|hospital[\s-]?grade|professional[\s-]?grade|clinical[\s-]?strength)',
        r'(?:over|more than)\s+(\d+(?:,\d{3})*)\s+(?:sold|customers|reviews|happy|satisfied)',
        r'(?:\d+(?:\.\d)?)\s*(?:star|★)\s*(?:rating|reviews?|rated)',
    ]

    HOOK_PATTERNS = [
        r'(?:feels like|like having)\s+(.{5,50})',
        r'(?:pain went from|went from\s+\d+\s+to\s+\d+)',
        r'(?:day\s+\d+[,:]?\s+.{5,40})',
        r'(\d+%\s+(?:success|satisfaction|improvement|reduction|of (?:users|customers|people)))',
        r'(?:created by|designed by|developed by|invented by)\s+(\w+(?:\s+\w+){0,3})',
        r'(\d+(?:,\d{3})*)\s+(?:people|customers|users|reviews|5[\s-]?star)',
        r'(?:before and after|transformation|results in)',
        r'(?:no one told you|secret|hidden|what (?:doctors|they) (?:don\'t|won\'t) tell)',
        r'(?:I (?:couldn\'t|can\'t) believe|game[\s-]?changer|life[\s-]?changing|changed my life)',
        r'(?:this is (?:the|why)|here\'s why|the reason|the truth about)',
        r'(?:stop (?:wasting|spending|suffering)|don\'t (?:buy|waste|ignore))',
        r'(?:went viral|trending|selling out|back in stock|limited (?:time|stock|supply))',
    ]

    PRICE_PATTERNS = [
        r'\$\s*(\d+(?:\.\d{2})?)',
        r'(\d+%)\s*(?:off|discount|savings?)',
        r'(?:save|saving)\s+\$?(\d+)',
        r'(?:only|just|now|from)\s+\$(\d+)',
        r'(?:BOGO|buy\s+one\s+get\s+one|buy\s+\d+\s+get\s+\d+)',
        r'(?:under|less than)\s+\$(\d+)',
        r'(?:was\s+\$\d+)\s*(?:now\s+\$\d+)',
        r'(?:use code|coupon|promo)\s+(\w+)',
    ]

    AUTHORITY_PATTERNS = [
        r'(?:doctor|MD|physician|surgeon)\s+(?:recommended|approved|created|designed|endorsed)',
        r'(?:clinical(?:ly)?)\s+(?:proven|tested|studied|validated)',
        r'(?:FDA|CE|ISO)\s+(?:cleared|approved|certified|registered)',
        r'(?:patent(?:ed)?)',
        r'(?:university|research|study|journal|published)\s+(?:backed|proven|tested|shows?)',
        r'(?:physical therapist|chiropractor|dermatologist|neurologist|orthopedic)',
        r'(?:board[\s-]?certified|licensed|registered)',
        r'(?:used (?:by|in) hospitals?|clinic[\s-]?(?:level|grade))',
        r'(?:as seen (?:on|in)|featured (?:on|in))\s+(.{3,30})',
    ]

    PRODUCT_TYPE_SUFFIXES = [
        'massager', 'device', 'machine', 'tool', 'belt', 'brace', 'pad',
        'pillow', 'cushion', 'roller', 'gun', 'mask', 'wrap', 'mat',
        'patch', 'cream', 'oil', 'supplement', 'capsule', 'table',
        'chair', 'stretcher', 'corrector', 'stimulator', 'unit',
        'boots', 'sleeve', 'tape', 'band', 'lamp', 'panel', 'wand',
        'insole', 'sock', 'glove', 'neck ring', 'knee support',
    ]

    def __init__(self):
        self._compiled = {}
        for name in ['PAIN', 'SCENE', 'VALUE', 'HOOK', 'PRICE', 'AUTHORITY']:
            patterns = getattr(self, f'{name}_PATTERNS')
            self._compiled[name.lower()] = [re.compile(p, re.IGNORECASE) for p in patterns]
        suffix_alt = '|'.join(re.escape(s) for s in self.PRODUCT_TYPE_SUFFIXES)
        self._product_suffix_re = re.compile(
            rf'(\w+(?:\s+\w+){{0,2}}\s+(?:{suffix_alt}))', re.IGNORECASE
        )

    def analyze_text(self, title: str, body: str, link_desc: str = '') -> dict:
        text = f"{title} {body} {link_desc}".strip()
        if not text:
            return self._empty_result()
        return {
            'product_keywords':  self._extract_product_keywords(text),
            'pain_keywords':     self._match_patterns(text, 'pain'),
            'scene_keywords':    self._match_patterns(text, 'scene'),
            'value_keywords':    self._match_patterns(text, 'value'),
            'hook_patterns':     self._match_patterns(text, 'hook'),
            'price_signals':     self._match_patterns(text, 'price'),
            'authority_signals': self._match_patterns(text, 'authority'),
        }

    def _extract_product_keywords(self, text: str) -> list:
        found = set()
        for m in self._product_suffix_re.finditer(text):
            val = m.group(0).strip()
            if len(val) > 4:
                found.add(val.lower())
        cap_names = re.findall(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b', text)
        for n in cap_names:
            if len(n) > 4:
                found.add(n)
        return list(found)[:10]

    def _match_patterns(self, text: str, category: str) -> list:
        found = []
        for pattern in self._compiled.get(category, []):
            for m in pattern.finditer(text):
                value = m.group(0).strip()
                if len(value) > 2 and value not in found:
                    found.append(value)
        return found[:10]

    def _empty_result(self) -> dict:
        return {k: [] for k in [
            'product_keywords', 'pain_keywords', 'scene_keywords',
            'value_keywords', 'hook_patterns', 'price_signals', 'authority_signals'
        ]}

# 全局实例
_text_analyzer = AdTextAnalyzer()
