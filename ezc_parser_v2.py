# -*- coding: utf-8 -*-
"""
ЭЗЦ Экометт-Луч — дашборд-инструмент по вовлечению промышленных отходов
в экономику замкнутого цикла. v2.

Что делает:
  1. Читает источники из sources.json (RSS, Telegram, Google News), скачивает свежие
     материалы, оценивает релевантность по SIGNALS и тегирует по министерствам /
     гос.подразделениям / регионам / типу отходов (TAG_RULES).
  2. Накапливает материалы в library.json — постоянную библиотеку находок между
     запусками: RSS-лента отдаёт только последние N материалов, поэтому без накопления
     старые находки исчезали бы из дашборда при каждом обновлении.
  3. Рендерит всю библиотеку в index.html: данные сериализуются в JSON и
     рендерятся/фильтруются на стороне клиента (JS, без сервера) — дашборд остаётся
     статическим для GitHub Pages. Каждый материал в таблице — рабочая ссылка на
     первоисточник (новость или нормативный документ).

ВАЖНО (v2, черновая калибровка):
  Веса сигналов, пороги приоритетов и ключевые слова тегов — стартовые значения.
  Точную настройку донастраиваем отдельно вместе, по образцу калибровки ved_parser_v3.

Запуск: двойной клик по run_parser.bat (Windows), либо `python ezc_parser_v2.py`.
Публикация: index.html — это корневая страница для GitHub Pages (см. README.md).
"""

import os
import re
import sys
import time
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
LIBRARY_JSON_PATH = os.path.join(SCRIPT_DIR, "library.json")

DASHBOARD_NAME = "MediaLab EML"
DASHBOARD_VERSION = "1.1.0"
COPYRIGHT_HOLDER_RU = "Скобцов Дмитрий Олегович"
COPYRIGHT_HOLDER_EN = "Dmitry Skobtsov"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REQUEST_TIMEOUT = 25
REQUEST_MAX_RETRIES = 2
REQUEST_RETRY_BACKOFF_SECONDS = 3
GOOGLE_NEWS_DELAY_SECONDS = 2

