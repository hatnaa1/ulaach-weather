from __future__ import annotations

import asyncio
import datetime
import html
import ipaddress
import re
import socket
import ssl
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any

import asyncpg
from fastapi import HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

UB_TZ = datetime.timezone(datetime.timedelta(hours=8))

WW_DESC: dict[int, str] = {
    0: "Цэлмэг",
    1: "Бага зэргийн үүлтэй",
    2: "Солигдмол үүлтэй",
    3: "Үүлэрхэг",
    5: "Солигдмол үүлтэй",
    7: "Үүлшинэ",
    9: "Ялимгүй цас",
    10: "Ялимгүй хур тунадас",
    20: "Ялимгүй цас",
    21: "Бага зэргийн бороо",
    22: "Ялимгүй цас",
    23: "Нойтон цас",
    24: "Цас орно",
    60: "Бага зэргийн бороо",
    61: "Бороо орно",
    70: "Цас орно",
    71: "Их цас орно",
    80: "Түр зуурын бороо",
    95: "Аадар бороо, дуу цахилгаан",
}

WX_WIDGET_I18N: dict[str, dict[str, str]] = {
    "MN": {
        "title": "Цаг агаар & AQI",
        "wind": "Салхи",
        "humidity": "Чийгшил",
        "sun": "Нар",
        "aqi": "Агаарын чанар (AQI)",
        "forecast": "Ойрын төлөв",
        "source": "ЦУОШГ / weather.ulaach.cc",
    },
    "EN": {
        "title": "Weather & AQI",
        "wind": "Wind",
        "humidity": "Humidity",
        "sun": "Sun",
        "aqi": "Air Quality (AQI)",
        "forecast": "Forecast",
        "source": "NAMEM / weather.ulaach.cc",
    },
    "CN": {
        "title": "蒙古天气与空气质量",
        "wind": "风速",
        "humidity": "湿度",
        "sun": "日出/日落",
        "aqi": "空气质量 (AQI)",
        "forecast": "近期预报",
        "source": "weather.ulaach.cc",
    },
    "JP": {
        "title": "モンゴル気象・大気質",
        "wind": "風速",
        "humidity": "湿度",
        "sun": "日の出/入",
        "aqi": "大気質指数 (AQI)",
        "forecast": "予報",
        "source": "weather.ulaach.cc",
    },
    "KR": {
        "title": "몽골 날씨 및 대기질",
        "wind": "풍속",
        "humidity": "습도",
        "sun": "일출/일몰",
        "aqi": "대기질 지수 (AQI)",
        "forecast": "예보",
        "source": "weather.ulaach.cc",
    },
}


def _ww_text(code: int | None, cloud_nh: int | None = None) -> str:
    if code is not None and code in WW_DESC:
        return WW_DESC[code]
    if cloud_nh is not None:
        if cloud_nh <= 2:
            return "Цэлмэг"
        if cloud_nh <= 6:
            return "Солигдмол үүлтэй"
        return "Үүлэрхэг"
    return "Тогтуун"


def _aqi_badge(aqi: int | None) -> tuple[str, str]:
    if aqi is None:
        return ("—", "#94a3b8")
    if aqi <= 50:
        return ("Цэвэр", "#34d399")
    if aqi <= 100:
        return ("Хэвийн", "#38bdf8")
    if aqi <= 150:
        return ("Бага зэрэг бохирдолтой", "#fbbf24")
    if aqi <= 200:
        return ("Бохирдолтой", "#f97316")
    return ("Маш их бохирдолтой", "#f43f5e")


def _fire_badge(idx: int | None) -> tuple[str, str]:
    if idx is None or idx <= 300:
        return ("Аюулгүй (I зэрэг)", "#34d399")
    if idx <= 1000:
        return ("Бага зэрэг (II зэрэг)", "#38bdf8")
    if idx <= 2500:
        return ("Анхаарах (III зэрэг)", "#fbbf24")
    if idx <= 4000:
        return ("Аюултай (IV зэрэг)", "#f97316")
    return ("Нэн аюултай (V зэрэг)", "#f43f5e")


def extract_visitor_info(request: Request) -> dict[str, Any]:
    h = request.headers
    ip = (
        h.get("cf-connecting-ip")
        or (h.get("x-forwarded-for", "").split(",")[0].strip() if h.get("x-forwarded-for") else "")
        or h.get("x-real-ip")
        or (request.client.host if request.client else "unknown")
    )
    ip_version = "IPv6" if ":" in ip else "IPv4"
    country = h.get("cf-ipcountry") or "MN"
    city = h.get("cf-ipcity") or ""
    region = h.get("cf-region") or ""
    continent = h.get("cf-ipcontinent") or ""
    asn = h.get("cf-asn") or ""
    colo = ""
    ray = h.get("cf-ray") or ""
    if "-" in ray:
        colo = ray.split("-")[-1]
    proto = h.get("x-forwarded-proto") or request.url.scheme or "https"
    ua = h.get("user-agent") or ""
    accept_lang = h.get("accept-language") or ""
    tls_ver = h.get("cf-visitor") or ""

    return {
        "ip": ip,
        "ip_version": ip_version,
        "country": country,
        "city": city or None,
        "region": region or None,
        "continent": continent or None,
        "asn": asn or None,
        "cf_ray": ray or None,
        "cf_edge_colo": colo or None,
        "protocol": proto.upper(),
        "user_agent": ua,
        "accept_language": accept_lang,
        "tls": tls_ver or None,
        "timestamp_ub": datetime.datetime.now(UB_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }


# ==============================================================================
# 1. NETWORK DIAGNOSTICS ENGINE (tool.ulaach.cc)
# ==============================================================================
def _is_safe_public_host(host: str) -> str:
    host = host.strip().lower()
    if "://" in host:
        host = urllib.parse.urlparse(host).hostname or host
    host = host.split("/")[0].split(":")[0].strip()
    if not host or any(c in host for c in " @;&|$`\\\"'"):
        raise HTTPException(status_code=400, detail="Буруу домэйн эсвэл IP хаяг байна.")
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".internal") or host.endswith(".local"):
        raise HTTPException(status_code=400, detail="Дотоод хаяг шалгах боломжгүй.")
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            addr = info[4][0]
            ip_obj = ipaddress.ip_address(addr)
            if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_reserved:
                raise HTTPException(status_code=400, detail="Дотоод болон тусгай сүлжээний IP шалгах хориотой.")
    except socket.gaierror:
        raise HTTPException(status_code=404, detail=f"Домэйн эсвэл IP олдсонгүй: {host}")
    return host


def sync_ip_lookup(target: str) -> dict[str, Any]:
    clean_host = _is_safe_public_host(target)
    resolved_ips = sorted({info[4][0] for info in socket.getaddrinfo(clean_host, None)})
    primary_ip = resolved_ips[0]
    ptr = None
    try:
        ptr = socket.gethostbyaddr(primary_ip)[0]
    except Exception:
        ptr = None

    geo_data: dict[str, Any] = {}
    try:
        url = f"http://ip-api.com/json/{urllib.parse.quote(primary_ip)}?fields=status,message,country,countryCode,regionName,city,isp,org,as,lat,lon,timezone"
        req = urllib.request.Request(url, headers={"User-Agent": "ulaach-tool/1.2"})
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            import json
            parsed = json.loads(raw)
            if parsed.get("status") == "success":
                geo_data = parsed
    except Exception:
        pass

    return {
        "query": target,
        "host": clean_host,
        "ip": primary_ip,
        "resolved_ips": resolved_ips,
        "ptr_record": ptr,
        "country": geo_data.get("country"),
        "country_code": geo_data.get("countryCode"),
        "region": geo_data.get("regionName"),
        "city": geo_data.get("city"),
        "isp": geo_data.get("isp"),
        "org": geo_data.get("org"),
        "asn": geo_data.get("as"),
        "lat": geo_data.get("lat"),
        "lon": geo_data.get("lon"),
        "timezone": geo_data.get("timezone"),
    }


def sync_dns_lookup(domain: str) -> dict[str, Any]:
    import json
    clean_domain = _is_safe_public_host(domain)
    rec_types = ["A", "AAAA", "MX", "NS", "TXT", "CNAME"]
    records: dict[str, list[str]] = {}
    for rtype in rec_types:
        try:
            url = f"https://cloudflare-dns.com/dns-query?name={urllib.parse.quote(clean_domain)}&type={rtype}"
            req = urllib.request.Request(url, headers={"Accept": "application/dns-json", "User-Agent": "ulaach-tool/1.2"})
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
                ans = data.get("Answer") or []
                records[rtype] = [str(a.get("data", "")).strip() for a in ans if a.get("data")]
        except Exception:
            records[rtype] = []
    return {
        "domain": clean_domain,
        "records": records,
        "checked_at": datetime.datetime.now(UB_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }


def sync_ssl_lookup(domain: str) -> dict[str, Any]:
    clean_domain = _is_safe_public_host(domain)
    ctx = ssl.create_default_context()
    t0 = time.perf_counter()
    try:
        with socket.create_connection((clean_domain, 443), timeout=5.0) as sock:
            with ctx.wrap_socket(sock, server_hostname=clean_domain) as ssock:
                latency_ms = round((time.perf_counter() - t0) * 1000, 1)
                cert = ssock.getpeercert()
                cipher = ssock.cipher()
                tls_version = ssock.version()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"SSL холболт амжилтгүй: {exc}")

    subject = dict(x[0] for x in cert.get("subject", []))
    issuer = dict(x[0] for x in cert.get("issuer", []))
    not_before = cert.get("notBefore", "")
    not_after = cert.get("notAfter", "")
    days_remaining = None
    if not_after:
        try:
            exp_dt = datetime.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=datetime.timezone.utc)
            days_remaining = (exp_dt - datetime.datetime.now(datetime.timezone.utc)).days
        except Exception:
            pass

    san_list = [v for k, v in cert.get("subjectAltName", []) if k == "DNS"]
    return {
        "domain": clean_domain,
        "valid": True,
        "days_remaining": days_remaining,
        "subject_cn": subject.get("commonName"),
        "issuer_org": issuer.get("organizationName") or issuer.get("commonName"),
        "issuer_cn": issuer.get("commonName"),
        "not_before": not_before,
        "not_after": not_after,
        "tls_version": tls_version,
        "cipher": cipher[0] if cipher else None,
        "handshake_ms": latency_ms,
        "san_domains": san_list[:15],
    }


