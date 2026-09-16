# -*- coding: utf-8 -*-
"""
ЭЗЦ-монитор v1 — сканер новостей/публикаций/каналов по теме
"экономика замкнутого цикла" (циркулярная экономика).

Что делает:
  1. Читает список источников из sources.json (RSS, Telegram-каналы, Google News по ключевым словам).
  2. Скачивает свежие материалы из каждого источника.
  3. Оценивает релевантность каждого материала по SIGNALS (см. ниже) —
     технологии / рейтинги-показатели / инвестиции / отрасли / география / регулирование.
  4. Собирает результат в один самодостаточный index.html — дашборд для GitHub Pages.

ВАЖНО (v1, черновая калибровка):
  Веса сигналов и пороги приоритетов — стартовые значения для проверки архитектуры.
  Точную настройку (что считать "топом", какие отрасли/регионы приоритетны) — донастраиваем
  отдельно вместе, по образцу того, как калибровали ved_parser_v3.

Запуск: двойной клик по run_parser.bat (Windows), либо `python ezc_parser_v1.py`.
Публикация: index.html — это корневая страница для GitHub Pages (см. README.md).
"""

import os
import re
import sys
import json
import html
import hashlib
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

try:
    import requests
except ImportError:
    print("Не найден модуль 'requests'. Установите зависимости: pip install -r requirements.txt")
    sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("Не найден модуль 'beautifulsoup4'. Установите зависимости: pip install -r requirements.txt")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_PATH = os.path.join(SCRIPT_DIR, "sources.json")
OUTPUT_HTML_PATH = os.path.join(SCRIPT_DIR, "index.html")

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REQUEST_TIMEOUT = 20

# ---------------------------------------------------------------------------
# SIGNALS — сигналы релевантности. Веса и self_sufficient — стартовая калибровка v1.
#   self_sufficient=True  -> одного попадания достаточно, чтобы материал не считался шумом
#   self_sufficient=False -> нужен ещё хотя бы один сигнал в паре (правило перекрёстного
#                             подтверждения, как в ved_parser_v3)
# ---------------------------------------------------------------------------
SIGNALS = [
    {"id": "circular_core", "label": "Прямая терминология ЭЗЦ", "weight": 20, "self_sufficient": True,
     "keywords": ["экономика замкнутого цикла", "циркулярная экономика", "circular economy",
                  "эзц", "замкнутый цикл", "циркулярность", "циркулярной экономики"]},

    {"id": "rating_metric", "label": "Рейтинг / показатели / индекс", "weight": 18, "self_sufficient": True,
     "keywords": ["рейтинг", "индекс циркулярности", "методика оценки", "circularity gap",
                  "эзц+", "интегральный рейтинг", "ранжирование регионов", "рэнкинг"]},

    {"id": "investment", "label": "Инвестиции / финансирование", "weight": 16, "self_sufficient": False,
     "keywords": ["инвестици", "зеленое финансирование", "зелёное финансирование", "esg-облигации",
                  "green bond", "финансирование проекта", "вложения в переработку", "грант",
                  "льготное финансирование"]},

    {"id": "recycling_tech", "label": "Технологии переработки", "weight": 15, "self_sufficient": False,
     "keywords": ["технология переработки", "переработка отходов", "вторсырье", "вторсырьё",
                  "вторичное сырье", "вторичное сырьё", "recycling technology", "recycling plant",
                  "мусоропереработка", "глубокая переработка", "рециклинг"]},

    {"id": "waste_management", "label": "Обращение с отходами / ТКО", "weight": 10, "self_sufficient": False,
     "keywords": ["тко", "обращение с отходами", "полигон", "твердые коммунальные отходы",
                  "твёрдые коммунальные отходы", "мусоросортировка", "раздельный сбор"]},

    {"id": "regulation", "label": "Регулирование / федеральный проект", "weight": 14, "self_sufficient": True,
     "keywords": ["федеральный проект", "нормативно-правов", "указ президента",
                  "постановление правительства", "рэо", "закон об эзц", "реформа отрасли"]},

    {"id": "industry_sector", "label": "Отрасли (металлургия, химия, упаковка и др.)", "weight": 12,
     "self_sufficient": False,
     "keywords": ["металлург", "химическая промышленность", "упаковка", "текстиль",
                  "строительные отходы", "агропром", "электроника", "батаре", "аккумулятор"]},

    {"id": "geography_global", "label": "Международный контекст / география", "weight": 8,
     "self_sufficient": False,
     "keywords": ["евросоюз", "eu circular economy", "global south", "глобальный юг", "атр",
                  "азия", "китай", "ближний восток"]},

    {"id": "corporate_case", "label": "Корпоративный кейс внедрения", "weight": 9, "self_sufficient": False,
     "keywords": ["запустил завод по переработке", "внедрила экономику замкнутого цикла",
                  "приобрела оборудование для переработки", "открыла линию переработки",
                  "построил завод по утилизации"]},
]
MAX_POSSIBLE = sum(sig["weight"] for sig in SIGNALS)

