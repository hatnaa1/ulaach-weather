from __future__ import annotations

import asyncio
import datetime
import html
import ipaddress
import json
import os
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


_AIMAG_STEMS: list[tuple[str, str]] = [
    ("Архангай", "Архангай"),
    ("Баян-Өлгий", "Баян-Өлгий"),
    ("Баянхонгор", "Баянхонгор"),
    ("Булган", "Булган"),
    ("Говь-Алтай", "Говь-Алтай"),
    ("Говьсүмбэр", "Говьсүмбэр"),
    ("Дархан", "Дархан-Уул"),
    ("Дорногов", "Дорноговь"),
    ("Дорнод", "Дорнод"),
    ("Дундгов", "Дундговь"),
    ("Завхан", "Завхан"),
    ("Орхон", "Орхон"),
    ("Өвөрхангай", "Өвөрхангай"),
    ("Өмнөгов", "Өмнөговь"),
    ("Сүхбаатар", "Сүхбаатар"),
    ("Сэлэнг", "Сэлэнгэ"),
    ("Төв", "Төв"),
    ("Увс", "Увс"),
    ("Ховд", "Ховд"),
    ("Хөвсгөл", "Хөвсгөл"),
    ("Хэнтий", "Хэнтий"),
]


def _extract_aimags_from_text(text: str) -> list[str]:
    found = []
    for stem, canonical in _AIMAG_STEMS:
        if stem in text and canonical not in found:
            found.append(canonical)
    return found


