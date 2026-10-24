"""pipeline/graphql_sniffer.py — Playwright /api/graphql/ response sniffer (v9.1).

抓取 FB Ad Library 页面真实发出的 GraphQL 请求, 比 DOM regex 拿到完整字段:
- regional_distribution (per-state percentages)
- demographic_distribution (age x gender x reach)
- ig_handle, ig_followers, page_profile_picture_url, byline
- video_duration_sec, video_hd/sd/preview URLs
- carousel cards 全部 (per-card title/body/cta/link)
- eu_total_reach, eu_political_ad
- spend / impressions ranges
- aaa_info (advertiser identifiers)

用法:
    state = install_sniffer(page)   # 在 page.goto 之前调用
    # ... wait for page to load and fetch responses
    ads = list(state['ads'].values())  # graphql 派生的富 ad 列表
"""
from __future__ import annotations

import json
from typing import Any, Optional


# 已知会返回 ad 数据的 doc 名 (用于轻量过滤, 非必须)
AD_DOC_HINTS = (
    'AdLibraryMobileFocusedStateProvider',
    'AdLibrarySearchPaginationQuery',
    'AdLibraryAdDetailsV2Query',
    'AdLibraryAdCollationDetailsQuery',
    'AdLibrarySearchTypeaheadDataSource',
)

# 单条 GraphQL 响应体上限 (FB streaming 偶尔会超大). 超过就跳过, 避免 OOM.
MAX_BODY_BYTES = 12 * 1024 * 1024   # 12 MB


def install_sniffer(page) -> dict:
    """Attach response listener. Returns dict that will be populated as responses arrive.

    state schema:
        ads:             {ad_archive_id: rich_ad_dict}
        graphql_count:   number of /api/graphql/ JSON responses processed
        ad_responses:    number of those that contained ad_archive_id
        skipped_too_big: count of responses skipped due to size cap
        errors:          soft errors during parse
        forward_cursor:  most recent forward_cursor seen in an ad response
                         (used by graphql_replay for HTTP pagination)
    """
    state: dict = {
        'ads': {},
        'graphql_count': 0,
        'ad_responses': 0,
        'skipped_too_big': 0,
        'errors': 0,
        'forward_cursor': '',
    }

    import re as _re
    _cursor_re = _re.compile(r'"forward_cursor"\s*:\s*"([^"]+)"')

    def _handler(response):
        try:
            url = response.url
            if '/api/graphql/' not in url:
                return
            try:
                status = response.status
            except Exception:
                status = 0
            if status and status >= 400:
                return
            try:
                ct = (response.headers.get('content-type') or '').lower()
            except Exception:
                ct = ''
            # GraphQL streaming responses are usually 'text/javascript' or 'application/json'
            if 'json' not in ct and 'javascript' not in ct and 'text/' not in ct:
                return
            try:
                body = response.text()
            except Exception:
                return
            if not body:
                return
            # Soft size cap — skip pathologically huge bodies (e.g. some ad-collation pages)
            if len(body) > MAX_BODY_BYTES:
                state['skipped_too_big'] += 1
                return
            state['graphql_count'] += 1
            if '"ad_archive_id"' not in body and '"adArchiveID"' not in body:
                return
            state['ad_responses'] += 1
            # Latch the latest forward_cursor from any ad-bearing response —
            # graphql_replay needs this to fetch pages 2..N via curl_cffi HTTP
            # without driving the browser through a scroll loop.
            cm = _cursor_re.search(body)
            if cm:
                state['forward_cursor'] = cm.group(1)
            # FB GraphQL streaming format = multiple JSON objects concatenated
            # (sometimes newline-separated, sometimes back-to-back '}{').
            # Use raw_decode in a loop instead of split('\n') so we don't lose
            # objects that happen to contain newlines in string values.
            _decode_stream_into(body, state)
        except Exception:
            state['errors'] += 1

    page.on('response', _handler)
    return state


