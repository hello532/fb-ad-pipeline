"""EXTRACT_JS — browser console extractor (v10: wide context + DOM fallback).

This is the DOM-fallback extractor. Whenever possible the GraphQL sniffer
(graphql_sniffer.install_sniffer) provides a richer payload; this script is
kept so we still get something even when GraphQL responses are not captured.

v10 vs v9:
- WIDENED context window: 12KB before + 60KB after (was 4KB+18KB)
  FB's SSR JSON has fields like byline, ig_handle, publisher_platforms,
  video_duration_sec far from the ad_archive_id anchor.
- Added FULL-TEXT second-pass scan: searches entire script content for
  high-value fields that fall outside even the widened window.
- Added DOM ELEMENT extraction pass: extracts byline from "Sponsored by",
  ig_handle from Instagram links, ad_snapshot_url from share buttons,
  publisher_platforms from platform badges, video duration from <video>.
- Three-layer extraction: JSON context → full-text scan → DOM elements.
"""

# ── Browser Console JS v10 ───────────────────────────────────────────────────
EXTRACT_JS_V9 = """(() => {
  const scripts = document.querySelectorAll('script');
  const a = [];
  for (const s of scripts) {
    const text = s.textContent || '';
    if (!text.includes('"ad_archive_id"')) continue;
    const regex = /"ad_archive_id"\\s*:\\s*"(\\d+)"/g;
    let m;
    while ((m = regex.exec(text)) !== null) {
      const id = m[1];
      // v10: widened context window (12KB before + 60KB after)
      const st = Math.max(0, m.index - 12000);
      const en = Math.min(text.length, m.index + 60000);
      const c = text.substring(st, en);

      const g = (k) => {
        const x = c.match(new RegExp('"' + k + '"\\\\s*:\\\\s*"([^"]*)"'));
        return x ? x[1].replace(/\\u[\\dA-Fa-f]{4}/g, q => String.fromCharCode(parseInt(q.substr(2),16))) : '';
      };
      const gi = (k) => {
        const x = c.match(new RegExp('"' + k + '"\\\\s*:\\\\s*(\\\\d+)'));
        return x ? parseInt(x[1]) : null;
      };
      const gf = (k) => {
        const x = c.match(new RegExp('"' + k + '"\\\\s*:\\\\s*([\\\\d.]+)'));
        return x ? parseFloat(x[1]) : null;
      };
      const gb = (k) => {
        const x = c.match(new RegExp('"' + k + '"\\\\s*:\\\\s*(true|false)'));
        return x ? x[1] === 'true' : null;
      };
      const ga = (k) => {
        const x = c.match(new RegExp('"' + k + '"\\\\s*:\\\\s*\\\\[([^\\\\]]*?)\\\\]'));
        return x ? x[1].split(',').map(v => v.replace(/"/g,'').trim()).filter(Boolean) : [];
      };
      const go = (k) => {
        const x = c.match(new RegExp('"' + k + '"\\\\s*:\\\\s*\\\\{[^}]*?"lower_bound"\\\\s*:\\\\s*"?(\\\\d+)"?[^}]*?"upper_bound"\\\\s*:\\\\s*"?(\\\\d+)"?'));
        return x ? {lower: parseInt(x[1]), upper: parseInt(x[2])} : null;
      };
      const gFull = (k) => {
        const x = c.match(new RegExp('"' + k + '"\\\\s*:\\\\s*"((?:[^"\\\\\\\\]|\\\\\\\\.)*)'));
        return x ? x[1].replace(/\\u[\\dA-Fa-f]{4}/g, q => String.fromCharCode(parseInt(q.substr(2),16))).substring(0,3000) : '';
      };
      // 提取所有 image/video URLs (carousel 会有多个)
      const gAllUrls = (keyPattern) => {
        const re = new RegExp('"(' + keyPattern + ')"\\\\s*:\\\\s*"(https?:[^"]+)"', 'g');
        const urls = new Set();
        let mm;
        while ((mm = re.exec(c)) !== null) urls.add(mm[2]);
        return Array.from(urls).slice(0, 10);
      };
      // 规范化 date (既可能是 unix int 也可能是 ISO)
      const gDateIso = (k) => {
        const iv = gi(k);
        if (iv && iv > 1_000_000) {
          try { return new Date(iv * 1000).toISOString().split('T')[0]; } catch (e) {}
        }
        return g(k);
      };
      // 平衡花括号匹配 — 抓子对象 (用于 page_categories / regional / demographic 原始 JSON)
      const gObjBalanced = (k) => {
        const sm = c.match(new RegExp('"' + k + '"\\\\s*:\\\\s*[{\\\\[]'));
        if (!sm) return '';
        const start = sm.index + sm[0].length - 1;
        const open = c[start];
        const close = open === '{' ? '}' : ']';
        let depth = 0, inStr = false, escape = false;
        for (let i = start; i < Math.min(c.length, start + 12000); i++) {
          const ch = c[i];
          if (escape) { escape = false; continue; }
          if (ch === '\\\\') { escape = true; continue; }
          if (ch === '"') { inStr = !inStr; continue; }
          if (inStr) continue;
          if (ch === open) depth++;
          else if (ch === close) {
            depth--;
            if (depth === 0) return c.substring(start, i + 1);
          }
        }
        return '';
      };

      // cards / carousel — 每张卡片自己也带 title/body/cta/link
      const gCards = () => {
        const out = [];
        const re = /"cards"\\s*:\\s*\\[/g;
        const mm = re.exec(c);
        if (!mm) return out;
        const start = mm.index + mm[0].length - 1;  // at "["
        let depth = 0, inStr = false, escape = false, end = -1;
        for (let i = start; i < Math.min(c.length, start + 30000); i++) {
          const ch = c[i];
          if (escape) { escape = false; continue; }
          if (ch === '\\\\') { escape = true; continue; }
          if (ch === '"') { inStr = !inStr; continue; }
          if (inStr) continue;
          if (ch === '[') depth++;
          else if (ch === ']') { depth--; if (depth === 0) { end = i + 1; break; } }
        }
        if (end < 0) return out;
        const arr = c.substring(start, end);
        try {
          const parsed = JSON.parse(arr);
          if (Array.isArray(parsed)) {
            return parsed.slice(0, 10).map(card => ({
              title:              card?.title || '',
              body:               card?.body || '',
              cta_type:           card?.cta_type || '',
              cta_text:           card?.cta_text || '',
              link_url:           card?.link_url || '',
              image_url:          card?.original_image_url || card?.resized_image_url || '',
              video_url:          card?.video_hd_url || card?.video_sd_url || '',
              video_duration_sec: card?.video_duration_sec || null,
            }));
          }
        } catch (e) {}
        return out;
      };

      const cards = gCards();

      // ── LAYER 2: full-text second-pass scan ──
      // Search the ENTIRE script text for high-value fields that may fall
      // outside even the widened 72KB context window. Use the ad_archive_id
      // to find the enclosing JSON object boundary, then search within it.
      const gFullText = (k, regexFlags) => {
        // Find the specific JSON node that contains this ad_archive_id
        // by walking outward from the match position to find balanced braces
        const anchorSt = Math.max(0, m.index - 50000);
        const anchorEn = Math.min(text.length, m.index + 150000);
        const region = text.substring(anchorSt, anchorEn);
        const re = new RegExp('"' + k + '"\\\\s*:\\\\s*"((?:[^"\\\\\\\\]|\\\\\\\\.)*)' + '"', regexFlags || '');
        const x = region.match(re);
        return x ? x[1].replace(/\\u[\\dA-Fa-f]{4}/g, q => String.fromCharCode(parseInt(q.substr(2),16))).substring(0,3000) : '';
      };
      const giFullText = (k) => {
        const anchorSt = Math.max(0, m.index - 50000);
        const anchorEn = Math.min(text.length, m.index + 150000);
        const region = text.substring(anchorSt, anchorEn);
        const x = region.match(new RegExp('"' + k + '"\\\\s*:\\\\s*(\\\\d+)'));
        return x ? parseInt(x[1]) : null;
      };
      const gaFullText = (k) => {
        const anchorSt = Math.max(0, m.index - 50000);
        const anchorEn = Math.min(text.length, m.index + 150000);
        const region = text.substring(anchorSt, anchorEn);
        const x = region.match(new RegExp('"' + k + '"\\\\s*:\\\\s*\\\\[([^\\\\]]*?)\\\\]'));
        return x ? x[1].split(',').map(v => v.replace(/"/g,'').trim()).filter(Boolean) : [];
      };

      a.push({
        ad_archive_id:                    id,
        page_name:                        g('page_name') || gFullText('page_name') || 'Unknown Brand',
        page_id:                          g('page_id') || gFullText('page_id') || '',
        page_profile_uri:                 g('page_profile_uri') || gFullText('page_profile_uri') || '',
        page_profile_picture_url:         g('page_profile_picture_url') || g('profile_picture_url') || gFullText('page_profile_picture_url') || '',
        page_like_count:                  gi('page_like_count') || giFullText('page_like_count') || 0,
        page_categories:                  ga('page_categories').length ? ga('page_categories') : gaFullText('page_categories'),
        ig_handle:                        g('instagram_actor_name') || g('ig_username') || gFullText('instagram_actor_name') || gFullText('ig_username') || '',
        ig_followers:                     gi('instagram_followers') || gi('ig_followers') || giFullText('instagram_followers') || giFullText('ig_followers') || 0,
        byline:                           g('byline') || gFullText('byline') || '',
        start_date:                       gi('start_date') || g('start_date') || giFullText('start_date') || 0,
        start_date_iso:                   gDateIso('start_date'),
        end_date:                         g('end_date') || g('ad_delivery_stop_time') || gFullText('end_date') || gFullText('ad_delivery_stop_time') || '',
        end_date_iso:                     gDateIso('end_date') || gDateIso('ad_delivery_stop_time'),
        is_active:                        c.includes('"is_active":true') || c.includes('"ad_delivery_status":"active"'),
        display_format:                   g('display_format') || gFullText('display_format') || '',
        archive_type:                     g('archive_type') || gFullText('archive_type') || '',
        cta_type:                         g('cta_type') || '',
        cta_text:                         g('cta_text') || '',
        link_url:                         g('link_url') || '',
        link_description:                 g('link_description') || '',
        link_caption:                     g('link_caption') || g('caption') || '',
        title:                            g('title') || '',
        body:                             gFull('body') || gFullText('body'),
        ad_snapshot_url:                  g('ad_snapshot_url') || g('snapshot_url') || gFullText('ad_snapshot_url') || gFullText('snapshot_url') || '',
        collation_count:                  gi('collation_count') || 1,
        total_active_time:                gi('total_active_time') || giFullText('total_active_time'),
        publisher_platforms:              ga('publisher_platforms').length ? ga('publisher_platforms') : (ga('publisher_platform').length ? ga('publisher_platform') : gaFullText('publisher_platforms')),
        spend:                            go('spend'),
        impressions:                      go('impressions') || go('impressions_with_index'),
        currency:                         g('currency') || '',
        target_locations:                 ga('target_locations').length ? ga('target_locations') : gaFullText('target_locations'),
        target_ages:                      g('target_ages') || '',
        target_genders:                   g('target_genders') || '',
        languages:                        ga('languages').length ? ga('languages') : gaFullText('languages'),
        image_url:                        g('original_image_url') || g('resized_image_url') || '',
        video_url:                        g('video_hd_url') || g('video_sd_url') || '',
        video_preview_image_url:          g('video_preview_image_url'),
        video_duration_sec:               gf('video_duration_sec') || gi('video_duration_sec') || giFullText('video_duration_sec'),
        carousel_images:                  gAllUrls('original_image_url|resized_image_url'),
        carousel_videos:                  gAllUrls('video_hd_url|video_sd_url'),
        cards:                            cards,
        cards_count:                      cards.length,
        disclaimer_label:                 g('disclaimer_label') || g('funding_entity') || '',
        funding_entity:                   g('funding_entity') || '',
        has_user_reported:                gb('has_user_reported'),
        contains_sensitive_content:       gb('contains_sensitive_content'),
        contains_digital_created_media:   gb('contains_digital_created_media'),
        branded_content:                  gb('branded_content'),
        ad_creative_id:                   g('ad_creative_id') || '',
        regional_distribution_raw:        gObjBalanced('regional_distribution'),
        demographic_distribution_raw:     gObjBalanced('age_country_gender_reach_breakdown') || gObjBalanced('demographic_distribution'),
        eu_total_reach:                   gi('eu_total_reach') || giFullText('eu_total_reach'),
        eu_political_ad:                  gb('is_political_ad') || gb('eu_political_ad'),
        targeted_or_reached_countries:    ga('targeted_or_reached_countries'),
        extracted_at:                     new Date().toISOString(),
        _source:                          'dom',
      });
    }
  }

  // ── LAYER 3: DOM element extraction pass ──
  // After all JSON-context extractions, scan rendered DOM elements for
  // fields that the inline JSON doesn't contain (or that fell through).

  // Build a lookup: ad_archive_id → ad dict (to patch in-place)
  const byId = {};
  for (const ad of a) {
    if (ad.ad_archive_id) byId[ad.ad_archive_id] = ad;
  }

  // 3a. byline — "Sponsored by <name>" text in ad cards
  try {
    const sponsorEls = document.querySelectorAll('[data-testid="sponsor-label"], [data-testid="byline"], .x1n2onr6 .x1rg5ohu');
    for (const el of sponsorEls) {
      const txt = (el.textContent || '').trim();
      const m2 = txt.match(/(?:Sponsored|Paid)\\s+by\\s+(.+)/i);
      if (m2 && m2[1]) {
        // Try to find which ad card this belongs to
        const card = el.closest('[data-testid="ad_card"], [data-testid="card"], .x1lliihq');
        if (card) {
          // Find ad_archive_id in card's data attributes or nearby text
          const idEl = card.querySelector('[data-ad-archive-id]');
          const aid = idEl ? idEl.getAttribute('data-ad-archive-id') : '';
          const target = aid && byId[aid] ? byId[aid] : null;
          if (target && !target.byline) {
            target.byline = m2[1].trim();
          }
        }
      }
    }
  } catch(e) {}

  // 3b. ig_handle — Instagram links in ad cards
  try {
    const igLinks = document.querySelectorAll('a[href*="instagram.com/"]');
    for (const link of igLinks) {
      const href = link.getAttribute('href') || '';
      const m2 = href.match(/instagram\\.com\\/([\\w.\\-]+)/);
      if (m2 && m2[1] && m2[1] !== 'p' && m2[1] !== 'reel') {
        const card = link.closest('[data-testid="ad_card"], [data-testid="card"], .x1lliihq');
        const idEl = card ? card.querySelector('[data-ad-archive-id]') : null;
        const aid = idEl ? idEl.getAttribute('data-ad-archive-id') : '';
        const target = aid && byId[aid] ? byId[aid] : null;
        if (target && !target.ig_handle) {
          target.ig_handle = m2[1];
        }
      }
    }
  } catch(e) {}

  // 3c. ad_snapshot_url — share/detail links
  try {
    const snapLinks = document.querySelectorAll('a[href*="facebook.com/ads/library/"]');
    for (const link of snapLinks) {
      const href = link.getAttribute('href') || '';
      const m2 = href.match(/id=(\\d+)/);
      if (m2 && m2[1]) {
        const aid = m2[1];
        if (byId[aid] && !byId[aid].ad_snapshot_url) {
          byId[aid].ad_snapshot_url = href;
        }
      }
    }
  } catch(e) {}

  // 3d. video_duration_sec — from <video> elements
  try {
    const videos = document.querySelectorAll('video');
    for (const vid of videos) {
      const dur = vid.duration;
      if (dur && isFinite(dur) && dur > 0) {
        // Find which ad card this video belongs to
        const card = vid.closest('[data-testid="ad_card"], [data-testid="card"], .x1lliihq');
        if (card) {
          const idEl = card.querySelector('[data-ad-archive-id]');
          const aid = idEl ? idEl.getAttribute('data-ad-archive-id') : '';
          const target = aid && byId[aid] ? byId[aid] : null;
          if (target && !target.video_duration_sec) {
            target.video_duration_sec = Math.round(dur);
          }
        }
      }
    }
  } catch(e) {}

  // 3e. publisher_platforms — from platform badge icons
  try {
    const platIcons = document.querySelectorAll('[data-testid*="platform"], [aria-label*="Instagram"], [aria-label*="Facebook"]');
    for (const icon of platIcons) {
      const label = (icon.getAttribute('aria-label') || '').toLowerCase();
      if (!label) continue;
      const card = icon.closest('[data-testid="ad_card"], [data-testid="card"], .x1lliihq');
      if (!card) continue;
      const idEl = card.querySelector('[data-ad-archive-id]');
      const aid = idEl ? idEl.getAttribute('data-ad-archive-id') : '';
      const target = aid && byId[aid] ? byId[aid] : null;
      if (target && (!target.publisher_platforms || !target.publisher_platforms.length)) {
        const plats = [];
        if (label.includes('facebook')) plats.push('FACEBOOK');
        if (label.includes('instagram')) plats.push('INSTAGRAM');
        if (label.includes('messenger')) plats.push('MESSENGER');
        if (label.includes('audience')) plats.push('AUDIENCE_NETWORK');
        if (plats.length) target.publisher_platforms = plats;
      }
    }
  } catch(e) {}

  const seen = new Set();
  const u = a.filter(x => { if(seen.has(x.ad_archive_id)) return false; seen.add(x.ad_archive_id); return true; });
  return JSON.stringify({count: u.length, version: '10.0', results: u});
})()"""

# Backwards-compat alias — older imports use EXTRACT_JS_V7
EXTRACT_JS_V7 = EXTRACT_JS_V9