def _build_weather_insights(
    hubs: list[dict[str, Any]],
    aqi_list: list[dict[str, Any]],
    fore_list: list[dict[str, Any]],
    fire_list: list[dict[str, Any]],
    bulletins: list[dict[str, Any]],
) -> dict[str, Any]:
    ub = next((x for x in hubs if x["sid"] == 292), hubs[0] if hubs else {})
    ub_temp = float(ub.get("temp_c") or 0.0)
    ub_wind = float(ub.get("wind_ms") or 0.0)

    # 1. Extract warnings, herder alerts, snow cover, and UB micro-climate from wx.content
    warning_signal = ""
    herder_alert = ""
    ub_zone_forecast: dict[str, str] = {}
    snow_cover_note = ""
    orchin_note = ""

    for b in bulletins:
        body = b.get("body", "") or ""
        author = (b.get("author") or "").lower()
        if not warning_signal and ("Дохио!!!" in body or author == "alert"):
            m = re.search(
                r"Дохио!!!\s*Цаг агаарын аюултай үзэгдлээс сэрэмжлүүлэх мэдээ:\s*([^\n]+)",
                body,
            )
            if m:
                warning_signal = m.group(1).strip()
            else:
                warning_signal = body.splitlines()[0].strip()

        if not herder_alert and "анхааралд:" in body:
            m = re.search(r"Малчид,\s*иргэд,\s*тээвэрч[и]?дийн анхааралд:\s*([^\n]+)", body)
            if m:
                herder_alert = m.group(1).strip()

        if not ub_zone_forecast and "УЛААНБААТАР ХОТ ОРЧМООР:" in body:
            for key, label in [
                ("УЛААНБААТАР ХОТ ОРЧМООР:", "Улаанбаатар хот"),
                ("БАГАНУУР ОРЧМООР:", "Багануур дүүрэг"),
                ("ТЭРЭЛЖ ОРЧМООР:", "Тэрэлж орчим"),
            ]:
                m = re.search(rf"{re.escape(key)}\s*([^\n]+)", body)
                if m:
                    ub_zone_forecast[label] = m.group(1).strip()

        if not snow_cover_note and "Цасан бүрхүүл:" in body:
            m = re.search(r"Цасан бүрхүүл:\s*([^\n]+)", body)
            if m:
                snow_cover_note = m.group(1).strip()

        if not orchin_note and author == "orchin" and "РМ10 тоосонцор" in body:
            m = re.search(r"(РМ10 тоосонцор[^\n]+)", body)
            if m:
                orchin_note = m.group(1).strip()

    affected_aimags = _extract_aimags_from_text(warning_signal + " " + herder_alert)
    max_wind_match = re.findall(r"секундэд\s*(\d+)(?:-(\d+))?\s*метр", warning_signal + " " + herder_alert)
    peak_gust_ms = 0
    for g1, g2 in max_wind_match:
        peak_gust_ms = max(peak_gust_ms, int(g2 or g1))

    # 2. Compute 4 Predictive Indices
    fore_temps = [float(f["temp_c"]) for f in fore_list if f.get("temp_c") is not None]
    fore_winds = [float(f["wind_ms"]) for f in fore_list if f.get("wind_ms") is not None]
    min_fore_temp = min(fore_temps) if fore_temps else ub_temp
    max_fore_temp = max(fore_temps) if fore_temps else ub_temp
    max_fore_wind = max(fore_winds) if fore_winds else ub_wind
    avg_fore_temp = sum(fore_temps) / len(fore_temps) if fore_temps else ub_temp

    ub_aqi_vals = [int(a["aqi"]) for a in aqi_list if a.get("aqi") is not None and str(a.get("sid", "")).startswith("12")]
    avg_ub_aqi = int(round(sum(ub_aqi_vals) / len(ub_aqi_vals))) if ub_aqi_vals else 85
    worst_aqi_station = aqi_list[0] if aqi_list else {"station": "Хайлааст", "aqi": avg_ub_aqi}

    # Prediction A: Air Quality & Inversion (Next 12-24h)
    if min_fore_temp <= -4.0 and max_fore_wind >= 5.0:
        aqi_pred_level = "ДУНД → САРНИНА"
        aqi_pred_color = "#fbbf24"
        aqi_pred_text = (
            f"Шөнөдөө хэм {min_fore_temp:+.0f}°C руу буурч галлагаа нэмэгдсэнээр орой/өглөөдөө гэр хорооллын цэгүүдэд "
            f"(одоо хамгийн өндөр нь {worst_aqi_station.get('station')}: AQI {worst_aqi_station.get('aqi')}) утаа өтгөрч AQI 150–190 хүрнэ. "
            f"Гэвч өдөртөө салхи {max_fore_wind:.0f}–13 м/с хүрч ширүүсэх тул температурын инверс задарч утаа хурдан сарнина."
        )
    elif min_fore_temp <= -4.0 and max_fore_wind < 3.5:
        aqi_pred_level = "ӨНДӨР БОХИРДОЛ (INVERS)"
        aqi_pred_color = "#f43f5e"
        aqi_pred_text = (
            f"Шөнөдөө {min_fore_temp:+.0f}°C хүйтэн бөгөөд салхи {max_fore_wind:.1f} м/с тогтуун байх тул "
            f"утаа сарнихгүй тогтож, өглөө 07:00–10:00 цагт AQI 200+ давах өндөр эрсдэлтэй."
        )
    else:
        aqi_pred_level = "ХЭВИЙН / ЦЭВЭРШИНЭ"
        aqi_pred_color = "#34d399"
        aqi_pred_text = (
            f"Улаанбаатарын дундаж AQI {avg_ub_aqi} байна. Салхины хурд {max_fore_wind:.1f} м/с байх тул агаарын солилцоо хэвийн явагдана."
        )

    # Prediction B: Road Ice, Pass Closures & Blizzard Risk
    has_ice_kw = ("халтиргаа" in herder_alert.lower()) or ("мөстөлт" in herder_alert.lower()) or ("цасан шуурга" in herder_alert.lower())
    has_snow_fore = any("цас" in (f.get("condition") or "").lower() or "хур" in (f.get("condition") or "").lower() for f in fore_list)
    if has_ice_kw or (has_snow_fore and min_fore_temp <= -2.0):
        road_level = "ӨНДӨР ЭРСДЭЛТЭЙ (HIGH)"
        road_color = "#f43f5e"
        road_text = (
            "Баруун болон төвийн аймгуудын нутгаар 0.0–8.0 мм нойтон цас орж, шөнөдөө -5°C..-12°C хүрч хөлдөх тул "
            "томоохон зам, даваа, гүвээнд (Төв, Сэлэнгэ, Булган, Архангай, Өвөрхангай, Завхан) халтиргаа гулгаа, мөстөлт үүсэж үзэгдэх орчин хязгаарлагдана."
        )
    else:
        road_level = "ХЭВИЙН (LOW)"
        road_color = "#34d399"
        road_text = "Улсын чанартай авто замуудад ноцтой мөстөлт болон цасан шуурганы эрсдэл бага байна."

    # Prediction C: Windstorm & Herder / Rural Hazard
    if peak_gust_ms >= 18:
        wind_level = f"АЮУЛТАЙ ДОХИО ({peak_gust_ms} м/с)"
        wind_color = "#f97316"
        wind_text = (
            f"Алтайн уулархаг нутаг болон Говийн бүс нутгаар ({', '.join(affected_aimags[:7])}) салхи секундэд 18–20 м/с, "
            f"зарим үед {peak_gust_ms} м/с хүрч шороон болон цасан шуурга шуурна. Малчид малаа ойрын бэлчээрт хариулахыг зөвлөж байна."
        )
    else:
        wind_level = "ТОГТУУН / ХЭВИЙН"
        wind_color = "#38bdf8"
        wind_text = "Нийт нутгаар салхи секундэд 4–9 м/с, аюултай хүчтэй шуурганы дохио бүртгэгдээгүй байна."

    # Prediction D: Heating Demand & Cold Wave Outlook (HDD base 18°C)
    hdd = max(0.0, 18.0 - avg_fore_temp)
    load_increase_pct = int(round((hdd / 18.0) * 45))
    heating_level = f"АЧААЛАЛ +{load_increase_pct}% (HDD {hdd:.1f})"
    heating_color = "#38bdf8" if avg_fore_temp > -5 else "#fbbf24"
    heating_text = (
        f"Улаанбаатарт ойрын 24 цагийн дундаж температур {avg_fore_temp:+.1f}°C (шөнөдөө Яармаг-Сонгиноор -10..-12°C, хотын төвөөр -5..-6°C) "
        f"болж өмнөх өдрөөс 5°C-аар сэрүүснэ. 10-р сарын 23–27-нд уулархаг нутгаар -17..-22°C хүрч эрс хүйтрэх тул дулаан/эрчим хүчний хэрэглээ өснө."
    )

    return {
        "generated_at_ub": datetime.datetime.now(UB_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "emergency_alert": {
            "active": bool(warning_signal),
            "signal_text": warning_signal,
            "herder_driver_notice": herder_alert,
            "affected_aimags": affected_aimags,
            "peak_wind_gust_ms": peak_gust_ms or 15,
        },
        "predictions": [
            {
                "id": "ub_aqi_outlook",
                "title": "1. УБ Агаарын Бохирдол & Утааны Таамаглал (24ц)",
                "badge": aqi_pred_level,
                "color": aqi_pred_color,
                "forecast": aqi_pred_text,
                "hotspot_note": orchin_note,
            },
            {
                "id": "road_ice_risk",
                "title": "2. Зам Тээвэр, Даваа Гүвээ & Халтиргааны Эрсдэл",
                "badge": road_level,
                "color": road_color,
                "forecast": road_text,
            },
            {
                "id": "wind_storm_hazard",
                "title": "3. Хүчтэй Салхи, Цасан & Шороон Шуурганы Прогноз",
                "badge": wind_level,
                "color": wind_color,
                "forecast": wind_text,
            },
            {
                "id": "heating_energy_load",
                "title": "4. Хүйтрэлт & Дулаан / Эрчим Хүчний Ачаалал",
                "badge": heating_level,
                "color": heating_color,
                "forecast": heating_text,
            },
        ],
        "ub_micro_zones": ub_zone_forecast,
        "snow_cover_summary": snow_cover_note,
    }


_LIVE_METEO_CACHE: dict[str, Any] = {"ts": 0.0, "by_sid": {}}

WMO_LIVE_DESC: dict[int, str] = {
    0: "Цэлмэг",
    1: "Бага зэргийн үүлтэй",
    2: "Солигдмол үүлтэй",
    3: "Үүлэрхэг",
    45: "Манантай",
    48: "Цан хүүрэгтэй",
    51: "Шиврээ бороо",
    53: "Шиврээ бороо",
    55: "Шиврээ бороо",
    61: "Бага зэргийн бороо",
    63: "Бороо орно",
    65: "Их бороо",
    71: "Ялимгүй цас",
    73: "Цас орно",
    75: "Их цас орно",
    77: "Цасан ширхэг",
    80: "Түр зуурын бороо",
    85: "Түр зуурын цас",
    86: "Их цас",
    95: "Аадар бороо",
}


def _sync_fetch_open_meteo(coords: list[tuple[int, float, float]]) -> dict[int, dict[str, Any]]:
    if not coords:
        return {}
    lats = ",".join(f"{c[1]:.4f}" for c in coords)
    lons = ",".join(f"{c[2]:.4f}" for c in coords)
    url = (
        f"https://api.open-meteo.com/v1/forecast?latitude={lats}&longitude={lons}"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,wind_speed_10m,wind_direction_10m,surface_pressure,weather_code"
        "&wind_speed_unit=ms&timezone=Asia%2FUlaanbaatar"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "ulaach-weather/1.2"})
    with urllib.request.urlopen(req, timeout=6) as resp:
        data = json.loads(resp.read().decode("utf-8", "ignore"))
    items = data if isinstance(data, list) else [data]
    out: dict[int, dict[str, Any]] = {}
    for (sid, _, _), item in zip(coords, items):
        cur = item.get("current") or {}
        if "temperature_2m" in cur:
            wcode = cur.get("weather_code")
            out[sid] = {
                "temp_c": round(float(cur["temperature_2m"]), 1),
                "feels_c": round(float(cur.get("apparent_temperature", cur["temperature_2m"])), 1),
                "humidity_pct": int(round(float(cur["relative_humidity_2m"]))) if cur.get("relative_humidity_2m") is not None else None,
                "wind_ms": round(float(cur["wind_speed_10m"]), 1) if cur.get("wind_speed_10m") is not None else None,
                "wind_dir_deg": int(round(float(cur["wind_direction_10m"]))) if cur.get("wind_direction_10m") is not None else None,
                "pressure_hpa": round(float(cur["surface_pressure"]), 1) if cur.get("surface_pressure") is not None else None,
                "condition": WMO_LIVE_DESC.get(int(wcode), "Тогтуун") if wcode is not None else "Тогтуун",
                "obs_time": str(cur.get("time", "")).replace("T", " "),
            }
    return out