# ---------------------------------------------------------------------------
# SIGNALS — сигналы релевантности. Веса и self_sufficient — стартовая калибровка v1.
#   self_sufficient=True  -> одного попадания достаточно, чтобы материал не считался шумом
#   self_sufficient=False -> нужен ещё хотя бы один сигнал в паре (правило перекрёстного
#                             подтверждения, как в ved_parser_v3)
# ---------------------------------------------------------------------------
SIGNALS = [
    {"id": "industrial_waste_core", "label": "Промышленные отходы (ЗШО, техноген. месторождения)",
     "weight": 20, "self_sufficient": True,
     "keywords": ["зшо", "золошлак", "золошлаковые отходы", "гидрозолоудаление",
                  "техногенное месторождение", "техногенных месторождений", "отходы добычи угля",
                  "отходы добычи металлических руд", "промышленные отходы", "промотходы",
                  "фосфогипс", "хвостохранилищ"]},

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
# TAG_RULES — теги для фильтров дашборда (министерства / гос.подразделения /
# регионы-округа / тип отходов). Стартовый набор ключевых слов — донастраивается
# отдельно по мере появления реальных материалов.
# ---------------------------------------------------------------------------
MINISTRY_RULES = [
    {"label": "Минэнерго", "keywords": ["минэнерго", "министерство энергетики"]},
    {"label": "Минприроды", "keywords": ["минприроды", "министерство природных ресурсов"]},
    {"label": "Минпромторг", "keywords": ["минпромторг", "министерство промышленности и торговли"]},
    {"label": "ФАС", "keywords": ["фас", "федеральная антимонопольная служба"]},
    {"label": "Росприроднадзор", "keywords": ["росприроднадзор"]},
]

GOV_BODY_RULES = [
    {"label": "РЭО", "keywords": ["рэо", "российский экологический оператор"]},
    {"label": "Росатом", "keywords": ["росатом"]},
    {"label": "Правительство РФ", "keywords": ["правительство рф", "распоряжение правительства",
                                                "постановление правительства"]},
    {"label": "Региональная администрация", "keywords": ["администрация", "правительство края",
                                                          "правительство области", "губернатор"]},
]

# Округ каждого субъекта РФ — стартовый набор основных регионов (не всех 89),
# расширяем по мере необходимости. Нужен, чтобы упоминание конкретного региона
# («Приморский край») само по себе давало и фильтр по округу (ДФО), даже если
# слово «округ» в тексте не встречается.
OKRUG_BY_REGION = {
    "Приморский край": "Дальневосточный ФО", "Хабаровский край": "Дальневосточный ФО",
    "Сахалинская область": "Дальневосточный ФО", "Амурская область": "Дальневосточный ФО",
    "Республика Саха (Якутия)": "Дальневосточный ФО", "Камчатский край": "Дальневосточный ФО",
    "Магаданская область": "Дальневосточный ФО", "Еврейская автономная область": "Дальневосточный ФО",
    "Чукотский автономный округ": "Дальневосточный ФО", "Забайкальский край": "Дальневосточный ФО",
    "Свердловская область": "Уральский ФО", "Челябинская область": "Уральский ФО",
    "Тюменская область": "Уральский ФО", "Курганская область": "Уральский ФО",
    "Ханты-Мансийский автономный округ": "Уральский ФО", "Ямало-Ненецкий автономный округ": "Уральский ФО",
    "Новосибирская область": "Сибирский ФО", "Красноярский край": "Сибирский ФО",
    "Иркутская область": "Сибирский ФО", "Кемеровская область": "Сибирский ФО",
    "Омская область": "Сибирский ФО", "Томская область": "Сибирский ФО",
    "Алтайский край": "Сибирский ФО", "Республика Бурятия": "Сибирский ФО",
    "Республика Тыва": "Сибирский ФО", "Республика Хакасия": "Сибирский ФО",
    "Республика Алтай": "Сибирский ФО",
    "Нижегородская область": "Приволжский ФО", "Республика Татарстан": "Приволжский ФО",
    "Республика Башкортостан": "Приволжский ФО", "Самарская область": "Приволжский ФО",
    "Пермский край": "Приволжский ФО", "Саратовская область": "Приволжский ФО",
    "Оренбургская область": "Приволжский ФО", "Ульяновская область": "Приволжский ФО",
    "Пензенская область": "Приволжский ФО", "Кировская область": "Приволжский ФО",
    "Удмуртская Республика": "Приволжский ФО", "Чувашская Республика": "Приволжский ФО",
    "Республика Марий Эл": "Приволжский ФО", "Республика Мордовия": "Приволжский ФО",
    "Краснодарский край": "Южный ФО", "Ростовская область": "Южный ФО",
    "Волгоградская область": "Южный ФО", "Астраханская область": "Южный ФО",
    "Республика Крым": "Южный ФО", "город Севастополь": "Южный ФО",
    "Республика Адыгея": "Южный ФО", "Республика Калмыкия": "Южный ФО",
    "Ставропольский край": "Северо-Кавказский ФО", "Республика Дагестан": "Северо-Кавказский ФО",
    "Чеченская Республика": "Северо-Кавказский ФО", "Кабардино-Балкарская Республика": "Северо-Кавказский ФО",
    "Республика Северная Осетия": "Северо-Кавказский ФО", "Республика Ингушетия": "Северо-Кавказский ФО",
    "Карачаево-Черкесская Республика": "Северо-Кавказский ФО",
    "Москва": "Центральный ФО", "Московская область": "Центральный ФО",
    "Воронежская область": "Центральный ФО", "Белгородская область": "Центральный ФО",
    "Липецкая область": "Центральный ФО", "Тульская область": "Центральный ФО",
    "Ярославская область": "Центральный ФО", "Тверская область": "Центральный ФО",
    "Рязанская область": "Центральный ФО", "Курская область": "Центральный ФО",
    "Брянская область": "Центральный ФО", "Смоленская область": "Центральный ФО",
    "Владимирская область": "Центральный ФО", "Ивановская область": "Центральный ФО",
    "Костромская область": "Центральный ФО", "Орловская область": "Центральный ФО",
    "Тамбовская область": "Центральный ФО", "Калужская область": "Центральный ФО",
    "Санкт-Петербург": "Северо-Западный ФО", "Ленинградская область": "Северо-Западный ФО",
    "Калининградская область": "Северо-Западный ФО", "Архангельская область": "Северо-Западный ФО",
    "Мурманская область": "Северо-Западный ФО", "Вологодская область": "Северо-Западный ФО",
    "Республика Коми": "Северо-Западный ФО", "Республика Карелия": "Северо-Западный ФО",
    "Псковская область": "Северо-Западный ФО", "Новгородская область": "Северо-Западный ФО",
    "Ненецкий автономный округ": "Северо-Западный ФО",
}

REGION_RULES = [{"label": region, "keywords": [region.lower()]} for region in OKRUG_BY_REGION]

# Прямое упоминание округа в тексте (на случай, если конкретный регион не назван).
OKRUG_DIRECT_RULES = [
    {"label": "Дальневосточный ФО", "keywords": ["дальневосточн", "дфо"]},
    {"label": "Сибирский ФО", "keywords": ["сибирск"]},
    {"label": "Уральский ФО", "keywords": ["уральск"]},
    {"label": "Приволжский ФО", "keywords": ["приволжск"]},
    {"label": "Южный ФО", "keywords": ["южного федерального", "южный федеральный округ"]},
    {"label": "Северо-Кавказский ФО", "keywords": ["северо-кавказск"]},
    {"label": "Центральный ФО", "keywords": ["центрального федерального", "центральный федеральный округ"]},
    {"label": "Северо-Западный ФО", "keywords": ["северо-западного федерального",
                                                  "северо-западный федеральный округ"]},
]

WASTE_TYPE_RULES = [
    {"label": "Промышленные", "keywords": ["промышленные отходы", "промотходы", "зшо", "золошлак",
                                            "отходы добычи", "техногенное месторождение", "фосфогипс",
                                            "хвостохранилищ", "золоотвал"]},
    {"label": "Непромышленные", "keywords": ["тко", "твердые коммунальные отходы",
                                              "твёрдые коммунальные отходы", "отходы упаковки",
                                              "бытовые отходы"]},
]

# Тип материала по метаданным — продукт (коммерческая сторона) / технология (R&D) /
# закон (нормативка). Один материал может нести несколько типов сразу.
CONTENT_TYPE_RULES = [
    {"label": "Продукт", "keywords": ["сертифицирован", "гост", "коммерческ", "поставка", "заказчик",
                                       "покупатель", "цена", "рынок сбыта", "продукция", "потребител"]},
    {"label": "Технология", "keywords": ["технология", "патент", "разработк", "инновац", "ноу-хау",
                                          "линия переработки", "оборудование", "ниокр", "опытно-промышленн"]},
    {"label": "Закон", "keywords": ["закон", "указ", "постановление", "распоряжение", "нормативно-правов",
                                     "поправк", "вступает в силу", "приказ", "федеральный закон", "кодекс"]},
]


def match_tags(text, rules):
    matched = []
    for rule in rules:
        if any(kw in text for kw in rule["keywords"]):
            matched.append(rule["label"])
    return matched


# ---------------------------------------------------------------------------
# law_dates — АРХИТЕКТУРА (не полноценный источник данных): эвристика по
# регулярным выражениям, вытаскивающая из заголовка/текста даты жизненного
# цикла нормативного акта (подписание, вступление в силу, изменения), если
# они явно упомянуты. Точность не гарантирована — для полной и достоверной
# картины нужна отдельная интеграция с open-data pravo.gov.ru (сознательно
# отложена, см. README). Поля пустые/None, если ничего не распознано.
# ---------------------------------------------------------------------------
_DATE_RE = r"(\d{1,2}\.\d{1,2}\.\d{4})"
_SIGNED_RE = re.compile(r"от\s+" + _DATE_RE + r"\s*(?:года)?\s*№\s*([\w\-/]+)", re.IGNORECASE)
_EFFECTIVE_RE = re.compile(r"вступ\w*\s+в\s+сил\w*[^.]{0,40}?" + _DATE_RE, re.IGNORECASE)
_AMENDMENT_RE = re.compile(r"(?:с\s+изменени\w+|в\s+редакции)[^.]{0,40}?от\s+" + _DATE_RE, re.IGNORECASE)


def extract_law_dates(text):
    signed_match = _SIGNED_RE.search(text)
    effective_match = _EFFECTIVE_RE.search(text)
    amendments = _AMENDMENT_RE.findall(text)
    return {
        "signed_date": signed_match.group(1) if signed_match else None,
        "signed_number": signed_match.group(2) if signed_match else None,
        "effective_from": effective_match.group(1) if effective_match else None,
        "amendments": amendments,
    }


def tag_item(item):
    text = (item.get("title", "") + " " + item.get("summary", "")).lower()
    regions_matched = match_tags(text, REGION_RULES)
    okrugs_matched = set(match_tags(text, OKRUG_DIRECT_RULES))
    for region in regions_matched:
        okrug = OKRUG_BY_REGION.get(region)
        if okrug:
            okrugs_matched.add(okrug)
    return {
        "ministries": match_tags(text, MINISTRY_RULES),
        "gov_bodies": match_tags(text, GOV_BODY_RULES),
        "regions": regions_matched,
        "okrugs": sorted(okrugs_matched),
        "waste_type": match_tags(text, WASTE_TYPE_RULES),
        "content_type": match_tags(text, CONTENT_TYPE_RULES),
        "law_dates": extract_law_dates(text),
    }


# ---------------------------------------------------------------------------
# Загрузка источников
# ---------------------------------------------------------------------------
def load_sources():
    with open(SOURCES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def build_proxies():
    # Некоторые провайдеры/сети в РФ блокируют или сильно тормозят t.me и
    # news.google.com, при этом российские RSS (infragreen, vedomosti и т.п.)
    # работают нормально. Если это твой случай — пропиши прокси в переменной
    # окружения EZC_PROXY перед запуском (см. README.md). Не задана — ничего
    # не меняется, поведение как раньше.
    proxy_url = os.environ.get("EZC_PROXY", "").strip()
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def fetch_url(url):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    proxies = build_proxies()
    last_exc = None
    for attempt in range(REQUEST_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, proxies=proxies)
            resp.raise_for_status()
            return resp.content
        except requests.exceptions.RequestException as e:
            last_exc = e
            if attempt < REQUEST_MAX_RETRIES:
                time.sleep(REQUEST_RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_exc


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
        time.sleep(GOOGLE_NEWS_DELAY_SECONDS)
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
        it["tags"] = tag_item(it)
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


def source_status_row(s):
    if s["ok"]:
        return ('<tr><td>' + esc(s["label"]) + '</td>'
                '<td class="ok">OK</td><td>' + str(s["count"]) + '</td></tr>')
    return ('<tr><td>' + esc(s["label"]) + '</td>'
            '<td class="fail">ОШИБКА</td><td>' + esc(s.get("error", ""))[:80] + '</td></tr>')


def extract_domain(link):
    try:
        netloc = urllib.parse.urlsplit(link).netloc
    except ValueError:
        return ""
    return netloc[4:] if netloc.startswith("www.") else netloc


def item_to_json(it):
    return {
        "date": format_dt(it["pub_dt"]),
        "domain": extract_domain(it["link"]),
        "date_sort": it["pub_dt"].timestamp() if it["pub_dt"] else 0,
        "source": it["source_label"],
        "title": it["title"] or "(без заголовка)",
        "link": it["link"],
        "summary": it["summary"][:220] + ("…" if len(it["summary"]) > 220 else ""),
        "pct": it["pct"],
        "priority_label": it["priority"]["label"],
        "priority_color": it["priority"]["color"],
        "priority_rank": it["priority"]["rank"],
        "hits": it["hits"],
        "ministries": it["tags"]["ministries"],
        "gov_bodies": it["tags"]["gov_bodies"],
        "regions": it["tags"]["regions"],
        "okrugs": it["tags"]["okrugs"],
        "waste_type": it["tags"]["waste_type"],
        "content_type": it["tags"]["content_type"],
        "law_dates": it["tags"]["law_dates"],
    }


# ---------------------------------------------------------------------------
# Библиотека — накопление материалов между запусками. RSS/Google News отдают
# только последние N материалов за раз, поэтому без накопления старые находки
# исчезали бы из дашборда при каждом обновлении. library.json хранит все
# материалы, когда-либо найденные скриптом (ключ — нормализованная ссылка).
# ---------------------------------------------------------------------------
def load_library():
    if not os.path.exists(LIBRARY_JSON_PATH):
        return {}
    try:
        with open(LIBRARY_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_library(library):
    with open(LIBRARY_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(library, f, ensure_ascii=False, indent=1)


def library_key(flat_item):
    normalized = normalize_link(flat_item.get("link", ""))
    if normalized:
        return normalized
    raw = (flat_item.get("title", "") + "|" + flat_item.get("source", ""))
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def merge_into_library(library, fresh_items):
    for it in fresh_items:
        flat = item_to_json(it)
        library[library_key(flat)] = flat
    return library


def build_html(library_items, source_status, collected_at):
    priority_groups = {lvl["rank"]: [] for lvl in PRIORITY_LEVELS}
    skip_group = []
    for it in library_items:
        rank = it["priority_rank"]
        if rank == SKIP_RANK:
            skip_group.append(it)
        else:
            priority_groups.setdefault(rank, []).append(it)

    kpi_tiles = []
    kpi_tiles.append('<div class="kpi"><div class="kpi-num">' + str(len(library_items)) + '</div>'
                      '<div class="kpi-label">Всего в библиотеке</div></div>')
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
                      + '</div><div class="kpi-label">Источники OK (этот запуск)</div></div>')
    kpi_html = "".join(kpi_tiles)

    src_rows = "".join(source_status_row(s) for s in source_status)
    sources_table_html = (
        '<table class="tbl"><thead><tr><th>Источник</th><th>Статус</th>'
        '<th>Материалов / ошибка</th></tr></thead><tbody>' + src_rows + '</tbody></table>'
    )

    priority_defs = [{"label": lvl["label"], "color": lvl["color"], "rank": lvl["rank"]} for lvl in PRIORITY_LEVELS]
    priority_defs.append({"label": SKIP_LABEL, "color": SKIP_COLOR, "rank": SKIP_RANK})

    items_sorted = sorted(library_items, key=lambda it: -it["date_sort"])
    items_json = json.dumps(items_sorted, ensure_ascii=False)
    items_json = items_json.replace("</script", "<\\/script")
    priority_defs_json = json.dumps(priority_defs, ensure_ascii=False)

    now_str = collected_at.strftime("%d.%m.%Y %H:%M")

    page = TEMPLATE.replace("__KPI__", kpi_html)
    page = page.replace("__SOURCES_TABLE__", sources_table_html)
    page = page.replace("__ITEMS_JSON__", items_json)
    page = page.replace("__PRIORITY_DEFS_JSON__", priority_defs_json)
    page = page.replace("__UPDATED__", esc(now_str))
    page = page.replace("__DASHBOARD_NAME__", esc(DASHBOARD_NAME))
    page = page.replace("__VERSION__", esc(DASHBOARD_VERSION))
    page = page.replace("__YEAR__", str(collected_at.year))
    page = page.replace("__COPYRIGHT_HOLDER_RU__", esc(COPYRIGHT_HOLDER_RU))
    page = page.replace("__COPYRIGHT_HOLDER_EN__", esc(COPYRIGHT_HOLDER_EN))
    return page


TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="version" content="__VERSION__">
<title>__DASHBOARD_NAME__ · Экометт-Луч — v__VERSION__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600;700&family=Manrope:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #f7f6f2;
    --panel: #ffffff;
    --border: #ddd8cc;
    --text: #26261f;
    --text-muted: #6d6a5c;
    --accent: #1b5e3a;
    --accent2: #1e5b8a;
    --warn: #8b2635;
    --heading: 'Cormorant Garamond', Georgia, serif;
    --body: 'Manrope', 'Segoe UI', Arial, sans-serif;
    --mono: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 0 0 4rem;
    background: var(--bg); color: var(--text);
    font-family: var(--body);
  }
  .wrap { padding: 2rem 3vw 0; }
  .brand-row { display: flex; align-items: baseline; gap: .7rem; flex-wrap: wrap; }
  h1 { font-family: var(--heading); font-size: 2.4rem; font-weight: 700; margin: 0 0 .4rem; color: var(--accent); letter-spacing: .01em; }
  .version-badge {
    font-family: var(--mono); font-size: .68rem; color: var(--accent2); border: 1px solid var(--accent2);
    border-radius: 999px; padding: .15rem .55rem; white-space: nowrap;
  }
  h2 { font-family: var(--heading); font-weight: 700; font-size: 1.35rem; margin: 0 0 .7rem;
    border-left: 4px solid var(--accent); padding-left: .6rem; }
  .subtitle { color: var(--text-muted); margin: 0 0 1.5rem; font-size: .95rem; }

  /* --- Мониторинг / библиотека --- */
  .monitor { max-width: 1400px; margin: 0 auto; }
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

  .filters {
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    padding: 1rem; margin-bottom: 1rem; display: flex; flex-direction: column; gap: .8rem;
  }
  .filter-row { display: flex; flex-wrap: wrap; gap: .6rem; align-items: center; }
  .filter-group-label { font-family: var(--mono); font-size: .72rem; text-transform: uppercase;
    color: var(--text-muted); min-width: 140px; }
  .chip {
    font-family: var(--mono); font-size: .76rem; padding: .3rem .7rem; border-radius: 999px;
    border: 1px solid var(--border); background: var(--bg); cursor: pointer; user-select: none;
  }
  .chip.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .search-input {
    flex: 1; min-width: 200px; font-family: var(--body); font-size: .9rem;
    padding: .4rem .6rem; border: 1px solid var(--border); border-radius: 6px;
  }
  .count-badge { font-family: var(--mono); font-size: .8rem; color: var(--text-muted); }

  .chart-card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
    padding: .9rem 1rem; margin-bottom: 1rem;
  }
  .chart-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: .5rem; flex-wrap: wrap; gap: .5rem; }
  #chart-svg { width: 100%; height: 130px; display: block; }
  .chart-bar { fill: var(--accent); }
  .chart-bar:hover { fill: var(--accent2); }
  .chart-axis-label { font-family: var(--mono); font-size: 9px; fill: var(--text-muted); }
  .scale-btn {
    font-family: var(--mono); font-size: .72rem; padding: .25rem .6rem; border-radius: 6px;
    border: 1px solid var(--border); background: var(--bg); cursor: pointer; margin-left: .3rem;
  }
  .scale-btn.active { background: var(--accent2); color: #fff; border-color: var(--accent2); }

  .molecule-cell { display: flex; flex-direction: column; align-items: center; gap: .15rem; }
  .molecule-pct { font-family: var(--mono); font-size: .78rem; font-weight: 700; }

  .tbl { width: 100%; border-collapse: collapse; background: var(--panel); border: 1px solid var(--border); table-layout: fixed; }
  .tbl th:nth-child(1), .tbl td:nth-child(1) { width: 8%; }
  .tbl th:nth-child(2), .tbl td:nth-child(2) { width: 11%; }
  .tbl th:nth-child(3), .tbl td:nth-child(3) { width: auto; }
  .tbl th:nth-child(4), .tbl td:nth-child(4) { width: 12%; }
  .tbl th {
    text-align: left; font-family: var(--mono); font-size: .72rem; text-transform: uppercase;
    letter-spacing: .04em; color: var(--text-muted); padding: .6rem .7rem; border-bottom: 1px solid var(--border);
  }
  .tbl td { padding: .6rem .7rem; border-bottom: 1px solid var(--border); vertical-align: top; font-size: .92rem; }
  .tdate, .tsource { font-family: var(--mono); font-size: .78rem; color: var(--text-muted); word-break: break-word; }
  .select-input {
    font-family: var(--body); font-size: .8rem; padding: .3rem .5rem;
    border: 1px solid var(--border); border-radius: 6px; background: var(--bg); color: var(--text);
  }
  .ttitle a { color: var(--text); font-weight: 700; text-decoration: none; }
  .ttitle a:hover { text-decoration: underline; }
  .domain-badge {
    font-family: var(--mono); font-size: .68rem; color: var(--accent2); background: #eef4f9;
    border-radius: 4px; padding: .05rem .4rem; margin-left: .4rem; white-space: nowrap;
  }
  .summary { color: var(--text-muted); font-size: .85rem; margin-top: .25rem; }
  .tags { margin-top: .35rem; display: flex; flex-wrap: wrap; gap: .3rem; }
  .tag {
    font-family: var(--mono); font-size: .68rem; background: #eef2ea; color: #3c5a3a;
    border-radius: 4px; padding: .1rem .4rem;
  }
  .tag-ministry { background: #e6eef5; color: var(--accent2); }
  .tag-none { background: #f0efe9; color: var(--text-muted); }
  .tscore { font-family: var(--mono); text-align: center; white-space: nowrap; }
  .pill { display: inline-block; margin-top: .3rem; border: 1px solid; border-radius: 999px; padding: .1rem .5rem; font-size: .68rem; }
  .ok { color: #1b5e3a; font-family: var(--mono); }
  .fail { color: #8b2635; font-family: var(--mono); }
  .empty { text-align: center; color: var(--text-muted); padding: 1.5rem; }
  details.sources-details { margin-top: 1.5rem; }
  details.sources-details summary { cursor: pointer; font-family: var(--mono); font-size: .8rem; color: var(--text-muted); }
  footer { margin-top: 2rem; font-size: .8rem; color: var(--text-muted); max-width: 1400px; margin-left: auto; margin-right: auto; }
  .footer-note { margin-bottom: 1rem; }
  .footer-legal {
    border-top: 1px solid var(--border); padding-top: .9rem;
    display: flex; flex-direction: column; gap: .3rem;
  }
  .pf { font-family: var(--mono); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .pf-line1 { font-size: .74rem; color: var(--text); }
  .pf-line2 { font-size: .66rem; color: var(--text-muted); white-space: normal; }
  noscript div { background: #fdf1e0; border: 1px solid #cbb27a; padding: 1rem; border-radius: 8px; }
</style>
</head>
<body>
<div class="wrap">
  <section class="monitor" id="monitor">
    <div class="brand-row"><h1>__DASHBOARD_NAME__</h1><span class="version-badge">v__VERSION__</span></div>
    <p class="subtitle">Экометт-Луч · Промышленные отходы и экономика замкнутого цикла: накопленная
      база находок (новости и нормативные документы) с фильтрами по министерствам, гос.подразделениям,
      регионам и типу отходов. Обновлено: __UPDATED__</p>

    <noscript><div>Для интерактивной фильтрации и таблицы материалов нужен включённый JavaScript.</div></noscript>

    <div class="kpis">__KPI__</div>

    <div class="filters" id="filters">
      <div class="filter-row"><input class="search-input" id="search" type="text" placeholder="Поиск по заголовку и тексту…"></div>
      <div class="filter-row"><span class="filter-group-label">Приоритет</span><span id="chips-priority"></span></div>
      <div class="filter-row"><span class="filter-group-label">Тип материала</span><span id="chips-content-type"></span></div>
      <div class="filter-row"><span class="filter-group-label">Тип отходов</span><span id="chips-waste"></span></div>
      <div class="filter-row"><span class="filter-group-label">Министерства</span><span id="chips-ministry"></span></div>
      <div class="filter-row"><span class="filter-group-label">Гос.подразделения</span><span id="chips-gov"></span></div>
      <div class="filter-row"><span class="filter-group-label">Регион</span>
        <select class="select-input" id="region-select"><option value="">Все регионы</option></select></div>
      <div class="filter-row"><span class="filter-group-label">Федеральный округ</span><span id="chips-okrug"></span></div>
      <div class="filter-row"><span class="count-badge" id="count-badge"></span></div>
    </div>

    <div class="chart-card">
      <div class="chart-header">
        <span class="filter-group-label">Публикации во времени</span>
        <span id="chart-scale-buttons"></span>
      </div>
      <svg id="chart-svg" viewBox="0 0 1000 200" preserveAspectRatio="none"></svg>
    </div>

    <table class="tbl">
      <thead><tr><th>Дата</th><th>Источник</th><th>Материал</th><th>Оценка</th></tr></thead>
      <tbody id="rows-body"></tbody>
    </table>

    <details class="sources-details">
      <summary>Статус источников</summary>
      __SOURCES_TABLE__
    </details>
  </section>

  <footer>
    <p class="footer-note">
      Черновая калибровка сигналов, тегов и порогов. Обновление: запустите run_parser.bat на своём ПК —
      новые материалы добавятся в library.json, index.html пересоберётся из всей накопленной библиотеки;
      затем закоммитьте и запушьте index.html и library.json в GitHub — см. README.md.
    </p>
    <div class="footer-legal">
      <div class="pf pf-line1">© __YEAR__ __COPYRIGHT_HOLDER_RU__ (__COPYRIGHT_HOLDER_EN__) · __DASHBOARD_NAME__ · v__VERSION__ · __UPDATED__</div>
      <div class="pf pf-line2">All analytical materials, scoring methodology, tagging rules and the accumulated
        monitoring library presented on this resource are the intellectual property of the author and protected
        by copyright. Reproduction, copying, distribution or commercial use without written consent of the
        author is prohibited.</div>
    </div>
  </footer>
</div>

<script id="ezc-data" type="application/json">__ITEMS_JSON__</script>
<script id="ezc-priority-defs" type="application/json">__PRIORITY_DEFS_JSON__</script>
<script>
(function () {
  var ITEMS = JSON.parse(document.getElementById('ezc-data').textContent);
  var PRIORITY_DEFS = JSON.parse(document.getElementById('ezc-priority-defs').textContent);

  function escHtml(s) {
    return (s || '').replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function uniqueValues(field) {
    var set = {};
    ITEMS.forEach(function (it) { (it[field] || []).forEach(function (v) { set[v] = true; }); });
    return Object.keys(set).sort();
  }

  var state = { priority: {}, waste: {}, ministry: {}, gov: {}, okrug: {}, contentType: {}, region: '', search: '' };

  function buildChips(containerId, values, stateKey, colorMap) {
    var container = document.getElementById(containerId);
    container.innerHTML = '';
    values.forEach(function (val) {
      var chip = document.createElement('span');
      chip.className = 'chip';
      chip.textContent = val;
      if (colorMap && colorMap[val]) { chip.style.borderColor = colorMap[val]; }
      chip.addEventListener('click', function () {
        state[stateKey][val] = !state[stateKey][val];
        chip.classList.toggle('active', !!state[stateKey][val]);
        render();
      });
      container.appendChild(chip);
    });
  }

  var priorityColors = {};
  var priorityLabels = PRIORITY_DEFS.map(function (p) { priorityColors[p.label] = p.color; return p.label; });
  buildChips('chips-priority', priorityLabels, 'priority', priorityColors);
  // По умолчанию показываем всё, кроме ШУМ
  PRIORITY_DEFS.forEach(function (p) {
    if (p.label !== 'ШУМ') { state.priority[p.label] = true; }
  });
  Array.prototype.forEach.call(document.getElementById('chips-priority').children, function (chip) {
    if (state.priority[chip.textContent]) { chip.classList.add('active'); }
  });

  buildChips('chips-content-type', uniqueValues('content_type'), 'contentType');
  buildChips('chips-waste', uniqueValues('waste_type'), 'waste');
  buildChips('chips-ministry', uniqueValues('ministries'), 'ministry');
  buildChips('chips-gov', uniqueValues('gov_bodies'), 'gov');
  buildChips('chips-okrug', uniqueValues('okrugs'), 'okrug');

  var regionSelect = document.getElementById('region-select');
  uniqueValues('regions').forEach(function (val) {
    var opt = document.createElement('option');
    opt.value = val;
    opt.textContent = val;
    regionSelect.appendChild(opt);
  });
  regionSelect.addEventListener('change', function (e) {
    state.region = e.target.value;
    render();
  });

  document.getElementById('search').addEventListener('input', function (e) {
    state.search = e.target.value.trim().toLowerCase();
    render();
  });

  function anyActive(group) { return Object.keys(group).some(function (k) { return group[k]; }); }
  function matchesGroup(group, values) {
    if (!anyActive(group)) { return true; }
    return (values || []).some(function (v) { return group[v]; });
  }

  function matchesFilters(it) {
    if (anyActive(state.priority) && !state.priority[it.priority_label]) { return false; }
    if (!matchesGroup(state.contentType, it.content_type)) { return false; }
    if (!matchesGroup(state.waste, it.waste_type)) { return false; }
    if (!matchesGroup(state.ministry, it.ministries)) { return false; }
    if (!matchesGroup(state.gov, it.gov_bodies)) { return false; }
    if (!matchesGroup(state.okrug, it.okrugs)) { return false; }
    if (state.region && (it.regions || []).indexOf(state.region) === -1) { return false; }
    if (state.search) {
      var hay = (it.title + ' ' + it.summary).toLowerCase();
      if (hay.indexOf(state.search) === -1) { return false; }
    }
    return true;
  }

  function moleculeSvg(pct, color) {
    var totalBonds = 6;
    var clamped = Math.max(0, Math.min(100, pct));
    var filled = Math.round(clamped / 100 * totalBonds);
    var cx = 23, cy = 23, r = 16, atomR = 4, centerR = 5;
    var parts = ['<svg class="molecule" width="38" height="38" viewBox="0 0 46 46" xmlns="http://www.w3.org/2000/svg">'];
    for (var i = 0; i < totalBonds; i++) {
      var angle = (Math.PI * 2 / totalBonds) * i - Math.PI / 2;
      var x = cx + r * Math.cos(angle);
      var y = cy + r * Math.sin(angle);
      var isFilled = i < filled;
      var lineColor = isFilled ? color : '#ddd8cc';
      var dotColor = isFilled ? color : '#eee9dc';
      parts.push('<line x1="' + cx + '" y1="' + cy + '" x2="' + x.toFixed(1) + '" y2="' + y.toFixed(1)
        + '" stroke="' + lineColor + '" stroke-width="2"></line>');
      parts.push('<circle cx="' + x.toFixed(1) + '" cy="' + y.toFixed(1) + '" r="' + atomR
        + '" fill="' + dotColor + '" stroke="' + lineColor + '" stroke-width="1.2"></circle>');
    }
    parts.push('<circle cx="' + cx + '" cy="' + cy + '" r="' + centerR + '" fill="' + color + '"></circle>');
    parts.push('</svg>');
    return parts.join('');
  }

  function tagSpan(cls, text) { return '<span class="tag ' + cls + '">' + escHtml(text) + '</span>'; }

  function rowHtml(it) {
    var hitsHtml = (it.hits || []).map(function (h) { return tagSpan('', h); }).join('')
      + (it.ministries || []).map(function (m) { return tagSpan('tag-ministry', m); }).join('')
      + (it.gov_bodies || []).map(function (g) { return tagSpan('tag-ministry', g); }).join('')
      + (it.regions || []).map(function (r) { return tagSpan('tag-ministry', r); }).join('')
      + (it.okrugs || []).map(function (o) { return tagSpan('tag-ministry', o); }).join('');
    if (!hitsHtml) { hitsHtml = tagSpan('tag-none', 'нет совпадений'); }
    var domainHtml = it.domain ? '<span class="domain-badge">' + escHtml(it.domain) + '</span>' : '';
    return '<tr>'
      + '<td class="tdate">' + escHtml(it.date) + '</td>'
      + '<td class="tsource">' + escHtml(it.source) + '</td>'
      + '<td class="ttitle"><a href="' + escHtml(it.link) + '" target="_blank" rel="noopener">'
      + escHtml(it.title) + '</a>' + domainHtml + '<div class="summary">' + escHtml(it.summary) + '</div>'
      + '<div class="tags">' + hitsHtml + '</div></td>'
      + '<td class="tscore"><div class="molecule-cell">' + moleculeSvg(it.pct, it.priority_color)
      + '<span class="molecule-pct" style="color:' + it.priority_color + '">' + it.pct + '%</span></div></td></tr>';
  }

  var CHART_SCALES = [
    { key: 'month', label: 'Месяц' },
    { key: 'year', label: 'Год' },
    { key: '5y', label: '5 лет' },
    { key: 'all', label: 'Всё время' }
  ];
  var chartState = { scale: 'year' };

  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function bucketFor(scale, dateSec) {
    var d = new Date(dateSec * 1000);
    if (scale === 'month') {
      return { key: d.getFullYear() + '-' + d.getMonth() + '-' + d.getDate(),
        label: pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1),
        sort: Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()) };
    }
    if (scale === 'year') {
      return { key: d.getFullYear() + '-' + d.getMonth(),
        label: pad2(d.getMonth() + 1) + '.' + d.getFullYear(),
        sort: Date.UTC(d.getFullYear(), d.getMonth(), 1) };
    }
    if (scale === '5y') {
      var q = Math.floor(d.getMonth() / 3) + 1;
      return { key: d.getFullYear() + '-Q' + q, label: 'Q' + q + ' ' + d.getFullYear(),
        sort: Date.UTC(d.getFullYear(), (q - 1) * 3, 1) };
    }
    return { key: String(d.getFullYear()), label: String(d.getFullYear()),
      sort: Date.UTC(d.getFullYear(), 0, 1) };
  }

  function buildBuckets(items, scale) {
    var nowSec = Date.now() / 1000;
    var rangeStart = null;
    if (scale === 'month') { rangeStart = nowSec - 30 * 86400; }
    else if (scale === 'year') { rangeStart = nowSec - 365 * 86400; }
    else if (scale === '5y') { rangeStart = nowSec - 5 * 365 * 86400; }
    var map = {};
    items.forEach(function (it) {
      if (!it.date_sort) { return; }
      if (rangeStart !== null && it.date_sort < rangeStart) { return; }
      var b = bucketFor(scale, it.date_sort);
      if (!map[b.key]) { map[b.key] = { label: b.label, sort: b.sort, count: 0 }; }
      map[b.key].count++;
    });
    return Object.keys(map).map(function (k) { return map[k]; }).sort(function (a, b) { return a.sort - b.sort; });
  }

  function renderChart(filtered) {
    var svg = document.getElementById('chart-svg');
    var buckets = buildBuckets(filtered, chartState.scale);
    if (!buckets.length) {
      svg.innerHTML = '<text x="500" y="100" text-anchor="middle" class="chart-axis-label">Нет данных за выбранный период</text>';
      return;
    }
    var maxCount = Math.max.apply(null, buckets.map(function (b) { return b.count; }));
    var w = 1000, h = 200, padBottom = 20;
    var barW = w / buckets.length;
    var labelEvery = Math.max(1, Math.ceil(buckets.length / 15));
    var parts = [];
    buckets.forEach(function (b, i) {
      var barH = maxCount ? (b.count / maxCount) * (h - padBottom - 10) : 0;
      var x = i * barW + barW * 0.15;
      var bw = barW * 0.7;
      var y = h - padBottom - barH;
      parts.push('<rect class="chart-bar" x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="'
        + bw.toFixed(1) + '" height="' + barH.toFixed(1) + '"><title>' + escHtml(b.label) + ': ' + b.count
        + '</title></rect>');
      if (i % labelEvery === 0) {
        parts.push('<text class="chart-axis-label" x="' + (x + bw / 2).toFixed(1) + '" y="' + (h - 5)
          + '" text-anchor="middle">' + escHtml(b.label) + '</text>');
      }
    });
    svg.innerHTML = parts.join('');
  }

  var scaleButtonsContainer = document.getElementById('chart-scale-buttons');
  CHART_SCALES.forEach(function (s) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'scale-btn' + (s.key === chartState.scale ? ' active' : '');
    btn.textContent = s.label;
    btn.addEventListener('click', function () {
      chartState.scale = s.key;
      Array.prototype.forEach.call(scaleButtonsContainer.children, function (b) { b.classList.remove('active'); });
      btn.classList.add('active');
      render();
    });
    scaleButtonsContainer.appendChild(btn);
  });

  function render() {
    var filtered = ITEMS.filter(matchesFilters);
    filtered.sort(function (a, b) { return b.date_sort - a.date_sort; });
    document.getElementById('rows-body').innerHTML = filtered.map(rowHtml).join('')
      || '<tr><td colspan="4" class="empty">Ничего не найдено по текущим фильтрам</td></tr>';
    document.getElementById('count-badge').textContent = 'Показано ' + filtered.length + ' из ' + ITEMS.length;
    renderChart(filtered);
  }

  render();
})();
</script>
</body>
</html>
"""


def main():
    print("ЭЗЦ Экометт-Луч: сбор материалов...")
    items, source_status = collect_all()
    for s in source_status:
        status_txt = ("OK, " + str(s["count"]) + " материалов") if s["ok"] else ("ОШИБКА: " + s.get("error", ""))
        print("  - " + s["label"] + ": " + status_txt)

    print("Материалов за этот запуск: " + str(len(items)))
    for lvl in PRIORITY_LEVELS:
        cnt = sum(1 for it in items if it["priority"]["rank"] == lvl["rank"])
        print("  " + lvl["label"] + " (" + str(lvl["min_pct"]) + "%+): " + str(cnt))
    skip_cnt = sum(1 for it in items if it["priority"]["rank"] == SKIP_RANK)
    print("  " + SKIP_LABEL + ": " + str(skip_cnt))

    library = load_library()
    library = merge_into_library(library, items)
    save_library(library)
    print("Всего материалов в библиотеке (накопительно): " + str(len(library)))

    collected_at = datetime.now()
    page = build_html(list(library.values()), source_status, collected_at)
    with open(OUTPUT_HTML_PATH, "w", encoding="utf-8") as f:
        f.write(page)
    print("Готово: " + OUTPUT_HTML_PATH)


if __name__ == "__main__":
    main()
