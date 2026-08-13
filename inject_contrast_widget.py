#!/usr/bin/env python3
"""
inject_contrast_widget.py
──────────────────────────────────────────────────────────────────────
Scrapes each PLL/WLL team page's own "Logos & Marks" and "Brand Colors"
sections for real asset paths / hex values / invisible-on rules, then
injects the Contrast Preview widget (self-contained style+markup+script)
right before <footer class="footer"> on every page in a folder.

No hand-typed per-team data — everything the widget needs is pulled
straight from markup that's already on each page, so it automatically
stays correct even if you add/remove logo variants or recolor a team
later (as long as you rerun this script after such changes — it's not
live-syncing, it's a one-shot batch injection).

USAGE
  python3 inject_contrast_widget.py /path/to/team-pages --dry-run
  python3 inject_contrast_widget.py /path/to/team-pages
  python3 inject_contrast_widget.py /path/to/team-pages --output-dir /path/to/out

FLAGS
  --dry-run        Parse + report what would happen, write nothing.
  --output-dir DIR Write modified copies here instead of editing in place.
  --no-backup       Skip writing .bak files when editing in place.
  --force           Re-inject even if a page already has the widget
                     (normally skipped to avoid duplicates).

WHAT IT ASSUMES ABOUT YOUR MARKUP (matches boston-cannons.html):
  - Logo variants live in  <div class="logo-group" data-type="...">
      containing  .logo-tile[data-orig-bg][data-invisible-on?]
        with  .logo-tile-label,  a.mini-chip.chip-svg[href],
        a.mini-chip.chip-png[href]
  - Brand colors live in  .palette > .swatch[data-hex]
      with  .swatch-role,  .swatch-name
  - Background bucket colors are defined in <style> as
      .bg-black / .bg-navy / .bg-red / .bg-grey / .bg-white { background: #hex; }
  - There's a <footer class="footer"> to insert before.
  - <h1 class="hero-title"> or <title> holds the team name.

If a page doesn't match this shape (different section names, missing
chips, etc.) it's skipped with a reason printed to the console — it
won't guess or partially inject.
"""

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency. Run: pip install beautifulsoup4 --break-system-packages")

WIDGET_MARKER = "cw-widget"  # used to detect an already-injected widget


def extract_bucket_colors(html_text):
    """Pull .bg-<name> { background: #hex; } rules from the page's <style>."""
    buckets = {}
    for name, hexval in re.findall(r'\.bg-(\w+)\s*\{\s*background:\s*(#[0-9a-fA-F]{3,6})', html_text):
        if name not in buckets:  # first definition wins
            buckets[name] = hexval.upper()
    return buckets


def clean_label(tile_label_el):
    if not tile_label_el:
        return "Untitled"
    return " ".join(tile_label_el.get_text(separator=" ", strip=True).split())


def slugify(text):
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')


def extract_logo_variants(soup):
    variants = []
    for group in soup.select('.logo-group'):
        category_raw = group.get('data-type', 'other')
        category = category_raw.capitalize()
        name_el = group.select_one('.logo-group-name')
        group_label = name_el.get_text(strip=True) if name_el else category

        for tile in group.select('.logo-tile'):
            orig_bg = (tile.get('data-orig-bg') or '').replace('bg-', '').strip()
            invisible_on = [
                v.strip() for v in (tile.get('data-invisible-on') or '').split(',') if v.strip()
            ]
            label = clean_label(tile.select_one('.logo-tile-label'))
            svg_el = tile.select_one('.mini-chip.chip-svg')
            png_el = tile.select_one('.mini-chip.chip-png')
            if not svg_el:
                continue  # can't render without at least an SVG source
            svg_href = svg_el.get('href')
            png_href = png_el.get('href') if png_el else svg_href
            is_full_color = 'full' in label.lower() and 'color' in label.lower()

            variants.append({
                'id': f"{slugify(category)}-{slugify(label)}",
                'category': category,
                'label': f"{group_label.split()[0][:4]} · {label}" if len(group_label) else label,
                'origBg': orig_bg or None,
                'invisibleOn': invisible_on,
                'svg': svg_href,
                'png': png_href,
                'isFullColor': is_full_color,
            })
    return variants