async def get_live_meteo_for_stations(coords: list[tuple[int, float, float]]) -> dict[int, dict[str, Any]]:
    now = time.time()
    cached = _LIVE_METEO_CACHE.get("by_sid", {})
    if now - float(_LIVE_METEO_CACHE.get("ts", 0.0)) < 600 and all(s in cached for s, _, _ in coords):
        return cached
    try:
        fresh = await asyncio.to_thread(_sync_fetch_open_meteo, coords)
        if fresh:
            merged = dict(cached)
            merged.update(fresh)
            _LIVE_METEO_CACHE["ts"] = now
            _LIVE_METEO_CACHE["by_sid"] = merged
            return merged
    except Exception:
        pass
    return cached


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


# ==============================================================================
# SEO METADATA, ROBOTS, SITEMAP & OPENGRAPH GENERATOR
# ==============================================================================
_SEO_CONFIG: dict[str, dict[str, str]] = {
    "portal": {
        "canonical": "https://ulaach.cc/",
        "title": "ulaach.cc — Нээлттэй Өгөгдөл ба Developer API Платформ",
        "description": "Монголбанк ба арилжааны банкуудын валютын ханш (fx.ulaach.cc), 21 аймаг 321 станцын цаг агаар, нар мандах ба агаарын чанар AQI (weather.ulaach.cc), сүлжээний оношилгоо (tool.ulaach.cc) болон монгол хэл/санхүүгийн хөрвүүлэлтийн (util.ulaach.cc) нээлттэй JSON API экосистем.",
        "keywords": "ulaach, developer api mongolia, нээлттэй өгөгдөл, валютын ханш api, цаг агаар api, монголбанк ханш, aqi ulaanbaatar, num2words mongolian",
        "badge": "OPEN DATA & DEVELOPER ECOSYSTEM",
        "accent": "#34d399",
    },
    "fx": {
        "canonical": "https://fx.ulaach.cc/",
        "title": "fx.ulaach.cc — Монголбанк ба Арилжааны Банкуудын Валютын Ханшийн Developer API",
        "description": "Монголбанкны 38 валютын албан ханш, өмнөх өдрөөс өөрчлөлт, 30 хоногийн түүхэн график, Хаан/Голомт/ХХБ-ны валютын ханш, шууд хөрвүүлэгч (/convert.json) болон 5 хэлний веб IFrame виджет (/xansh.html).",
        "keywords": "валютын ханш, монголбанк ханш, ханшийн api, xansh json, валют хөрвүүлэгч, хаан банк ханш, голомт банк ханш, fx.ulaach.cc",
        "badge": "FOREIGN EXCHANGE & BANK RATES API",
        "accent": "#10b981",
    },
    "weather": {
        "canonical": "https://weather.ulaach.cc/",
        "title": "weather.ulaach.cc — Монгол Орны Цаг Агаар, Нар Мандах, AQI & Эрсдэлийн Таамаглалын API",
        "description": "Монгол улсын 21 аймаг, 321 сумын бодит цагийн цаг агаар, нар мандах/жаргах цаг, Улаанбаатарын агаарын чанар (AQI/PM2.5/PM10), ой хээрийн түймрийн индекс, синоптик эрсдэлийн 24 цагийн таамаглал (/insights.json) болон цаг агаарын виджет.",
        "keywords": "цаг агаар, улаанбаатар цаг агаар, агаарын чанар aqi, нар мандах цаг, цаг агаарын мэдээ api, түймрийн эрсдэл, weather.ulaach.cc",
        "badge": "METEOROLOGY, AQI & SYNOPTIC INSIGHTS API",
        "accent": "#38bdf8",
    },
    "tool": {
        "canonical": "https://tool.ulaach.cc/",
        "title": "tool.ulaach.cc — Миний IP Хаяг, GeoIP/ASN, DNS, SSL & TCP Порт Оношилгооны API",
        "description": "Таны бодит IPv4/IPv6 хаяг, Cloudflare Edge зангилаа, дурын IP болон домэйны ISP/ASN/GeoIP мэдээлэл (/lookup), DNS рекордууд (/dns), SSL сертификатын хугацаа (/ssl), TCP порт шалгагч (/port).",
        "keywords": "what is my ip mongolia, миний ip хаяг, dns lookup api, ssl checker api, port checker, network diagnostic tool, tool.ulaach.cc",
        "badge": "NETWORK & IP DIAGNOSTIC SUITE",
        "accent": "#fbbf24",
    },
    "util": {
        "canonical": "https://util.ulaach.cc/",
        "title": "util.ulaach.cc — Монгол Тоог Үсгээр Бичих, Кирилл-Латин Галиг & Регистр Задлагч API",
        "description": "Санхүү, ERP, нэхэмжлэхийн мөнгөн дүнг монгол үсгээр бичих (/num2words), Кирилл ба Латин текст хооронд галиглах (/translit), иргэний регистрийн дугаараас төрсөн огноо, нас, хүйс, аймаг задлах (/regno) Developer API.",
        "keywords": "тоог үсгээр бичих, мөнгөн дүн үсгээр, кирилл латин хөрвүүлэгч, регистрийн дугаар шалгах, mongolian num2words api, util.ulaach.cc",
        "badge": "MONGOLIAN FINTECH & LANGUAGE UTILITIES",
        "accent": "#a78bfa",
    },
}