# ---------------------------------------------------------------------------
# Приоритеты (упрощённая 3-уровневая шкала для v1 — детализацию по образцу
# 8-уровневой П1..П8 из ved_parser можно добавить позже, если понадобится).
# ---------------------------------------------------------------------------
PRIORITY_LEVELS = [
    {"rank": 1, "min_pct": 45, "label": "ТОП", "color": "#1b5e3a"},
    {"rank": 2, "min_pct": 28, "label": "ВАЖНОЕ", "color": "#4d7c2f"},
    {"rank": 3, "min_pct": 16, "label": "ОБЩИЙ ФОН", "color": "#8a7a1e"},
]
SKIP_RANK = 4
SKIP_LABEL = "ШУМ"
SKIP_COLOR = "#8a8780"


def get_priority(pct):
    for lvl in PRIORITY_LEVELS:
        if pct >= lvl["min_pct"]:
            return {"label": lvl["label"], "color": lvl["color"], "rank": lvl["rank"]}
    return {"label": SKIP_LABEL, "color": SKIP_COLOR, "rank": SKIP_RANK}


def score_item(item):
    text = (item.get("title", "") + " " + item.get("summary", "")).lower()
    total = 0
    hits = []
    hit_ids = []
    for sig in SIGNALS:
        if any(kw in text for kw in sig["keywords"]):
            hits.append(sig["label"])
            hit_ids.append(sig["id"])
            total += sig["weight"]
    pct = round((total / MAX_POSSIBLE) * 100) if MAX_POSSIBLE else 0
    priority = get_priority(pct)

    hit_sigs = [s for s in SIGNALS if s["id"] in hit_ids]
    has_strong_hit = any(s.get("self_sufficient") for s in hit_sigs)
    if not has_strong_hit and len(hit_ids) < 2:
        priority = {"label": SKIP_LABEL, "color": SKIP_COLOR, "rank": SKIP_RANK}

    return {"pct": pct, "priority": priority, "hits": hits}