def extract_brand_colors(soup):
    colors = []
    for swatch in soup.select('.palette .swatch[data-hex]'):
        role_el = swatch.select_one('.swatch-role')
        name_el = swatch.select_one('.swatch-name')
        colors.append({
            'role': role_el.get_text(strip=True) if role_el else '',
            'name': name_el.get_text(strip=True) if name_el else '',
            'hex': swatch['data-hex'].upper(),
        })
    return colors


def extract_team_name(soup):
    hero = soup.select_one('.hero-title')
    if hero:
        return " ".join(hero.get_text(separator=' ', strip=True).split())
    if soup.title:
        return soup.title.get_text(strip=True).split('—')[0].strip()
    return "Team"


def next_section_number(html_text):
    nums = [int(n) for n in re.findall(r'<div class="section-number">(\d+)\s*—', html_text)]
    return (max(nums) + 1) if nums else 1


def get_max_viewbox_dim(variants, html_dir):
    """Sample SVG viewBox dimensions to find the largest dimension used by this team's logos."""
    max_dim = 0
    for v in variants:
        svg_rel = v.get('svg', '')
        if not svg_rel:
            continue
        try:
            svg_path = (html_dir / svg_rel).resolve()
            content = svg_path.read_text(encoding='utf-8', errors='ignore')[:500]
            m = re.search(r'viewBox="[0-9. ]* [0-9. ]* ([0-9.]+) ([0-9.]+)"', content)
            if m:
                w, h = float(m.group(1)), float(m.group(2))
                max_dim = max(max_dim, w, h)
        except Exception:
            pass
    return max_dim


def compute_logo_scale(variants, html_dir):
    """Return (width_pct, height_pct) for .cw-logo based on SVG viewBox size.
    PLL/Guard logos have large viewBoxes (800-1080) with internal whitespace.
    WLL compact logos have small viewBoxes (100-600) with tight artwork that fills the canvas.
    """
    max_dim = get_max_viewbox_dim(variants, html_dir)
    if max_dim > 800:
        return 50, 45   # PLL-scale: large viewBox, internal whitespace keeps visual size balanced
    elif max_dim > 350:
        return 40, 36   # WLL medium: 521-604 unit viewBoxes (Palms, Charm, Charging primary)
    else:
        return 33, 30   # very compact: < 350 unit viewBoxes