def sync_port_check(host: str, port: int) -> dict[str, Any]:
    if port < 1 or port > 65535:
        raise HTTPException(status_code=400, detail="Портын дугаар 1-65535 хооронд байх ёстой.")
    clean_host = _is_safe_public_host(host)
    t0 = time.perf_counter()
    is_open = False
    err_msg = None
    try:
        with socket.create_connection((clean_host, port), timeout=3.5):
            is_open = True
    except Exception as exc:
        err_msg = str(exc)
    latency_ms = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "host": clean_host,
        "port": port,
        "open": is_open,
        "latency_ms": latency_ms,
        "error": err_msg,
    }


# ==============================================================================
# 2. MONGOLIAN DEVELOPER UTILITIES ENGINE (util.ulaach.cc)
# ==============================================================================
_ONES = ["", "нэг", "хоёр", "гурав", "дөрөв", "тав", "зургаа", "долоо", "найм", "ес"]
_ONES_ATTR = ["", "нэгэн", "хоёр", "гурван", "дөрвөн", "таван", "зургаан", "долоон", "найман", "есөн"]
_TENS = ["", "арав", "хорь", "гуч", "дөч", "тарь", "жар", "дал", "ная", "ер"]
_TENS_ATTR = ["", "арван", "хорин", "гучин", "дөчин", "тавин", "жаран", "далан", "наян", "ерэн"]
_SCALES = [
    (10**12, "их наяд"),
    (10**9, "тэрбум"),
    (10**6, "сая"),
    (10**3, "мянга"),
]


def _three_digits_mn(n: int, is_final_group: bool) -> str:
    parts: list[str] = []
    h = n // 100
    rem = n % 100
    t = rem // 10
    u = rem % 10

    if h > 0:
        h_word = "нэг зуун" if h == 1 else f"{_ONES_ATTR[h]} зуун"
        if rem == 0 and is_final_group:
            h_word = "нэг зуу" if h == 1 else f"{_ONES_ATTR[h]} зуу"
        parts.append(h_word)

    if t > 0:
        if u == 0 and is_final_group:
            parts.append(_TENS[t])
        else:
            parts.append(_TENS_ATTR[t])

    if u > 0:
        parts.append(_ONES[u] if is_final_group else _ONES_ATTR[u])

    return " ".join(parts)


def int_to_mn_words(n: int, attributive_end: bool = False) -> str:
    if n == 0:
        return "тэг"
    if n < 0:
        return "хасах " + int_to_mn_words(-n, attributive_end=attributive_end)

    parts: list[str] = []
    rem = n
    for scale_val, scale_name in _SCALES:
        if rem >= scale_val:
            grp = rem // scale_val
            rem = rem % scale_val
            grp_text = _three_digits_mn(grp, is_final_group=False)
            if scale_val == 1000 and rem > 0 and scale_name == "мянга":
                parts.append(f"{grp_text} мянга")
            else:
                parts.append(f"{grp_text} {scale_name}")

    if rem > 0:
        parts.append(_three_digits_mn(rem, is_final_group=not attributive_end))

    return " ".join(parts)


def num2words_mn(amount: str | float | Decimal, currency: str = "MNT") -> dict[str, Any]:
    try:
        dec = Decimal(str(amount).replace(",", "").strip()).quantize(Decimal("0.01"))
    except Exception:
        raise HTTPException(status_code=400, detail="Тоон утга буруу байна.")

    if abs(dec) >= Decimal("1000000000000000"):
        raise HTTPException(status_code=400, detail="Тоо хэт том байна.")

    neg = dec < 0
    abs_dec = abs(dec)
    whole = int(abs_dec)
    frac = int((abs_dec - Decimal(whole)) * 100)

    whole_words_plain = int_to_mn_words(whole, attributive_end=False)
    whole_words_attr = int_to_mn_words(whole, attributive_end=True)
    if neg:
        whole_words_plain = "хасах " + whole_words_plain
        whole_words_attr = "хасах " + whole_words_attr

    cur_upper = (currency or "MNT").strip().upper()
    cur_map = {
        "MNT": ("төгрөг", "мөнгө"),
        "USD": ("ам.доллар", "цент"),
        "EUR": ("евро", "цент"),
        "CNY": ("юань", "фэнь"),
    }
    main_unit, sub_unit = cur_map.get(cur_upper, ("төгрөг", "мөнгө"))

    if frac > 0:
        frac_words = int_to_mn_words(frac, attributive_end=True)
        accounting_text = f"{whole_words_attr} {main_unit} {frac_words} {sub_unit}"
    else:
        accounting_text = f"{whole_words_attr} {main_unit}"

    return {
        "amount": str(dec),
        "integer_part": whole,
        "fraction_part": frac,
        "currency": cur_upper,
        "words_number": whole_words_plain,
        "words_accounting": accounting_text,
        "words_accounting_cap": accounting_text[:1].upper() + accounting_text[1:],
    }


_CYR_TO_LAT: dict[str, str] = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "ye", "ё": "yo",
    "ж": "j", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "ө": "u", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ү": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "sh", "ъ": "", "ы": "y", "ь": "i", "э": "e", "ю": "yu", "я": "ya",
}


def transliterate_mn(text: str, direction: str = "cyr2lat") -> dict[str, str]:
    d = (direction or "cyr2lat").strip().lower()
    if d == "cyr2lat":
        out_chars: list[str] = []
        for ch in text:
            low = ch.lower()
            if low in _CYR_TO_LAT:
                mapped = _CYR_TO_LAT[low]
                if ch.isupper() and mapped:
                    mapped = mapped[0].upper() + mapped[1:]
                out_chars.append(mapped)
            else:
                out_chars.append(ch)
        res = "".join(out_chars)
    else:
        # Basic Latin -> Cyrillic phonetic helper
        s = text
        multi = [
            ("sh", "ш"), ("Sh", "Ш"), ("SH", "Ш"),
            ("ch", "ч"), ("Ch", "Ч"), ("CH", "Ч"),
            ("kh", "х"), ("Kh", "Х"), ("KH", "Х"),
            ("ts", "ц"), ("Ts", "Ц"), ("TS", "Ц"),
            ("yo", "ё"), ("Yo", "Ё"), ("YO", "Ё"),
            ("yu", "ю"), ("Yu", "Ю"), ("YU", "Ю"),
            ("ya", "я"), ("Ya", "Я"), ("YA", "Я"),
            ("ye", "е"), ("Ye", "Е"), ("YE", "Е"),
        ]
        for k, v in multi:
            s = s.replace(k, v)
        single = {
            "a": "а", "b": "б", "v": "в", "g": "г", "d": "д", "e": "э",
            "j": "ж", "z": "з", "i": "и", "k": "к", "l": "л", "m": "м",
            "n": "н", "o": "о", "p": "п", "r": "р", "s": "с", "t": "т",
            "u": "у", "f": "ф", "y": "ы", "w": "в",
        }
        out_c = []
        for ch in s:
            low = ch.lower()
            if low in single:
                m = single[low]
                out_c.append(m.upper() if ch.isupper() else m)
            else:
                out_c.append(ch)
        res = "".join(out_c)

    return {"input": text, "direction": d, "output": res}


_REG_REGION_MAP: dict[str, str] = {
    "А": "Архангай",
    "Б": "Баян-Өлгий",
    "В": "Баянхонгор",
    "Г": "Булган",
    "Д": "Говь-Алтай",
    "Е": "Дорноговь",
    "Ж": "Дорнод",
    "З": "Дундговь",
    "И": "Завхан",
    "Й": "Өвөрхангай",
    "К": "Өмнөговь",
    "Л": "Сүхбаатар",
    "М": "Сэлэнгэ",
    "Н": "Төв",
    "О": "Увс",
    "П": "Ховд",
    "Р": "Хөвсгөл",
    "С": "Хэнтий",
    "Т": "Дархан-Уул",
    "Ф": "Орхон",
    "Х": "Говьсүмбэр",
    "У": "Улаанбаатар",
    "Ч": "Улаанбаатар",
}


def parse_mongolian_regno(regno: str) -> dict[str, Any]:
    s = (regno or "").strip().upper()
    m = re.match(r"^([А-ЯЁӨҮ]{2})(\d{2})(\d{2})(\d{2})(\d)(\d)$", s)
    if not m:
        return {"regno": s, "valid": False, "error": "Регистрийн дугаарын формат буруу (2 кирилл үсэг + 8 цифр байх ёстой)."}

    letters, yy_s, mm_s, dd_s, gender_d, check_d = m.groups()
    yy = int(yy_s)
    mm = int(mm_s)
    dd = int(dd_s)

    if mm > 20:
        year = 2000 + yy
        month = mm - 20
    else:
        year = 1900 + yy
        month = mm

    try:
        birth_date = datetime.date(year, month, dd)
    except ValueError:
        return {"regno": s, "valid": False, "error": "Төрсөн огноо хүчингүй байна."}

    today = datetime.datetime.now(UB_TZ).date()
    age = today.year - birth_date.year - ((today.month, today.day) < (birth_date.month, birth_date.day))
    gender = "Эрэгтэй" if int(gender_d) % 2 == 1 else "Эмэгтэй"
    region = _REG_REGION_MAP.get(letters[0], "Тодорхойгүй бүс")

    return {
        "regno": s,
        "valid": True,
        "letters": letters,
        "birth_place": region,
        "birth_date": birth_date.isoformat(),
        "age": age,
        "gender": gender,
    }