def build_seo_meta_html(active_site: str, title_override: str | None = None) -> str:
    cfg = _SEO_CONFIG.get(active_site, _SEO_CONFIG["portal"])
    title = title_override or cfg["title"]
    desc = cfg["description"]
    canonical = cfg["canonical"]
    keywords = cfg["keywords"]
    og_img = f"{canonical.rstrip('/')}/og-image.svg?site={active_site}"

    verify_tags = []
    g_ver = os.getenv("GOOGLE_SITE_VERIFICATION", "").strip()
    b_ver = os.getenv("BING_SITE_VERIFICATION", "").strip()
    if g_ver:
        verify_tags.append(f'  <meta name="google-site-verification" content="{html.escape(g_ver)}" />')
    if b_ver:
        verify_tags.append(f'  <meta name="msvalidate.01" content="{html.escape(b_ver)}" />')
    verify_block = ("\n" + "\n".join(verify_tags)) if verify_tags else ""

    json_ld = {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebSite",
                "@id": f"{canonical}#website",
                "url": canonical,
                "name": title,
                "description": desc,
                "inLanguage": "mn-MN",
                "publisher": {
                    "@type": "Organization",
                    "name": "Ulaach Developer Platform",
                    "url": "https://ulaach.cc/",
                },
            },
            {
                "@type": "WebAPI",
                "@id": f"{canonical}#webapi",
                "name": title,
                "description": desc,
                "url": canonical,
                "documentation": canonical,
                "termsOfService": "https://ulaach.cc/",
                "provider": {
                    "@type": "Organization",
                    "name": "Ulaach Open Data Ecosystem",
                    "url": "https://ulaach.cc/",
                },
            },
        ],
    }
    json_ld_str = json.dumps(json_ld, ensure_ascii=False)

    return f"""  <meta name="description" content="{html.escape(desc)}" />
  <meta name="keywords" content="{html.escape(keywords)}" />
  <meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1" />
  <link rel="canonical" href="{html.escape(canonical)}" />
  <link rel="alternate" hreflang="mn" href="{html.escape(canonical)}" />
  <link rel="alternate" hreflang="x-default" href="{html.escape(canonical)}" />
  <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
  <meta name="theme-color" content="#090d16" />{verify_block}
  <meta property="og:type" content="website" />
  <meta property="og:site_name" content="ulaach.cc Developer Platform" />
  <meta property="og:locale" content="mn_MN" />
  <meta property="og:title" content="{html.escape(title)}" />
  <meta property="og:description" content="{html.escape(desc)}" />
  <meta property="og:url" content="{html.escape(canonical)}" />
  <meta property="og:image" content="{html.escape(og_img)}" />
  <meta property="og:image:width" content="1200" />
  <meta property="og:image:height" content="630" />
  <meta name="twitter:card" content="summary_large_image" />
  <meta name="twitter:title" content="{html.escape(title)}" />
  <meta name="twitter:description" content="{html.escape(desc)}" />
  <meta name="twitter:image" content="{html.escape(og_img)}" />
  <script type="application/ld+json">{json_ld_str}</script>"""


def render_favicon_svg() -> str:
    return """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <rect width="64" height="64" rx="14" fill="#090d16"/>
  <rect x="3" y="3" width="58" height="58" rx="12" fill="none" stroke="#1e293b" stroke-width="2"/>
  <circle cx="48" cy="16" r="5" fill="#10b981"/>
  <path d="M18 18v18c0 6.6 5.4 12 12 12s12-5.4 12-12V18" fill="none" stroke="#38bdf8" stroke-width="6" stroke-linecap="round"/>
</svg>"""


def render_og_image_svg(site: str = "portal") -> str:
    cfg = _SEO_CONFIG.get(site, _SEO_CONFIG["portal"])
    domain = cfg["canonical"].replace("https://", "").rstrip("/")
    badge = cfg["badge"]
    accent = cfg["accent"]
    title_short = cfg["title"].split("—")[-1].strip() if "—" in cfg["title"] else cfg["title"]
    if len(title_short) > 48:
        title_short = title_short[:46] + "…"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630" width="1200" height="630">
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#090d16"/>
      <stop offset="100%" stop-color="#0f172a"/>
    </linearGradient>
  </defs>
  <rect width="1200" height="630" fill="url(#bg)"/>
  <rect x="44" y="44" width="1112" height="542" rx="22" fill="#0f1523" stroke="#1e293b" stroke-width="2"/>
  <circle cx="96" cy="108" r="10" fill="#10b981"/>
  <text x="122" y="115" fill="#e2e8f0" font-family="monospace" font-size="24" font-weight="700">{html.escape(domain)} // Ulaach Developer Platform</text>
  <rect x="84" y="170" width="460" height="42" rx="8" fill="#070a12" stroke="{accent}" stroke-width="1.5"/>
  <text x="104" y="197" fill="{accent}" font-family="monospace" font-size="18" font-weight="700">{html.escape(badge)}</text>
  <text x="84" y="290" fill="#f8fafc" font-family="sans-serif" font-size="46" font-weight="700">{html.escape(title_short)}</text>
  <text x="84" y="365" fill="#94a3b8" font-family="monospace" font-size="23">fx.ulaach.cc • weather.ulaach.cc • tool.ulaach.cc • util.ulaach.cc</text>
  <rect x="84" y="445" width="1032" height="84" rx="12" fill="#070a12" stroke="#1e293b" stroke-width="1.5"/>
  <text x="112" y="495" fill="#34d399" font-family="monospace" font-size="22">GET /xansh.json   GET /weather.json   GET /insights.json   GET /num2words</text>