# ---------------------------------------------------------------------------
# Загрузка источников
# ---------------------------------------------------------------------------
def load_sources():
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_url(url):
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def strip_html(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_pub_date(raw):
    if not raw:
        return None
    raw = raw.strip()
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def make_item_id(link, title):
    key = (link or "") + "|" + (title or "")
    return hashlib.sha1(key.encode("utf-8", errors="ignore")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# RSS / Atom
# ---------------------------------------------------------------------------
def parse_rss_bytes(raw_bytes, source_id, source_label, source_lang):
    items = []
    try:
        root = ET.fromstring(raw_bytes)
    except ET.ParseError:
        return items

    ns_atom = "{http://www.w3.org/2005/Atom}"

    channel_items = root.findall("./channel/item")
    if channel_items:
        for it in channel_items:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            pub_raw = it.findtext("pubDate") or it.findtext("{http://purl.org/dc/elements/1.1/}date")
            desc = it.findtext("description") or ""
            summary = strip_html(desc)
            pub_dt = parse_pub_date(pub_raw)
            items.append({
                "id": make_item_id(link, title),
                "title": strip_html(title),
                "link": link,
                "summary": summary,
                "pub_dt": pub_dt,
                "source_id": source_id,
                "source_label": source_label,
                "source_lang": source_lang,
            })
        return items

    entries = root.findall(ns_atom + "entry")
    for it in entries:
        title = (it.findtext(ns_atom + "title") or "").strip()
        link_el = it.find(ns_atom + "link")
        link = link_el.get("href") if link_el is not None else ""
        pub_raw = it.findtext(ns_atom + "updated") or it.findtext(ns_atom + "published")
        summary_raw = it.findtext(ns_atom + "summary") or it.findtext(ns_atom + "content") or ""
        pub_dt = parse_pub_date(pub_raw)
        items.append({
            "id": make_item_id(link, title),
            "title": strip_html(title),
            "link": link,
            "summary": strip_html(summary_raw),
            "pub_dt": pub_dt,
            "source_id": source_id,
            "source_label": source_label,
            "source_lang": source_lang,
        })
    return items


def fetch_rss_source(src):
    raw = fetch_url(src["url"])
    return parse_rss_bytes(raw, src["id"], src["label"], src.get("lang", "ru"))


# ---------------------------------------------------------------------------
# Google News (RSS по ключевым словам, без API-ключа)
# ---------------------------------------------------------------------------
def build_google_news_url(query, lang):
    if lang == "en":
        hl, gl, ceid = "en-US", "US", "US:en"
    else:
        hl, gl, ceid = "ru", "RU", "RU:ru"
    params = {"q": query, "hl": hl, "gl": gl, "ceid": ceid}
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(params)


def fetch_google_news_source(gn):
    url = build_google_news_url(gn["query"], gn.get("lang", "ru"))
    raw = fetch_url(url)
    return parse_rss_bytes(raw, gn["id"], gn["label"], gn.get("lang", "ru"))


# ---------------------------------------------------------------------------
# Telegram (публичный веб-превью t.me/s/<канал>, без бот-токена)
# ---------------------------------------------------------------------------
def fetch_telegram_channel(tg):
    url = "https://t.me/s/" + tg["username"]
    raw = fetch_url(url)
    soup = BeautifulSoup(raw, "html.parser")
    items = []
    for msg in soup.select(".tgme_widget_message"):
        text_el = msg.select_one(".tgme_widget_message_text")
        text = text_el.get_text(" ", strip=True) if text_el else ""
        if not text:
            continue
        link_el = msg.select_one("a.tgme_widget_message_date")
        link = link_el.get("href") if link_el else ("https://t.me/" + tg["username"])
        time_el = msg.select_one("time")
        pub_raw = time_el.get("datetime") if time_el else None
        pub_dt = parse_pub_date(pub_raw)
        title = text[:140]
        items.append({
            "id": make_item_id(link, text[:80]),
            "title": title,
            "link": link,
            "summary": text,
            "pub_dt": pub_dt,
            "source_id": tg["id"],
            "source_label": tg["label"],
            "source_lang": "ru",
        })
    return items


# ---------------------------------------------------------------------------
# Сбор всех источников
# ---------------------------------------------------------------------------
def normalize_link(link):
    if not link:
        return ""
    parsed = urllib.parse.urlsplit(link)
    query_pairs = [
        (k, v) for k, v in urllib.parse.parse_qsl(parsed.query)
        if not k.lower().startswith("utm_") and k.lower() not in ("ref", "ref_src", "fbclid")
    ]
    clean_query = urllib.parse.urlencode(query_pairs)
    clean = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, clean_query, ""))
    return clean.rstrip("/").lower()