def _common_head(title: str, active_site: str) -> str:
    def _cls(s: str) -> str:
        return "nav-pill active" if s == active_site else "nav-pill"

    return f"""<!DOCTYPE html>
<html lang="mn">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {{
      --bg: #090d16;
      --bg-panel: #0f1523;
      --bg-code: #070a12;
      --border: #1e293b;
      --border-strong: #334155;
      --text: #e2e8f0;
      --muted: #94a3b8;
      --dim: #64748b;
      --accent: #38bdf8;
      --green: #34d399;
      --amber: #fbbf24;
      --rose: #f43f5e;
      --purple: #a78bfa;
      --sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      --mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      font-family: var(--sans);
      color: var(--text);
      background-color: var(--bg);
      background-image:
        linear-gradient(to right, rgba(30, 41, 59, 0.25) 1px, transparent 1px),
        linear-gradient(to bottom, rgba(30, 41, 59, 0.25) 1px, transparent 1px);
      background-size: 32px 32px;
      line-height: 1.55;
      -webkit-font-smoothing: antialiased;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}

    .topbar {{
      position: sticky;
      top: 0;
      z-index: 40;
      backdrop-filter: blur(12px);
      background: rgba(9, 13, 22, 0.88);
      border-bottom: 1px solid var(--border);
    }}
    .topbar-inner {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 12px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 10px;
      font-family: var(--mono);
      font-weight: 700;
      font-size: 14px;
      color: #f8fafc;
      letter-spacing: -0.02em;
      text-decoration: none !important;
    }}
    .brand-dot {{
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--green);
      box-shadow: 0 0 10px var(--green);
    }}
    .nav-links {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .nav-pill {{
      font-family: var(--mono);
      font-size: 12px;
      color: var(--muted);
      padding: 6px 11px;
      border-radius: 6px;
      border: 1px solid transparent;
      transition: all 0.15s;
      text-decoration: none !important;
    }}
    .nav-pill:hover {{
      color: #f8fafc;
      background: rgba(30, 41, 59, 0.65);
      border-color: var(--border-strong);
    }}
    .nav-pill.active {{
      color: var(--green);
      background: rgba(16, 185, 129, 0.12);
      border-color: rgba(16, 185, 129, 0.35);
      font-weight: 600;
    }}

    .wrap {{
      max-width: 1120px;
      margin: 0 auto;
      padding: 28px 24px 72px;
      display: flex;
      flex-direction: column;
      gap: 24px;
    }}
    .hero {{
      background: linear-gradient(180deg, rgba(15, 21, 35, 0.95) 0%, rgba(9, 13, 22, 0.95) 100%);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 24px 28px;
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    .hero h1 {{
      margin: 0;
      font-size: 24px;
      font-weight: 700;
      letter-spacing: -0.025em;
      color: #f8fafc;
    }}
    .hero p {{
      margin: 0;
      font-size: 14px;
      color: var(--muted);
      max-width: 860px;
    }}
    .status-strip {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 4px;
    }}
    .stat-chip {{
      font-family: var(--mono);
      font-size: 12px;
      padding: 5px 11px;
      border-radius: 6px;
      background: var(--bg-code);
      border: 1px solid var(--border);
      color: var(--muted);
    }}
    .stat-chip strong {{ color: #f8fafc; }}

    .section-card {{
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      overflow: hidden;
      box-shadow: 0 8px 28px rgba(0, 0, 0, 0.35);
    }}
    .section-head {{
      padding: 15px 24px;
      border-bottom: 1px solid var(--border);
      background: rgba(7, 10, 18, 0.55);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .section-title {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin: 0;
      font-size: 15px;
      font-weight: 600;
      color: #f8fafc;
    }}
    .step-badge {{
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 700;
      padding: 3px 8px;
      border-radius: 5px;
      background: rgba(16, 185, 129, 0.14);
      color: var(--green);
      border: 1px solid rgba(16, 185, 129, 0.35);
    }}
    .section-meta {{
      font-family: var(--mono);
      font-size: 12px;
      color: var(--dim);
    }}
    .section-body {{
      padding: 22px 24px;
    }}

    .ep-list {{
      display: flex;
      flex-direction: column;
      gap: 10px;
      margin-bottom: 18px;
    }}
    .ep-item {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 11px 14px;
      background: var(--bg-code);
      border: 1px solid var(--border);
      border-radius: 8px;
      flex-wrap: wrap;
    }}
    .ep-left {{
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
      flex-wrap: wrap;
    }}
    .method-get {{
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 700;
      padding: 3px 7px;
      border-radius: 4px;
      background: rgba(16, 185, 129, 0.15);
      color: var(--green);
      border: 1px solid rgba(16, 185, 129, 0.35);
    }}
    .ep-url {{
      font-family: var(--mono);
      font-size: 13px;
      color: var(--accent);
      font-weight: 500;
      word-break: break-all;
    }}
    .ep-desc {{
      font-size: 12.5px;
      color: var(--muted);
    }}

    .code-split {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
    }}
    @media (max-width: 900px) {{
      .code-split {{ grid-template-columns: 1fr; }}
    }}
    .code-pane {{
      background: var(--bg-code);
      border: 1px solid var(--border);
      border-radius: 8px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }}
    .code-pane-bar {{
      padding: 8px 14px;
      background: rgba(15, 23, 42, 0.75);
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-family: var(--mono);
      font-size: 11.5px;
      color: var(--muted);
      gap: 8px;
      flex-wrap: wrap;
    }}
    .copy-btn, .run-btn {{
      font-family: var(--mono);
      font-size: 11.5px;
      padding: 5px 12px;
      border-radius: 5px;
      background: rgba(30, 41, 59, 0.85);
      color: var(--text);
      border: 1px solid var(--border-strong);
      cursor: pointer;
      font-weight: 600;
    }}
    .run-btn {{
      background: rgba(16, 185, 129, 0.16);
      color: var(--green);
      border-color: rgba(16, 185, 129, 0.4);
    }}
    .copy-btn:hover, .run-btn:hover {{
      border-color: var(--accent);
      color: #fff;
    }}
    .inp {{
      background: var(--bg-code);
      border: 1px solid var(--border-strong);
      color: #f8fafc;
      font-family: var(--mono);
      font-size: 12.5px;
      padding: 7px 11px;
      border-radius: 6px;
      outline: none;
    }}
    .inp:focus {{
      border-color: var(--accent);
    }}
    pre {{
      margin: 0;
      padding: 14px;
      font-family: var(--mono);
      font-size: 12.5px;
      line-height: 1.55;
      color: #cbd5e1;
      overflow-x: auto;
    }}

    .table-wrap {{
      overflow-x: auto;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--bg-code);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      padding: 9px 12px;
      border-bottom: 1px solid rgba(30, 41, 59, 0.7);
      text-align: left;
      white-space: nowrap;
    }}
    th {{
      background: rgba(15, 23, 42, 0.85);
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }}
    tbody tr:hover {{
      background: rgba(30, 41, 59, 0.35);
    }}
    .r {{ text-align: right; }}
    .c {{ text-align: center; }}
    .mono {{ font-family: var(--mono); }}
    .bold {{ font-weight: 600; color: #f8fafc; }}
    .muted {{ color: var(--muted); }}
    .code-pill {{
      display: inline-block;
      font-family: var(--mono);
      font-size: 11.5px;
      font-weight: 700;
      padding: 2px 7px;
      border-radius: 4px;
      background: rgba(16, 185, 129, 0.13);
      color: var(--green);
      border: 1px solid rgba(16, 185, 129, 0.3);
    }}
    code {{
      font-family: var(--mono);
      font-size: 12px;
      background: rgba(15, 23, 42, 0.9);
      border: 1px solid var(--border);
      padding: 2px 6px;
      border-radius: 4px;
      color: var(--accent);
    }}

    .svc-grid {{
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 18px;
    }}
    @media (max-width: 900px) {{
      .svc-grid {{ grid-template-columns: 1fr; }}
    }}
    .svc-card {{
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 22px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 16px;
      transition: border-color 0.15s, transform 0.15s;
    }}
    .svc-card:hover {{
      border-color: var(--accent);
      transform: translateY(-2px);
    }}
    .svc-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }}
    .svc-domain {{
      font-family: var(--mono);
      font-size: 14px;
      font-weight: 700;
      color: var(--accent);
    }}
    .svc-live {{
      font-family: var(--mono);
      font-size: 11px;
      padding: 3px 8px;
      border-radius: 999px;
      background: rgba(16, 185, 129, 0.14);
      color: var(--green);
      border: 1px solid rgba(16, 185, 129, 0.35);
    }}
    .svc-title {{
      margin: 8px 0 6px;
      font-size: 17px;
      font-weight: 700;
      color: #f8fafc;
    }}
    .svc-desc {{
      margin: 0;
      font-size: 13px;
      color: var(--muted);
    }}
    .svc-metric {{
      background: var(--bg-code);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px 14px;
      font-family: var(--mono);
      font-size: 12.5px;
      display: flex;
      flex-direction: column;
      gap: 5px;
    }}
    .svc-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 9px 14px;
      border-radius: 7px;
      font-family: var(--mono);
      font-size: 12.5px;
      font-weight: 600;
      background: rgba(56, 189, 248, 0.12);
      color: var(--accent);
      border: 1px solid rgba(56, 189, 248, 0.35);
      text-decoration: none !important;
    }}
    .svc-btn:hover {{
      background: rgba(56, 189, 248, 0.22);
      color: #fff;
    }}
    .footer {{
      text-align: center;
      font-family: var(--mono);
      font-size: 12px;
      color: var(--dim);
      padding-top: 8px;
    }}
  </style>
</head>
<body>
  <header class="topbar">
    <div class="topbar-inner">
      <a class="brand" href="https://ulaach.cc">
        <span class="brand-dot"></span>
        <span>ulaach.cc // Developer Platform</span>
      </a>
      <nav class="nav-links">
        <a class="{_cls('portal')}" href="https://ulaach.cc">Portal (ulaach.cc)</a>
        <a class="{_cls('fx')}" href="https://fx.ulaach.cc">FX Rates (fx.ulaach.cc)</a>
        <a class="{_cls('weather')}" href="https://weather.ulaach.cc">Weather &amp; AQI (weather.ulaach.cc)</a>
        <a class="{_cls('tool')}" href="https://tool.ulaach.cc">IP &amp; Net Tools (tool.ulaach.cc)</a>
        <a class="{_cls('util')}" href="https://util.ulaach.cc">MN Dev Utils (util.ulaach.cc)</a>
      </nav>
    </div>
  </header>
"""