def build_widget_block(team_name, team_slug, section_number, logo_variants, bucket_colors, html_dir=None):
    variants_json = json.dumps(logo_variants, indent=6)
    buckets_json = json.dumps(
        [{'name': k, 'hex': v} for k, v in bucket_colors.items()], indent=6
    )
    section_num_str = f"{section_number:02d} —"

    # Logo display scale: derived from max SVG viewBox dimension
    logo_w, logo_h = compute_logo_scale(logo_variants, html_dir) if html_dir else (50, 45)

    # Init color: prefer navy (dark team color), then black, then first bucket
    preferred_dark = ['navy', 'black']
    init_hex = next((bucket_colors[n] for n in preferred_dark if n in bucket_colors),
                    next(iter(bucket_colors.values()), '#000000'))

    return f"""
    <!-- ═══ AUTO-INJECTED: Contrast Preview widget (inject_contrast_widget.py) ═══ -->
  <div class="content" style="padding-top:0;">
    <section class="section">
      <div class="section-header" data-target="contrast-body" style="cursor:pointer;user-select:none;">
        <div class="section-number">{section_num_str}</div>
        <h2 class="section-title">Contrast Preview</h2>
        <button class="section-toggle" aria-label="Collapse section">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M6 9l6 6 6-6"/></svg>
        </button>
      </div>

      <div class="section-body" id="contrast-body">
        <div class="cw-widget" id="cwWidget">
          <div class="cw-left">
            <div class="cw-stage" id="cwStage">
              <img class="cw-logo" id="cwLogo" alt="Logo contrast preview" />
              <div class="cw-variant-tag" id="cwVariantTag"></div>
            </div>
          </div>
          <div class="cw-right">
            <div class="cw-logo-grid" id="cwLogoGrid"></div>
            <div class="cw-downloads">
              <button class="cw-dl-btn" id="cwDownloadSvg">Download SVG</button>
              <button class="cw-dl-btn" id="cwDownloadPng">Download PNG</button>
            </div>
            <div class="cw-divider"></div>
            <div class="cw-picker-wrap" id="cwPickerWrap">
              <div class="cw-hsb-row">
                <label class="cw-hsb-letter">H</label>
                <input type="range" class="cw-slider cw-slider-h" id="cwHueSlider" min="0" max="360" value="0" />
                <span class="cw-hsb-value" id="cwHueValue">0°</span>
              </div>
              <div class="cw-hsb-row">
                <label class="cw-hsb-letter">S</label>
                <input type="range" class="cw-slider cw-slider-s" id="cwSatSlider" min="0" max="100" value="0" />
                <span class="cw-hsb-value" id="cwSatValue">0%</span>
              </div>
              <div class="cw-hsb-row">
                <label class="cw-hsb-letter">B</label>
                <input type="range" class="cw-slider cw-slider-b" id="cwBriSlider" min="0" max="100" value="100" />
                <span class="cw-hsb-value" id="cwBriValue">100%</span>
              </div>
              <div class="cw-hex-row">
                <span class="cw-hex-prefix">#</span>
                <input class="cw-hex-input" id="cwHexInput" maxlength="6" spellcheck="false" />
              </div>
              <div class="cw-swatch-row" id="cwSwatchRow"></div>
            </div>
          </div>
        </div>
      </div>
    </section>
  </div>

    <style>
      .cw-widget {{ display: flex; gap: 2.5rem; align-items: flex-start; }}
      .cw-left {{ flex: 0 0 58%; }}
      .cw-right {{ flex: 1; min-width: 0; display: flex; flex-direction: column; }}
      .cw-stage {{ position: relative; width: 100%; aspect-ratio: 16 / 9; max-height: 360px; border-radius: 6px; border: 1px solid var(--rule); display: flex; align-items: center; justify-content: center; transition: background-color 0.2s ease; padding: 2rem; overflow: hidden; box-sizing: border-box; }}
      .cw-logo {{ width: {logo_w}%; height: {logo_h}%; object-fit: contain; display: block; flex-shrink: 0; }}
      .cw-variant-tag {{ position: absolute; bottom: 0.6rem; right: 0.7rem; font-family: 'JetBrains Mono', monospace; font-size: 0.6rem; letter-spacing: 0.1em; text-transform: uppercase; background: rgba(0,0,0,0.55); color: #fff; padding: 0.25rem 0.55rem; border-radius: 2px; }}
      .cw-logo-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.4rem; margin-bottom: 1rem; max-height: 300px; overflow-y: auto; padding-right: 2px; }}
      .cw-logo-grid::-webkit-scrollbar {{ width: 4px; }} .cw-logo-grid::-webkit-scrollbar-track {{ background: transparent; }} .cw-logo-grid::-webkit-scrollbar-thumb {{ background: var(--rule); border-radius: 2px; }}
      .cw-tile {{ position: relative; border: 2px solid var(--rule); border-radius: 4px; cursor: pointer; overflow: hidden; aspect-ratio: 4 / 3; display: flex; align-items: center; justify-content: center; padding: 0.6rem; transition: border-color 0.15s ease, transform 0.15s ease; }}
      .cw-tile:hover {{ border-color: var(--gray-dim); transform: translateY(-2px); }}
      .cw-tile.active {{ border-color: #ffcb06; }}
      .cw-tile.full-color {{ outline: 1px dashed var(--rule); outline-offset: -3px; }}
      .cw-tile img {{ max-width: 90%; max-height: 80%; object-fit: contain; display: block; }}
      .cw-tile-label {{ position: absolute; bottom: 0; left: 0; right: 0; font-family: 'JetBrains Mono', monospace; font-size: 0.42rem; letter-spacing: 0.08em; text-transform: uppercase; text-align: center; padding: 0.2rem 0.3rem; background: rgba(0,0,0,0.65); color: #fff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
      .cw-downloads {{ display: flex; gap: 0.5rem; margin-bottom: 1.4rem; }}
      .cw-dl-btn {{ flex: 1; background: none; border: 1px solid var(--rule); color: var(--white); font-family: 'JetBrains Mono', monospace; font-size: 0.65rem; letter-spacing: 0.15em; text-transform: uppercase; padding: 0.65rem; border-radius: 2px; cursor: pointer; transition: all 0.15s ease; }}
      .cw-dl-btn:hover {{ background: var(--team-primary); color: var(--black); border-color: var(--team-primary); }}
      .cw-divider {{ border-top: 1px solid var(--rule); margin-bottom: 1.4rem; }}
      .cw-picker-wrap {{ transition: opacity 0.2s ease; }}
      .cw-picker-wrap.disabled {{ opacity: 0.25; pointer-events: none; }}
      .cw-picker-wrap.disabled::before {{ content: 'Background picker only applies to 1-color logos'; display: block; font-family: 'JetBrains Mono', monospace; font-size: 0.6rem; letter-spacing: 0.1em; text-transform: uppercase; color: var(--gray-dim); margin-bottom: 0.75rem; opacity: 1; }}
      .cw-hsb-row {{ display: flex; align-items: center; gap: 0.85rem; margin-bottom: 1.1rem; }}
      .cw-hsb-letter {{ width: 14px; font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; color: var(--gray-dim); flex-shrink: 0; }}
      .cw-hsb-value {{ width: 46px; text-align: right; font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; color: var(--white); flex-shrink: 0; }}
      .cw-slider {{ flex: 1; -webkit-appearance: none; appearance: none; height: 4px; border-radius: 2px; outline: none; cursor: pointer; }}
      .cw-slider::-webkit-slider-thumb {{ -webkit-appearance: none; appearance: none; width: 18px; height: 18px; border-radius: 50%; background: var(--cw-thumb, #6b6b6b); border: 2px solid #fff; box-shadow: 0 0 0 1px rgba(0,0,0,0.5); cursor: pointer; }}
      .cw-slider::-moz-range-thumb {{ width: 18px; height: 18px; border-radius: 50%; background: var(--cw-thumb, #6b6b6b); border: 2px solid #fff; box-shadow: 0 0 0 1px rgba(0,0,0,0.5); cursor: pointer; }}
      .cw-slider-h {{ background: linear-gradient(to right, #ff0000, #ffff00, #00ff00, #00ffff, #0000ff, #ff00ff, #ff0000); }}
      .cw-hex-row {{ display: flex; align-items: center; gap: 0.5rem; margin: 0.4rem 0 1.4rem; background: var(--ink); border: 1px solid var(--rule); border-radius: 3px; padding: 0.7rem 0.8rem; }}
      .cw-hex-prefix {{ font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; color: var(--gray-dim); }}
      .cw-hex-input {{ flex: 1; background: none; border: none; color: var(--white); font-family: 'JetBrains Mono', monospace; font-size: 0.75rem; letter-spacing: 0.04em; outline: none; }}
      .cw-swatch-row {{ display: flex; gap: 0.5rem; }}
      .cw-swatch {{ width: 32px; height: 32px; border-radius: 3px; border: 2px solid transparent; cursor: pointer; flex-shrink: 0; }}
      .cw-swatch.active {{ border-color: #ffcb06; }}
      @media (max-width: 768px) {{ .cw-widget {{ flex-direction: column; }} .cw-left {{ flex: none; width: 100%; }} }}
    </style>

    <script>
    (function () {{
      const LOGO_VARIANTS = {variants_json};
      const BUCKETS = {buckets_json};
      const BUCKET_HEX = Object.fromEntries(BUCKETS.map(b => [b.name, b.hex]));
      const TEAM_SLUG = {json.dumps(team_slug)};

      const el = {{
        stage: document.getElementById('cwStage'), logo: document.getElementById('cwLogo'),
        tag: document.getElementById('cwVariantTag'), logoGrid: document.getElementById('cwLogoGrid'),
        dlSvg: document.getElementById('cwDownloadSvg'), dlPng: document.getElementById('cwDownloadPng'),
        hueSlider: document.getElementById('cwHueSlider'), satSlider: document.getElementById('cwSatSlider'),
        briSlider: document.getElementById('cwBriSlider'), hueValue: document.getElementById('cwHueValue'),
        satValue: document.getElementById('cwSatValue'), briValue: document.getElementById('cwBriValue'),
        hexInput: document.getElementById('cwHexInput'), swatchRow: document.getElementById('cwSwatchRow'),
        pickerWrap: document.getElementById('cwPickerWrap')
      }};

      let hsb = {{ h: 0, s: 0, b: 100 }};
      let lockedVariant = null;   // set only for full-color tiles
      let activeCategory = null;  // constrains auto-pick to one logo group

      function hsbToRgb(h, s, b) {{
        s /= 100; b /= 100;
        const k = n => (n + h / 60) % 6;
        const f = n => b - b * s * Math.max(0, Math.min(k(n), 4 - k(n), 1));
        return {{ r: Math.round(f(5) * 255), g: Math.round(f(3) * 255), b: Math.round(f(1) * 255) }};
      }}
      function rgbToHex({{ r, g, b }}) {{ return [r, g, b].map(v => v.toString(16).padStart(2, '0')).join('').toUpperCase(); }}
      function hexToRgb(hex) {{
        hex = hex.replace('#', ''); if (hex.length === 3) hex = hex.split('').map(c => c + c).join('');
        const num = parseInt(hex, 16); return {{ r: (num >> 16) & 255, g: (num >> 8) & 255, b: num & 255 }};
      }}
      function rgbToHsb({{ r, g, b }}) {{
        r /= 255; g /= 255; b /= 255;
        const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
        let h = 0;
        if (d !== 0) {{
          if (max === r) h = 60 * (((g - b) / d) % 6);
          else if (max === g) h = 60 * ((b - r) / d + 2);
          else h = 60 * ((r - g) / d + 4);
        }}
        if (h < 0) h += 360;
        return {{ h, s: max === 0 ? 0 : (d / max) * 100, b: max * 100 }};
      }}
      function currentHex() {{ return '#' + rgbToHex(hsbToRgb(hsb.h, hsb.s, hsb.b)); }}

      // Perceived luminance of a hex color (0=black, 1=white)
      function luminance(hex) {{
        const {{ r, g, b }} = hexToRgb(hex);
        return (0.299 * r + 0.587 * g + 0.114 * b) / 255;
      }}

      // Determine whether a variant is intended for dark vs light backgrounds
      // using its label text — far more reliable than origBg luminance since
      // some teams use unexpected colors (e.g. Redwoods yellow as a "dark" bg).
      function isForDarkBg(v) {{
        const lbl = (v.label || '').toLowerCase();
        if (lbl.includes('light')) return false;
        if (lbl.includes('dark'))  return true;
        if (lbl.includes('white')) return false;
        // Named-bg indigenous variants (e.g. "Black BKG", "Orange BKG"):
        // fall back to origBg luminance
        const hex = BUCKET_HEX[v.origBg];
        return hex ? luminance(hex) < 0.5 : true;
      }}

      // Picks the right 1-color variant from background luminance,
      // always constrained to activeCategory so the logo group never changes.
      function bestVariant(bgHex, category) {{
        const bgDark = luminance(bgHex) < 0.5;
        let pool = LOGO_VARIANTS.filter(v => !v.isFullColor && (!category || v.category === category));
        if (pool.length === 0) pool = LOGO_VARIANTS.filter(v => !v.isFullColor);
        if (pool.length === 0) pool = LOGO_VARIANTS;
        const preferred = pool.filter(v => isForDarkBg(v) === bgDark);
        return (preferred.length > 0 ? preferred : pool)[0];
      }}

      function renderLogoGrid() {{
        el.logoGrid.innerHTML = LOGO_VARIANTS.map(v => {{
          const tileBg = BUCKET_HEX[v.origBg] || '#000000';
          const shortLabel = v.label.includes('·') ? v.label.split('· ')[1] : v.label;
          return `<div class="cw-tile${{v.isFullColor ? ' full-color' : ''}}" data-variant="${{v.id}}" style="background:${{tileBg}};" title="${{v.label}}">
            <img src="${{v.svg}}" alt="${{shortLabel}}" loading="lazy" />
            <span class="cw-tile-label">${{shortLabel}}</span>
          </div>`;
        }}).join('');
        [...el.logoGrid.children].forEach(tile => {{
          tile.addEventListener('click', () => {{
            const clicked = LOGO_VARIANTS.find(v => v.id === tile.dataset.variant);
            activeCategory = clicked.category;
            lockedVariant = clicked.isFullColor ? clicked.id : null;
            update();
          }});
        }});
      }}

      function renderSwatches() {{
        el.swatchRow.innerHTML = BUCKETS.map(b =>
          `<div class="cw-swatch" data-hex="${{b.hex}}" style="background:${{b.hex}};${{b.name === 'white' ? 'border:1px solid var(--rule);' : ''}}" title="${{b.name}}"></div>`
        ).join('');
        [...el.swatchRow.children].forEach(sw => {{
          sw.addEventListener('click', () => setFromHex(sw.dataset.hex));
        }});
      }}

      function setFromHex(hex) {{ hsb = rgbToHsb(hexToRgb(hex)); lockedVariant = null; update(); }}

      function updateSliderPositions() {{
        el.hueSlider.value = hsb.h; el.satSlider.value = hsb.s; el.briSlider.value = hsb.b;
        el.hueValue.textContent = Math.round(hsb.h) + '°';
        el.satValue.textContent = Math.round(hsb.s) + '%';
        el.briValue.textContent = Math.round(hsb.b) + '%';
        const fullSat = '#' + rgbToHex(hsbToRgb(hsb.h, 100, hsb.b));
        const noSat = '#' + rgbToHex(hsbToRgb(hsb.h, 0, hsb.b));
        el.satSlider.style.background = `linear-gradient(to right, ${{noSat}}, ${{fullSat}})`;
        const fullBri = '#' + rgbToHex(hsbToRgb(hsb.h, hsb.s, 100));
        el.briSlider.style.background = `linear-gradient(to right, #000000, ${{fullBri}})`;
        const thumbHex = currentHex();
        [el.hueSlider, el.satSlider, el.briSlider].forEach(s => s.style.setProperty('--cw-thumb', thumbHex));
      }}

      function update() {{
        const pickerHex = currentHex();
        const autoPick = bestVariant(pickerHex, activeCategory);
        const active = (lockedVariant ? LOGO_VARIANTS.find(v => v.id === lockedVariant) : null) || autoPick;
        const isFullColor = active.isFullColor;

        // Full-color logos always show on their own designed-for background
        const displayBg = isFullColor
          ? (BUCKET_HEX[active.origBg] || '#000000')
          : pickerHex;

        el.stage.style.backgroundColor = displayBg;
        el.hexInput.value = pickerHex.replace('#', '');
        updateSliderPositions();
        el.pickerWrap.classList.toggle('disabled', isFullColor);

        el.logo.src = active.svg;
        el.tag.textContent = `${{active.category}} · ${{active.label.includes('·') ? active.label.split('· ')[1] : active.label}}`;
        [...el.logoGrid.children].forEach(tile => {{
          tile.classList.toggle('active', tile.dataset.variant === active.id);
        }});
        [...el.swatchRow.children].forEach(sw => {{
          sw.classList.toggle('active', sw.dataset.hex.toLowerCase() === pickerHex.toLowerCase());
        }});
        el.dlSvg.onclick = () => downloadAsset(active.svg, active.id, 'svg');
        el.dlPng.onclick = () => downloadAsset(active.png, active.id, 'png');
      }}

      function downloadAsset(url, id, ext) {{
        const a = document.createElement('a');
        a.href = url; a.download = `${{TEAM_SLUG}}-${{id}}.${{ext}}`;
        document.body.appendChild(a); a.click(); a.remove();
      }}

      el.hueSlider.addEventListener('input', () => {{ hsb.h = Number(el.hueSlider.value); lockedVariant = null; update(); }});
      el.satSlider.addEventListener('input', () => {{ hsb.s = Number(el.satSlider.value); lockedVariant = null; update(); }});
      el.briSlider.addEventListener('input', () => {{ hsb.b = Number(el.briSlider.value); lockedVariant = null; update(); }});
      el.hexInput.addEventListener('input', () => {{
        const val = el.hexInput.value.replace(/[^0-9a-fA-F]/g, '').slice(0, 6);
        el.hexInput.value = val;
        if (val.length === 6) setFromHex('#' + val);
      }});

      renderLogoGrid();
      renderSwatches();
      hsb = rgbToHsb(hexToRgb({json.dumps(init_hex)}));
      // Default to Primary category; fall back to whatever auto-picks first
      const _primaryVariant = LOGO_VARIANTS.find(v => !v.isFullColor && v.category === 'Primary');
      activeCategory = _primaryVariant ? 'Primary' : (bestVariant(currentHex(), null) || LOGO_VARIANTS[0]).category;
      update();
    }})();
    </script>
    <!-- ═══ /AUTO-INJECTED ═══ -->
"""


