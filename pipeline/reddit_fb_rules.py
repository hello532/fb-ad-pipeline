"""Reddit-derived rules for FB memory_scan scoring (applied to Excel).

Built from 50+ real Reddit posts (OpenCLI) + local gold playbooks.
Does NOT invent ROAS/spend. Signal scores only.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_CATALOG = _ROOT / "reddit_gold_posts.json"

_FRAGILE = re.compile(r"\b(electronic|battery|glass|fragile|led strip|drone|laptop|phone case charger)\b", re.I)
_OFFER = re.compile(r"\b(bundle|bogo|buy\s*\d\s*get|guarantee|money[\s-]?back|free shipping|%\s*off)\b", re.I)
_SELL_CTA = {"SHOP_NOW", "BUY_NOW", "ORDER_NOW", "GET_OFFER", "ADD_TO_CART", "VIEW_PRODUCT"}


def load_catalog() -> dict[str, Any]:
    if _CATALOG.exists():
        return json.loads(_CATALOG.read_text(encoding="utf-8"))
    return {"posts": [], "rules": [], "post_count": 0, "rule_count": 0}


def score_competitor_reddit(row: dict[str, Any], verify: dict[str, Any] | None = None) -> tuple[int, list[str], list[str]]:
    """Apply Reddit rules to a competitor/product aggregate row.

    row keys: ads, landings, keywords, max_days, ctas(top), domain, sample_url, body/title optional
    verify: lp_prober-like dict
    returns (bonus_score_0_100_component, flags, rule_ids)
    """
    verify = verify or {}
    score = 0
    flags: list[str] = []
    hit_rules: list[str] = []

    ads = int(row.get("ads") or 0)
    landings = int(row.get("landings") or row.get("landing_count") or 0)
    kws = int(row.get("keywords") or row.get("keyword_count") or 0)
    days = int(row.get("max_days") or row.get("active_days") or 0)
    cta = str(row.get("top_cta") or row.get("cta") or "").upper()
    domain = str(row.get("domain") or "").lower()
    url = str(row.get("sample_url") or row.get("url") or "").lower()
    text = str(row.get("title") or row.get("body") or row.get("path") or "")

    # platform kill
    if any(p in domain for p in ("amazon.", "temu.", "shein.", "walmart.", "ebay.", "etsy.", "shopee.", "aliexpress.")):
        return 0, ["平台货"], ["R_PLATFORM_KILL"]

    # dropship kill
    if verify.get("has_dropship") or int(verify.get("discount_stack_count") or 0) >= 3:
        flags.append("Dropship指纹")
        hit_rules.append("R_DROPSHIP_KILL")
        return min(score, 15), flags, hit_rules

    # ad longevity
    if days >= 90:
        score += 30; flags.append(f"长寿{days}d"); hit_rules.append("R_AD_AGE_30")
    elif days >= 30:
        score += 20; flags.append(f"验证期{days}d"); hit_rules.append("R_AD_AGE_30")
    elif days >= 14:
        score += 10; flags.append(f"两周+{days}d"); hit_rules.append("R_AD_AGE_30")
    elif days < 7 and ads <= 1:
        score -= 10; flags.append("单新广告"); hit_rules.append("R_AD_AGE_30")

    # creative density
    if ads >= 20:
        score += 28; flags.append(f"重仓{ads}创意"); hit_rules.append("R_CREATIVE_DENSITY")
    elif ads >= 10:
        score += 20; flags.append(f"密创意{ads}"); hit_rules.append("R_CREATIVE_DENSITY")
    elif ads >= 5:
        score += 12; flags.append(f"多创意{ads}"); hit_rules.append("R_CREATIVE_DENSITY")
    elif ads >= 3:
        score += 6; flags.append(f"创意{ads}"); hit_rules.append("R_CREATIVE_DENSITY")

    # multi landing
    if landings >= 5:
        score += 12; flags.append(f"落地{landings}"); hit_rules.append("R_MULTI_LANDING")
    elif landings >= 3:
        score += 8; flags.append(f"落地{landings}"); hit_rules.append("R_MULTI_LANDING")

    # CTA
    if cta in _SELL_CTA:
        score += 15; flags.append(f"CTA={cta}"); hit_rules.append("R_SHOP_CTA")
    elif cta in {"LEARN_MORE", "SUBSCRIBE", "GET_PROMOTIONS"}:
        score += 6; flags.append(f"CTA弱={cta}"); hit_rules.append("R_SHOP_CTA")

    # single product LP
    if re.search(r"/products/[^/?#]+", url):
        score += 12; flags.append("单品路径"); hit_rules.append("R_SINGLE_PRODUCT_LP")
    if verify.get("is_single_product"):
        score += 8; flags.append("单品LP验证"); hit_rules.append("R_SINGLE_PRODUCT_LP")

    # price sweet
    price = verify.get("price_hint")
    try:
        price = float(price) if price is not None else None
    except Exception:
        price = None
    if price is not None:
        if 40 <= price <= 89:
            score += 12; flags.append(f"${price:.0f}甜蜜区"); hit_rules.append("R_PRICE_SWEET")
        elif 20 <= price < 40:
            score += 10; flags.append(f"${price:.0f}冲动区"); hit_rules.append("R_PRICE_SWEET")
        elif 15 <= price < 20:
            score += 4; flags.append(f"${price:.0f}低客单"); hit_rules.append("R_PRICE_SWEET")
        elif price > 150:
            score -= 5; flags.append(f"${price:.0f}高客单难"); hit_rules.append("R_PRICE_SWEET")

    # trust stack
    rd = int(verify.get("return_policy_days") or 0)
    if rd >= 30:
        score += 8; flags.append(f"退货{rd}d"); hit_rules.append("R_TRUST_STACK")
    tb = int(verify.get("trust_badges") or 0)
    if tb >= 2:
        score += 6; flags.append(f"信任标{tb}"); hit_rules.append("R_TRUST_STACK")
    tags = set(verify.get("tags") or [])
    if "lp:klaviyo" in tags or "lp:sms" in tags:
        score += 6; flags.append("Klaviyo/SMS"); hit_rules.append("R_TRUST_STACK")
    if "lp:shopify" in tags:
        score += 5; flags.append("Shopify"); hit_rules.append("R_TRUST_STACK")

    # keyword breadth
    if kws >= 5:
        score += 12; flags.append(f"跨词{kws}"); hit_rules.append("R_KEYWORD_BREADTH")
    elif kws >= 3:
        score += 8; flags.append(f"跨词{kws}"); hit_rules.append("R_KEYWORD_BREADTH")

    # offer
    if _OFFER.search(text) or _OFFER.search(url):
        score += 6; flags.append("Offer结构"); hit_rules.append("R_OFFER_STRUCTURE")

    # boring / fragile
    if _FRAGILE.search(text) or _FRAGILE.search(url):
        score -= 8; flags.append("易碎/电子风险"); hit_rules.append("R_BORING_EASYSHIP")
    else:
        score += 4; flags.append("偏Boring好运"); hit_rules.append("R_BORING_EASYSHIP")

    # quiet steady
    disc = int(verify.get("discount_stack_count") or 0)
    if 3 <= ads <= 15 and days >= 45 and disc < 3:
        score += 8; flags.append("安静稳卖"); hit_rules.append("R_QUIET_STEADY")

    # cross platform hint flag only
    if str(verify.get("verdict") or "").startswith("PASS"):
        flags.append("待TT/AMZ交叉"); hit_rules.append("R_CROSS_PLATFORM_HINT")

    score = max(0, min(100, score))
    # unique preserve order
    hit_rules = list(dict.fromkeys(hit_rules))
    flags = list(dict.fromkeys(flags))
    return score, flags, hit_rules


def advice_from_scores(reddit_score: int, verify_verdict: str = "") -> str:
    v = verify_verdict or ""
    if v == "FAIL_DROPSHIP" or v.startswith("FAIL"):
        return "跳过"
    if reddit_score >= 70 and v.startswith("PASS"):
        return "重点跟"
    if reddit_score >= 55:
        return "可深挖/跟品验证"
    if reddit_score >= 40:
        return "观察"
    if reddit_score >= 25:
        return "低优先"
    return "跳过"


# ── Reddit 真实赚钱 SOP（社区可复现流程）──────────────────────────────
# Meta Library → 落地/Offer → 履约风险 → 交叉平台 → 小预算测 → 再扩
# 参考: multi-platform reddit-field-playbook R1-R10 + 60 金帖

PIPELINE_STAGES = [
    ("S1_META", "①Meta长跑+多创意"),
    ("S2_LP", "②独立站LP/Offer"),
    ("S3_PRICE", "③标价/客单带"),
    ("S4_SHIP", "④履约/Boring"),
    ("S5_CROSS", "⑤交叉平台待验"),
    ("S6_TEST", "⑥小预算测门"),
]

# S5 是并行待办，不阻断「可测」主阶段展示（Reddit: 测前可同步做交叉）
_NON_BLOCKING_PENDING = {"S5_CROSS"}


def _kill_result(reddit_score: int, label: str, blockers: list[str], next_action: str) -> dict[str, Any]:
    return {
        "stage": "KILL",
        "stage_label": label,
        "pipeline_rank": 0,
        "funnel_score": 0,
        "gates": {k: "FAIL" for k, _ in PIPELINE_STAGES},
        "gate_detail": {},
        "blockers": blockers,
        "next_action": next_action,
        "next_actions": [next_action],
        "test_budget": "",
        "money_process": "KILL",
        "reddit_score": reddit_score,
        "s1": "FAIL", "s2": "FAIL", "s3": "FAIL", "s4": "FAIL", "s5": "FAIL", "s6": "FAIL",
        "offer_hit": False,
        "price_hint": None,
        "intensity": 0,
        "priority_queue": "D-跳过",
    }




def evaluate_money_pipeline(
    row: dict[str, Any],
    verify: dict[str, Any] | None = None,
    *,
    reddit_score: int | None = None,
) -> dict[str, Any]:
    """把候选映射到 Reddit 赚钱流程关卡。

    不编造 ROAS/花费/COGS。缺成本时 S3 只能给「标价带」提示，毛利硬门标 UNKNOWN。
    """
    verify = verify or {}
    if reddit_score is None:
        reddit_score, _, _ = score_competitor_reddit(row, verify)

    ads = int(row.get("ads") or 0)
    landings = int(row.get("landings") or row.get("landing_count") or 0)
    kws = int(row.get("keywords") or row.get("keyword_count") or 0)
    days = int(row.get("max_days") or row.get("active_days") or 0)
    cta = str(row.get("top_cta") or row.get("cta") or "").upper()
    domain = str(row.get("domain") or "").lower()
    url = str(row.get("sample_url") or row.get("url") or "").lower()
    text = str(row.get("title") or row.get("body") or row.get("path") or "")
    verdict = str(verify.get("verdict") or "")
    tags = set(verify.get("tags") or [])
    price = verify.get("price_hint")
    try:
        price_f = float(price) if price is not None else None
    except Exception:
        price_f = None

    gates: dict[str, str] = {}
    detail: dict[str, str] = {}
    blockers: list[str] = []
    next_actions: list[str] = []

    # hard kills
    if any(p in domain for p in ("amazon.", "temu.", "shein.", "walmart.", "ebay.", "shopee.", "aliexpress.")):
        return _kill_result(reddit_score, "×平台货淘汰", ["平台货"], "跳过，不跟平台货")
    if verify.get("has_dropship") or int(verify.get("discount_stack_count") or 0) >= 3:
        return _kill_result(reddit_score, "×Dropship淘汰", ["Dropship指纹/折扣三叠"], "跳过，别碰 dsers/三叠折扣店")


    # S1 Meta spy: longevity + creative density (R1/R2)
    s1_ok = days >= 30 and ads >= 3
    s1_strong = days >= 90 and ads >= 5
    if s1_strong:
        gates["S1_META"] = "PASS_STRONG"
        detail["S1_META"] = f"长寿{days}d + 创意{ads}"
    elif s1_ok:
        gates["S1_META"] = "PASS"
        detail["S1_META"] = f"≥30d({days}) + 创意{ads}"
    elif days >= 14 or ads >= 5:
        gates["S1_META"] = "WEAK"
        detail["S1_META"] = f"寿命{days}d/创意{ads}未同时达标"
        blockers.append("Meta信号偏弱(要≥30天且≥3创意)")
    else:
        gates["S1_META"] = "FAIL"
        detail["S1_META"] = f"寿命{days}d/创意{ads}"
        blockers.append("Meta库未过：缺长跑或多创意")

    # S2 LP + Offer (R10 + LP verify)
    has_product_path = bool(re.search(r"/products/[^/?#]+", url))
    has_offer = bool(_OFFER.search(text) or _OFFER.search(url))
    lp_pass = verdict.startswith("PASS") or (
        "lp:shopify" in tags and (cta in _SELL_CTA or has_product_path)
    )
    if verdict.startswith("PASS") and (has_product_path or "lp:shopify" in tags):
        gates["S2_LP"] = "PASS_STRONG" if (has_offer or int(verify.get("return_policy_days") or 0) >= 30) else "PASS"
        detail["S2_LP"] = f"{verdict}; 单品={has_product_path}; offer={has_offer}"
    elif lp_pass or (has_product_path and cta in _SELL_CTA):
        gates["S2_LP"] = "WEAK"
        detail["S2_LP"] = "有卖货形态但LP验证弱/未测"
        blockers.append("补LP验证或Offer(bundle/guarantee)")
    else:
        gates["S2_LP"] = "FAIL"
        detail["S2_LP"] = f"verdict={verdict or '未测'}"
        blockers.append("独立站LP/Offer未过")

    # S3 price band only (margin UNKNOWN without COGS) R6 partial
    if price_f is None:
        gates["S3_PRICE"] = "UNKNOWN"
        detail["S3_PRICE"] = "无标价，毛利硬门未知(需COGS)"
        blockers.append("缺标价/COGS→不能宣称能赚钱")
    elif 20 <= price_f <= 89:
        gates["S3_PRICE"] = "PASS"
        detail["S3_PRICE"] = f"${price_f:.0f} 在FB冲动带；毛利仍需3-5x验算"
        next_actions.append(f"用1688实价算 downside：售价${price_f:.0f} 目标COGS≤${price_f/3.5:.1f}")
    elif 15 <= price_f < 20 or 89 < price_f <= 120:
        gates["S3_PRICE"] = "WEAK"
        detail["S3_PRICE"] = f"${price_f:.0f} 边缘客单"
        blockers.append("客单偏离甜蜜区，先算毛利")
    else:
        gates["S3_PRICE"] = "FAIL"
        detail["S3_PRICE"] = f"${price_f:.0f} 不利FB单位经济"
        blockers.append("客单带差")

    # S4 fulfill / boring R5
    if _FRAGILE.search(text) or _FRAGILE.search(url):
        gates["S4_SHIP"] = "FAIL"
        detail["S4_SHIP"] = "易碎/电子风险"
        blockers.append("履约风险高(电子/易碎)")
    else:
        gates["S4_SHIP"] = "PASS"
        detail["S4_SHIP"] = "偏Boring/好运(未检出高风险词)"

    # S5 cross-platform always pending unless we have external (we don't in pure FB scan)
    gates["S5_CROSS"] = "PENDING"
    detail["S5_CROSS"] = "Reddit: 单FB不够，需TT Creative Center + Amazon NR/BSR"
    next_actions.append("交叉：TikTok Creative Center 同款 + Amazon New Releases/低评论")

    # S6 test gate R8 — only open if S1+S2 pass-ish and not ship fail
    s1_okish = gates["S1_META"] in ("PASS", "PASS_STRONG", "WEAK")
    s2_okish = gates["S2_LP"] in ("PASS", "PASS_STRONG", "WEAK")
    s4_ok = gates["S4_SHIP"] == "PASS"
    if gates["S1_META"] in ("PASS", "PASS_STRONG") and gates["S2_LP"] in ("PASS", "PASS_STRONG") and s4_ok:
        gates["S6_TEST"] = "READY"
        detail["S6_TEST"] = "可进小预算测(DRAFT/烟雾/微测)，未验证前不扩量"
        # budget hint by price band — operational not claim
        if price_f and 20 <= price_f <= 89:
            test_budget = "$20-$40/day × 3-5天，先测钩子/Offer"
        else:
            test_budget = "$15-$30/day × 3天，先看CTR/ATC再谈购"
        next_actions.insert(0, "开DRAFT上架 + mvp-validator 微测，禁止直接LIVE放量")
    elif s1_okish and s2_okish and s4_ok:
        gates["S6_TEST"] = "HOLD"
        detail["S6_TEST"] = "差一脚：补强Meta或LP后再测"
        test_budget = ""
        next_actions.insert(0, "先补关卡再测，勿烧预算")
    else:
        gates["S6_TEST"] = "BLOCKED"
        detail["S6_TEST"] = "前置关卡未过"
        test_budget = ""
    # ── L1: 阶段指针优化 ──
    # READY 优先展示⑥；S5 PENDING 不抢主阶段（并行交叉）
    # 卡关优先 FAIL/BLOCKED；其次 WEAK/HOLD/UNKNOWN；S5 仅作副标签
    stage = "S6_TEST"
    stage_label = "⑥可测"
    side_notes: list[str] = []

    if gates.get("S6_TEST") == "READY":
        stage = "S6_TEST"
        stage_label = "⑥小预算测·READY"
        if gates.get("S5_CROSS") == "PENDING":
            side_notes.append("⑤交叉可并行")
    else:
        picked = False
        for key, label in PIPELINE_STAGES:
            g = gates.get(key, "")
            if key in _NON_BLOCKING_PENDING and g == "PENDING":
                side_notes.append(label + "·并行")
                continue
            if g in ("FAIL", "BLOCKED"):
                stage = key
                stage_label = label + "·卡关"
                picked = True
                break
            if g in ("WEAK", "UNKNOWN", "HOLD"):
                stage = key
                stage_label = label + "·待补"
                picked = True
                break
            if g == "PENDING" and key not in _NON_BLOCKING_PENDING:
                stage = key
                stage_label = label + "·待补"
                picked = True
                break
        if not picked and gates.get("S6_TEST") == "HOLD":
            stage = "S6_TEST"
            stage_label = "⑥测门·HOLD"
        if side_notes and "READY" not in stage_label:
            stage_label = stage_label + "｜" + side_notes[0]

    if side_notes and gates.get("S6_TEST") == "READY":
        stage_label = "⑥READY｜" + side_notes[0]

    # ── L4 funnel_score + L7 intensity ──
    weights = {
        "PASS_STRONG": 18,
        "PASS": 14,
        "READY": 22,
        "WEAK": 6,
        "UNKNOWN": 4,
        "PENDING": 3,  # S5 pending 低权重，不虚高
        "HOLD": 8,
        "FAIL": 0,
        "BLOCKED": 0,
    }
    rank = 0
    for key, _ in PIPELINE_STAGES:
        rank += weights.get(gates.get(key, ""), 0)
    # READY 额外顶格，保证可测排最前
    if gates.get("S6_TEST") == "READY":
        rank += 25
    rank = int(rank + reddit_score * 0.35)
    if stage == "KILL":
        rank = 0

    intensity = min(100, ads * 3 + landings * 4 + kws * 5 + min(40, days // 3))
    funnel_score = min(100, rank)

    # ── L5 next_action 优先级：卡关 > 待补 > 可测动作 ──
    ordered_fixes: list[str] = []
    gate_fix_map = {
        "S1_META": "回Meta库补：找≥30天且同域≥3条创意的页",
        "S2_LP": "打开落地页：确认Shopify/单品路径+Offer(保证/bundle)",
        "S3_PRICE": "记下标价并用1688算3-5x毛利硬门",
        "S4_SHIP": "排除易碎电子/重货，或换履约方案",
        "S5_CROSS": "并行：TT Creative Center同款 + Amazon NR/低评论",
        "S6_TEST": "开DRAFT + mvp微测，禁止LIVE放量",
    }
    for key, _label in PIPELINE_STAGES:
        g = gates.get(key, "")
        if g in ("FAIL", "BLOCKED", "WEAK", "HOLD", "UNKNOWN"):
            ordered_fixes.append(gate_fix_map.get(key, key))
        elif key == "S5_CROSS" and g == "PENDING" and gates.get("S6_TEST") == "READY":
            ordered_fixes.append(gate_fix_map["S5_CROSS"] + "（可与测试并行）")

    if gates.get("S6_TEST") == "READY":
        next_action = "①" + gate_fix_map["S6_TEST"] + " ②" + gate_fix_map["S5_CROSS"]
        next_actions = [gate_fix_map["S6_TEST"], gate_fix_map["S5_CROSS"], "测后只扩 breakeven 以上素材"]
    elif ordered_fixes:
        next_action = " → ".join(ordered_fixes[:2])
        next_actions = ordered_fixes[:4]
    elif blockers:
        next_action = "；".join(blockers[:2])
        next_actions = blockers[:4]
    else:
        next_action = "继续观察池，不烧预算"
        next_actions = [next_action]

    # money_process + priority queue (Reddit ops)
    if gates.get("S6_TEST") == "READY" and gates.get("S1_META") in ("PASS", "PASS_STRONG"):
        money_process = "TEST_READY"
        priority_queue = "A-立刻可测"
    elif gates.get("S2_LP") in ("PASS", "PASS_STRONG") and gates.get("S1_META") in ("PASS", "PASS_STRONG"):
        money_process = "ALMOST_READY"
        priority_queue = "B-补交叉/客单"
    elif gates.get("S2_LP") in ("PASS", "PASS_STRONG") and gates.get("S1_META") in ("PASS", "PASS_STRONG", "WEAK"):
        money_process = "LP_OK_待交叉"
        priority_queue = "B-补Meta或交叉"
    elif gates.get("S1_META") in ("PASS", "PASS_STRONG"):
        money_process = "META_OK_补LP"
        priority_queue = "C-补落地验证"
    elif reddit_score >= 40:
        money_process = "观察池"
        priority_queue = "C-观察"
    else:
        money_process = "弱/跳过"
        priority_queue = "D-跳过"

    return {
        "stage": stage,
        "stage_label": stage_label,
        "pipeline_rank": min(100, rank),
        "funnel_score": funnel_score,
        "gates": gates,
        "gate_detail": detail,
        "blockers": blockers,
        "next_action": next_action,
        "next_actions": next_actions[:4],
        "test_budget": test_budget if gates.get("S6_TEST") == "READY" else "",
        "money_process": money_process,
        "reddit_score": reddit_score,
        "s1": gates.get("S1_META"),
        "s2": gates.get("S2_LP"),
        "s3": gates.get("S3_PRICE"),
        "s4": gates.get("S4_SHIP"),
        "s5": gates.get("S5_CROSS"),
        "s6": gates.get("S6_TEST"),
        "offer_hit": has_offer,
        "price_hint": price_f,
        "intensity": intensity,
        "priority_queue": priority_queue,
    }