def render_tool_page(request: Request) -> HTMLResponse:
    info = extract_visitor_info(request)
    h = request.headers
    header_rows = []
    for k in sorted(h.keys()):
        if k.lower() in ("cookie", "authorization"):
            continue
        header_rows.append(
            f"<tr><td class='mono' style='color:#38bdf8;'>{html.escape(k)}</td>"
            f"<td class='mono' style='white-space:normal;word-break:break-all;'>{html.escape(h.get(k, ''))}</td></tr>"
        )

    loc_str = " / ".join(x for x in [info["country"], info["city"] or "", info["region"] or ""] if x)

    html_doc = (
        _common_head("tool.ulaach.cc — What Is My IP & Network Diagnostic Suite", "tool")
        + f"""
  <main class="wrap">
    <section class="hero">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;">
        <div>
          <div class="mono" style="font-size:12px;color:var(--green);margin-bottom:4px;">YOUR PUBLIC CONNECTION IP ({html.escape(info['ip_version'])})</div>
          <h1 id="my-ip" class="mono" style="font-size:34px;color:#38bdf8;letter-spacing:-0.02em;">{html.escape(info['ip'])}</h1>
        </div>
        <button class="copy-btn" style="padding:8px 16px;font-size:12.5px;" data-copy="my-ip">Copy IP Address</button>
      </div>
      <p>Хандаж буй хэрэглэгчийн бодит IP хаяг, Cloudflare Edge зангилаа, мөн дурын IP/Домэйн дээр GeoIP/ASN, DNS Records, SSL Certificate болон TCP Port шалгах Developer API.</p>
      <div class="status-strip">
        <span class="stat-chip">IP_VERSION: <strong>{html.escape(info['ip_version'])}</strong></span>
        <span class="stat-chip">LOCATION: <strong>{html.escape(loc_str)}</strong></span>
        <span class="stat-chip">CF_EDGE: <strong>{html.escape(info['cf_edge_colo'] or 'Direct')}</strong></span>
        <span class="stat-chip">PROTOCOL: <strong>{html.escape(info['protocol'])}</strong></span>
        <span class="stat-chip">TIME (UB): <strong>{html.escape(info['timestamp_ub'])}</strong></span>
      </div>
    </section>

    <!-- 01. Interactive Network Diagnostic Workbench -->
    <section class="section-card">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">01</span> Интерактив Сүлжээний Оношилгоо (IP / DNS / SSL / Port)</h2>
        <span class="section-meta">Live Network Probe</span>
      </div>
      <div class="section-body">
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px;">
          <input id="probe-target" class="inp" style="flex:1;min-width:220px;" value="ulaach.cc" placeholder="Домэйн эсвэл IP (ж.нь: ulaach.cc, 8.8.8.8)" />
          <input id="probe-port" class="inp" style="width:95px;" value="443" placeholder="Port" />
          <button class="run-btn" onclick="runProbe('lookup')">1. IP / ISP Lookup</button>
          <button class="run-btn" onclick="runProbe('dns')">2. DNS Records</button>
          <button class="run-btn" onclick="runProbe('ssl')">3. SSL Certificate</button>
          <button class="run-btn" onclick="runProbe('port')">4. TCP Port Check</button>
        </div>
        <div class="code-pane">
          <div class="code-pane-bar">
            <span id="probe-status">GET /dns?domain=ulaach.cc — Дээрх товчнуудаас дарж шалгана уу</span>
            <button class="copy-btn" data-copy="probe-out">Copy JSON</button>
          </div>
          <pre id="probe-out">// Жишээ нь "1. IP / ISP Lookup" эсвэл "3. SSL Certificate" товч дээр дарж бодит хариуг харна уу.</pre>
        </div>
      </div>
    </section>

    <!-- 02. Developer CLI & JSON Endpoints -->
    <section class="section-card">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">02</span> Шууд дуудах Developer CLI &amp; JSON Endpoints</h2>
        <span class="section-meta">Content-Type: application/json</span>
      </div>
      <div class="section-body">
        <div class="ep-list">
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/ip" target="_blank">/ip</a>
            </div>
            <span class="ep-desc">Зөвхөн таны IP хаягийг цэвэр текстээр буцаана (<code>curl -s https://tool.ulaach.cc/ip</code>)</span>
          </div>
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/json" target="_blank">/json</a>
            </div>
            <span class="ep-desc">Таны IP, улс, протокол, Cloudflare Edge болон User-Agent мэдээлэл</span>
          </div>
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/lookup?target=8.8.8.8" target="_blank">/lookup?target=8.8.8.8</a>
            </div>
            <span class="ep-desc">Дурын IP эсвэл домэйны ISP, ASN, Улс/Хот, PTR рекордыг шалгах</span>
          </div>
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/dns?domain=ulaach.cc" target="_blank">/dns?domain=ulaach.cc</a>
            </div>
            <span class="ep-desc">Домэйны <code>A</code>, <code>AAAA</code>, <code>MX</code>, <code>NS</code>, <code>TXT</code>, <code>CNAME</code> рекордууд</span>
          </div>
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/ssl?domain=ulaach.cc" target="_blank">/ssl?domain=ulaach.cc</a>
            </div>
            <span class="ep-desc">SSL сертификатын хүчинтэй хугацаа, үлдсэн хоног, олгогч (Issuer), TLS хувилбар</span>
          </div>
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/port?host=ulaach.cc&port=443" target="_blank">/port?host=ulaach.cc&amp;port=443</a>
            </div>
            <span class="ep-desc">TCP порт нээлттэй эсэх болон холболтын хурд (<code>latency_ms</code>) шалгах</span>
          </div>
        </div>
      </div>
    </section>

    <!-- 03. Connection Telemetry -->
    <section class="section-card">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">03</span> Таны Холболтын Дэлгэрэнгүй &amp; HTTP Headers ({len(header_rows)})</h2>
        <span class="section-meta">GET /headers</span>
      </div>
      <div class="section-body">
        <div class="table-wrap">
          <table>
            <thead>
              <tr><th style="width:260px;">Header / Property</th><th>Value</th></tr>
            </thead>
            <tbody>
              <tr><td class="mono bold" style="color:#34d399;">client.ip</td><td class="mono bold" style="color:#38bdf8;">{html.escape(info['ip'])} ({html.escape(info['ip_version'])})</td></tr>
              {''.join(header_rows)}
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <div class="footer">tool.ulaach.cc • Ulaach Network &amp; IP Diagnostic Suite</div>
  </main>
  <script>
    async function runProbe(kind) {{
      var target = document.getElementById('probe-target').value.trim() || 'ulaach.cc';
      var port = document.getElementById('probe-port').value.trim() || '443';
      var url = '';
      if (kind === 'lookup') url = '/lookup?target=' + encodeURIComponent(target);
      else if (kind === 'dns') url = '/dns?domain=' + encodeURIComponent(target);
      else if (kind === 'ssl') url = '/ssl?domain=' + encodeURIComponent(target);
      else if (kind === 'port') url = '/port?host=' + encodeURIComponent(target) + '&port=' + encodeURIComponent(port);
      document.getElementById('probe-status').textContent = 'Running GET ' + url + ' ...';
      try {{
        var res = await fetch(url);
        var data = await res.json();
        document.getElementById('probe-status').textContent = res.status + ' OK — GET ' + url;
        document.getElementById('probe-out').textContent = JSON.stringify(data, null, 2);
      }} catch (e) {{
        document.getElementById('probe-out').textContent = 'Error: ' + e;
      }}
    }}
    document.querySelectorAll('.copy-btn').forEach(function(btn) {{
      btn.addEventListener('click', function() {{
        var targetId = btn.getAttribute('data-copy');
        var el = document.getElementById(targetId);
        if (!el) return;
        navigator.clipboard.writeText(el.innerText).then(function() {{
          var old = btn.textContent;
          btn.textContent = 'Copied!';
          setTimeout(function() {{ btn.textContent = old; }}, 1200);
        }});
      }});
    }});
  </script>
</body>
</html>"""
    )
    return HTMLResponse(content=html_doc)