def process_file(path: Path, args):
    html_text = path.read_text(encoding='utf-8')

    if WIDGET_MARKER in html_text:
        if not args.force:
            return 'skipped', 'already has widget (use --force to re-inject)'
        # Strip the old widget entirely before re-injecting
        html_text = re.sub(
            r'<!--\s*═+\s*AUTO-INJECTED.*?/AUTO-INJECTED[^\n]*-->',
            '',
            html_text,
            flags=re.DOTALL,
        ).strip()

    if '<footer class="footer">' not in html_text:
        return 'skipped', 'no <footer class="footer"> found to insert before'

    soup = BeautifulSoup(html_text, 'html.parser')

    logo_variants = extract_logo_variants(soup)
    if not logo_variants:
        return 'skipped', 'no .logo-tile entries found (check .logo-group / .logo-tile markup)'

    bucket_colors = extract_bucket_colors(html_text)
    if not bucket_colors:
        return 'skipped', 'no .bg-* bucket colors found in <style>'

    team_name = extract_team_name(soup)
    team_slug = path.stem
    section_number = next_section_number(html_text)

    widget_html = build_widget_block(team_name, team_slug, section_number, logo_variants, bucket_colors, html_dir=path.parent)

    new_html = html_text.replace('<footer class="footer">', widget_html + '\n  <footer class="footer">', 1)

    if args.dry_run:
        return 'would-inject', f'{len(logo_variants)} variants, {len(bucket_colors)} buckets, team="{team_name}"'

    if args.output_dir:

        out_path = Path(args.output_dir) / path.name
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(new_html, encoding='utf-8')
    else:
        if not args.no_backup:
            shutil.copy2(path, path.with_suffix(path.suffix + '.bak'))
        path.write_text(new_html, encoding='utf-8')

    return 'injected', f'{len(logo_variants)} variants, {len(bucket_colors)} buckets, team="{team_name}"'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('folder', help='Folder containing team .html pages')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--output-dir', default=None)
    parser.add_argument('--no-backup', action='store_true')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()

    folder = Path(args.folder)
    html_files = sorted(folder.glob('*.html'))
    if not html_files:
        sys.exit(f'No .html files found in {folder}')

    print(f'Found {len(html_files)} HTML file(s) in {folder}\n')

    results = {'injected': 0, 'would-inject': 0, 'skipped': 0}
    for path in html_files:
        status, detail = process_file(path, args)
        results[status] = results.get(status, 0) + 1
        marker = {'injected': '✓', 'would-inject': '→', 'skipped': '·'}[status]
        print(f'  {marker} {path.name:40s} {detail}')

    print(f"\nDone. {results.get('injected',0)} injected, "
          f"{results.get('would-inject',0)} would-inject, "
          f"{results.get('skipped',0)} skipped.")


if __name__ == '__main__':
    main()
