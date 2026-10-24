"""opportunity.py — extracted from fb-scan-pipeline-v7.py:696-852"""
import re
from collections import Counter
from typing import Dict, List

from core.logger import get_logger
log = get_logger(__name__)

# ── ProductOpportunityAnalyzer — 产品扩充/升级/组合建议 ───────────────────────

class ProductOpportunityAnalyzer:
    """v7.1: 基于搜索关键词和竞品数据, 生成产品机会建议"""

    PRODUCT_TAXONOMY = {
        'massager': {
            'body_parts': ['neck', 'back', 'foot', 'leg', 'hand', 'shoulder', 'knee', 'hip',
                           'scalp', 'eye', 'jaw', 'wrist', 'calf', 'thigh', 'elbow'],
            'technologies': ['heated', 'EMS', 'infrared', 'vibration', 'shiatsu', 'percussion',
                             'compression', 'acupressure', 'deep tissue', 'ultrasonic', 'red light'],
            'form_factors': ['gun', 'pad', 'mat', 'pillow', 'cushion', 'belt', 'wrap',
                             'roller', 'ball', 'chair pad'],
        },
        'therapy_device': {
            'types': ['TENS unit', 'EMS stimulator', 'red light panel', 'infrared lamp',
                      'cold therapy machine', 'cupping set', 'traction device'],
        },
        'support': {
            'types': ['brace', 'sleeve', 'corrector', 'stretcher', 'insole', 'tape', 'strap'],
            'body_parts': ['knee', 'back', 'wrist', 'ankle', 'elbow', 'neck', 'shoulder', 'posture'],
        },
    }

    UPGRADE_PATHS = {
        'basic': ['pro', 'premium', 'professional grade', 'medical grade', 'clinical strength'],
        'manual': ['electric', 'rechargeable', 'wireless', 'smart', 'app-controlled'],
        'single': ['bundle', 'combo pack', 'family pack', 'gift set'],
        'standard': ['with heat', 'with LED', 'with EMS', 'with infrared', 'with compression'],
    }

    BUNDLE_COMBOS = [
        ('neck massager', 'posture corrector', '颈部按摩+姿势矫正'),
        ('TENS unit', 'electrode pads', 'TENS+电极贴片'),
        ('massage gun', 'foam roller', '筋膜枪+泡沫轴'),
        ('knee brace', 'knee massager', '护膝+膝部按摩'),
        ('back brace', 'heating pad', '护腰+加热垫'),
        ('foot massager', 'compression socks', '足部按摩+压力袜'),
        ('eye massager', 'sleep mask', '眼部按摩+睡眠面罩'),
        ('hand massager', 'wrist brace', '手部按摩+护腕'),
        ('red light panel', 'collagen cream', '红光面板+胶原蛋白'),
        ('compression boots', 'foam roller', '气压靴+泡沫轴'),
    ]

    CN_MAP = {
        'neck': '颈部', 'back': '背部', 'foot': '足部', 'leg': '腿部', 'hand': '手部',
        'shoulder': '肩部', 'knee': '膝盖', 'hip': '髋部', 'scalp': '头皮', 'eye': '眼部',
        'jaw': '下颌', 'wrist': '手腕', 'calf': '小腿', 'thigh': '大腿', 'elbow': '肘部',
        'heated': '加热', 'EMS': 'EMS', 'infrared': '红外', 'vibration': '振动',
        'shiatsu': '指压', 'percussion': '冲击', 'compression': '压缩',
        'gun': '枪', 'pad': '垫', 'mat': '垫', 'pillow': '枕', 'belt': '带', 'wrap': '包裹',
        'roller': '滚轮', 'brace': '护具', 'sleeve': '套', 'corrector': '矫正器',
    }

    def analyze_opportunities(self, keyword: str, competitors: list,
                              all_scanned: set) -> dict:
        kw_lower = keyword.lower()
        brand_count = len(competitors)

        result = {
            'product_expansions': [],
            'product_upgrades': [],
            'bundle_ideas': [],
            'niche_opportunities': [],
            'market_gaps': [],
        }

        # 识别基础产品和修饰词
        base_product = ''
        base_body_part = ''
        base_tech = ''
        for bp in self.PRODUCT_TAXONOMY['massager']['body_parts']:
            if bp in kw_lower:
                base_body_part = bp
                break
        for tech in self.PRODUCT_TAXONOMY['massager']['technologies']:
            if tech.lower() in kw_lower:
                base_tech = tech
                break
        for ff in self.PRODUCT_TAXONOMY['massager']['form_factors']:
            if ff in kw_lower:
                base_product = ff
                break
        if not base_product:
            for cat_key in ['massager', 'brace', 'device', 'stimulator', 'corrector', 'stretcher']:
                if cat_key in kw_lower:
                    base_product = cat_key
                    break

        # 1. 产品扩展: 同品类不同身体部位
        if base_product:
            parts = self.PRODUCT_TAXONOMY['massager']['body_parts']
            for part in parts:
                if part != base_body_part:
                    suggestion = f"{part} {base_product}"
                    if suggestion.lower() not in all_scanned:
                        cn = f"{self.CN_MAP.get(part, part)}{self.CN_MAP.get(base_product, base_product)}"
                        score = 0.7 if part in ['neck', 'back', 'foot', 'knee'] else 0.5
                        result['product_expansions'].append({
                            'keyword': suggestion, 'cn': cn, 'score': round(score, 2),
                            'reason': f'同品类{base_product}扩展到{part}部位'
                        })
            result['product_expansions'] = sorted(result['product_expansions'], key=lambda x: -x['score'])[:8]

        # 2. 产品升级: 增加技术修饰
        if base_body_part:
            target = base_product or 'massager'
            for tech in self.PRODUCT_TAXONOMY['massager']['technologies']:
                if tech.lower() != (base_tech or '').lower():
                    suggestion = f"{tech.lower()} {base_body_part} {target}"
                    if suggestion.lower() not in all_scanned:
                        cn = f"{self.CN_MAP.get(tech, tech)}{self.CN_MAP.get(base_body_part, base_body_part)}{self.CN_MAP.get(target, target)}"
                        result['product_upgrades'].append({
                            'keyword': suggestion, 'cn': cn, 'score': 0.65,
                            'reason': f'为{base_body_part} {target}添加{tech}技术'
                        })
            # 高级版本升级
            for base_tier, upgrades in self.UPGRADE_PATHS.items():
                for upgrade in upgrades[:2]:
                    suggestion = f"{upgrade} {base_body_part} {target}"
                    if suggestion.lower() not in all_scanned:
                        result['product_upgrades'].append({
                            'keyword': suggestion, 'cn': f'{upgrade} {base_body_part} {target}',
                            'score': 0.55, 'reason': f'升级路径: {base_tier}→{upgrade}'
                        })
            result['product_upgrades'] = sorted(result['product_upgrades'], key=lambda x: -x['score'])[:8]

        # 3. 组合建议
        for combo in self.BUNDLE_COMBOS:
            if any(part in kw_lower for part in combo[0].split()):
                if combo[1].lower() not in all_scanned:
                    result['bundle_ideas'].append({
                        'keywords': [combo[0], combo[1]], 'cn': combo[2],
                        'reason': f'互补组合: {combo[0]} + {combo[1]}'
                    })

        # 4. 市场空白检测
        if brand_count <= 5:
            gap_score = round(1.0 - brand_count / 20, 2)
            result['niche_opportunities'].append({
                'keyword': keyword, 'gap_score': gap_score,
                'brand_count': brand_count,
                'reason': f'低竞争({brand_count}品牌), 可能是蓝海市场'
            })

        # 5. 交叉细分建议
        if base_body_part and not base_tech:
            for tech in ['heated', 'EMS', 'infrared']:
                combo = f"{tech} {kw_lower}"
                if combo not in all_scanned:
                    result['market_gaps'].append({
                        'keyword': combo,
                        'cn': f'{self.CN_MAP.get(tech, tech)}版 {keyword}',
                        'reason': f'技术细分: 为{keyword}添加{tech}技术'
                    })

        return result