def collect_all():
    sources = load_sources()
    all_items = {}
    source_status = []

    for src in sources.get("rss", []):
        if not src.get("enabled", True):
            continue
        try:
            fetched = fetch_rss_source(src)
            source_status.append({"label": src["label"], "ok": True, "count": len(fetched)})
        except Exception as e:
            source_status.append({"label": src["label"], "ok": False, "error": str(e)})
            continue
        for it in fetched:
            key = normalize_link(it["link"]) or it["id"]
            if key not in all_items:
                all_items[key] = it

    for tg in sources.get("telegram", []):
        if not tg.get("enabled", True):
            continue
        try:
            fetched = fetch_telegram_channel(tg)
            source_status.append({"label": tg["label"], "ok": True, "count": len(fetched)})
        except Exception as e:
            source_status.append({"label": tg["label"], "ok": False, "error": str(e)})
            continue
        for it in fetched:
            key = normalize_link(it["link"]) or it["id"]
            if key not in all_items:
                all_items[key] = it

    for gn in sources.get("google_news", []):
        if not gn.get("enabled", True):
            continue
        try:
            fetched = fetch_google_news_source(gn)
            source_status.append({"label": gn["label"], "ok": True, "count": len(fetched)})
        except Exception as e:
            source_status.append({"label": gn["label"], "ok": False, "error": str(e)})
            continue
        for it in fetched:
            key = normalize_link(it["link"]) or it["id"]
            if key not in all_items:
                all_items[key] = it

    scored_items = []
    for it in all_items.values():
        result = score_item(it)
        it["pct"] = result["pct"]
        it["priority"] = result["priority"]
        it["hits"] = result["hits"]
        scored_items.append(it)

    def sort_key(it):
        dt = it["pub_dt"] or datetime(1970, 1, 1, tzinfo=timezone.utc)
        return (it["priority"]["rank"], -it["pct"], -dt.timestamp())

    scored_items.sort(key=sort_key)
    return scored_items, source_status


# ---------------------------------------------------------------------------
# HTML-дашборд
# ---------------------------------------------------------------------------
def esc(text):
    return html.escape(text or "", quote=True)


def format_dt(dt):
    if not dt:
        return "—"
    try:
        local = dt.astimezone()
    except (ValueError, OSError):
        local = dt
    return local.strftime("%d.%m.%Y %H:%M")


def item_row(it):
    color = it["priority"]["color"]
    title_html = esc(it["title"]) if it["title"] else "(без заголовка)"
    link = esc(it["link"])
    summary = esc(it["summary"][:220] + ("…" if len(it["summary"]) > 220 else ""))
    hits_html = "".join(
        '<span class="tag">' + esc(h) + "</span>" for h in it["hits"]
    ) or '<span class="tag tag-none">нет совпадений</span>'
    row = (
        '<tr>'
        '<td class="tdate">' + esc(format_dt(it["pub_dt"])) + '</td>'
        '<td class="tsource">' + esc(it["source_label"]) + '</td>'
        '<td class="ttitle">'
        '<a href="' + link + '" target="_blank" rel="noopener">' + title_html + '</a>'
        '<div class="summary">' + summary + '</div>'
        '<div class="tags">' + hits_html + '</div>'
        '</td>'
        '<td class="tscore" style="color:' + color + '">'
        + str(it["pct"]) + '%<br>'
        '<span class="pill" style="border-color:' + color + ';color:' + color + '">'
        + esc(it["priority"]["label"]) + '</span>'
        '</td>'
        '</tr>'
    )
    return row


def source_status_row(s):
    if s["ok"]:
        return ('<tr><td>' + esc(s["label"]) + '</td>'
                '<td class="ok">OK</td><td>' + str(s["count"]) + '</td></tr>')
    return ('<tr><td>' + esc(s["label"]) + '</td>'
            '<td class="fail">ОШИБКА</td><td>' + esc(s.get("error", ""))[:80] + '</td></tr>')