def render_util_page() -> HTMLResponse:
    sample_w = num2words_mn("1254800.50", "MNT")
    sample_r = parse_mongolian_regno("УШ95061512")
    sample_t = transliterate_mn("Улаанбаатар хот, Монгол Улс", "cyr2lat")

    html_doc = (
        _common_head("util.ulaach.cc — Монгол Хэл ба Санхүүгийн Developer Utilities API", "util")
        + f"""
  <main class="wrap">
    <section class="hero">
      <div class="mono" style="font-size:12px;color:var(--green);">MONGOLIAN DEVELOPER &amp; FINTECH UTILITIES</div>
      <h1>Монгол Тоог Үсгээр Бичих, Галиглах ба РД Шалгах API</h1>
      <p>Санхүү, нягтлан бодох бүртгэл, ERP системүүдэд мөнгөн дүнг монгол бичвэр рүү хөрвүүлэх (<code>/num2words</code>), Кирилл ↔ Латин галиглах (<code>/translit</code>), Регистрийн дугаар задлах (<code>/regno</code>) нээлттэй JSON сервис.</p>
      <div class="status-strip">
        <span class="stat-chip">NUM2WORDS: <strong>Мөнгөн дүн → Үсгээр (MNT, USD, EUR, CNY)</strong></span>
        <span class="stat-chip">TRANSLIT: <strong>Кирилл ↔ Латин</strong></span>
        <span class="stat-chip">REGNO_PARSER: <strong>Төрсөн огноо, Нас, Хүйс, Аймаг</strong></span>
      </div>
    </section>

    <!-- 01. Interactive Sandbox -->
    <section class="section-card">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">01</span> Шууд Турших Талбар (Interactive Sandbox)</h2>
        <span class="section-meta">Live Execution</span>
      </div>
      <div class="section-body">
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:16px;">
          <div style="background:var(--bg-code);border:1px solid var(--border);border-radius:8px;padding:14px;display:flex;flex-direction:column;gap:8px;">
            <div class="mono bold" style="font-size:12px;color:var(--green);">1. Мөнгөн дүнг үсгээр (/num2words)</div>
            <div style="display:flex;gap:6px;">
              <input id="u-amount" class="inp" style="flex:1;" value="1254800.50" />
              <select id="u-cur" class="inp" style="width:78px;">
                <option value="MNT">MNT</option>
                <option value="USD">USD</option>
                <option value="EUR">EUR</option>
                <option value="CNY">CNY</option>
              </select>
            </div>
            <button class="run-btn" onclick="testUtil('num2words')">Хөрвүүлэх →</button>
          </div>

          <div style="background:var(--bg-code);border:1px solid var(--border);border-radius:8px;padding:14px;display:flex;flex-direction:column;gap:8px;">
            <div class="mono bold" style="font-size:12px;color:var(--accent);">2. Кирилл ↔ Латин (/translit)</div>
            <input id="u-text" class="inp" value="Улаанбаатар хот, Монгол Улс" />
            <div style="display:flex;gap:6px;">
              <button class="run-btn" style="flex:1;" onclick="testUtil('cyr2lat')">Кирилл → Латин</button>
              <button class="run-btn" style="flex:1;" onclick="testUtil('lat2cyr')">Латин → Кирилл</button>
            </div>
          </div>

          <div style="background:var(--bg-code);border:1px solid var(--border);border-radius:8px;padding:14px;display:flex;flex-direction:column;gap:8px;">
            <div class="mono bold" style="font-size:12px;color:var(--amber);">3. Регистрийн дугаар (/regno)</div>
            <input id="u-regno" class="inp" value="УШ95061512" />
            <button class="run-btn" onclick="testUtil('regno')">РД задлах →</button>
          </div>
        </div>

        <div class="code-pane">
          <div class="code-pane-bar">
            <span id="u-status">200 OK — GET /num2words?amount=1254800.50&amp;currency=MNT</span>
            <button class="copy-btn" data-copy="u-out">Copy JSON</button>
          </div>
          <pre id="u-out">{html.escape(
f'''{{
  "amount": "{sample_w['amount']}",
  "currency": "MNT",
  "words_number": "{sample_w['words_number']}",
  "words_accounting": "{sample_w['words_accounting']}",
  "words_accounting_cap": "{sample_w['words_accounting_cap']}"
}}'''
          )}</pre>
        </div>
      </div>
    </section>

    <!-- 02. API Endpoints -->
    <section class="section-card">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">02</span> Шууд дуудах JSON Endpoint-ууд</h2>
        <span class="section-meta">Content-Type: application/json; charset=utf-8</span>
      </div>
      <div class="section-body">
        <div class="ep-list">
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/num2words?amount=1254800.50&currency=MNT" target="_blank">/num2words?amount=1254800.50&amp;currency=MNT</a>
            </div>
            <span class="ep-desc">Тоо болон мөнгөн дүнг монгол үсгээр бичих (Нэхэмжлэх, Зарлагын баримт, Гэрээнд)</span>
          </div>
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/translit?text=Улаанбаатар&dir=cyr2lat" target="_blank">/translit?text=Улаанбаатар&amp;dir=cyr2lat</a>
            </div>
            <span class="ep-desc">Кирилл текстийг Латин руу (<code>dir=cyr2lat</code>) эсвэл Латиныг Кирилл рүү (<code>dir=lat2cyr</code>) хөрвүүлэх</span>
          </div>
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/regno?regno=УШ95061512" target="_blank">/regno?regno=УШ95061512</a>
            </div>
            <span class="ep-desc">Монгол Улсын иргэний регистрийн дугаараас төрсөн аймаг/хот, огноо, нас, хүйс тодорхойлох</span>
          </div>
        </div>

        <div class="code-split">
          <div class="code-pane">
            <div class="code-pane-bar"><span>GET /regno?regno=УШ95061512</span></div>
            <pre>{html.escape(
f'''{{
  "regno": "УШ95061512",
  "valid": true,
  "birth_place": "{sample_r['birth_place']}",
  "birth_date": "{sample_r['birth_date']}",
  "age": {sample_r['age']},
  "gender": "{sample_r['gender']}"
}}'''
            )}</pre>
          </div>
          <div class="code-pane">
            <div class="code-pane-bar"><span>GET /translit?text=Улаанбаатар хот, Монгол Улс</span></div>
            <pre>{html.escape(
f'''{{
  "input": "{sample_t['input']}",
  "direction": "cyr2lat",
  "output": "{sample_t['output']}"
}}'''
            )}</pre>
          </div>
        </div>
      </div>
    </section>

    <div class="footer">util.ulaach.cc • Mongolian Language &amp; Accounting Developer Utilities</div>
  </main>
  <script>
    async function testUtil(kind) {{
      var url = '';
      if (kind === 'num2words') {{
        var amt = document.getElementById('u-amount').value.trim();
        var cur = document.getElementById('u-cur').value;
        url = '/num2words?amount=' + encodeURIComponent(amt) + '&currency=' + encodeURIComponent(cur);
      }} else if (kind === 'cyr2lat' || kind === 'lat2cyr') {{
        var txt = document.getElementById('u-text').value;
        url = '/translit?text=' + encodeURIComponent(txt) + '&dir=' + kind;
      }} else if (kind === 'regno') {{
        var r = document.getElementById('u-regno').value.trim();
        url = '/regno?regno=' + encodeURIComponent(r);
      }}
      document.getElementById('u-status').textContent = 'Running GET ' + url + ' ...';
      var res = await fetch(url);
      var data = await res.json();
      document.getElementById('u-status').textContent = res.status + ' OK — GET ' + url;
      document.getElementById('u-out').textContent = JSON.stringify(data, null, 2);
    }}
    document.querySelectorAll('.copy-btn').forEach(function(btn) {{
      btn.addEventListener('click', function() {{
        var targetId = btn.getAttribute('data-copy');
        var el = document.getElementById(targetId);
        if (!el) return;
        navigator.clipboard.writeText(el.innerText).then(function() {{
          var old = btn.textContent;
          btn.textContent = 'Copied!';
          setTimeout(function() {{ btn.textContent = old; }}, 1200);
        }});
      }});
    }});
  </script>
</body>
</html>"""
    )
    return HTMLResponse(content=html_doc)


# ==============================================================================
# 3. WEATHER & AQI HUB + WIDGET + SUNRISE + FIRE RISK (weather.ulaach.cc)
# ==============================================================================
async def fetch_weather_hub_data(wx_pool: asyncpg.Pool) -> dict[str, Any]:
    async with wx_pool.acquire() as conn:
        hubs = await conn.fetch(
            """
            SELECT
                s.sid,
                s.aimag_name,
                s.sum_name,
                s.sta_name,
                s.sta_28,
                s.elev,
                o.obs_time AT TIME ZONE 'Asia/Ulaanbaatar' AS obs_time_ub,
                o.temp,
                o.temp_min,
                o.temp_max,
                o.humidity,
                o.wind_speed,
                o.wind_dir,
                o.pressure_slp,
                o.precip,
                o.ww,
                o.cloud_nh,
                sr.sunrise,
                sr.sunset,
                sr.day_length_min
            FROM wx.stations s
            LEFT JOIN LATERAL (
                SELECT *
                FROM wx.obs_data d
                WHERE d.sid = s.sid
                ORDER BY d.obs_time DESC
                LIMIT 1
            ) o ON true
            LEFT JOIN wx.v_sunrise_today sr ON sr.sid = s.sid
            WHERE s.sta_28 <> 0
            ORDER BY s.sta_28 ASC, s.sid ASC
            """
        )

        aqi_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (sid)
                sid,
                sname,
                aqi,
                pm25_value,
                pm10_value,
                no2_value,
                so2_value,
                obs_time AT TIME ZONE 'Asia/Ulaanbaatar' AS obs_time_ub
            FROM wx.aqi_data
            WHERE obs_time > NOW() - INTERVAL '48 hours'
            ORDER BY sid, obs_time DESC
            """
        )

        ub_fore = await conn.fetch(
            """
            SELECT DISTINCT ON (f_time)
                f_time AT TIME ZONE 'Asia/Ulaanbaatar' AS f_time_ub,
                pre_code,
                temp,
                temp_feels,
                precip_prob,
                wind
            FROM wx.fore_3hours
            WHERE sid = 292 AND f_time >= NOW() - INTERVAL '3 hours'
            ORDER BY f_time ASC, issued_at DESC
            LIMIT 8
            """
        )

        fire_rows = await conn.fetch(
            """
            SELECT aimag_name, sum_name, obs_date, idx, lat, lon
            FROM wx.tuimer
            WHERE obs_date = (SELECT MAX(obs_date) FROM wx.tuimer)
              AND idx > 0
            ORDER BY idx DESC
            LIMIT 15
            """
        )

        bulletins = await conn.fetch(
            """
            SELECT api_id, title, author, created AT TIME ZONE 'Asia/Ulaanbaatar' AS created_ub, LEFT(body_text, 450) AS summary
            FROM wx.content
            ORDER BY created DESC
            LIMIT 4
            """
        )

        stats = await conn.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM wx.stations) AS total_stations,
                (SELECT COUNT(*) FROM wx.obs_data) AS total_obs,
                (SELECT MAX(obs_time AT TIME ZONE 'Asia/Ulaanbaatar') FROM wx.obs_data) AS latest_obs_ub
            """
        )

    hub_list = []
    for r in hubs:
        hub_list.append(
            {
                "sid": r["sid"],
                "aimag": r["aimag_name"],
                "sum": r["sum_name"],
                "station": r["sta_name"],
                "elev_m": r["elev"],
                "obs_time": r["obs_time_ub"].strftime("%Y-%m-%d %H:%M") if r["obs_time_ub"] else None,
                "temp_c": round(float(r["temp"]), 1) if r["temp"] is not None else None,
                "temp_min_c": round(float(r["temp_min"]), 1) if r["temp_min"] is not None else None,
                "temp_max_c": round(float(r["temp_max"]), 1) if r["temp_max"] is not None else None,
                "humidity_pct": r["humidity"],
                "wind_ms": round(float(r["wind_speed"]), 1) if r["wind_speed"] is not None else None,
                "pressure_hpa": round(float(r["pressure_slp"]), 1) if r["pressure_slp"] is not None else None,
                "condition": _ww_text(r["ww"], r["cloud_nh"]),
                "sunrise": r["sunrise"].strftime("%H:%M") if r["sunrise"] else None,
                "sunset": r["sunset"].strftime("%H:%M") if r["sunset"] else None,
                "day_length_min": r["day_length_min"],
            }
        )

    aqi_list = []
    for r in aqi_rows:
        status_label, color = _aqi_badge(r["aqi"])
        aqi_list.append(
            {
                "sid": r["sid"],
                "station": r["sname"],
                "aqi": r["aqi"],
                "status": status_label,
                "color": color,
                "pm25": round(float(r["pm25_value"]), 1) if r["pm25_value"] is not None else None,
                "pm10": round(float(r["pm10_value"]), 1) if r["pm10_value"] is not None else None,
                "obs_time": r["obs_time_ub"].strftime("%Y-%m-%d %H:%M") if r["obs_time_ub"] else None,
            }
        )
    aqi_list.sort(key=lambda x: (x["aqi"] is None, -(x["aqi"] or 0)))

    fore_list = []
    for r in ub_fore:
        fore_list.append(
            {
                "time_ub": r["f_time_ub"].strftime("%m-%d %H:%M") if r["f_time_ub"] else "",
                "temp_c": round(float(r["temp"]), 1) if r["temp"] is not None else None,
                "feels_c": round(float(r["temp_feels"]), 1) if r["temp_feels"] is not None else None,
                "wind_ms": round(float(r["wind"]), 1) if r["wind"] is not None else None,
                "precip_prob": r["precip_prob"],
                "condition": _ww_text(r["pre_code"]),
            }
        )

    fire_list = []
    for r in fire_rows:
        lbl, col = _fire_badge(r["idx"])
        fire_list.append(
            {
                "aimag": r["aimag_name"],
                "sum": r["sum_name"],
                "date": r["obs_date"].isoformat() if r["obs_date"] else None,
                "index": r["idx"],
                "level": lbl,
                "color": col,
                "lat": r["lat"],
                "lon": r["lon"],
            }
        )

    bulletin_list = []
    for r in bulletins:
        bulletin_list.append(
            {
                "id": r["api_id"],
                "title": r["title"],
                "created_ub": r["created_ub"].strftime("%Y-%m-%d %H:%M") if r["created_ub"] else "",
                "summary": (r["summary"] or "").strip(),
            }
        )

    return {
        "total_stations": stats["total_stations"] if stats else 321,
        "total_obs": stats["total_obs"] if stats else 0,
        "latest_obs_ub": stats["latest_obs_ub"].strftime("%Y-%m-%d %H:%M") if stats and stats["latest_obs_ub"] else "—",
        "hubs": hub_list,
        "aqi": aqi_list,
        "ub_forecast": fore_list,
        "fire_risk": fire_list,
        "bulletins": bulletin_list,
    }