def _decode_stream_into(body: str, state: dict) -> None:
    """Iterate over concatenated/streamed JSON objects in `body`, walking each."""
    decoder = json.JSONDecoder()
    i = 0
    n = len(body)
    safety = 0
    while i < n:
        # skip whitespace and stray separators
        while i < n and body[i] in ' \t\r\n,':
            i += 1
        if i >= n:
            break
        if body[i] not in '{[':
            # garbage/text chunk — advance to next plausible JSON start
            j = body.find('{', i)
            if j < 0:
                break
            i = j
        try:
            obj, end = decoder.raw_decode(body, i)
        except ValueError:
            # current position not a valid JSON start; skip a char and retry
            i += 1
            safety += 1
            # safety limit so a degenerate body can't pin the loop forever
            if safety > 20000:
                state['errors'] += 1
                return
            continue
        _walk_and_extract(obj, state)
        i = end


# ── Walking ────────────────────────────────────────────────────────────────

def _walk_and_extract(obj: Any, state: dict, depth: int = 0) -> None:
    """递归遍历 GraphQL JSON tree, 在每个含 ad_archive_id 的 dict 节点提取字段."""
    if depth > 40:
        return
    if isinstance(obj, dict):
        # 直接命中
        if obj.get('ad_archive_id') or obj.get('adArchiveID'):
            ad = _extract_ad_node(obj)
            if ad and ad.get('ad_archive_id'):
                aid = ad['ad_archive_id']
                existing = state['ads'].get(aid)
                if existing is None or _completeness(ad) > _completeness(existing):
                    state['ads'][aid] = ad
        for v in obj.values():
            _walk_and_extract(v, state, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _walk_and_extract(v, state, depth + 1)


def _completeness(ad: dict) -> int:
    """字段非空 ⇒ +1. 简单完整度评分用于 merge 时择优."""
    return sum(1 for v in ad.values() if v not in (None, '', [], {}, 0, False))


# ── Field extraction ───────────────────────────────────────────────────────

def _bound(d: Any, key: str) -> Optional[int]:
    if not isinstance(d, dict):
        return None
    v = d.get(key)
    if v in (None, ''):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _str(v: Any) -> str:
    return v if isinstance(v, str) else ''


def _list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _bool(v: Any) -> bool:
    if v is True or v is False:
        return v
    if v in ('true', 'True', 1):
        return True
    if v in ('false', 'False', 0, None):
        return False
    return bool(v)


def _extract_ad_node(node: dict) -> Optional[dict]:
    """从 GraphQL ad node 提取标准化字段."""
    aid = _str(node.get('ad_archive_id') or node.get('adArchiveID'))
    if not aid or not aid.isdigit():
        return None

    snap = node.get('snapshot') if isinstance(node.get('snapshot'), dict) else {}
    page_snap = snap.get('page') if isinstance(snap.get('page'), dict) else {}

    # 视频 / 图片 / cards
    videos = _list(snap.get('videos'))
    images = _list(snap.get('images'))
    cards = _list(snap.get('cards'))

    # spend / impressions
    spend_obj = node.get('spend') if isinstance(node.get('spend'), dict) else {}
    imp_obj = (node.get('impressions_with_index')
               or node.get('impressions')
               or {})
    if not isinstance(imp_obj, dict):
        imp_obj = {}

    # 地理 / 受众 — 直接保留原始结构, server 端再聚合
    regional = _list(node.get('regional_distribution'))
    demographic = (_list(node.get('age_country_gender_reach_breakdown'))
                   or _list(node.get('demographic_distribution')))

    platforms_raw = (node.get('publisher_platform')
                     or node.get('publisher_platforms')
                     or snap.get('publisher_platforms')
                     or [])
    platforms = platforms_raw if isinstance(platforms_raw, list) else [platforms_raw]
    platforms = [p for p in platforms if p]

    categories = _list(node.get('categories')) or _list(snap.get('categories'))

    # cards normalize
    cards_norm = []
    for c in cards[:10]:
        if not isinstance(c, dict):
            continue
        cards_norm.append({
            'title':              _str(c.get('title')),
            'body':               _str(c.get('body')),
            'cta_type':           _str(c.get('cta_type')),
            'cta_text':           _str(c.get('cta_text')),
            'link_url':           _str(c.get('link_url')),
            'image_url':          _str(c.get('original_image_url') or c.get('resized_image_url')),
            'video_url':          _str(c.get('video_hd_url') or c.get('video_sd_url')),
            'video_duration_sec': c.get('video_duration_sec') or 0,
        })

    videos_norm = []
    for v in videos[:5]:
        if not isinstance(v, dict):
            continue
        videos_norm.append({
            'video_hd_url':            _str(v.get('video_hd_url')),
            'video_sd_url':            _str(v.get('video_sd_url')),
            'video_preview_image_url': _str(v.get('video_preview_image_url')),
            'video_duration_sec': v.get('video_duration_sec') or 0,
        })

    images_norm = []
    for im in images[:10]:
        if isinstance(im, dict):
            url = _str(im.get('original_image_url') or im.get('resized_image_url'))
            if url:
                images_norm.append(url)

    # text fields
    body_obj = snap.get('body')
    if isinstance(body_obj, dict):
        body_text = _str(body_obj.get('text'))
    else:
        body_text = _str(body_obj) or _str(node.get('body'))

    title_obj = snap.get('title')
    if isinstance(title_obj, dict):
        title = _str(title_obj.get('text'))
    else:
        title = _str(title_obj) or _str(node.get('title'))

    link_url = _str(snap.get('link_url') or node.get('link_url'))
    link_caption = _str(snap.get('caption') or snap.get('link_caption') or node.get('link_caption'))
    link_description = _str(snap.get('link_description') or node.get('link_description'))
    cta_type = _str(snap.get('cta_type') or node.get('cta_type'))
    cta_text = _str(snap.get('cta_text') or node.get('cta_text'))
    display_format = _str(snap.get('display_format') or node.get('display_format'))

    # page info
    page_id = _str(page_snap.get('id') or node.get('page_id'))
    page_name = _str(page_snap.get('name') or node.get('page_name')) or 'Unknown Brand'
    page_likes = page_snap.get('likes') or node.get('page_like_count') or 0
    page_profile_uri = _str(page_snap.get('profile_uri') or node.get('page_profile_uri'))
    page_profile_picture_url = _str(
        page_snap.get('profile_picture_url')
        or snap.get('page_profile_picture_url')
    )
    page_categories_field = (page_snap.get('categories')
                              or node.get('page_categories')
                              or [])
    page_categories = page_categories_field if isinstance(page_categories_field, list) else []

    ig_handle = _str(snap.get('instagram_actor_name')
                     or page_snap.get('instagram_handle')
                     or node.get('instagram_actor_name'))
    ig_followers_raw = (page_snap.get('instagram_followers')
                        or node.get('ig_followers')
                        or snap.get('instagram_followers')
                        or 0)
    try:
        ig_followers = int(ig_followers_raw or 0)
    except (TypeError, ValueError):
        ig_followers = 0

    byline = _str(snap.get('byline') or page_snap.get('byline') or node.get('byline'))

    return {
        'ad_archive_id':                  aid,
        'page_name':                      page_name,
        'page_id':                        page_id,
        'page_profile_uri':               page_profile_uri,
        'page_profile_picture_url':       page_profile_picture_url,
        'page_like_count':                int(page_likes or 0),
        'page_categories':                page_categories,
        'ig_handle':                      ig_handle,
        'ig_followers':                   ig_followers,
        'byline':                         byline,
        'start_date':                     node.get('start_date'),
        'end_date':                       node.get('end_date') or node.get('ad_delivery_stop_time'),
        'is_active':                      _bool(node.get('is_active')),
        'display_format':                 display_format,
        'archive_type':                   _str(node.get('archive_type')),
        'cta_type':                       cta_type,
        'cta_text':                       cta_text,
        'link_url':                       link_url,
        'link_description':               link_description,
        'link_caption':                   link_caption,
        'title':                          title,
        'body':                           body_text[:3000] if body_text else '',
        'ad_snapshot_url':                _str(node.get('ad_snapshot_url')),
        'collation_count':                int(node.get('collation_count') or 1),
        'total_active_time':              node.get('total_active_time'),
        'publisher_platforms':            platforms,
        'categories':                     categories,
        'spend':                          {
                'lower': _bound(spend_obj, 'lower_bound'),
                'upper': _bound(spend_obj, 'upper_bound'),
            } if spend_obj else {},
        'impressions':                    {
                'lower': _bound(imp_obj, 'lower_bound'),
                'upper': _bound(imp_obj, 'upper_bound'),
            } if imp_obj else {},
        'currency':                       _str(node.get('currency')),
        'target_locations':               _list(node.get('target_locations')),
        'target_ages':                    _str(node.get('target_ages')),
        'target_genders':                 _str(node.get('target_genders')),
        'languages':                      _list(node.get('languages')),
        'image_url':                      images_norm[0] if images_norm else '',
        'video_url':                      videos_norm[0]['video_hd_url'] if videos_norm else '',
        'video_preview_image_url':        videos_norm[0]['video_preview_image_url'] if videos_norm else '',
        'video_duration_sec':             videos_norm[0].get('video_duration_sec') or 0 if videos_norm else 0,
        'carousel_images':                images_norm,
        'carousel_videos':                [v['video_hd_url'] or v['video_sd_url'] for v in videos_norm],
        'cards':                          cards_norm,
        'cards_count':                    len(cards_norm),
        'disclaimer_label':               _str(node.get('disclaimer_label')),
        'funding_entity':                 _str(node.get('funding_entity')),
        'has_user_reported':              _bool(node.get('has_user_reported')),
        'contains_sensitive_content':     _bool(node.get('contains_sensitive_content')),
        'contains_digital_created_media': _bool(node.get('contains_digital_created_media')),
        'branded_content':                _bool(node.get('branded_content')),
        'ad_creative_id':                 _str(node.get('ad_creative_id')),
        'regional_distribution':          regional,
        'demographic_distribution':       demographic,
        'eu_total_reach':                 node.get('eu_total_reach'),
        'eu_political_ad':                _bool(node.get('is_political_ad') or node.get('eu_political_ad')),
        'targeted_or_reached_countries':  _list(node.get('targeted_or_reached_countries')),
        'aaa_info':                       node.get('aaa_info') if isinstance(node.get('aaa_info'), dict) else {},
        '_source':                        'graphql',
    }


def merge_dom_with_graphql(dom_ads: list, graphql_ads_by_id: dict) -> list:
    """Merge two extraction results by ad_archive_id. GraphQL fields win when non-empty.

    DOM 路径产出更稳定 (兜底), GraphQL 路径产出更丰富. 对同一 id 的字段取较丰富者.
    """
    if not graphql_ads_by_id:
        return dom_ads
    out_by_id: dict = {}
    for ad in dom_ads or []:
        aid = ad.get('ad_archive_id')
        if aid:
            out_by_id[aid] = dict(ad)
    for aid, gad in graphql_ads_by_id.items():
        base = out_by_id.get(aid, {})
        for k, v in gad.items():
            # 优先用 GraphQL 的非空值
            if v in (None, '', [], {}):
                continue
            if k not in base or base.get(k) in (None, '', [], {}, 0, False):
                base[k] = v
        # 标注 source
        if 'graphql' in (gad.get('_source') or ''):
            base['_source'] = (
                'graphql+dom' if base.get('_source') == 'dom' else 'graphql'
            )
        out_by_id[aid] = base
    return list(out_by_id.values())