def build_html(items, source_status, collected_at):
    priority_groups = {lvl["rank"]: [] for lvl in PRIORITY_LEVELS}
    skip_group = []
    for it in items:
        rank = it["priority"]["rank"]
        if rank == SKIP_RANK:
            skip_group.append(it)
        else:
            priority_groups.setdefault(rank, []).append(it)

    kpi_tiles = []
    kpi_tiles.append('<div class="kpi"><div class="kpi-num">' + str(len(items)) + '</div>'
                      '<div class="kpi-label">Всего материалов</div></div>')
    for lvl in PRIORITY_LEVELS:
        cnt = len(priority_groups.get(lvl["rank"], []))
        kpi_tiles.append(
            '<div class="kpi"><div class="kpi-num" style="color:' + lvl["color"] + '">'
            + str(cnt) + '</div><div class="kpi-label">' + esc(lvl["label"]) + '</div></div>'
        )
    kpi_tiles.append('<div class="kpi"><div class="kpi-num" style="color:' + SKIP_COLOR + '">'
                      + str(len(skip_group)) + '</div><div class="kpi-label">Шум (скрыто из топа)</div></div>')
    ok_sources = sum(1 for s in source_status if s["ok"])
    kpi_tiles.append('<div class="kpi"><div class="kpi-num">' + str(ok_sources) + '/' + str(len(source_status))
                      + '</div><div class="kpi-label">Источники OK</div></div>')
    kpi_html = "".join(kpi_tiles)

    tabs_html = ['<button class="tab active" data-tab="tab-all">Все (' + str(len(items)) + ')</button>']
    tab_contents = []

    all_rows = "".join(item_row(it) for it in items) or '<tr><td colspan="4" class="empty">Пусто</td></tr>'
    tab_contents.append(
        '<div class="tab-panel active" id="tab-all"><table class="tbl">'
        '<thead><tr><th>Дата</th><th>Источник</th><th>Материал</th><th>Оценка</th></tr></thead>'
        '<tbody>' + all_rows + '</tbody></table></div>'
    )

    for lvl in PRIORITY_LEVELS:
        rank = lvl["rank"]
        group = priority_groups.get(rank, [])
        tab_id = "tab-p" + str(rank)
        tabs_html.append('<button class="tab" data-tab="' + tab_id + '">' + esc(lvl["label"])
                          + ' (' + str(len(group)) + ')</button>')
        rows = "".join(item_row(it) for it in group) or '<tr><td colspan="4" class="empty">Пусто</td></tr>'
        tab_contents.append(
            '<div class="tab-panel" id="' + tab_id + '"><table class="tbl">'
            '<thead><tr><th>Дата</th><th>Источник</th><th>Материал</th><th>Оценка</th></tr></thead>'
            '<tbody>' + rows + '</tbody></table></div>'
        )

    tabs_html.append('<button class="tab" data-tab="tab-skip">' + SKIP_LABEL + ' (' + str(len(skip_group)) + ')</button>')
    skip_rows = "".join(item_row(it) for it in skip_group) or '<tr><td colspan="4" class="empty">Пусто</td></tr>'
    tab_contents.append(
        '<div class="tab-panel" id="tab-skip"><table class="tbl">'
        '<thead><tr><th>Дата</th><th>Источник</th><th>Материал</th><th>Оценка</th></tr></thead>'
        '<tbody>' + skip_rows + '</tbody></table></div>'
    )

    tabs_html.append('<button class="tab" data-tab="tab-sources">Источники</button>')
    src_rows = "".join(source_status_row(s) for s in source_status)
    tab_contents.append(
        '<div class="tab-panel" id="tab-sources"><table class="tbl">'
        '<thead><tr><th>Источник</th><th>Статус</th><th>Материалов / ошибка</th></tr></thead>'
        '<tbody>' + src_rows + '</tbody></table></div>'
    )

    now_str = collected_at.strftime("%d.%m.%Y %H:%M")

    page = TEMPLATE.replace("__KPI__", kpi_html)
    page = page.replace("__TABS__", "".join(tabs_html))
    page = page.replace("__TAB_CONTENTS__", "".join(tab_contents))
    page = page.replace("__UPDATED__", esc(now_str))
    return page


TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ЭЗЦ-монитор — экономика замкнутого цикла</title>
<style>
  :root {
    --bg: #f7f6f2;
    --panel: #ffffff;
    --border: #ddd8cc;
    --text: #26261f;
    --text-muted: #6d6a5c;
    --accent: #1b5e3a;
    --mono: 'SFMono-Regular', Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 2rem 3vw 4rem;
    background: var(--bg); color: var(--text);
    font-family: 'PT Serif', Georgia, serif;
  }
  h1 { font-size: 1.9rem; margin: 0 0 .2rem; }
  .subtitle { color: var(--text-muted); margin: 0 0 1.5rem; font-size: .95rem; }
  .kpis {
    display: grid; grid-template-columns: repeat(3, minmax(0,1fr)); gap: .75rem;
    margin-bottom: 1.5rem;
  }
  @media (min-width: 900px) { .kpis { grid-template-columns: repeat(6, minmax(0,1fr)); } }
  .kpi {
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    padding: .9rem; text-align: center;
  }
  .kpi-num { font-family: var(--mono); font-size: 1.5rem; font-weight: 700; }
  .kpi-label { font-size: .78rem; color: var(--text-muted); margin-top: .2rem; }
  .tabs { display: flex; flex-wrap: wrap; gap: .4rem; margin-bottom: 1rem; }
  .tab {
    font-family: var(--mono); font-size: .8rem; padding: .5rem .9rem;
    border: 1px solid var(--border); background: var(--panel); border-radius: 6px;
    cursor: pointer; color: var(--text);
  }
  .tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }
  .tbl { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--border); }
  .tbl th {
    text-align: left; font-family: var(--mono); font-size: .72rem; text-transform: uppercase;
    letter-spacing: .04em; color: var(--text-muted); padding: .6rem .7rem; border-bottom: 1px solid var(--border);
  }
  .tbl td { padding: .6rem .7rem; border-bottom: 1px solid var(--border); vertical-align: top; font-size: .92rem; }
  .tdate, .tsource { font-family: var(--mono); font-size: .78rem; color: var(--text-muted); white-space: nowrap; }
  .ttitle a { color: var(--text); font-weight: 700; text-decoration: none; }
  .ttitle a:hover { text-decoration: underline; }
  .summary { color: var(--text-muted); font-size: .85rem; margin-top: .25rem; }
  .tags { margin-top: .35rem; display: flex; flex-wrap: wrap; gap: .3rem; }
  .tag {
    font-family: var(--mono); font-size: .68rem; background: #eef2ea; color: #3c5a3a;
    border-radius: 4px; padding: .1rem .4rem;
  }
  .tag-none { background: #f0efe9; color: var(--text-muted); }
  .tscore { font-family: var(--mono); text-align: center; white-space: nowrap; }
  .pill { display: inline-block; margin-top: .3rem; border: 1px solid; border-radius: 999px; padding: .1rem .5rem; font-size: .68rem; }
  .ok { color: #1b5e3a; font-family: var(--mono); }
  .fail { color: #8b2635; font-family: var(--mono); }
  .empty { text-align: center; color: var(--text-muted); padding: 1.5rem; }
  footer { margin-top: 2rem; font-size: .8rem; color: var(--text-muted); }
</style>
</head>
<body>
  <h1>ЭЗЦ-монитор</h1>
  <p class="subtitle">Экономика замкнутого цикла: технологии, рейтинги, инвестиции, отрасли, география. Обновлено: __UPDATED__</p>

  <div class="kpis">__KPI__</div>

  <div class="tabs">__TABS__</div>
  __TAB_CONTENTS__

  <footer>
    v1 (черновая калибровка сигналов и порогов). Обновление: запустите run_parser.bat на своём ПК,
    затем закоммитьте и запушьте index.html в GitHub — см. README.md.
  </footer>

<script>
  document.querySelectorAll('.tab').forEach(function (btn) {
    btn.addEventListener('click', function () {
      document.querySelectorAll('.tab').forEach(function (b) { b.classList.remove('active'); });
      document.querySelectorAll('.tab-panel').forEach(function (p) { p.classList.remove('active'); });
      btn.classList.add('active');
      document.getElementById(btn.dataset.tab).classList.add('active');
    });
  });
</script>
</body>
</html>
"""


def main():
    print("ЭЗЦ-монитор v1: сбор материалов...")
    items, source_status = collect_all()
    for s in source_status:
        status_txt = ("OK, " + str(s["count"]) + " материалов") if s["ok"] else ("ОШИБКА: " + s.get("error", ""))
        print("  - " + s["label"] + ": " + status_txt)

    print("Всего уникальных материалов: " + str(len(items)))
    for lvl in PRIORITY_LEVELS:
        cnt = sum(1 for it in items if it["priority"]["rank"] == lvl["rank"])
        print("  " + lvl["label"] + " (" + str(lvl["min_pct"]) + "%+): " + str(cnt))
    skip_cnt = sum(1 for it in items if it["priority"]["rank"] == SKIP_RANK)
    print("  " + SKIP_LABEL + ": " + str(skip_cnt))

    collected_at = datetime.now()
    page = build_html(items, source_status, collected_at)
    with open(OUTPUT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(page)
    print("Готово: " + OUTPUT_HTML_PATH)


if __name__ == "__main__":
    main()