async def render_weather_widget(
    wx_pool: asyncpg.Pool,
    sid: int = 292,
    lang: str = "MN",
    theme: str = "dark",
) -> HTMLResponse:
    lg = (lang or "MN").strip().upper()
    if lg not in WX_WIDGET_I18N:
        lg = "MN"
    labels = WX_WIDGET_I18N[lg]
    is_light = (theme or "").strip().lower() == "light"

    async with wx_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                s.sid, s.aimag_name, s.sta_name,
                o.temp, o.temp_min, o.temp_max, o.humidity, o.wind_speed, o.ww, o.cloud_nh,
                o.obs_time AT TIME ZONE 'Asia/Ulaanbaatar' AS obs_time_ub,
                sr.sunrise, sr.sunset
            FROM wx.stations s
            LEFT JOIN LATERAL (
                SELECT * FROM wx.obs_data d WHERE d.sid = s.sid ORDER BY d.obs_time DESC LIMIT 1
            ) o ON true
            LEFT JOIN wx.v_sunrise_today sr ON sr.sid = s.sid
            WHERE s.sid = $1
            """,
            sid,
        )
        aqi_row = await conn.fetchrow(
            """
            SELECT ROUND(AVG(aqi))::int AS avg_aqi
            FROM (
                SELECT DISTINCT ON (sid) aqi
                FROM wx.aqi_data
                WHERE sid LIKE '12%' AND obs_time > NOW() - INTERVAL '12 hours'
                ORDER BY sid, obs_time DESC
            ) t
            """
        )
        fore_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (f_time)
                f_time AT TIME ZONE 'Asia/Ulaanbaatar' AS f_time_ub,
                temp, pre_code
            FROM wx.fore_3hours
            WHERE sid = $1 AND f_time >= NOW()
            ORDER BY f_time ASC, issued_at DESC
            LIMIT 3
            """,
            sid,
        )

    sta_name = row["sta_name"] if row else "Улаанбаатар"
    temp_str = f"{float(row['temp']):+.1f}°C" if row and row["temp"] is not None else "—"
    cond_str = _ww_text(row["ww"] if row else None, row["cloud_nh"] if row else None)
    wind_str = f"{float(row['wind_speed'] or 0):.1f} m/s"
    hum_str = f"{row['humidity'] if row and row['humidity'] is not None else '—'}%"
    sun_str = (
        f"{row['sunrise'].strftime('%H:%M')} / {row['sunset'].strftime('%H:%M')}"
        if row and row["sunrise"] and row["sunset"]
        else "—"
    )
    aqi_val = aqi_row["avg_aqi"] if aqi_row and aqi_row["avg_aqi"] is not None else None
    aqi_lbl, aqi_col = _aqi_badge(aqi_val)

    bg = "#ffffff" if is_light else "#0f1523"
    panel = "#f8fafc" if is_light else "#090d16"
    border = "#e2e8f0" if is_light else "#1e293b"
    text = "#0f172a" if is_light else "#f8fafc"
    muted = "#64748b" if is_light else "#94a3b8"

    fore_items = []
    for f in fore_rows:
        tm = f["f_time_ub"].strftime("%H:%M") if f["f_time_ub"] else ""
        tp = f"{float(f['temp']):+.0f}°C" if f["temp"] is not None else "—"
        fore_items.append(
            f"<div style='flex:1;background:{panel};border:1px solid {border};border-radius:6px;padding:5px;text-align:center;'>"
            f"<div style='font-size:10px;color:{muted};font-family:monospace;'>{tm}</div>"
            f"<div style='font-size:12.5px;font-weight:700;color:#38bdf8;font-family:monospace;margin-top:2px;'>{tp}</div>"
            f"</div>"
        )

    w_html = f"""<!DOCTYPE html>
<html lang="{lg.lower()}">
<head>
  <meta charset="utf-8" />
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: transparent;
      color: {text};
      font-size: 12px;
    }}
    .card {{
      width: 100%;
      background: {bg};
      border: 1px solid {border};
      border-radius: 10px;
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }}
    .head {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid {border};
      padding-bottom: 7px;
    }}
    .title {{ font-weight: 700; font-size: 12.5px; display: flex; align-items: center; gap: 6px; }}
    .dot {{ width: 7px; height: 7px; border-radius: 50%; background: #10b981; }}
    .main-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .temp {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 26px;
      font-weight: 700;
      color: #38bdf8;
    }}
    .cond {{ font-size: 12px; color: {muted}; margin-top: 2px; }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
      background: {panel};
      border: 1px solid {border};
      border-radius: 7px;
      padding: 8px;
      font-size: 11px;
    }}
    .kv {{ display: flex; justify-content: space-between; }}
    .k {{ color: {muted}; }}
    .v {{ font-family: ui-monospace, monospace; font-weight: 600; }}
    .fore {{ display: flex; gap: 6px; }}
    .foot {{
      display: flex;
      justify-content: space-between;
      font-size: 10px;
      color: {muted};
      border-top: 1px solid {border};
      padding-top: 6px;
    }}
    .foot a {{ color: #10b981; text-decoration: none; font-weight: 600; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="head">
      <span class="title"><span class="dot"></span>{html.escape(sta_name)}</span>
      <span style="font-size:10.5px;color:{muted};">{html.escape(labels['title'])}</span>
    </div>
    <div class="main-row">
      <div>
        <div class="temp">{html.escape(temp_str)}</div>
        <div class="cond">{html.escape(cond_str)}</div>
      </div>
      <div style="text-align:right;">
        <div style="font-size:10.5px;color:{muted};">{html.escape(labels['aqi'])}</div>
        <div style="font-family:monospace;font-size:18px;font-weight:700;color:{aqi_col};">{aqi_val if aqi_val is not None else '—'}</div>
        <div style="font-size:10px;color:{aqi_col};font-weight:600;">{html.escape(aqi_lbl)}</div>
      </div>
    </div>
    <div class="grid">
      <div class="kv"><span class="k">{html.escape(labels['wind'])}:</span><span class="v">{html.escape(wind_str)}</span></div>
      <div class="kv"><span class="k">{html.escape(labels['humidity'])}:</span><span class="v">{html.escape(hum_str)}</span></div>
      <div class="kv" style="grid-column:span 2;"><span class="k">{html.escape(labels['sun'])}:</span><span class="v">{html.escape(sun_str)}</span></div>
    </div>
    <div class="fore">
      {''.join(fore_items)}
    </div>
    <div class="foot">
      <span>{html.escape(labels['source'])}</span>
      <a href="https://weather.ulaach.cc" target="_blank">weather.ulaach.cc</a>
    </div>
  </div>
</body>
</html>"""
    return HTMLResponse(content=w_html)