</svg>"""


def render_robots_txt(host: str) -> str:
    clean_host = (host or "ulaach.cc").split(":")[0].lower()
    if clean_host not in ("ulaach.cc", "fx.ulaach.cc", "weather.ulaach.cc", "tool.ulaach.cc", "util.ulaach.cc"):
        clean_host = "ulaach.cc"
    return (
        "User-agent: *\n"
        "Disallow: /health\n\n"
        "User-agent: GPTBot\n"
        "Disallow: /\n\n"
        "User-agent: CCBot\n"
        "Disallow: /\n\n"
        f"Sitemap: https://{clean_host}/sitemap.xml\n"
    )


def render_sitemap_xml(host: str) -> str:
    clean_host = (host or "ulaach.cc").split(":")[0].lower()
    today_iso = datetime.datetime.now(UB_TZ).date().isoformat()

    host_urls: dict[str, list[tuple[str, str, str]]] = {
        "ulaach.cc": [
            ("https://ulaach.cc/", "hourly", "1.0"),
        ],
        "fx.ulaach.cc": [
            ("https://fx.ulaach.cc/", "hourly", "1.0"),
            ("https://fx.ulaach.cc/xansh.json", "hourly", "0.9"),
            ("https://fx.ulaach.cc/banks.json", "hourly", "0.9"),
            ("https://fx.ulaach.cc/convert.json?from=USD&to=MNT&amount=100", "hourly", "0.8"),
            ("https://fx.ulaach.cc/history.json?currency=USD&days=30", "daily", "0.8"),
            ("https://fx.ulaach.cc/xansh.html?lang=MN&theme=dark", "daily", "0.8"),
        ],
        "weather.ulaach.cc": [
            ("https://weather.ulaach.cc/", "hourly", "1.0"),
            ("https://weather.ulaach.cc/weather.json", "hourly", "0.9"),
            ("https://weather.ulaach.cc/insights.json", "hourly", "0.9"),
            ("https://weather.ulaach.cc/aqi.json", "hourly", "0.9"),
            ("https://weather.ulaach.cc/forecast.json?sid=292", "hourly", "0.8"),
            ("https://weather.ulaach.cc/fire-risk.json", "daily", "0.8"),
            ("https://weather.ulaach.cc/weather.html?sid=292&lang=MN&theme=dark", "hourly", "0.8"),
        ],
        "tool.ulaach.cc": [
            ("https://tool.ulaach.cc/", "daily", "1.0"),
            ("https://tool.ulaach.cc/json", "daily", "0.8"),
            ("https://tool.ulaach.cc/headers", "daily", "0.7"),
        ],
        "util.ulaach.cc": [
            ("https://util.ulaach.cc/", "weekly", "1.0"),
            ("https://util.ulaach.cc/num2words?amount=1254800.50&currency=MNT", "weekly", "0.8"),
            ("https://util.ulaach.cc/translit?text=Ulaanbaatar&dir=lat2cyr", "weekly", "0.8"),
        ],
    }

    entries = host_urls.get(clean_host, host_urls["ulaach.cc"])
    url_blocks = []
    for loc, freq, prio in entries:
        url_blocks.append(
            f"  <url>\n"
            f"    <loc>{html.escape(loc)}</loc>\n"
            f"    <lastmod>{today_iso}</lastmod>\n"
            f"    <changefreq>{freq}</changefreq>\n"
            f"    <priority>{prio}</priority>\n"
            f"  </url>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(url_blocks)
        + "\n</urlset>\n"
    )


def _common_head(title: str, active_site: str) -> str:
    def _cls(s: str) -> str:
        return "nav-pill active" if s == active_site else "nav-pill"

    seo_meta = build_seo_meta_html(active_site, title_override=title)

    return f"""<!DOCTYPE html>
