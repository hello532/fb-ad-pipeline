"""keyword_enricher.py — extracted from fb-scan-pipeline-v7.py:439-694"""
import re
from typing import Dict, List

from core.logger import get_logger
log = get_logger(__name__)

# ── KeywordEnricher — 从搜索关键词自身提取痛点/产品/场景 ─────────────────────

class KeywordEnricher:
    """v7.1: 当广告正文缺失时，从搜索关键词本身推导痛点/产品/场景/价值信息"""

    BODY_PARTS = {
        'neck':     {'cn': '颈部', 'pains': ['stiffness', 'herniated disc', 'pinched nerve', 'cervical spondylosis', 'whiplash']},
        'back':     {'cn': '背部', 'pains': ['sciatica', 'herniated disc', 'spinal stenosis', 'scoliosis', 'muscle spasm', 'degenerative disc']},
        'lower back': {'cn': '下背', 'pains': ['sciatica', 'herniated disc', 'SI joint', 'lumbar strain']},
        'upper back': {'cn': '上背', 'pains': ['thoracic pain', 'posture strain', 'muscle knot']},
        'shoulder':  {'cn': '肩部', 'pains': ['rotator cuff', 'frozen shoulder', 'impingement', 'bursitis']},
        'knee':     {'cn': '膝盖', 'pains': ['arthritis', 'ACL', 'meniscus tear', 'runner knee', 'baker cyst']},
        'foot':     {'cn': '足部', 'pains': ['plantar fasciitis', 'neuropathy', 'edema', 'flat feet', 'bunion', 'heel spur']},
        'hand':     {'cn': '手部', 'pains': ['carpal tunnel', 'arthritis', 'trigger finger', 'Dupuytren']},
        'wrist':    {'cn': '手腕', 'pains': ['carpal tunnel', 'tendinitis', 'RSI', 'ganglion cyst']},
        'hip':      {'cn': '髋部', 'pains': ['bursitis', 'arthritis', 'labral tear', 'IT band']},
        'leg':      {'cn': '腿部', 'pains': ['restless leg', 'cramps', 'varicose veins', 'DVT', 'shin splints']},
        'calf':     {'cn': '小腿', 'pains': ['cramps', 'shin splints', 'compartment syndrome']},
        'elbow':    {'cn': '肘部', 'pains': ['tennis elbow', 'golfer elbow', 'lateral epicondylitis']},
        'jaw':      {'cn': '下颌', 'pains': ['TMJ', 'bruxism', 'clenching', 'lockjaw']},
        'scalp':    {'cn': '头皮', 'pains': ['tension headache', 'migraine', 'scalp tension']},
        'eye':      {'cn': '眼部', 'pains': ['eye strain', 'dry eye', 'dark circles', 'eye fatigue']},
        'tailbone': {'cn': '尾椎', 'pains': ['coccyx pain', 'coccydynia']},
        'thigh':    {'cn': '大腿', 'pains': ['hamstring strain', 'quad strain', 'IT band']},
        'ankle':    {'cn': '脚踝', 'pains': ['sprain', 'instability', 'Achilles tendinitis']},
    }

    PAIN_CONDITIONS = {
        'sciatica':             {'category': 'nerve', 'severity': 'high', 'cn': '坐骨神经痛', 'parts': ['back', 'leg']},
        'carpal tunnel':        {'category': 'nerve', 'severity': 'high', 'cn': '腕管综合征', 'parts': ['wrist', 'hand']},
        'plantar fasciitis':    {'category': 'inflammation', 'severity': 'high', 'cn': '足底筋膜炎', 'parts': ['foot']},
        'arthritis':            {'category': 'chronic', 'severity': 'high', 'cn': '关节炎', 'parts': ['all']},
        'neuropathy':           {'category': 'nerve', 'severity': 'high', 'cn': '神经病变', 'parts': ['foot', 'hand']},
        'fibromyalgia':         {'category': 'chronic', 'severity': 'high', 'cn': '纤维肌痛', 'parts': ['all']},
        'TMJ':                  {'category': 'joint', 'severity': 'medium', 'cn': '颞下颌关节紊乱', 'parts': ['jaw']},
        'tennis elbow':         {'category': 'tendon', 'severity': 'medium', 'cn': '网球肘', 'parts': ['elbow']},
        'rotator cuff':         {'category': 'tendon', 'severity': 'high', 'cn': '肩袖损伤', 'parts': ['shoulder']},
        'frozen shoulder':      {'category': 'joint', 'severity': 'high', 'cn': '冻结肩', 'parts': ['shoulder']},
        'herniated disc':       {'category': 'structural', 'severity': 'high', 'cn': '椎间盘突出', 'parts': ['back', 'neck']},
        'pinched nerve':        {'category': 'nerve', 'severity': 'high', 'cn': '神经压迫', 'parts': ['neck', 'back']},
        'restless leg':         {'category': 'neurological', 'severity': 'medium', 'cn': '不宁腿综合征', 'parts': ['leg']},
        'varicose veins':       {'category': 'circulatory', 'severity': 'medium', 'cn': '静脉曲张', 'parts': ['leg']},
        'edema':                {'category': 'circulatory', 'severity': 'medium', 'cn': '水肿', 'parts': ['leg', 'foot']},
        'spinal stenosis':      {'category': 'structural', 'severity': 'high', 'cn': '椎管狭窄', 'parts': ['back']},
        'trigger point':        {'category': 'muscle', 'severity': 'medium', 'cn': '触发点', 'parts': ['all']},
        'myofascial':           {'category': 'muscle', 'severity': 'medium', 'cn': '肌筋膜', 'parts': ['all']},
        'bursitis':             {'category': 'inflammation', 'severity': 'medium', 'cn': '滑囊炎', 'parts': ['hip', 'shoulder', 'knee']},
        'tendinitis':           {'category': 'tendon', 'severity': 'medium', 'cn': '肌腱炎', 'parts': ['wrist', 'ankle', 'elbow']},
        'stiffness':            {'category': 'general', 'severity': 'low', 'cn': '僵硬', 'parts': ['neck', 'back']},
        'muscle pain':          {'category': 'muscle', 'severity': 'medium', 'cn': '肌肉疼痛', 'parts': ['all']},
        'nerve pain':           {'category': 'nerve', 'severity': 'high', 'cn': '神经痛', 'parts': ['all']},
        'joint pain':           {'category': 'joint', 'severity': 'medium', 'cn': '关节痛', 'parts': ['all']},
        'chronic pain':         {'category': 'chronic', 'severity': 'high', 'cn': '慢性疼痛', 'parts': ['all']},
        'inflammation':         {'category': 'inflammation', 'severity': 'medium', 'cn': '炎症', 'parts': ['all']},
        'circulation':          {'category': 'circulatory', 'severity': 'medium', 'cn': '血液循环', 'parts': ['leg', 'foot']},
        'lymphatic':            {'category': 'circulatory', 'severity': 'medium', 'cn': '淋巴引流', 'parts': ['all']},
        'posture':              {'category': 'structural', 'severity': 'low', 'cn': '姿势不良', 'parts': ['back', 'neck']},
        'migraine':             {'category': 'neurological', 'severity': 'high', 'cn': '偏头痛', 'parts': ['scalp']},
    }

    SCENE_MAP = {
        'desk worker':      {'demo': '办公族', 'situation': 'sedentary', 'cn': '久坐办公'},
        'office':           {'demo': '办公族', 'situation': 'sedentary', 'cn': '办公室'},
        'work from home':   {'demo': '远程办公', 'situation': 'sedentary', 'cn': '居家办公'},
        'after workout':    {'demo': '健身人群', 'situation': 'recovery', 'cn': '运动后恢复'},
        'after gym':        {'demo': '健身人群', 'situation': 'recovery', 'cn': '健身后'},
        'runner':           {'demo': '跑步者', 'situation': 'sports', 'cn': '跑步恢复'},
        'golfer':           {'demo': '高尔夫', 'situation': 'sports', 'cn': '高尔夫运动'},
        'gamer':            {'demo': '游戏玩家', 'situation': 'sedentary', 'cn': '游戏久坐'},
        'pregnancy':        {'demo': '孕妇', 'situation': 'prenatal', 'cn': '孕期'},
        'post surgery':     {'demo': '术后患者', 'situation': 'recovery', 'cn': '术后恢复'},
        'elderly':          {'demo': '老年人', 'situation': 'aging', 'cn': '老年护理'},
        'nurse':            {'demo': '护士', 'situation': 'standing', 'cn': '站立工作'},
        'driver':           {'demo': '驾驶员', 'situation': 'long sitting', 'cn': '长途驾驶'},
        'athlete':          {'demo': '运动员', 'situation': 'sports', 'cn': '运动训练'},
        'morning':          {'demo': '晨间', 'situation': 'daily', 'cn': '晨起僵硬'},
        'sleep':            {'demo': '睡眠', 'situation': 'nighttime', 'cn': '改善睡眠'},
        'standing all day': {'demo': '站立工作者', 'situation': 'standing', 'cn': '站立疲劳'},
        'sitting all day':  {'demo': '久坐工作者', 'situation': 'sedentary', 'cn': '久坐疼痛'},
        'long drive':       {'demo': '驾驶员', 'situation': 'long sitting', 'cn': '长途驾驶'},
        'stress relief':    {'demo': '压力人群', 'situation': 'mental', 'cn': '减压放松'},
        'weekend warrior':  {'demo': '周末运动者', 'situation': 'sports', 'cn': '周末运动'},
    }

    PRODUCT_MODIFIERS = {
        'heated': '加热型', 'EMS': 'EMS电刺激', 'infrared': '红外线', 'vibration': '振动',
        'shiatsu': '指压', 'percussion': '冲击式', 'compression': '气压压缩', 'acupressure': '穴位按压',
        'ultrasonic': '超声波', 'red light': '红光', 'cold therapy': '冷疗', 'microcurrent': '微电流',
        'deep tissue': '深层组织', 'electric': '电动', 'wireless': '无线', 'portable': '便携',
        'professional': '专业级', 'medical grade': '医疗级',
    }

    PRODUCT_TYPES = {
        'massager': '按摩器', 'gun': '筋膜枪', 'pad': '按摩垫', 'mat': '按摩垫',
        'pillow': '按摩枕', 'cushion': '坐垫', 'belt': '腰带', 'wrap': '包裹',
        'roller': '滚轮', 'ball': '按摩球', 'brace': '护具', 'sleeve': '护套',
        'corrector': '矫正器', 'stretcher': '牵引器', 'insole': '鞋垫',
        'device': '设备', 'machine': '机器', 'unit': '治疗仪', 'boots': '压缩靴',
        'mask': '面罩', 'lamp': '灯', 'panel': '板', 'wand': '棒', 'tool': '工具',
        'stimulator': '刺激器', 'table': '床', 'chair': '椅', 'cream': '霜',
        'patch': '贴片', 'tape': '胶带', 'supplement': '补充剂',
    }

    def enrich_from_keyword(self, keyword: str) -> dict:
        """从搜索关键词本身推导痛点/产品/场景"""
        kw_lower = keyword.lower().strip()
        tokens = kw_lower.split()

        result = {
            'product_keywords': [], 'pain_keywords': [], 'scene_keywords': [],
            'value_keywords': [], 'hook_patterns': [], 'price_signals': [], 'authority_signals': [],
            '_body_parts': [], '_pain_category': '', '_severity': '',
        }

        # 1. 匹配身体部位
        for part, info in self.BODY_PARTS.items():
            if part in kw_lower:
                result['_body_parts'].append(part)
                for related_pain in info['pains'][:3]:
                    if related_pain not in result['pain_keywords']:
                        result['pain_keywords'].append(related_pain)

        # 2. 匹配痛点条件 (多词匹配优先)
        for condition, info in sorted(self.PAIN_CONDITIONS.items(), key=lambda x: -len(x[0])):
            if condition in kw_lower:
                if condition not in result['pain_keywords']:
                    result['pain_keywords'].insert(0, condition)
                result['_pain_category'] = info['category']
                result['_severity'] = info['severity']
                for part in info['parts']:
                    if part != 'all' and part not in result['_body_parts']:
                        result['_body_parts'].append(part)

        # 3. 匹配场景
        for scene, info in sorted(self.SCENE_MAP.items(), key=lambda x: -len(x[0])):
            if scene in kw_lower:
                result['scene_keywords'].append(f"{scene} ({info['cn']})")

        # 4. 匹配产品修饰词
        for modifier, cn in self.PRODUCT_MODIFIERS.items():
            if modifier.lower() in kw_lower:
                result['value_keywords'].append(f"{modifier} ({cn})")

        # 5. 匹配产品类型
        for ptype, cn in self.PRODUCT_TYPES.items():
            if ptype in kw_lower:
                result['product_keywords'].append(f"{ptype} ({cn})")

        # 6. 额外映射: cervical=neck, lumbar=lower back, thoracic=upper back
        alias_map = {
            'cervical': 'neck', 'lumbar': 'lower back', 'thoracic': 'upper back',
            'coccyx': 'tailbone', 'plantar': 'foot', 'hamstring': 'thigh',
            'achilles': 'ankle', 'rotator': 'shoulder', 'carpal': 'wrist',
        }
        for alias, part in alias_map.items():
            if alias in kw_lower and part not in result['_body_parts']:
                result['_body_parts'].append(part)
                info = self.BODY_PARTS.get(part, {})
                for related_pain in info.get('pains', [])[:3]:
                    if related_pain not in result['pain_keywords']:
                        result['pain_keywords'].append(related_pain)

        # 7. 通用痛点信号词
        pain_signals = ['pain', 'relief', 'ache', 'sore', 'stiff', 'hurt', 'discomfort',
                        'treatment', 'therapy', 'recovery', 'healing', 'remedy']
        for sig in pain_signals:
            if sig in tokens and sig not in result['pain_keywords']:
                result['pain_keywords'].append(sig)

        # 8. 如果有body_part但无具体痛点，添加通用 "{part} pain"
        if result['_body_parts'] and not any(p in self.PAIN_CONDITIONS for p in result['pain_keywords']):
            for part in result['_body_parts'][:2]:
                generic = f"{part} pain"
                if generic not in result['pain_keywords']:
                    result['pain_keywords'].append(generic)

        # 9. 如果完全没有痛点但关键词含产品词(massager/device/brace等)，添加通用 "muscle relief"
        if not result['pain_keywords'] and result['product_keywords']:
            result['pain_keywords'].append('muscle tension')
            result['pain_keywords'].append('general pain relief')

        # 10. 自动推断场景: 产品类型/痛点→常见使用场景
        if not result['scene_keywords']:
            if any(p in kw_lower for p in ['massager', 'massage', 'pad', 'mat', 'pillow', 'cushion',
                                            'device', 'machine', 'unit', 'stimulator', 'therapy',
                                            'treatment', 'relief', 'lamp', 'panel', 'heat']):
                result['scene_keywords'].append('家庭自用 (home self-care)')
            if any(p in kw_lower for p in ['brace', 'sleeve', 'corrector', 'strap', 'tape',
                                            'support', 'insole', 'wrap', 'belt', 'compression']):
                result['scene_keywords'].append('日常佩戴 (daily wear)')
            if any(p in kw_lower for p in ['gun', 'percussion', 'foam roller', 'recovery',
                                            'muscle', 'workout', 'runner', 'athlete', 'sport']):
                result['scene_keywords'].append('运动恢复 (sports recovery)')
            if any(p in kw_lower for p in ['office', 'desk', 'computer', 'chair', 'sitting',
                                            'posture', 'work from home', 'ergonomic']):
                result['scene_keywords'].append('办公使用 (office use)')
            if any(p in kw_lower for p in ['elderly', 'senior', 'mobility', 'aging',
                                            'arthritis', 'neuropathy', 'circulation']):
                result['scene_keywords'].append('老年护理 (senior care)')
            if any(p in kw_lower for p in ['pain', 'ache', 'stiff', 'nerve', 'chronic',
                                            'sciatica', 'plantar', 'carpal', 'fibromyalgia']):
                result['scene_keywords'].append('疼痛管理 (pain management)')
            if any(p in kw_lower for p in ['sleep', 'night', 'bed', 'rest', 'relax']):
                result['scene_keywords'].append('睡前放松 (bedtime relaxation)')
            if any(p in kw_lower for p in ['red light', 'led', 'infrared', 'microcurrent', 'skin']):
                result['scene_keywords'].append('美容护肤 (beauty & skincare)')

        # 11. 如果关键词有"relief/treatment/therapy"，标记为高购买意图
        intent_words = ['relief', 'treatment', 'therapy', 'cure', 'remedy', 'solution']
        if any(w in tokens for w in intent_words):
            result['value_keywords'].append('高购买意图')

        return result

    def enrich_from_learned_array(self, learned: list) -> dict:
        """解析旧JSON的learned数组"""
        result = {
            'product_keywords': [], 'pain_keywords': [], 'scene_keywords': [],
            'value_keywords': [], 'hook_patterns': [], 'price_signals': [], 'authority_signals': [],
        }
        if not learned or not isinstance(learned, list):
            return result

        type_map = {
            'pain': 'pain_keywords', 'product': 'product_keywords', 'scene': 'scene_keywords',
            'value': 'value_keywords', 'hook': 'hook_patterns', 'brand': 'product_keywords',
        }
        for item in learned:
            if not isinstance(item, str):
                continue
            parts = item.split('|')
            if len(parts) >= 3:
                en_kw = parts[0].strip()
                kw_type = parts[2].strip().lower()
                target = type_map.get(kw_type)
                if target and en_kw and en_kw not in result.get(target, []):
                    result[target].append(en_kw)
        return result

    @staticmethod
    def merge_enrichments(*dicts) -> dict:
        """合并多个enrichment dict，去重"""
        merged = {}
        for d in dicts:
            for k, v in d.items():
                if k.startswith('_'):
                    continue
                if k not in merged:
                    merged[k] = []
                if isinstance(v, list):
                    for item in v:
                        if item and item not in merged[k]:
                            merged[k].append(item)
        for k in merged:
            merged[k] = merged[k][:10]
        return merged