async def render_weather_page(wx_pool: asyncpg.Pool) -> HTMLResponse:
    data = await fetch_weather_hub_data(wx_pool)
    ub = next((x for x in data["hubs"] if x["sid"] == 292), data["hubs"][0] if data["hubs"] else {})

    hub_rows = []
    for h in data["hubs"]:
        t_val = f"{h['temp_c']:+.1f}°C" if h["temp_c"] is not None else "—"
        t_color = "#fbbf24" if (h["temp_c"] or 0) > 0 else "#38bdf8"
        tmin_max = f"{h['temp_min_c'] if h['temp_min_c'] is not None else '—'}° / {h['temp_max_c'] if h['temp_max_c'] is not None else '—'}°"
        sun_col = f"{h['sunrise']} – {h['sunset']}" if h.get("sunrise") and h.get("sunset") else "—"
        hub_rows.append(
            f"<tr>"
            f"<td class='c'><span class='code-pill'>{h['sid']}</span></td>"
            f"<td class='bold'>{html.escape(h['station'])}</td>"
            f"<td class='muted'>{html.escape(h['aimag'])}</td>"
            f"<td class='r mono bold' style='color:{t_color};'>{t_val}</td>"
            f"<td class='r mono muted'>{tmin_max}</td>"
            f"<td class='r mono'>{h['humidity_pct'] if h['humidity_pct'] is not None else '—'}%</td>"
            f"<td class='r mono' style='color:#34d399;'>{h['wind_ms'] if h['wind_ms'] is not None else '—'} м/с</td>"
            f"<td>{html.escape(h['condition'])}</td>"
            f"<td class='c mono muted'>{html.escape(sun_col)}</td>"
            f"<td class='c mono muted'>{html.escape(h['obs_time'] or '—')}</td>"
            f"</tr>"
        )

    aqi_rows = []
    for a in data["aqi"][:25]:
        aqi_rows.append(
            f"<tr>"
            f"<td class='c mono muted'>{html.escape(str(a['sid']))}</td>"
            f"<td class='bold'>{html.escape(a['station'] or '—')}</td>"
            f"<td class='r mono bold' style='color:{a['color']};'>{a['aqi'] if a['aqi'] is not None else '—'}</td>"
            f"<td><span style='color:{a['color']};font-weight:600;'>{html.escape(a['status'])}</span></td>"
            f"<td class='r mono'>{a['pm25'] if a['pm25'] is not None else '—'}</td>"
            f"<td class='r mono'>{a['pm10'] if a['pm10'] is not None else '—'}</td>"
            f"<td class='c mono muted'>{html.escape(a['obs_time'] or '—')}</td>"
            f"</tr>"
        )

    fire_rows_html = []
    for fr in data["fire_risk"][:12]:
        fire_rows_html.append(
            f"<tr>"
            f"<td class='bold'>{html.escape(fr['aimag'])}</td>"
            f"<td>{html.escape(fr['sum'])}</td>"
            f"<td class='r mono bold' style='color:{fr['color']};'>{fr['index']:,}</td>"
            f"<td><span style='color:{fr['color']};font-weight:600;'>{html.escape(fr['level'])}</span></td>"
            f"<td class='c mono muted'>{html.escape(fr['date'] or '—')}</td>"
            f"</tr>"
        )

    fore_cards = []
    for f in data["ub_forecast"]:
        t_str = f"{f['temp_c']:+.0f}°C" if f["temp_c"] is not None else "—"
        fore_cards.append(
            f"<div style='background:var(--bg-code);border:1px solid var(--border);border-radius:8px;padding:12px;text-align:center;'>"
            f"<div class='mono muted' style='font-size:11.5px;'>{html.escape(f['time_ub'])}</div>"
            f"<div class='mono bold' style='font-size:18px;color:#38bdf8;margin:6px 0;'>{t_str}</div>"
            f"<div style='font-size:12px;color:var(--text);'>{html.escape(f['condition'])}</div>"
            f"<div class='mono muted' style='font-size:11px;margin-top:4px;'>Салхи {f['wind_ms']} м/с</div>"
            f"</div>"
        )

    ub_temp_str = f"{ub.get('temp_c'):+.1f}°C" if ub.get("temp_c") is not None else "—"
    ub_sun_str = f"{ub.get('sunrise', '07:19')} – {ub.get('sunset', '17:54')}"

    html_doc = (
        _common_head("weather.ulaach.cc — Монгол орны Цаг агаар, Нар мандах & AQI Developer API", "weather")
        + f"""
  <main class="wrap">
    <section class="hero">
      <h1>Монгол орны Цаг агаар, Нар мандах ба Агаарын чанарын Developer API</h1>
      <p>Монгол улсын 21 аймаг, {data['total_stations']} сум/станцын цаг тутмын бодит ажиглалт, нар мандах/жаргах цаг, ой хээрийн түймрийн эрсдэл, агаарын чанар (AQI) болон олон хэлний веб виджет.</p>
      <div class="status-strip">
        <span class="stat-chip">ULAANBAATAR: <strong>{ub_temp_str} ({html.escape(ub.get('condition', 'Тогтуун'))}, салхи {ub.get('wind_ms', '—')} м/с)</strong></span>
        <span class="stat-chip">SUNRISE/SUNSET: <strong>{html.escape(ub_sun_str)}</strong></span>
        <span class="stat-chip">STATIONS: <strong>{data['total_stations']} станц</strong></span>
        <span class="stat-chip">LAST_OBS (UB): <strong>{html.escape(data['latest_obs_ub'])}</strong></span>
      </div>
    </section>

    <!-- 01. Endpoints & Forecast -->
    <section class="section-card">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">01</span> Шууд дуудах Цаг агаарын JSON Endpoint-ууд ба IFrame Виджет</h2>
        <span class="section-meta">Content-Type: application/json; charset=utf-8</span>
      </div>
      <div class="section-body">
        <div class="ep-list">
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/weather.json" target="_blank">/weather.json</a>
            </div>
            <span class="ep-desc">Улаанбаатар ба 21 аймгийн төвийн (28 гол станц) цаг агаар + нар мандах/жаргах цаг</span>
          </div>
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/aqi.json" target="_blank">/aqi.json</a>
            </div>
            <span class="ep-desc">Улаанбаатар ба орон нутгийн агаарын чанарын индекс (AQI, PM2.5, PM10, SO2, NO2)</span>
          </div>
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/forecast.json?sid=292" target="_blank">/forecast.json?sid=292</a>
            </div>
            <span class="ep-desc">3 цаг тутмын болон өдрийн урьдчилсан мэдээ</span>
          </div>
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/fire-risk.json" target="_blank">/fire-risk.json</a>
            </div>
            <span class="ep-desc">Ой, хээрийн түймрийн эрсдэлийн индекс ба цаг агаарын сэрэмжлүүлэг мэдээ</span>
          </div>
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/weather.html?sid=292&lang=MN&theme=dark" target="_blank">/weather.html?sid=292&amp;lang=MN&amp;theme=dark</a>
            </div>
            <span class="ep-desc">Вэб сайтад шигтгэдэг олон хэлний (<code>MN/EN/CN/JP/KR</code>) Цаг агаар + AQI IFrame виджет</span>
          </div>
        </div>

        <div style="display:grid;grid-template-columns:1fr 270px;gap:18px;align-items:start;margin-top:16px;">
          <div>
            <div class="mono muted" style="font-size:12px;margin-bottom:10px;">УЛААНБААТАР ХОТЫН ОЙРЫН ЦАГУУДЫН УРЬДЧИЛСАН МЭДЭЭ (SID=292)</div>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;">
              {''.join(fore_cards)}
            </div>
          </div>
          <div>
            <div class="mono muted" style="font-size:11.5px;margin-bottom:6px;">IFRAME WIDGET (/weather.html)</div>
            <iframe style="width:270px;height:255px;border:none;border-radius:10px;overflow:hidden;" src="/weather.html?sid=292&lang=MN&theme=dark"></iframe>
          </div>
        </div>
      </div>
    </section>

    <!-- 02. 28 Hub Stations with Sunrise/Sunset -->
    <section class="section-card">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">02</span> 21 Аймаг ба Гол Станцуудын Бодит Ажиглалт + Нар мандах/жаргах</h2>
        <span class="section-meta">Шинэчлэгдсэн: {html.escape(data['latest_obs_ub'])} (GET /weather.json)</span>
      </div>
      <div class="section-body">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th class="c">SID</th>
                <th>Станц</th>
                <th>Аймаг / Нийслэл</th>
                <th class="r">Одоогийн хэм</th>
                <th class="r">Доод / Дээд</th>
                <th class="r">Чийгшил</th>
                <th class="r">Салхи</th>
                <th>Төлөв</th>
                <th class="c">Нар мандах / жаргах</th>
                <th class="c">Ажигласан цаг</th>
              </tr>
            </thead>
            <tbody>
              {''.join(hub_rows)}
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- 03. AQI Table -->
    <section class="section-card">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">03</span> Агаарын Чанарын Индекс — AQI / PM2.5 / PM10</h2>
        <span class="section-meta">GET /aqi.json</span>
      </div>
      <div class="section-body">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th class="c">Код</th>
                <th>Байршил / Станц</th>
                <th class="r">AQI</th>
                <th>Түвшин</th>
                <th class="r">PM2.5 (μg/m³)</th>
                <th class="r">PM10 (μg/m³)</th>
                <th class="c">Хэмжсэн цаг</th>
              </tr>
            </thead>
            <tbody>
              {''.join(aqi_rows)}
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- 04. Fire Risk Table -->
    <section class="section-card">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">04</span> Ой, Хээрийн Түймрийн Эрсдэл ба Хуурайшилтын Индекс</h2>
        <span class="section-meta">GET /fire-risk.json</span>
      </div>
      <div class="section-body">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Аймаг</th>
                <th>Сум / Станц</th>
                <th class="r">Хуурайшилтын индекс</th>
                <th>Зэрэглэл</th>
                <th class="c">Огноо</th>
              </tr>
            </thead>
            <tbody>
              {''.join(fire_rows_html)}
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <div class="footer">weather.ulaach.cc • Real-time Meteorological, Sunrise &amp; AQI Data API</div>
  </main>
</body>
</html>"""
    )
    return HTMLResponse(content=html_doc)