<html lang="mn">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
{seo_meta}
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
                s.lat,
                s.lon,
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
            SELECT api_id, title, author, created AT TIME ZONE 'Asia/Ulaanbaatar' AS created_ub, body_text
            FROM wx.content
            ORDER BY created DESC
            LIMIT 12
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

    coords = [(r["sid"], float(r["lat"]), float(r["lon"])) for r in hubs if r["lat"] is not None and r["lon"] is not None]
    live_map = await get_live_meteo_for_stations(coords)

    hub_list = []
    latest_live_time = None
    for r in hubs:
        lv = live_map.get(r["sid"], {})
        if lv.get("obs_time"):
            latest_live_time = lv["obs_time"]
        hub_list.append(
            {
                "sid": r["sid"],
                "aimag": r["aimag_name"],
                "sum": r["sum_name"],
                "station": r["sta_name"],
                "elev_m": r["elev"],
                "obs_time": lv.get("obs_time") or (r["obs_time_ub"].strftime("%Y-%m-%d %H:%M") if r["obs_time_ub"] else None),
                "temp_c": lv.get("temp_c") if lv.get("temp_c") is not None else (round(float(r["temp"]), 1) if r["temp"] is not None else None),
                "feels_c": lv.get("feels_c"),
                "temp_synop_c": round(float(r["temp"]), 1) if r["temp"] is not None else None,
                "temp_min_c": round(float(r["temp_min"]), 1) if r["temp_min"] is not None else None,
                "temp_max_c": round(float(r["temp_max"]), 1) if r["temp_max"] is not None else None,
                "humidity_pct": lv.get("humidity_pct") if lv.get("humidity_pct") is not None else r["humidity"],
                "wind_ms": lv.get("wind_ms") if lv.get("wind_ms") is not None else (round(float(r["wind_speed"]), 1) if r["wind_speed"] is not None else None),
                "pressure_hpa": round(float(r["pressure_slp"]), 1) if r["pressure_slp"] is not None else lv.get("pressure_hpa"),
                "condition": lv.get("condition") or _ww_text(r["ww"], r["cloud_nh"]),
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
        full_body = (r["body_text"] or "").strip()
        bulletin_list.append(
            {
                "id": r["api_id"],
                "title": r["title"],
                "author": r["author"],
                "created_ub": r["created_ub"].strftime("%Y-%m-%d %H:%M") if r["created_ub"] else "",
                "summary": full_body[:450],
                "body": full_body,
            }
        )

    insights = _build_weather_insights(hub_list, aqi_list, fore_list, fire_list, bulletin_list)

    return {
        "total_stations": stats["total_stations"] if stats else 321,
        "total_obs": stats["total_obs"] if stats else 0,
        "latest_obs_ub": latest_live_time or (stats["latest_obs_ub"].strftime("%Y-%m-%d %H:%M") if stats and stats["latest_obs_ub"] else "—"),
        "hubs": hub_list,
        "aqi": aqi_list,
        "ub_forecast": fore_list,
        "fire_risk": fire_list,
        "bulletins": bulletin_list,
        "insights": insights,
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
                s.sid, s.aimag_name, s.sta_name, s.lat, s.lon,
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

    lv = {}
    if row and row["lat"] is not None and row["lon"] is not None:
        lmap = await get_live_meteo_for_stations([(row["sid"], float(row["lat"]), float(row["lon"]))])
        lv = lmap.get(row["sid"], {})

    sta_name = row["sta_name"] if row else "Улаанбаатар"
    eff_temp = lv.get("temp_c") if lv.get("temp_c") is not None else (float(row["temp"]) if row and row["temp"] is not None else None)
    temp_str = f"{eff_temp:+.1f}°C" if eff_temp is not None else "—"
    cond_str = lv.get("condition") or _ww_text(row["ww"] if row else None, row["cloud_nh"] if row else None)
    eff_wind = lv.get("wind_ms") if lv.get("wind_ms") is not None else float(row["wind_speed"] or 0) if row else 0.0
    wind_str = f"{eff_wind:.1f} m/s"
    eff_hum = lv.get("humidity_pct") if lv.get("humidity_pct") is not None else (row["humidity"] if row else None)
    hum_str = f"{eff_hum if eff_hum is not None else '—'}%"
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

    station_options = []
    for r in data["hubs"]:
        sel = " selected" if r["sid"] == 292 else ""
        station_options.append(
            f"<option value='{r['sid']}'{sel}>{html.escape(r['station'])} ({html.escape(r['aimag'])}) — sid={r['sid']}</option>"
        )

    ins = data.get("insights", {})
    em = ins.get("emergency_alert", {})
    aimag_badges = "".join(
        f"<span class='badge badge-amber' style='margin-right:6px;margin-bottom:4px;'>{html.escape(a)}</span>"
        for a in em.get("affected_aimags", [])
    )

    alert_banner_html = ""
    if em.get("active"):
        alert_banner_html = f"""
        <div style="background:rgba(244,63,94,0.09);border:1px solid rgba(244,63,94,0.45);border-radius:10px;padding:16px 18px;margin-bottom:16px;">
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px;">
            <span class="mono bold" style="color:#f43f5e;font-size:13px;">🚨 ДОХИО!!! ЦАГ АГААРЫН АЮУЛТАЙ ҮЗЭГДЛИЙН СЭРЭМЖЛҮҮЛЭГ</span>
            <span class="badge badge-red">Салхины оргил хурд: {em.get('peak_wind_gust_ms', 20)} м/с</span>
          </div>
          <p style="margin:0 0 8px;color:#f8fafc;font-size:13.5px;line-height:1.55;">{html.escape(em.get('signal_text', ''))}</p>
          <p style="margin:0 0 10px;color:#cbd5e1;font-size:13px;line-height:1.5;"><strong>Малчид, тээвэрчдийн анхааралд:</strong> {html.escape(em.get('herder_driver_notice', ''))}</p>
          <div>{aimag_badges}</div>
        </div>
        """

    pred_cards_html = []
    for p in ins.get("predictions", []):
        hotspot_extra = (
            f"<div class='mono muted' style='font-size:11.5px;margin-top:8px;padding-top:8px;border-top:1px dashed var(--border);'>"
            f"📌 {html.escape(p['hotspot_note'])}</div>"
            if p.get("hotspot_note")
            else ""
        )
        pred_cards_html.append(
            f"<div style='background:var(--bg-code);border:1px solid var(--border);border-radius:10px;padding:15px 16px;display:flex;flex-direction:column;justify-content:space-between;'>"
            f"<div>"
            f"<div style='display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap;'>"
            f"<span class='bold' style='font-size:13.5px;color:#f8fafc;'>{html.escape(p['title'])}</span>"
            f"<span class='mono bold' style='font-size:11px;padding:3px 9px;border-radius:99px;background:rgba(15,23,42,0.9);color:{p['color']};border:1px solid {p['color']};'>{html.escape(p['badge'])}</span>"
            f"</div>"
            f"<p style='margin:0;font-size:13px;color:var(--text);line-height:1.55;'>{html.escape(p['forecast'])}</p>"
            f"{hotspot_extra}"
            f"</div>"
            f"</div>"
        )

    zone_rows_html = []
    for zname, zdesc in (ins.get("ub_micro_zones") or {}).items():
        zone_rows_html.append(
            f"<tr>"
            f"<td class='bold' style='width:190px;color:#38bdf8;'>{html.escape(zname)}</td>"
            f"<td style='white-space:normal;'>{html.escape(zdesc)}</td>"
            f"</tr>"
        )
    if ins.get("snow_cover_summary"):
        zone_rows_html.append(
            f"<tr>"
            f"<td class='bold' style='color:#fbbf24;'>Цасан бүрхүүлийн тойм</td>"
            f"<td style='white-space:normal;'>{html.escape(ins['snow_cover_summary'])}</td>"
            f"</tr>"
        )

    bulletin_cards_html = []
    for b in data["bulletins"][:6]:
        b_badge = "badge-red" if b.get("author") == "alert" else ("badge-amber" if b.get("author") == "orchin" else "badge-blue")
        bulletin_cards_html.append(
            f"<details style='background:var(--bg-code);border:1px solid var(--border);border-radius:8px;padding:12px 15px;margin-bottom:10px;'>"
            f"<summary style='cursor:pointer;display:flex;justify-content:space-between;align-items:center;gap:10px;font-weight:600;color:#f8fafc;font-size:13.5px;'>"
            f"<span><span class='badge {b_badge}' style='margin-right:8px;'>{html.escape((b.get('author') or 'info').upper())}</span>{html.escape(b['title'])}</span>"
            f"<span class='mono muted' style='font-size:11.5px;flex-shrink:0;'>{html.escape(b['created_ub'])}</span>"
            f"</summary>"
            f"<div class='mono' style='margin-top:10px;padding-top:10px;border-top:1px solid var(--border);font-size:12px;color:#cbd5e1;white-space:pre-wrap;line-height:1.6;'>{html.escape(b['body'])}</div>"
            f"</details>"
        )

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
              <a class="ep-url" href="/insights.json" target="_blank">/insights.json</a>
            </div>
            <span class="ep-desc">Тоон ажиглалт + синоптик мэдээн (<code>wx.content</code>) дээр суурилсан 24 цагийн таамаглал, сэрэмжлүүлэг ба эрсдэлийн индекс</span>
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

        <div style="margin-top:16px;">
          <div class="mono muted" style="font-size:12px;margin-bottom:10px;">УЛААНБААТАР ХОТЫН ОЙРЫН ЦАГУУДЫН УРЬДЧИЛСАН МЭДЭЭ (SID=292)</div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;">
            {''.join(fore_cards)}
          </div>
        </div>
      </div>
    </section>

    <!-- 02. AI & Synoptic Weather Predictions + Alert Engine -->
    <section class="section-card">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">02</span> Цаг Агаарын Эрсдэлийн Таамаглал &amp; Синоптик Шинжилгээ (24 цаг)</h2>
        <span class="section-meta">GET /insights.json • Тоон телеметр + wx.content синоптик мэдээ</span>
      </div>
      <div class="section-body">
        {alert_banner_html}
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(460px,1fr));gap:14px;margin-bottom:16px;">
          {''.join(pred_cards_html)}
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th style="width:200px;">Бүс нутаг / Ангилал</th>
                <th>Синоптик мэдээнээс задалсан нарийвчилсан төлөв</th>
              </tr>
            </thead>
            <tbody>
              {''.join(zone_rows_html)}
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- 03. GET Parameters & Multi-language Weather IFrame Widget -->
    <section class="section-card">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">03</span> Цаг агаарын Олон Хэлний IFrame Виджет ба GET Параметрүүд</h2>
        <span class="section-meta">Supported Languages: MN, EN, CN, JP, KR • Dark &amp; Light</span>
      </div>
      <div class="section-body">
        <div class="table-wrap" style="margin-bottom:16px;">
          <table>
            <thead>
              <tr>
                <th style="width:140px;">Параметр</th>
                <th style="width:210px;">Жишээ утга</th>
                <th>Тайлбар</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td><span class="badge badge-blue">sid</span></td>
                <td class="mono">292 | 244 | 236 | 373</td>
                <td>Станцын дугаар (<code>292</code> = Улаанбаатар, <code>244</code> = Дархан, <code>236</code> = Эрдэнэт, <code>373</code> = Даланзадгад г.м. 321 станц)</td>
              </tr>
              <tr>
                <td><span class="badge badge-blue">lang</span></td>
                <td class="mono">MN | EN | CN | JP | KR</td>
                <td>Виджетийн хэлний сонголт (Монгол, Англи, Хятад, Япон, Солонгос)</td>
              </tr>
              <tr>
                <td><span class="badge badge-blue">theme</span></td>
                <td class="mono">dark | light</td>
                <td>Бараан (<code>dark</code>) эсвэл Цайвар (<code>light</code>) загвар</td>
              </tr>
            </tbody>
          </table>
        </div>

        <style>
          .wx-tabs {{ display:inline-flex; gap:4px; background:#090d16; padding:3px; border-radius:6px; border:1px solid var(--border); }}
          .wx-tab-btn {{
            background:transparent; border:none; color:var(--muted); font-family:var(--mono);
            font-size:11px; font-weight:600; padding:4px 9px; border-radius:4px; cursor:pointer;
          }}
          .wx-tab-btn.active {{ background:rgba(56,189,248,0.18); color:var(--accent); }}
        </style>

        <div style="display:grid;grid-template-columns:1fr 290px;gap:18px;align-items:start;">
          <div class="code-box">
            <div class="code-head" style="flex-wrap:wrap;gap:8px;">
              <span>HTML Embed (IFrame Widget)</span>
              <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                <select id="wx-sid-sel" style="background:#090d16;border:1px solid var(--border);color:var(--text);border-radius:6px;padding:4px 8px;font-family:var(--mono);font-size:11.5px;">
                  {''.join(station_options)}
                </select>
                <div class="wx-tabs" id="wx-lang-tabs">
                  <button type="button" class="wx-tab-btn active" data-lang="MN">MN</button>
                  <button type="button" class="wx-tab-btn" data-lang="EN">EN</button>
                  <button type="button" class="wx-tab-btn" data-lang="CN">CN</button>
                  <button type="button" class="wx-tab-btn" data-lang="JP">JP</button>
                  <button type="button" class="wx-tab-btn" data-lang="KR">KR</button>
                </div>
                <div class="wx-tabs" id="wx-theme-tabs">
                  <button type="button" class="wx-tab-btn active" data-theme="dark">Dark</button>
                  <button type="button" class="wx-tab-btn" data-theme="light">Light</button>
                </div>
                <button class="copy-btn" data-copy="wx-code-iframe">Copy</button>
              </div>
            </div>
            <pre id="wx-code-iframe">&lt;iframe
  style="width:275px;height:255px;border:none;overflow:hidden;border-radius:10px;"
  src="https://weather.ulaach.cc/weather.html?sid=292&amp;lang=MN&amp;theme=dark"&gt;
&lt;/iframe&gt;</pre>
          </div>
          <div style="background:var(--bg-code);border:1px solid var(--border);border-radius:8px;padding:12px;display:flex;justify-content:center;">
            <iframe id="wx-widget-frame" style="width:270px;height:255px;border:none;border-radius:10px;overflow:hidden;" src="/weather.html?sid=292&lang=MN&theme=dark" title="Ulaach Weather Widget"></iframe>
          </div>
        </div>
      </div>
    </section>

    <!-- 04. 28 Hub Stations with Sunrise/Sunset -->
    <section class="section-card">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">04</span> 21 Аймаг ба Гол Станцуудын Бодит Ажиглалт + Нар мандах/жаргах</h2>
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

    <!-- 05. AQI Table -->
    <section class="section-card">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">05</span> Агаарын Чанарын Индекс — AQI / PM2.5 / PM10</h2>
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

    <!-- 06. Fire Risk & Synoptic Bulletins -->
    <section class="section-card">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">06</span> Цаг Уурын Мэдээний Архив (wx.content) ба Ой, Хээрийн Түймрийн Индекс</h2>
        <span class="section-meta">GET /fire-risk.json • GET /insights.json</span>
      </div>
      <div class="section-body">
        <div class="mono muted" style="font-size:12px;margin-bottom:8px;">СҮҮЛИЙН СИНОПТИК МЭДЭЭ, СЭРЭМЖЛҮҮЛЭГ БА ОРЧНЫ ТОЙМ (Дарж дэлгэрэнгүй уншина уу)</div>
        <div style="margin-bottom:18px;">
          {''.join(bulletin_cards_html)}
        </div>
        <div class="mono muted" style="font-size:12px;margin-bottom:8px;">ОЙ, ХЭЭРИЙН ТҮЙМРИЙН ХУУРАЙШИЛТЫН ИНДЕКС ӨНДӨР СУМД</div>
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
  <script>
    (function() {{
      var curSid = '292';
      var curLang = 'MN';
      var curTheme = 'dark';
      var frame = document.getElementById('wx-widget-frame');
      var codeEl = document.getElementById('wx-code-iframe');
      var sidSel = document.getElementById('wx-sid-sel');

      function updateWxWidget() {{
        var rel = '/weather.html?sid=' + encodeURIComponent(curSid) + '&lang=' + curLang + '&theme=' + curTheme;
        frame.src = rel;
        codeEl.textContent = [
          '<iframe',
          '  style="width:275px;height:255px;border:none;overflow:hidden;border-radius:10px;"',
          '  src="https://weather.ulaach.cc' + rel + '">',
          '</iframe>'
        ].join(String.fromCharCode(10));
      }}

      if (sidSel) {{
        sidSel.addEventListener('change', function() {{
          curSid = sidSel.value;
          updateWxWidget();
        }});
      }}

      document.querySelectorAll('#wx-lang-tabs .wx-tab-btn').forEach(function(btn) {{
        btn.addEventListener('click', function() {{
          document.querySelectorAll('#wx-lang-tabs .wx-tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
          btn.classList.add('active');
          curLang = btn.getAttribute('data-lang');
          updateWxWidget();
        }});
      }});

      document.querySelectorAll('#wx-theme-tabs .wx-tab-btn').forEach(function(btn) {{
        btn.addEventListener('click', function() {{
          document.querySelectorAll('#wx-theme-tabs .wx-tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
          btn.classList.add('active');
          curTheme = btn.getAttribute('data-theme');
          updateWxWidget();
        }});
      }});

      document.querySelectorAll('.copy-btn').forEach(function(btn) {{
        btn.addEventListener('click', function() {{
          var targetId = btn.getAttribute('data-copy');
          var el = document.getElementById(targetId);
          if (!el) return;
          navigator.clipboard.writeText(el.innerText).then(function() {{
            var orig = btn.textContent;
            btn.textContent = 'Copied!';
            setTimeout(function() {{ btn.textContent = orig; }}, 1400);
          }});
        }});
      }});
    }})();
  </script>
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
    wx_alert_line = ""
    try:
        async with wx_pool.acquire() as conn:
            wrow = await conn.fetchrow(
                """
                SELECT s.lat, s.lon, o.temp, o.wind_speed, o.ww, o.cloud_nh
                FROM wx.stations s
                LEFT JOIN LATERAL (
                    SELECT temp, wind_speed, ww, cloud_nh
                    FROM wx.obs_data
                    WHERE sid = s.sid
                    ORDER BY obs_time DESC
                    LIMIT 1
                ) o ON true
                WHERE s.sid = 292
                """
            )
            lmap = await get_live_meteo_for_stations([(292, 47.9185, 106.8483)])
            lv_ub = lmap.get(292, {})
            if lv_ub.get("temp_c") is not None:
                ub_temp = f"{lv_ub['temp_c']:+.1f}°C"
                ub_wind = f"{lv_ub.get('wind_ms', 0):.1f} м/с"
                ub_cond = lv_ub.get("condition", "Тогтуун")
            elif wrow and wrow["temp"] is not None:
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
            crow = await conn.fetchrow(
                """
                SELECT body_text
                FROM wx.content
                WHERE body_text LIKE '%Дохио!!!%' OR author = 'alert'
                ORDER BY created DESC
                LIMIT 1
                """
            )
            if crow and crow["body_text"]:
                m_sig = re.search(
                    r"Дохио!!!\s*Цаг агаарын аюултай үзэгдлээс сэрэмжлүүлэх мэдээ:\s*([^\n]+)",
                    crow["body_text"],
                )
                wx_alert_line = m_sig.group(1).strip() if m_sig else crow["body_text"].splitlines()[0].strip()
    except Exception:
        pass

    portal_alert_html = ""
    if wx_alert_line:
        portal_alert_html = f"""
        <div style="margin-top:14px;background:rgba(244,63,94,0.1);border:1px solid rgba(244,63,94,0.45);border-radius:8px;padding:10px 14px;display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;">
          <div style="font-size:12.5px;color:#f8fafc;"><strong style="color:#f43f5e;">🚨 ЦАГ АГААРЫН ДОХИО:</strong> {html.escape(wx_alert_line)}</div>
          <a href="https://weather.ulaach.cc" class="mono bold" style="font-size:12px;color:#38bdf8;white-space:nowrap;">Дэлгэрэнгүй таамаглал →</a>
        </div>
        """

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
      {portal_alert_html}
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