# ==============================================================================
# 4. CENTRAL PORTAL (ulaach.cc)
# ==============================================================================
async def render_portal_page(request: Request, fx_pool: asyncpg.Pool, wx_pool: asyncpg.Pool) -> HTMLResponse:
    visitor = extract_visitor_info(request)

    usd_rate = "—"
    eur_rate = "—"
    cny_rate = "—"
    mnb_date = "—"
    try:
        async with fx_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT code, rate, rate_date
                FROM mnb_rates
                WHERE rate_date = (SELECT MAX(rate_date) FROM mnb_rates)
                  AND code IN ('USD', 'EUR', 'CNY')
                """
            )
            for r in rows:
                mnb_date = r["rate_date"].isoformat()
                val = f"{float(r['rate']):,.2f} ₮"
                if r["code"] == "USD":
                    usd_rate = val
                elif r["code"] == "EUR":
                    eur_rate = val
                elif r["code"] == "CNY":
                    cny_rate = val
    except Exception:
        pass

    ub_temp = "—"
    ub_wind = "—"
    ub_cond = "Тогтуун"
    ub_aqi = "—"
    try:
        async with wx_pool.acquire() as conn:
            wrow = await conn.fetchrow(
                """
                SELECT temp, wind_speed, ww, cloud_nh
                FROM wx.obs_data
                WHERE sid = 292
                ORDER BY obs_time DESC
                LIMIT 1
                """
            )
            if wrow and wrow["temp"] is not None:
                ub_temp = f"{float(wrow['temp']):+.1f}°C"
                ub_wind = f"{float(wrow['wind_speed'] or 0):.1f} м/с"
                ub_cond = _ww_text(wrow["ww"], wrow["cloud_nh"])
            arow = await conn.fetchrow(
                """
                SELECT ROUND(AVG(aqi))::int AS avg_aqi
                FROM (
                    SELECT DISTINCT ON (sid) aqi
                    FROM wx.aqi_data
                    WHERE sid LIKE '12%' AND obs_time > NOW() - INTERVAL '12 hours'
                    ORDER BY sid, obs_time DESC
                ) t
                """
            )
            if arow and arow["avg_aqi"] is not None:
                ub_aqi = str(arow["avg_aqi"])
    except Exception:
        pass

    html_doc = (
        _common_head("ulaach.cc — Open Developer APIs & Public Data Platform", "portal")
        + f"""
  <main class="wrap">
    <section class="hero">
      <div class="mono" style="font-size:12px;color:var(--green);">ULAACH OPEN DATA &amp; DEVELOPER ECOSYSTEM</div>
      <h1>Нээлттэй Өгөгдөл ба Developer API Платформ</h1>
      <p>Монголбанк ба арилжааны банкуудын валютын ханш, Монгол орны 321 станцын цаг агаар ба агаарын чанар (AQI), сүлжээний оношилгоо болон монгол хэл/санхүүгийн хөрвүүлэлтийн нээлттэй JSON API-ууд.</p>
      <div class="status-strip">
        <span class="stat-chip">STATUS: <strong style="color:#34d399;">4 MICROSERVICES OPERATIONAL</strong></span>
        <span class="stat-chip">USD/MNT: <strong>{html.escape(usd_rate)}</strong></span>
        <span class="stat-chip">ULAANBAATAR: <strong>{html.escape(ub_temp)} ({html.escape(ub_cond)})</strong></span>
        <span class="stat-chip">YOUR_IP: <strong>{html.escape(visitor['ip'])} ({html.escape(visitor['country'])})</strong></span>
      </div>
    </section>

    <div class="svc-grid">
      <!-- 1. FX Service -->
      <div class="svc-card">
        <div>
          <div class="svc-head">
            <span class="svc-domain">fx.ulaach.cc</span>
            <span class="svc-live">● LIVE API</span>
          </div>
          <h2 class="svc-title">Валютын Ханш, График &amp; Хөрвүүлэгч</h2>
          <p class="svc-desc">Монголбанкны 38 валютын өдөр тутмын болон түүхэн албан ханш (<code>change_1d</code> өөрчлөлттэй), Хаан/Голомт/ХХБ-ны ханш, шууд хөрвүүлэгч (<code>/convert.json</code>) ба 5 хэлний IFrame виджет.</p>
        </div>
        <div class="svc-metric">
          <div style="display:flex;justify-content:space-between;"><span class="muted">USD (Албан ханш):</span><strong style="color:#38bdf8;">{html.escape(usd_rate)}</strong></div>
          <div style="display:flex;justify-content:space-between;"><span class="muted">EUR (Евро):</span><strong style="color:#34d399;">{html.escape(eur_rate)}</strong></div>
          <div style="display:flex;justify-content:space-between;"><span class="muted">CNY (Юань):</span><strong style="color:#fbbf24;">{html.escape(cny_rate)}</strong></div>
          <div style="display:flex;justify-content:space-between;"><span class="muted">Ханшийн огноо:</span><span>{html.escape(mnb_date)}</span></div>
        </div>
        <a class="svc-btn" href="https://fx.ulaach.cc">fx.ulaach.cc руу орох →</a>
      </div>

      <!-- 2. Weather Service -->
      <div class="svc-card">
        <div>
          <div class="svc-head">
            <span class="svc-domain">weather.ulaach.cc</span>
            <span class="svc-live">● LIVE API</span>
          </div>
          <h2 class="svc-title">Цаг Агаар, Нар Мандах, AQI &amp; Түймэр</h2>
          <p class="svc-desc">21 аймаг, 321 сумын цаг тутмын ажиглалт, нар мандах/жаргах цаг, УБ хотын агаарын чанар (AQI/PM2.5), түймрийн эрсдэл (<code>/fire-risk.json</code>) болон <code>/weather.html</code> виджет.</p>
        </div>
        <div class="svc-metric">
          <div style="display:flex;justify-content:space-between;"><span class="muted">Улаанбаатар:</span><strong style="color:#38bdf8;">{html.escape(ub_temp)}</strong></div>
          <div style="display:flex;justify-content:space-between;"><span class="muted">Төлөв / Салхи:</span><strong style="color:#34d399;">{html.escape(ub_cond)} ({html.escape(ub_wind)})</strong></div>
          <div style="display:flex;justify-content:space-between;"><span class="muted">УБ дундаж AQI:</span><strong style="color:#fbbf24;">{html.escape(ub_aqi)}</strong></div>
          <div style="display:flex;justify-content:space-between;"><span class="muted">Хяналтын станц:</span><span>321 станц</span></div>
        </div>
        <a class="svc-btn" href="https://weather.ulaach.cc">weather.ulaach.cc руу орох →</a>
      </div>

      <!-- 3. Network & IP Tool -->
      <div class="svc-card">
        <div>
          <div class="svc-head">
            <span class="svc-domain">tool.ulaach.cc</span>
            <span class="svc-live">● LIVE TOOL</span>
          </div>
          <h2 class="svc-title">What Is My IP, DNS, SSL &amp; Port Tools</h2>
          <p class="svc-desc">Таны бодит IP хаяг, дурын IP/Домэйны ISP ба ASN шалгах (<code>/lookup</code>), DNS рекордууд (<code>/dns</code>), SSL сертификатын хугацаа (<code>/ssl</code>), TCP порт шалгагч (<code>/port</code>).</p>
        </div>
        <div class="svc-metric">
          <div style="display:flex;justify-content:space-between;"><span class="muted">Таны IP хаяг:</span><strong style="color:#38bdf8;">{html.escape(visitor['ip'])}</strong></div>
          <div style="display:flex;justify-content:space-between;"><span class="muted">Хувилбар / Улс:</span><strong style="color:#34d399;">{html.escape(visitor['ip_version'])} / {html.escape(visitor['country'])}</strong></div>
          <div style="display:flex;justify-content:space-between;"><span class="muted">Оношилгоо:</span><strong style="color:#fbbf24;">GeoIP • DNS • SSL • Port</strong></div>
          <div style="display:flex;justify-content:space-between;"><span class="muted">CLI дуудалт:</span><span>curl tool.ulaach.cc/ip</span></div>
        </div>
        <a class="svc-btn" href="https://tool.ulaach.cc">tool.ulaach.cc руу орох →</a>
      </div>

      <!-- 4. Mongolian Developer Utilities -->
      <div class="svc-card">
        <div>
          <div class="svc-head">
            <span class="svc-domain">util.ulaach.cc</span>
            <span class="svc-live">● LIVE API</span>
          </div>
          <h2 class="svc-title">Монгол Тоог Үсгээр, Галиг &amp; РД Задлагч</h2>
          <p class="svc-desc">Нэхэмжлэх, гэрээ, санхүүгийн баримтын мөнгөн дүнг монгол үсгээр бичих (<code>/num2words</code>), Кирилл ↔ Латин галиглах (<code>/translit</code>), Регистрийн дугаар задлах (<code>/regno</code>).</p>
        </div>
        <div class="svc-metric">
          <div style="display:flex;justify-content:space-between;"><span class="muted">Тоог үсгээр:</span><strong style="color:#38bdf8;">GET /num2words?amount=...</strong></div>
          <div style="display:flex;justify-content:space-between;"><span class="muted">Галиглагч:</span><strong style="color:#34d399;">GET /translit?text=...</strong></div>
          <div style="display:flex;justify-content:space-between;"><span class="muted">РД шалгагч:</span><strong style="color:#fbbf24;">GET /regno?regno=...</strong></div>
          <div style="display:flex;justify-content:space-between;"><span class="muted">Хэрэглээ:</span><span>ERP / Нягтлан / Веб</span></div>
        </div>
        <a class="svc-btn" href="https://util.ulaach.cc">util.ulaach.cc руу орох →</a>
      </div>
    </div>

    <div class="footer">ulaach.cc • Open Data &amp; Developer APIs Platform</div>
  </main>
</body>
</html>"""
    )
    return HTMLResponse(content=html_doc)
