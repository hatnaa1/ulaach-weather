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
        "source": "weather.ulaach.cc",
    },
    "EN": {
        "title": "Weather & AQI",
        "wind": "Wind",
        "humidity": "Humidity",
        "sun": "Sun",
        "aqi": "Air Quality (AQI)",
        "forecast": "Forecast",
        "source": "weather.ulaach.cc",
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
    "les": {
        "canonical": "https://les.ulaach.cc/",
        "title": "les.ulaach.cc — Монгол Улсын Хуулийн Этгээдийн Бүртгэлийн Нээлттэй Өгөгдөл & Developer API",
        "description": "Монгол улсын 272,869 хуулийн этгээдийн регистрийн дугаар, оноосон нэр, үүсгэн байгуулагч/хувьцаа эзэмшигч, гүйцэтгэх удирдлага, эцсийн өмчлөгч, үйл ажиллагааны чиглэлийн хайлт болон Developer JSON API (/company.json, /search.json, /stats.json).",
        "keywords": "хуулийн этгээдийн бүртгэл, компанийн регистр шалгах, хуулийн этгээд хайх, эцсийн өмчлөгч, хувьцаа эзэмшигч, opendata 8438, les.ulaach.cc",
        "badge": "LEGAL ENTITIES REGISTRY OPEN DATA API",
        "accent": "#f59e0b",
    },
}


# Self-hosted Umami (https://a.ulaach.cc) — сайт бүр өөрийн website-id-тай.
# Embed widget-үүд (/xansh.html, /weather.html) гадны сайт дээр ачаалагддаг тул энд хамаарахгүй.
_UMAMI_SCRIPT = "https://a.ulaach.cc/insight.js"
_UMAMI_IDS = {
    "portal": "40b9df94-25dc-4f9d-84e6-3a1f10ffa281",
    "fx": "f703c531-96f1-4874-b333-85372837c27e",
    "weather": "344fd823-6959-47e6-8a48-989516001756",
    "tool": "067c5fae-b9f7-459f-972e-1ab66ec34e00",
    "util": "e0be8613-69d8-4b26-b058-03dbd9d1d58e",
}


def _umami_tag(active_site: str) -> str:
    wid = _UMAMI_IDS.get(active_site)
    if not wid:
        return ""
    return f'\n  <script defer src="{_UMAMI_SCRIPT}" data-website-id="{wid}"></script>'


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
  <script type="application/ld+json">{json_ld_str}</script>{_umami_tag(active_site)}"""


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
    if clean_host not in ("ulaach.cc", "fx.ulaach.cc", "weather.ulaach.cc", "tool.ulaach.cc", "util.ulaach.cc", "les.ulaach.cc"):
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
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")

    host_urls: dict[str, list[tuple[str, str, str]]] = {
        "ulaach.cc": [
            ("https://ulaach.cc/", "hourly", "1.0"),
        ],
        "fx.ulaach.cc": [
            ("https://fx.ulaach.cc/", "hourly", "1.0"),
            ("https://fx.ulaach.cc/xansh.html?lang=MN&theme=dark", "daily", "0.8"),
            ("https://fx.ulaach.cc/xansh.html?lang=EN&theme=dark", "daily", "0.7"),
        ],
        "weather.ulaach.cc": [
            ("https://weather.ulaach.cc/", "hourly", "1.0"),
            ("https://weather.ulaach.cc/weather.html?sid=292&lang=MN&theme=dark", "hourly", "0.8"),
            ("https://weather.ulaach.cc/weather.html?sid=292&lang=EN&theme=dark", "hourly", "0.7"),
        ],
        "tool.ulaach.cc": [
            ("https://tool.ulaach.cc/", "daily", "1.0"),
        ],
        "util.ulaach.cc": [
            ("https://util.ulaach.cc/", "weekly", "1.0"),
        ],
        "les.ulaach.cc": [
            ("https://les.ulaach.cc/", "daily", "1.0"),
        ],
    }

    entries = host_urls.get(clean_host, host_urls["ulaach.cc"])
    url_blocks = []
    for loc, freq, prio in entries:
        url_blocks.append(
            f"<url>\n"
            f"<loc>{html.escape(loc)}</loc>\n"
            f"<lastmod>{now_iso}</lastmod>\n"
            f"<changefreq>{freq}</changefreq>\n"
            f"<priority>{prio}</priority>\n"
            f"</url>"
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(url_blocks)
        + "\n</urlset>"
    )


def _common_head(title: str, active_site: str) -> str:
    def _cls(s: str) -> str:
        return "nav-pill active" if s == active_site else "nav-pill"

    seo_meta = build_seo_meta_html(active_site, title_override=title)

    return f"""<!DOCTYPE html>
<html lang="mn" data-theme="light">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{html.escape(title)}</title>
{seo_meta}
  <script>
    (function() {{
      try {{
        var saved = localStorage.getItem('ulaach-theme');
        if (saved === 'dark' || saved === 'light') {{
          document.documentElement.setAttribute('data-theme', saved);
        }}
      }} catch (e) {{}}
    }})();
  </script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root, html[data-theme="light"] {{
      --bg: #F6F5F0;
      --bg-panel: #FFFFFF;
      --bg-code: #FAF9F5;
      --border: #E5E3DC;
      --border-strong: #D1CFC7;
      --text: #141413;
      --muted: #5E5D59;
      --dim: #787771;
      --accent: #0284C7;
      --green: #0D9488;
      --amber: #D97706;
      --rose: #E11D48;
      --purple: #7C3AED;
      --brand-gold: #F5A11C;
      --topbar-bg: rgba(250, 249, 245, 0.92);
      --nav-seg-bg: #EAE8E0;
      --card-shadow: 0 1px 2px rgba(20, 20, 19, 0.04), 0 4px 16px rgba(20, 20, 19, 0.03);
      --sans: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', Roboto, Arial, sans-serif;
      --mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }}
    html[data-theme="dark"] {{
      --bg: #0A0B0E;
      --bg-panel: #111318;
      --bg-code: #0D0F14;
      --border: rgba(255, 255, 255, 0.08);
      --border-strong: rgba(255, 255, 255, 0.16);
      --text: #F3F4F6;
      --muted: #9CA3AF;
      --dim: #6B7280;
      --accent: #38BDF8;
      --green: #34D399;
      --amber: #FBBF24;
      --rose: #F43F5E;
      --purple: #A78BFA;
      --brand-gold: #F5A11C;
      --topbar-bg: rgba(10, 11, 14, 0.88);
      --nav-seg-bg: rgba(255, 255, 255, 0.05);
      --card-shadow: 0 4px 20px rgba(0, 0, 0, 0.35);
    }}
    * {{ box-sizing: border-box; }}
    html {{ scroll-behavior: smooth; }}
    body {{
      margin: 0;
      font-family: var(--sans);
      color: var(--text);
      background-color: var(--bg);
      background-image: none;
      line-height: 1.55;
      -webkit-font-smoothing: antialiased;
      transition: background-color 0.15s ease, color 0.15s ease;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}

    /* Universal inline-color overrides in Light Mode so legacy dark inline styles stay high-contrast */
    html[data-theme="light"] [style*="color:#f8fafc"],
    html[data-theme="light"] [style*="color: #f8fafc"],
    html[data-theme="light"] [style*="color:#e2e8f0"],
    html[data-theme="light"] [style*="color:#cbd5e1"] {{
      color: var(--text) !important;
    }}
    html[data-theme="light"] [style*="color:#38bdf8"] {{
      color: #0369A1 !important;
    }}
    html[data-theme="light"] [style*="color:#34d399"] {{
      color: #0F766E !important;
    }}
    html[data-theme="light"] [style*="color:#fbbf24"],
    html[data-theme="light"] [style*="color:#f59e0b"] {{
      color: #B45309 !important;
    }}
    html[data-theme="light"] [style*="color:#f43f5e"] {{
      color: #BE123C !important;
    }}
    html[data-theme="light"] [style*="color:#a78bfa"] {{
      color: #6D28D9 !important;
    }}
    html[data-theme="light"] [style*="background:rgba(7,10,18"],
    html[data-theme="light"] [style*="background:linear-gradient(180deg,rgba(15,23,42"],
    html[data-theme="light"] [style*="background:#090d16"],
    html[data-theme="light"] [style*="background:#0f1523"],
    html[data-theme="light"] [style*="background:rgba(15,23,42"] {{
      background: var(--bg-code) !important;
      color: var(--text) !important;
    }}

    .topbar {{
      position: sticky;
      top: 0;
      z-index: 40;
      backdrop-filter: blur(12px);
      background: var(--topbar-bg);
      border-bottom: 1px solid var(--border);
    }}
    .topbar-inner {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 10px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      flex-wrap: wrap;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 10px;
      font-family: var(--sans);
      font-weight: 700;
      font-size: 15px;
      color: var(--text);
      letter-spacing: -0.02em;
      text-decoration: none !important;
    }}
    .brand-dot {{
      width: 24px;
      height: 24px;
      border-radius: 6px;
      background: #F5A11C;
      color: #111111;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 13px;
      box-shadow: 0 1px 3px rgba(245, 161, 28, 0.35);
    }}
    .brand-dot::after {{
      content: "У";
    }}
    .nav-links {{
      display: flex;
      align-items: center;
      gap: 4px;
      flex-wrap: wrap;
      background: var(--nav-seg-bg);
      padding: 4px;
      border-radius: 10px;
      border: 1px solid var(--border);
    }}
    .nav-pill {{
      font-family: var(--sans);
      font-size: 12.5px;
      font-weight: 500;
      color: var(--muted);
      padding: 5px 11px;
      border-radius: 7px;
      border: 1px solid transparent;
      transition: all 0.15s;
      text-decoration: none !important;
    }}
    .nav-pill:hover {{
      color: var(--text);
      background: rgba(128, 128, 128, 0.08);
    }}
    .nav-pill.active {{
      color: var(--text);
      background: var(--bg-panel);
      border-color: var(--border);
      font-weight: 600;
      box-shadow: 0 1px 2px rgba(0, 0, 0, 0.06);
    }}
    .theme-toggle-btn {{
      font-family: var(--sans);
      font-size: 12px;
      font-weight: 600;
      padding: 6px 11px;
      border-radius: 8px;
      border: 1px solid var(--border-strong);
      background: var(--bg-panel);
      color: var(--text);
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s;
    }}
    .theme-toggle-btn:hover {{
      border-color: #F5A11C;
    }}

    /* Segmented View Tabs (Open-Meteo / macOS style) */
    .view-tabs-bar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
      background: var(--nav-seg-bg);
      padding: 5px;
      border-radius: 11px;
      border: 1px solid var(--border);
    }}
    .view-tabs-list {{
      display: flex;
      align-items: center;
      gap: 4px;
      flex-wrap: wrap;
    }}
    .view-tab-btn {{
      font-family: var(--sans);
      font-size: 13px;
      font-weight: 600;
      padding: 7px 15px;
      border-radius: 8px;
      border: 1px solid transparent;
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      transition: all 0.15s;
    }}
    .view-tab-btn:hover {{
      color: var(--text);
    }}
    .view-tab-btn.active {{
      background: var(--bg-panel);
      color: var(--text);
      border-color: var(--border);
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.07);
    }}

    .wrap {{
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px 24px 72px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }}
    .hero {{
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 22px 26px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      box-shadow: var(--card-shadow);
    }}
    .hero h1 {{
      margin: 0;
      font-size: 24px;
      font-weight: 700;
      letter-spacing: -0.025em;
      color: var(--text);
    }}
    .hero p {{
      margin: 0;
      font-size: 14px;
      color: var(--muted);
      max-width: 880px;
      line-height: 1.55;
    }}
    .status-strip {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 4px;
    }}
    .stat-chip {{
      font-family: var(--mono);
      font-size: 12px;
      padding: 5px 11px;
      border-radius: 7px;
      background: var(--bg-code);
      border: 1px solid var(--border);
      color: var(--muted);
    }}
    .stat-chip strong {{ color: var(--text); font-weight: 600; }}

    .section-card {{
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      overflow: hidden;
      box-shadow: var(--card-shadow);
    }}
    .section-head {{
      padding: 15px 22px;
      border-bottom: 1px solid var(--border);
      background: var(--bg-code);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .section-title {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin: 0;
      font-size: 15.5px;
      font-weight: 600;
      color: var(--text);
      letter-spacing: -0.01em;
    }}
    .step-badge {{
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 700;
      padding: 2px 8px;
      border-radius: 6px;
      background: rgba(245, 161, 28, 0.16);
      color: #D97706;
      border: 1px solid rgba(245, 161, 28, 0.4);
    }}
    html[data-theme="dark"] .step-badge {{
      color: #F5A11C;
    }}
    .section-meta {{
      font-family: var(--mono);
      font-size: 11.5px;
      color: var(--dim);
    }}
    .section-body {{
      padding: 20px 22px;
    }}

    .ep-list {{
      display: flex;
      flex-direction: column;
      gap: 8px;
      margin-bottom: 16px;
    }}
    .ep-item {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 10px 14px;
      background: var(--bg-code);
      border: 1px solid var(--border);
      border-radius: 9px;
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
      padding: 2px 7px;
      border-radius: 5px;
      background: rgba(13, 148, 136, 0.13);
      color: var(--green);
      border: 1px solid rgba(13, 148, 136, 0.3);
    }}
    .ep-url {{
      font-family: var(--mono);
      font-size: 13px;
      color: var(--accent);
      font-weight: 600;
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
      border-radius: 10px;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }}
    .code-pane-bar {{
      padding: 8px 14px;
      background: var(--nav-seg-bg);
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
      font-family: var(--sans);
      font-size: 12px;
      padding: 5px 12px;
      border-radius: 6px;
      background: var(--bg-panel);
      color: var(--text);
      border: 1px solid var(--border-strong);
      cursor: pointer;
      font-weight: 600;
      transition: all 0.15s;
    }}
    .run-btn {{
      background: #141413;
      color: #FFFFFF;
      border-color: #141413;
    }}
    html[data-theme="dark"] .run-btn {{
      background: #F5A11C;
      color: #111111;
      border-color: #F5A11C;
    }}
    .copy-btn:hover, .run-btn:hover {{
      border-color: var(--accent);
      opacity: 0.92;
    }}
    .inp {{
      background: var(--bg-panel);
      border: 1px solid var(--border-strong);
      color: var(--text);
      font-family: var(--sans);
      font-size: 13px;
      padding: 8px 11px;
      border-radius: 7px;
      outline: none;
    }}
    .inp:focus {{
      border-color: var(--accent);
      box-shadow: 0 0 0 2px rgba(2, 132, 199, 0.15);
    }}
    pre {{
      margin: 0;
      padding: 14px;
      font-family: var(--mono);
      font-size: 12px;
      line-height: 1.55;
      color: var(--text);
      overflow-x: auto;
    }}

    .table-wrap {{
      overflow-x: auto;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: var(--bg-panel);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }}
    th, td {{
      padding: 9px 12px;
      border-bottom: 1px solid var(--border);
      text-align: left;
      white-space: nowrap;
    }}
    th {{
      background: var(--bg-code);
      color: var(--muted);
      font-family: var(--sans);
      font-size: 11.5px;
      font-weight: 600;
      letter-spacing: 0.01em;
    }}
    tbody tr:hover {{
      background: var(--bg-code);
    }}
    .r {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .c {{ text-align: center; }}
    .mono {{ font-family: var(--mono); font-variant-numeric: tabular-nums; }}
    .bold {{ font-weight: 600; color: var(--text); }}
    .muted {{ color: var(--muted); }}
    .code-pill {{
      display: inline-block;
      font-family: var(--mono);
      font-size: 11.5px;
      font-weight: 600;
      padding: 2px 7px;
      border-radius: 5px;
      background: var(--bg-code);
      color: var(--text);
      border: 1px solid var(--border-strong);
    }}
    code {{
      font-family: var(--mono);
      font-size: 12px;
      background: var(--bg-code);
      border: 1px solid var(--border);
      padding: 2px 6px;
      border-radius: 5px;
      color: var(--accent);
    }}

    .svc-grid {{
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 16px;
    }}
    @media (max-width: 900px) {{
      .svc-grid {{ grid-template-columns: 1fr; }}
    }}
    .svc-card {{
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 22px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 16px;
      box-shadow: var(--card-shadow);
      transition: border-color 0.15s, transform 0.15s;
    }}
    .svc-card:hover {{
      border-color: var(--border-strong);
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
      font-size: 13.5px;
      font-weight: 700;
      color: var(--accent);
    }}
    .svc-live {{
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 600;
      padding: 3px 9px;
      border-radius: 999px;
      background: rgba(13, 148, 136, 0.12);
      color: var(--green);
      border: 1px solid rgba(13, 148, 136, 0.28);
    }}
    .svc-title {{
      margin: 6px 0 4px;
      font-size: 17.5px;
      font-weight: 700;
      color: var(--text);
      letter-spacing: -0.015em;
    }}
    .svc-desc {{
      margin: 0;
      font-size: 13.5px;
      color: var(--muted);
      line-height: 1.5;
    }}
    .svc-metric {{
      background: var(--bg-code);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px 14px;
      font-family: var(--mono);
      font-size: 12.5px;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .svc-btn {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 9px 14px;
      border-radius: 8px;
      font-family: var(--sans);
      font-size: 13px;
      font-weight: 600;
      background: #141413;
      color: #FFFFFF;
      border: 1px solid #141413;
      text-decoration: none !important;
      transition: opacity 0.15s;
    }}
    html[data-theme="dark"] .svc-btn {{
      background: rgba(245, 161, 28, 0.15);
      color: #F5A11C;
      border-color: rgba(245, 161, 28, 0.4);
    }}
    .svc-btn:hover {{
      opacity: 0.9;
    }}
    .footer {{
      text-align: center;
      font-family: var(--sans);
      font-size: 12.5px;
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
        <span>Улаач • Open Data &amp; API</span>
      </a>
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
        <nav class="nav-links">
          <a class="{_cls('portal')}" href="https://ulaach.cc">Портал</a>
          <a class="{_cls('fx')}" href="https://fx.ulaach.cc">Валютын Ханш</a>
          <a class="{_cls('weather')}" href="https://weather.ulaach.cc">Цаг Агаар &amp; AQI</a>
          <a class="{_cls('les')}" href="https://les.ulaach.cc">Хуулийн Этгээд</a>
          <a class="{_cls('tool')}" href="https://tool.ulaach.cc">IP &amp; Сүлжээ</a>
          <a class="{_cls('util')}" href="https://util.ulaach.cc">Хөрвүүлэгч</a>
        </nav>
        <button type="button" class="theme-toggle-btn" id="ulaach-theme-btn" onclick="window.__toggleUlaachTheme()" title="Өдөр / Шөнийн горим солих">
          <span id="ulaach-theme-icon">🌙</span>
          <span id="ulaach-theme-label">Dark</span>
        </button>
      </div>
    </div>
  </header>
  <script>
    (function() {{
      function syncBtn() {{
        var cur = document.documentElement.getAttribute('data-theme') || 'light';
        var ic = document.getElementById('ulaach-theme-icon');
        var lb = document.getElementById('ulaach-theme-label');
        if (ic && lb) {{
          if (cur === 'dark') {{
            ic.textContent = '☀️';
            lb.textContent = 'Light';
          }} else {{
            ic.textContent = '🌙';
            lb.textContent = 'Dark';
          }}
        }}
      }}
      window.__toggleUlaachTheme = function() {{
        var cur = document.documentElement.getAttribute('data-theme') || 'light';
        var next = (cur === 'dark') ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', next);
        try {{ localStorage.setItem('ulaach-theme', next); }} catch (e) {{}}
        syncBtn();
      }};
      window.__switchViewTab = function(group, tabId, btn) {{
        document.querySelectorAll('[data-tab-group="' + group + '"]').forEach(function(sec) {{
          var list = (sec.getAttribute('data-tab-id') || '').split(' ');
          if (tabId === 'all' || list.indexOf(tabId) !== -1) {{
            sec.style.display = '';
          }} else {{
            sec.style.display = 'none';
          }}
        }});
        if (btn && btn.parentElement) {{
          btn.parentElement.querySelectorAll('.view-tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
          btn.classList.add('active');
        }}
        window.dispatchEvent(new Event('resize'));
      }};
      syncBtn();
    }})();
  </script>
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
      if (kind === 'lookup') url = '/lookup' + '?target=' + encodeURIComponent(target);
      else if (kind === 'dns') url = '/dns' + '?domain=' + encodeURIComponent(target);
      else if (kind === 'ssl') url = '/ssl' + '?domain=' + encodeURIComponent(target);
      else if (kind === 'port') url = '/port' + '?host=' + encodeURIComponent(target) + '&port=' + encodeURIComponent(port);
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
        url = '/num2words' + '?amount=' + encodeURIComponent(amt) + '&currency=' + encodeURIComponent(cur);
      }} else if (kind === 'cyr2lat' || kind === 'lat2cyr') {{
        var txt = document.getElementById('u-text').value;
        url = '/translit' + '?text=' + encodeURIComponent(txt) + '&dir=' + kind;
      }} else if (kind === 'regno') {{
        var r = document.getElementById('u-regno').value.trim();
        url = '/regno' + '?regno=' + encodeURIComponent(r);
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
                o.wind_max,
                o.wind_dir,
                o.pressure_st,
                o.pressure_slp,
                o.precip,
                o.ww,
                o.cloud_nh,
                o.snow_depth,
                o.soil_tmin,
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
                co_value,
                o3_value,
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

        ub_daily_rows = await conn.fetch(
            """
            SELECT obs_date, temp_min, temp_max, temp_avg, precip_max, wind_max, humidity_avg, obs_count
            FROM wx.v_obs_daily
            WHERE sid = 292 AND obs_date >= CURRENT_DATE - INTERVAL '14 days'
            ORDER BY obs_date DESC
            LIMIT 14
            """
        )

        hub_5day_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (f.sid, f.fore_date)
                f.sid,
                s.aimag_name,
                s.sum_name,
                s.sta_name,
                s.sta_28,
                f.fore_date,
                f.ww_code,
                f.precip_prob,
                f.temp_day,
                f.temp_day_feels,
                f.wind_day,
                f.temp_night,
                f.temp_night_feels,
                f.wind_night,
                f.ww_night,
                f.precip_prob_night,
                f.pheno_day,
                f.pheno_night
            FROM wx.fore_day f
            JOIN wx.stations s ON s.sid = f.sid
            WHERE s.sta_28 <> 0
              AND f.fore_date > CURRENT_DATE
              AND f.fore_date <= CURRENT_DATE + INTERVAL '5 days'
            ORDER BY f.sid, f.fore_date ASC, (f.temp_night IS NOT NULL) DESC, f.issued_at DESC
            """
        )

        snow_soil_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (d.sid)
                d.sid,
                s.aimag_name,
                s.sum_name,
                s.sta_name,
                d.snow_depth,
                d.soil_tmin,
                d.temp,
                d.wind_max,
                d.obs_time AT TIME ZONE 'Asia/Ulaanbaatar' AS obs_ub
            FROM wx.obs_data d
            JOIN wx.stations s ON s.sid = d.sid
            WHERE d.obs_time > NOW() - INTERVAL '36 hours'
              AND (d.snow_depth > 0 OR d.soil_tmin IS NOT NULL)
            ORDER BY d.sid, d.obs_time DESC
            """
        )

        acc_summary = await conn.fetchrow(
            """
            SELECT
                ROUND(AVG(abs_error_c), 2) AS mae_c,
                ROUND(100.0 * COUNT(*) FILTER (WHERE abs_error_c <= 2.5) / NULLIF(COUNT(*), 0), 1) AS within_25_pct,
                COUNT(*) AS eval_count
            FROM wx.v_forecast_accuracy
            WHERE lead_days BETWEEN 0 AND 3
            """
        )

        ub_acc_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (fore_date)
                fore_date,
                lead_days,
                forecast_temp,
                actual_temp_max,
                error_c,
                abs_error_c
            FROM wx.v_forecast_accuracy
            WHERE sid = 292 AND lead_days BETWEEN 0 AND 2
            ORDER BY fore_date DESC, lead_days ASC
            LIMIT 7
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
            LIMIT 18
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
                "wind_max_ms": round(float(r["wind_max"]), 1) if r["wind_max"] is not None else None,
                "pressure_st_hpa": round(float(r["pressure_st"]), 1) if r["pressure_st"] is not None else None,
                "pressure_hpa": round(float(r["pressure_slp"]), 1) if r["pressure_slp"] is not None else lv.get("pressure_hpa"),
                "snow_depth_cm": round(float(r["snow_depth"]), 1) if r["snow_depth"] is not None else None,
                "soil_tmin_c": round(float(r["soil_tmin"]), 1) if r["soil_tmin"] is not None else None,
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
                "so2": round(float(r["so2_value"]), 1) if r["so2_value"] is not None else None,
                "no2": round(float(r["no2_value"]), 1) if r["no2_value"] is not None else None,
                "co": round(float(r["co_value"]), 2) if r["co_value"] is not None else None,
                "o3": round(float(r["o3_value"]), 1) if r["o3_value"] is not None else None,
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

    ub_daily_list = []
    for r in ub_daily_rows:
        ub_daily_list.append(
            {
                "date": r["obs_date"].isoformat() if r["obs_date"] else "",
                "temp_min_c": round(float(r["temp_min"]), 1) if r["temp_min"] is not None else None,
                "temp_max_c": round(float(r["temp_max"]), 1) if r["temp_max"] is not None else None,
                "temp_avg_c": round(float(r["temp_avg"]), 1) if r["temp_avg"] is not None else None,
                "precip_mm": round(float(r["precip_max"]), 1) if r["precip_max"] is not None else 0.0,
                "wind_max_ms": round(float(r["wind_max"]), 1) if r["wind_max"] is not None else None,
                "humidity_pct": int(round(float(r["humidity_avg"]))) if r["humidity_avg"] is not None else None,
                "obs_count": int(r["obs_count"] or 0),
            }
        )

    hub_5day_map: dict[int, dict[str, Any]] = {}
    for r in hub_5day_rows:
        sid = r["sid"]
        if sid not in hub_5day_map:
            hub_5day_map[sid] = {
                "sid": sid,
                "aimag": r["aimag_name"],
                "sum": r["sum_name"],
                "station": r["sta_name"],
                "sta_28": r["sta_28"],
                "days": [],
            }
        hub_5day_map[sid]["days"].append(
            {
                "date": r["fore_date"].isoformat(),
                "date_short": r["fore_date"].strftime("%m-%d"),
                "temp_day_c": int(round(float(r["temp_day"]))) if r["temp_day"] is not None else None,
                "temp_night_c": int(round(float(r["temp_night"]))) if r["temp_night"] is not None else None,
                "feels_day_c": int(round(float(r["temp_day_feels"]))) if r["temp_day_feels"] is not None else None,
                "feels_night_c": int(round(float(r["temp_night_feels"]))) if r["temp_night_feels"] is not None else None,
                "wind_day_ms": int(round(float(r["wind_day"]))) if r["wind_day"] is not None else None,
                "wind_night_ms": int(round(float(r["wind_night"]))) if r["wind_night"] is not None else None,
                "precip_prob_day": r["precip_prob"],
                "precip_prob_night": r["precip_prob_night"],
                "cond_day": r["pheno_day"] or _ww_text(r["ww_code"]),
                "cond_night": r["pheno_night"] or _ww_text(r["ww_night"]),
            }
        )
    hub_5day_list = sorted(hub_5day_map.values(), key=lambda x: (x["sta_28"] or 999, x["sid"]))

    snow_list = []
    for r in snow_soil_rows:
        snow_list.append(
            {
                "sid": r["sid"],
                "aimag": r["aimag_name"],
                "sum": r["sum_name"],
                "station": r["sta_name"],
                "snow_cm": round(float(r["snow_depth"]), 1) if r["snow_depth"] is not None else 0.0,
                "soil_tmin_c": round(float(r["soil_tmin"]), 1) if r["soil_tmin"] is not None else None,
                "temp_c": round(float(r["temp"]), 1) if r["temp"] is not None else None,
                "wind_max_ms": round(float(r["wind_max"]), 1) if r["wind_max"] is not None else None,
                "obs_time": r["obs_ub"].strftime("%Y-%m-%d %H:%M") if r["obs_ub"] else "—",
            }
        )
    snow_list.sort(key=lambda x: (-(x["snow_cm"] or 0.0), (x["soil_tmin_c"] if x["soil_tmin_c"] is not None else 99.0)))

    acc_info = {
        "mae_c": float(acc_summary["mae_c"]) if acc_summary and acc_summary["mae_c"] is not None else 1.65,
        "within_25_pct": float(acc_summary["within_25_pct"]) if acc_summary and acc_summary["within_25_pct"] is not None else 82.0,
        "eval_count": int(acc_summary["eval_count"]) if acc_summary and acc_summary["eval_count"] is not None else 0,
        "ub_recent": [
            {
                "date": r["fore_date"].isoformat(),
                "lead_days": r["lead_days"],
                "forecast_c": round(float(r["forecast_temp"]), 1) if r["forecast_temp"] is not None else None,
                "actual_max_c": round(float(r["actual_temp_max"]), 1) if r["actual_temp_max"] is not None else None,
                "error_c": round(float(r["error_c"]), 1) if r["error_c"] is not None else None,
                "abs_error_c": round(float(r["abs_error_c"]), 1) if r["abs_error_c"] is not None else None,
            }
            for r in ub_acc_rows
        ],
    }

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
        "ub_daily": ub_daily_list,
        "hub_5day": hub_5day_list,
        "snow_soil": snow_list,
        "accuracy": acc_info,
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
        aqi_rows = await conn.fetch(
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
        fore_day_rows = await conn.fetch(
            """
            SELECT DISTINCT ON (fore_date)
                fore_date, temp_day, temp_night, pheno_day, ww_code
            FROM wx.fore_day
            WHERE sid = $1 AND fore_date > CURRENT_DATE
            ORDER BY fore_date ASC, (temp_night IS NOT NULL) DESC, issued_at DESC
            LIMIT 3
            """,
            sid,
        )
    aqi_row = aqi_rows[0] if aqi_rows else None

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
    for f in fore_day_rows:
        tm = f["fore_date"].strftime("%m/%d") if f["fore_date"] else ""
        td = f"{int(round(float(f['temp_day']))):+d}°" if f["temp_day"] is not None else "—"
        tn = f"{int(round(float(f['temp_night']))):+d}°" if f["temp_night"] is not None else "—"
        fore_items.append(
            f"<div style='flex:1;background:{panel};border:1px solid {border};border-radius:6px;padding:5px;text-align:center;'>"
            f"<div style='font-size:10px;color:{muted};font-family:monospace;'>{tm}</div>"
            f"<div style='font-size:11.5px;font-weight:700;color:#fbbf24;font-family:monospace;margin-top:2px;'>{td} <span style='color:#38bdf8;font-weight:600;'>/ {tn}</span></div>"
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
      <span>{html.escape(labels['forecast'])}</span>
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
            f"<td class='r mono muted'>{a['so2'] if a.get('so2') is not None else '—'}</td>"
            f"<td class='r mono muted'>{a['no2'] if a.get('no2') is not None else '—'}</td>"
            f"<td class='r mono muted'>{a['co'] if a.get('co') is not None else '—'}</td>"
            f"<td class='r mono muted'>{a['o3'] if a.get('o3') is not None else '—'}</td>"
            f"<td class='c mono muted'>{html.escape(a['obs_time'] or '—')}</td>"
            f"</tr>"
        )

    hub_5day_list = data.get("hub_5day", [])
    ub_5day = next((x for x in hub_5day_list if x["sid"] == 292), hub_5day_list[0] if hub_5day_list else {"days": []})
    ub_5day_cards = []
    for idx_d, d in enumerate(ub_5day.get("days", [])[:5]):
        lbl_tag = "МАРГААШ" if idx_d == 0 else f"+{idx_d + 1} ӨДӨР"
        td = f"{d['temp_day_c']:+d}°C" if d.get("temp_day_c") is not None else "—"
        tn = f"{d['temp_night_c']:+d}°C" if d.get("temp_night_c") is not None else "—"
        fd = f"{d['feels_day_c']:+d}°" if d.get("feels_day_c") is not None else "—"
        fn = f"{d['feels_night_c']:+d}°" if d.get("feels_night_c") is not None else "—"
        wd = d.get("wind_day_ms") if d.get("wind_day_ms") is not None else "—"
        wn = d.get("wind_night_ms") if d.get("wind_night_ms") is not None else "—"
        pp = max(d.get("precip_prob_day") or 0, d.get("precip_prob_night") or 0)
        ub_5day_cards.append(
            f"<div style='background:linear-gradient(180deg,rgba(15,23,42,0.9),rgba(7,10,18,0.95));border:1px solid var(--border-strong);border-radius:10px;padding:14px;display:flex;flex-direction:column;gap:8px;'>"
            f"<div style='display:flex;justify-content:space-between;align-items:center;gap:6px;'>"
            f"<span class='mono bold' style='font-size:12.5px;color:#f8fafc;white-space:nowrap;'>{html.escape(d['date'])}</span>"
            f"<span class='badge badge-blue' style='white-space:nowrap;flex-shrink:0;'>{lbl_tag}</span>"
            f"</div>"
            f"<div style='display:grid;grid-template-columns:1fr 1fr;gap:8px;background:var(--bg-code);border:1px solid var(--border);border-radius:8px;padding:10px;'>"
            f"<div>"
            f"<div class='mono muted' style='font-size:10px;'>☀️ ӨДӨРТӨӨ</div>"
            f"<div class='mono bold' style='font-size:20px;color:#fbbf24;margin-top:2px;'>{td}</div>"
            f"<div class='mono muted' style='font-size:10.5px;'>Мэдрэгдэх: {fd}</div>"
            f"</div>"
            f"<div style='border-left:1px solid var(--border);padding-left:8px;'>"
            f"<div class='mono muted' style='font-size:10px;'>🌙 ШӨНӨДӨӨ</div>"
            f"<div class='mono bold' style='font-size:20px;color:#38bdf8;margin-top:2px;'>{tn}</div>"
            f"<div class='mono muted' style='font-size:10.5px;'>Мэдрэгдэх: {fn}</div>"
            f"</div>"
            f"</div>"
            f"<div style='font-size:12px;color:#e2e8f0;display:flex;justify-content:space-between;'><span>☀️ {html.escape(d.get('cond_day') or '—')}</span><span class='muted'>🌙 {html.escape(d.get('cond_night') or '—')}</span></div>"
            f"<div class='mono muted' style='font-size:11px;display:flex;justify-content:space-between;border-top:1px dashed var(--border);padding-top:6px;'>"
            f"<span>💨 Салхи: {wd}/{wn} м/с</span>"
            f"<span style='color:#38bdf8;'>💧 Тунадас: {pp}%</span>"
            f"</div>"
            f"</div>"
        )

    date_headers = [d["date_short"] for d in ub_5day.get("days", [])[:5]]
    while len(date_headers) < 5:
        date_headers.append("—")

    matrix_5day_rows = []
    for st in hub_5day_list:
        cells = []
        for d in st.get("days", [])[:5]:
            td = f"{d['temp_day_c']:+d}°" if d.get("temp_day_c") is not None else "—"
            tn = f"{d['temp_night_c']:+d}°" if d.get("temp_night_c") is not None else "—"
            cd = d.get("cond_day") or "—"
            wd = d.get("wind_day_ms") or "—"
            cells.append(
                f"<td class='c' style='padding:8px 10px;'>"
                f"<div class='mono bold' style='font-size:13px;'><span style='color:#fbbf24;'>{td}</span> <span class='muted'>/</span> <span style='color:#38bdf8;'>{tn}</span></div>"
                f"<div style='font-size:11px;color:var(--muted);margin-top:2px;'>{html.escape(cd)} • {wd}м/с</div>"
                f"</td>"
            )
        while len(cells) < 5:
            cells.append("<td class='c muted'>—</td>")
        matrix_5day_rows.append(
            f"<tr>"
            f"<td class='c'><span class='code-pill'>{st['sid']}</span></td>"
            f"<td class='bold'>{html.escape(st['station'] or st['sum'])}</td>"
            f"<td class='muted'>{html.escape(st['aimag'])}</td>"
            + "".join(cells)
            + "</tr>"
        )

    snow_rows_html = []
    for sn in data.get("snow_soil", [])[:15]:
        snow_badge = f"<strong style='color:#38bdf8;'>{sn['snow_cm']:.0f} см</strong>" if (sn.get("snow_cm") or 0) > 0 else "<span class='muted'>0 см</span>"
        soil_val = f"{sn['soil_tmin_c']:+.1f}°C" if sn.get("soil_tmin_c") is not None else "—"
        air_val = f"{sn['temp_c']:+.1f}°C" if sn.get("temp_c") is not None else "—"
        snow_rows_html.append(
            f"<tr>"
            f"<td class='c'><span class='code-pill'>{sn['sid']}</span></td>"
            f"<td class='bold'>{html.escape(sn['aimag'])}</td>"
            f"<td>{html.escape(sn['sum'])}</td>"
            f"<td class='r mono'>{snow_badge}</td>"
            f"<td class='r mono bold' style='color:#a78bfa;'>{soil_val}</td>"
            f"<td class='r mono'>{air_val}</td>"
            f"<td class='r mono muted'>{sn['wind_max_ms'] if sn.get('wind_max_ms') is not None else '—'} м/с</td>"
            f"<td class='c mono muted'>{html.escape(sn['obs_time'])}</td>"
            f"</tr>"
        )

    acc = data.get("accuracy", {})
    acc_rows_html = []
    for ar in acc.get("ub_recent", []):
        err = ar.get("error_c") or 0.0
        err_col = "#34d399" if abs(err) <= 1.5 else ("#fbbf24" if abs(err) <= 3.0 else "#f43f5e")
        acc_rows_html.append(
            f"<tr>"
            f"<td class='mono bold'>{html.escape(ar['date'])}</td>"
            f"<td class='c mono muted'>{ar['lead_days']} өдрийн өмнө</td>"
            f"<td class='r mono bold' style='color:#fbbf24;'>{ar['forecast_c']:+.1f}°C</td>"
            f"<td class='r mono bold' style='color:#38bdf8;'>{ar['actual_max_c']:+.1f}°C</td>"
            f"<td class='r mono bold' style='color:{err_col};'>{err:+.1f}°C (|{ar['abs_error_c']:.1f}°C|)</td>"
            f"</tr>"
        )

    ub_daily_rows_html = []
    for ud in data.get("ub_daily", [])[:14]:
        ub_daily_rows_html.append(
            f"<tr>"
            f"<td class='mono bold'>{html.escape(ud['date'])}</td>"
            f"<td class='r mono' style='color:#38bdf8;'>{ud['temp_min_c']:+.1f}°C</td>"
            f"<td class='r mono bold' style='color:#fbbf24;'>{ud['temp_max_c']:+.1f}°C</td>"
            f"<td class='r mono'>{ud['temp_avg_c']:+.1f}°C</td>"
            f"<td class='r mono muted'>{ud['humidity_pct'] if ud.get('humidity_pct') is not None else '—'}%</td>"
            f"<td class='r mono' style='color:#34d399;'>{ud['wind_max_ms'] if ud.get('wind_max_ms') is not None else '—'} м/с</td>"
            f"<td class='r mono'>{ud['precip_mm']:.1f} мм</td>"
            f"<td class='c mono muted'>{ud['obs_count']} удаа</td>"
            f"</tr>"
        )

    heatmap_js_items = []
    for st in reversed(hub_5day_list):
        st_label = f"{st['station'] or st['sum']} ({st['aimag']})"
        for idx_d, d in enumerate(st.get("days", [])[:5]):
            tag = "Маргааш" if idx_d == 0 else f"+{idx_d + 1} өдөр"
            day_col = f"{d['date_short']} ({tag})"
            heatmap_js_items.append(
                {
                    "station": st_label,
                    "sid": st["sid"],
                    "day": day_col,
                    "date": d["date"],
                    "temp_day": d.get("temp_day_c"),
                    "temp_night": d.get("temp_night_c"),
                    "feels_day": d.get("feels_day_c"),
                    "feels_night": d.get("feels_night_c"),
                    "wind_day": d.get("wind_day_ms"),
                    "wind_night": d.get("wind_night_ms"),
                    "cond_day": d.get("cond_day") or "—",
                    "cond_night": d.get("cond_night") or "—",
                    "label_both": f"{d['temp_day_c']:+d}° / {d['temp_night_c']:+d}°" if (d.get("temp_day_c") is not None and d.get("temp_night_c") is not None) else "—",
                    "label_day": f"{d['temp_day_c']:+d}°C" if d.get("temp_day_c") is not None else "—",
                    "label_night": f"{d['temp_night_c']:+d}°C" if d.get("temp_night_c") is not None else "—",
                }
            )
    heatmap_json_str = json.dumps(heatmap_js_items, ensure_ascii=False)
    ub_3h_json_str = json.dumps(data.get("ub_forecast", []), ensure_ascii=False)
    ub_daily_json_str = json.dumps(list(reversed(data.get("ub_daily", []))), ensure_ascii=False)

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
    for b in data["bulletins"][:12]:
        au = (b.get("author") or "info").lower()
        b_badge = "badge-red" if au == "alert" else ("badge-amber" if au in ("orchin", "agro") else ("badge-green" if au in ("water", "climate") else "badge-blue"))
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
      <div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px;">
        <div>
          <div class="mono" style="font-size:11.5px;color:var(--green);font-weight:700;">METEOROLOGY, AQI &amp; SYNOPTIC INSIGHTS // OPEN-METEO + 321 STATIONS</div>
          <h1>Монгол орны Цаг агаар, Нар мандах ба Агаарын чанарын Платформ</h1>
        </div>
        <a href="/weather.json" target="_blank" class="run-btn" style="text-decoration:none;">GET /weather.json →</a>
      </div>
      <p>Монгол улсын 21 аймаг, {data['total_stations']} сум/станцын цаг тутмын бодит ажиглалт, 5 хоногийн ба 3 цагийн урьдчилсан мэдээ, 7 хоног × 24 цагийн дулааны зураглал (Heat Map), агаарын чанар (AQI) болон олон хэлний веб виджет.</p>
      <div class="status-strip">
        <span class="stat-chip">УЛААНБААТАР: <strong>{ub_temp_str} ({html.escape(ub.get('condition', 'Тогтуун'))}, салхи {ub.get('wind_ms', '—')} м/с)</strong></span>
        <span class="stat-chip">НАР МАНДАХ/ЖАРГАХ: <strong>{html.escape(ub_sun_str)}</strong></span>
        <span class="stat-chip">СТАНЦУУД: <strong>{data['total_stations']} станц</strong></span>
        <span class="stat-chip">СҮҮЛИЙН ХЭМЖИЛТ: <strong>{html.escape(data['latest_obs_ub'])}</strong></span>
      </div>
    </section>

    <div class="view-tabs-bar">
      <div class="view-tabs-list">
        <button type="button" class="view-tab-btn active" onclick="window.__switchViewTab('wx', 'forecast', this)">🌤️ Таамаг &amp; 7×24ц Heat Map</button>
        <button type="button" class="view-tab-btn" onclick="window.__switchViewTab('wx', 'stations', this)">🌫️ AQI, 28 Станц &amp; Өвөлжилт</button>
        <button type="button" class="view-tab-btn" onclick="window.__switchViewTab('wx', 'synoptic', this)">🚨 Синоптик Дохио &amp; Мэдээ</button>
        <button type="button" class="view-tab-btn" onclick="window.__switchViewTab('wx', 'api', this)">⚡ Developer API &amp; Виджет</button>
      </div>
      <button type="button" class="copy-btn" onclick="window.__switchViewTab('wx', 'all', this)">Бүгдийг дэлгэх</button>
    </div>

    <!-- 01. Forecast Cards -->
    <section class="section-card" data-tab-group="wx" data-tab-id="forecast">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">01</span> Улаанбаатар хотын Ирэх 5 Хоногийн болон 3 Цаг тутмын Урьдчилсан Мэдээ</h2>
        <span class="section-meta">Эх сурвалж: wx.fore_day • wx.fore_3hours • GET /forecast.json?sid=292</span>
      </div>
      <div class="section-body">
        <div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;">
            {''.join(ub_5day_cards)}
          </div>
        </div>

        <div style="margin-top:18px;">
          <div class="mono muted" style="font-size:12px;margin-bottom:10px;">⏱️ УЛААНБААТАР ХОТЫН ОЙРЫН ЦАГУУДЫН НАРИЙВЧИЛСАН ТӨЛӨВ (3 ЦАГ ТУТМЫН ПРОГНОЗ • wx.fore_3hours)</div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px;">
            {''.join(fore_cards)}
          </div>
        </div>
      </div>
    </section>

    <!-- 01B. 7-Day x 24-Hour Temperature Heat Map with Legend + 28 Stations Matrix -->
    <section class="section-card" data-tab-group="wx" data-tab-id="forecast">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">02</span> 7 Хоног × 24 Цагийн Цаг Агаарын Дулааны Зураглал (amCharts 5 Heat Map with Legend)</h2>
        <span class="section-meta">amCharts 5 HeatLegend • GET /forecast.json?sid=292 • 28 Станцаар сонгох</span>
      </div>
      <div class="section-body">
        <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:12px;">
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
            <span class="mono bold" style="font-size:12.5px;color:var(--green);">📍 БАЙРШИЛ / СТАНЦ СОНГОХ:</span>
            <select id="wx-hm-sid-sel" class="inp" style="font-family:var(--mono);font-size:12.5px;font-weight:700;cursor:pointer;">
              {''.join(station_options)}
            </select>
            <span id="wx-hm-status" class="mono muted" style="font-size:11.5px;">Сүүлийн 7 хоногийн 24 цагийн (00:00 – 23:00) дулааны матриц • Хулгана ойртуулж харна уу</span>
          </div>
          <a id="wx-hm-api-link" href="/forecast.json?sid=292" target="_blank" class="mono" style="font-size:11.5px;color:var(--accent);">GET /forecast.json?sid=292 →</a>
        </div>

        <!-- Keep dark canvas specifically for amCharts 5 Heat Map & 3h chart so colors pop identically in Light & Dark modes -->
        <div id="wx-heatmap-chart" style="width:100%;height:420px;background:#0B0F17 !important;color:#F8FAFC !important;border:1px solid var(--border);border-radius:12px;padding:12px;"></div>

        <div style="margin-top:16px;background:#0B0F17 !important;color:#F8FAFC !important;border:1px solid var(--border);border-radius:12px;padding:14px;">
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px;">
            <span id="wx-3h-title" class="mono bold" style="font-size:12.5px;color:#38bdf8 !important;">📈 СОНГОСОН СТАНЦЫН ИРЭХ 24–48 ЦАГИЙН ТЕМПЕРАТУР &amp; МЭДРЭГДЭХ ХЭМИЙН МУРУЙ (amCharts 5)</span>
            <span class="mono" style="font-size:11px;color:#94a3b8 !important;">wx.fore_3hours • 3 цаг тутмын нарийвчилсан прогноз</span>
          </div>
          <div id="wx-ub3h-chart" style="width:100%;height:280px;"></div>
        </div>

        <div style="margin-top:20px;">
          <div class="mono muted" style="font-size:12px;margin-bottom:10px;">📋 21 АЙМАГ БА УЛААНБААТАРЫН ИРЭХ 5 ХОНОГИЙН ӨДӨР / ШӨНИЙН ТААМГИЙН ХҮСНЭГТ</div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th class="c">SID</th>
                  <th>Станц / Төв</th>
                  <th>Аймаг / Нийслэл</th>
                  <th class="c">{html.escape(date_headers[0])} (Маргааш)</th>
                  <th class="c">{html.escape(date_headers[1])} (+2 өдөр)</th>
                  <th class="c">{html.escape(date_headers[2])} (+3 өдөр)</th>
                  <th class="c">{html.escape(date_headers[3])} (+4 өдөр)</th>
                  <th class="c">{html.escape(date_headers[4])} (+5 өдөр)</th>
                </tr>
              </thead>
              <tbody>
                {''.join(matrix_5day_rows)}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </section>

    <!-- 03. AI & Synoptic Weather Predictions + Alert Engine -->
    <section class="section-card" data-tab-group="wx" data-tab-id="synoptic" style="display:none;">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">03</span> Цаг Агаарын Эрсдэлийн Таамаглал &amp; Синоптик Шинжилгээ (24 цаг)</h2>
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

    <!-- 04. Developer API Endpoints & Multi-language Weather IFrame Widget -->
    <section class="section-card" data-tab-group="wx" data-tab-id="api" style="display:none;">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">04</span> Шууд дуудах JSON API Endpoint-ууд ба Олон Хэлний IFrame Виджет</h2>
        <span class="section-meta">Supported Languages: MN, EN, CN, JP, KR • Dark &amp; Light</span>
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
            <span class="ep-desc">3 цаг тутмын, 5 хоногийн урьдчилсан мэдээ ба 7×24ц дулааны матриц</span>
          </div>
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/insights.json" target="_blank">/insights.json</a>
            </div>
            <span class="ep-desc">Тоон ажиглалт + синоптик мэдээн (<code>wx.content</code>) дээр суурилсан 24 цагийн таамаглал</span>
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

        <style>
          .wx-tabs {{ display:inline-flex; gap:4px; background:var(--bg-panel); padding:3px; border-radius:6px; border:1px solid var(--border); }}
          .wx-tab-btn {{
            background:transparent; border:none; color:var(--muted); font-family:var(--mono);
            font-size:11px; font-weight:600; padding:4px 9px; border-radius:4px; cursor:pointer;
          }}
          .wx-tab-btn.active {{ background:var(--accent); color:#fff; }}
        </style>

        <div style="display:grid;grid-template-columns:1fr 290px;gap:18px;align-items:start;">
          <div class="code-pane">
            <div class="code-pane-bar" style="flex-wrap:wrap;gap:8px;">
              <span>HTML Embed (IFrame Widget)</span>
              <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                <select id="wx-sid-sel" class="inp" style="padding:4px 8px;font-family:var(--mono);font-size:11.5px;">
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

    <!-- 05. 28 Hub Stations with Sunrise/Sunset -->
    <section class="section-card" data-tab-group="wx" data-tab-id="stations" style="display:none;">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">05</span> 21 Аймаг ба Гол Станцуудын Бодит Ажиглалт + Нар мандах/жаргах</h2>
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

    <!-- 06. AQI Table -->
    <section class="section-card" data-tab-group="wx" data-tab-id="stations" style="display:none;">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">06</span> Агаарын Чанарын Индекс — AQI / PM2.5 / PM10 / SO2 / NO2 / CO / O3</h2>
        <span class="section-meta">GET /aqi.json • Бүх 6 бохирдуулагч үзүүлэлт</span>
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
                <th class="r">PM2.5</th>
                <th class="r">PM10</th>
                <th class="r">SO2</th>
                <th class="r">NO2</th>
                <th class="r">CO</th>
                <th class="r">O3</th>
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

    <!-- 07. Snow Depth, Soil Frost & Forecast Verification -->
    <section class="section-card" data-tab-group="wx" data-tab-id="stations" style="display:none;">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">07</span> Цасан Бүрхүүл, Хөрсний Гүний Хөлдөлт, Өдрийн Түүх &amp; Прогнозын Магадлал</h2>
        <span class="section-meta">wx.obs_data (snow_depth, soil_tmin) • wx.v_obs_daily • wx.v_forecast_accuracy</span>
      </div>
      <div class="section-body">
        <div>
          <div class="mono muted" style="font-size:12px;margin-bottom:8px;">❄️ ЦАСНЫ ЗУЗААН (СМ) БА ХӨРСНИЙ ГАДАРГЫН ДООД ТЕМПЕРАТУР (ӨВӨЛЖИЛТИЙН МОНИТОРИНГ • wx.obs_data)</div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th class="c">SID</th>
                  <th>Аймаг</th>
                  <th>Сум</th>
                  <th class="r">Цасны зузаан</th>
                  <th class="r">Хөрсний доод хэм</th>
                  <th class="r">Агаарын хэм</th>
                  <th class="r">Оргил салхи</th>
                  <th class="c">Цаг</th>
                </tr>
              </thead>
              <tbody>
                {''.join(snow_rows_html)}
              </tbody>
            </table>
          </div>
        </div>

        <div style="margin-top:20px;">
          <div class="mono muted" style="font-size:12px;margin-bottom:8px;">🎯 ПРОГНОЗЫН БОДИТ БИЕЛЭЛТ БА УЛААНБААТАРЫН СҮҮЛИЙН ХАРЬЦУУЛАЛТ (wx.v_forecast_accuracy • {acc.get('eval_count', 0):,} ХЭМЖИЛТ)</div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin-bottom:10px;">
            <div style="background:var(--bg-code);border:1px solid var(--border);border-radius:8px;padding:10px 14px;">
              <div class="mono muted" style="font-size:11px;">ДУНДАЖ АБСОЛЮТ АЛДАА (MAE)</div>
              <div class="mono bold" style="font-size:19px;color:#34d399;margin-top:3px;">±{acc.get('mae_c', 1.65):.2f}°C</div>
            </div>
            <div style="background:var(--bg-code);border:1px solid var(--border);border-radius:8px;padding:10px 14px;">
              <div class="mono muted" style="font-size:11px;">ОНОВЧТОЙ ТААМАГ (±2.5°C ДОТОР)</div>
              <div class="mono bold" style="font-size:19px;color:#38bdf8;margin-top:3px;">{acc.get('within_25_pct', 82.0):.1f}%</div>
            </div>
            <div style="background:var(--bg-code);border:1px solid var(--border);border-radius:8px;padding:10px 14px;">
              <div class="mono muted" style="font-size:11px;">ШАЛГАСАН НИЙТ ПРОГНОЗ</div>
              <div class="mono bold" style="font-size:19px;color:#fbbf24;margin-top:3px;">{acc.get('eval_count', 0):,} мөр</div>
            </div>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Огноо (Улаанбаатар • SID=292)</th>
                  <th class="c">Урьдчилж гаргасан хугацаа</th>
                  <th class="r">Урьдчилсан таамаг (Өдөр)</th>
                  <th class="r">Бодит хэмжсэн MAX</th>
                  <th class="r">Зөрүү (Алдаа)</th>
                </tr>
              </thead>
              <tbody>
                {''.join(acc_rows_html)}
              </tbody>
            </table>
          </div>
        </div>

        <div style="margin-top:20px;">
          <div class="mono muted" style="font-size:12px;margin-bottom:8px;">📊 УЛААНБААТАР ХОТЫН СҮҮЛИЙН 14 ХОНОГИЙН ӨДРИЙН НЭГДСЭН ТҮҮХ (wx.v_obs_daily • MIN / MAX / AVG / САЛХИ / ТУНАДАС)</div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Огноо</th>
                  <th class="r">Доод хэм (MIN)</th>
                  <th class="r">Дээд хэм (MAX)</th>
                  <th class="r">Дундаж хэм (AVG)</th>
                  <th class="r">Дундаж чийгшил</th>
                  <th class="r">Оргил салхи</th>
                  <th class="r">Тунадас</th>
                  <th class="c">Хэмжилтийн тоо</th>
                </tr>
              </thead>
              <tbody>
                {''.join(ub_daily_rows_html)}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </section>

    <!-- 08. Fire Risk & Synoptic Bulletins -->
    <section class="section-card" data-tab-group="wx" data-tab-id="synoptic" style="display:none;">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">08</span> Цаг Уурын Мэдээний Архив (wx.content: Прогноз, ХАА, Гол мөрөн, Орчин) &amp; Түймрийн Индекс</h2>
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
        var rel = '/weather.html' + '?sid=' + encodeURIComponent(curSid) + '&lang=' + curLang + '&theme=' + curTheme;
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
  <script src="https://cdn.amcharts.com/lib/5/index.js"></script>
  <script src="https://cdn.amcharts.com/lib/5/xy.js"></script>
  <script src="https://cdn.amcharts.com/lib/5/themes/Animated.js"></script>
  <script src="https://cdn.amcharts.com/lib/5/themes/Dark.js"></script>
  <script>
    (function() {{
      if (typeof am5 === 'undefined') return;

      // 1. amCharts 5 Demo-style Heat Map with Legend (7 Days x 24 Hours, NO text inside cells, Tooltip + HeatLegend on hover)
      var hmEl = document.getElementById('wx-heatmap-chart');
      var ub3hEl = document.getElementById('wx-ub3h-chart');
      if (!hmEl) return;

      var rootHm = am5.Root.new('wx-heatmap-chart');
      rootHm.setThemes([am5themes_Animated.new(rootHm), am5themes_Dark.new(rootHm)]);

      var chartHm = rootHm.container.children.push(
        am5xy.XYChart.new(rootHm, {{
          panX: false,
          panY: false,
          wheelX: 'none',
          wheelY: 'none',
          paddingLeft: 0,
          paddingRight: 10,
          layout: rootHm.verticalLayout
        }})
      );

      var yRenderer = am5xy.AxisRendererY.new(rootHm, {{
        visible: false,
        minGridDistance: 20,
        inversed: true
      }});
      yRenderer.grid.template.set('visible', false);
      yRenderer.labels.template.setAll({{
        fill: am5.color(0xe2e8f0),
        fontSize: 12,
        fontWeight: '600',
        fontFamily: 'JetBrains Mono, monospace',
        paddingRight: 10
      }});

      var yAxis = chartHm.yAxes.push(
        am5xy.CategoryAxis.new(rootHm, {{
          maxDeviation: 0,
          renderer: yRenderer,
          categoryField: 'day'
        }})
      );

      var xRenderer = am5xy.AxisRendererX.new(rootHm, {{
        visible: false,
        minGridDistance: 30,
        opposite: true
      }});
      xRenderer.grid.template.set('visible', false);
      xRenderer.labels.template.setAll({{
        fill: am5.color(0xe2e8f0),
        fontSize: 12,
        fontFamily: 'JetBrains Mono, monospace',
        paddingBottom: 8,
        location: 0.5,
        multiLocation: 0.5,
        centerX: am5.p50
      }});
      xRenderer.labels.template.adapters.add('forceHidden', function(forceHidden, target) {{
        var di = target.dataItem;
        if (di && di.dataContext && di.dataContext.hour) {{
          var hNum = parseInt(String(di.dataContext.hour).split(':')[0], 10);
          if (!isNaN(hNum) && (hNum % 2 !== 0)) {{
            return true;
          }}
        }}
        return false;
      }});

      var xAxis = chartHm.xAxes.push(
        am5xy.CategoryAxis.new(rootHm, {{
          renderer: xRenderer,
          categoryField: 'hour'
        }})
      );

      var hmTooltip = am5.Tooltip.new(rootHm, {{
        autoTextColor: false,
        getFillFromSprite: true,
        pointerOrientation: 'vertical',
        labelText: '[bold #ffffff]{{day}}, {{hour}}: {{value}}°C[/]\\n[#f8fafc]Мэдрэгдэх: {{feels_c}}°C • Салхи: {{wind_ms}} м/с • {{condition}}[/]'
      }});
      hmTooltip.get('background').setAll({{
        fillOpacity: 0.96,
        stroke: am5.color(0xffffff),
        strokeWidth: 1.5,
        cornerRadius: 6
      }});
      hmTooltip.label.setAll({{
        fill: am5.color(0xffffff),
        fontSize: 12.5,
        fontFamily: 'JetBrains Mono, monospace'
      }});

      var seriesHm = chartHm.series.push(
        am5xy.ColumnSeries.new(rootHm, {{
          calculateAggregates: true,
          stroke: am5.color(0x090d16),
          clustered: false,
          xAxis: xAxis,
          yAxis: yAxis,
          categoryXField: 'hour',
          categoryYField: 'day',
          valueField: 'value',
          tooltip: hmTooltip
        }})
      );

      // Exact amCharts Heat Map with Legend demo styling: flat crisp rectangles, 2px gap, slate-indigo (#4f5d95) -> warm coral (#e07a5f)
      seriesHm.columns.template.setAll({{
        tooltipText: '{{day}}, {{hour}}: {{value}}°C',
        tooltipY: am5.percent(10),
        strokeOpacity: 1,
        strokeWidth: 2,
        stroke: am5.color(0x090d16),
        cornerRadiusTL: 2,
        cornerRadiusTR: 2,
        cornerRadiusBL: 2,
        cornerRadiusBR: 2,
        width: am5.percent(100),
        height: am5.percent(100)
      }});

      seriesHm.set('heatRules', [{{
        target: seriesHm.columns.template,
        min: am5.color(0x4b5897),
        max: am5.color(0xe27653),
        dataField: 'value',
        key: 'fill'
      }}]);

      var heatLegend = chartHm.bottomAxesContainer.children.push(
        am5.HeatLegend.new(rootHm, {{
          orientation: 'horizontal',
          startColor: am5.color(0x4b5897),
          endColor: am5.color(0xe27653),
          stepCount: 5,
          paddingTop: 16
        }})
      );
      heatLegend.startLabel.setAll({{
        fontSize: 12.5,
        fill: am5.color(0xcbd5e1),
        fontWeight: '700',
        fontFamily: 'JetBrains Mono, monospace'
      }});
      heatLegend.endLabel.setAll({{
        fontSize: 12.5,
        fill: am5.color(0xf8fafc),
        fontWeight: '700',
        fontFamily: 'JetBrains Mono, monospace'
      }});

      seriesHm.columns.template.events.on('pointerover', function(ev) {{
        var di = ev.target.dataItem;
        if (di) {{
          heatLegend.showValue(di.get('value', 0), di.get('value', 0) + '°C');
        }}
      }});

      seriesHm.events.on('datavalidated', function() {{
        var lo = seriesHm.getPrivate('valueLow');
        var hi = seriesHm.getPrivate('valueHigh');
        heatLegend.set('startValue', lo);
        heatLegend.set('endValue', hi);
        heatLegend.set('startText', (lo !== undefined ? lo + '°C' : ''));
        heatLegend.set('endText', (hi !== undefined ? (hi > 0 ? '+' + hi : hi) + '°C' : ''));
      }});

      // 2. amCharts 5 Selected Station 3-Hour Forecast Curve
      var root3h = null;
      var xAxis3 = null;
      var sTemp = null;
      var sFeels = null;

      if (ub3hEl) {{
        root3h = am5.Root.new('wx-ub3h-chart');
        root3h.setThemes([am5themes_Animated.new(root3h), am5themes_Dark.new(root3h)]);

        var chart3h = root3h.container.children.push(
          am5xy.XYChart.new(root3h, {{
            panX: false,
            panY: false,
            paddingLeft: 0,
            paddingRight: 10
          }})
        );

        var xR3 = am5xy.AxisRendererX.new(root3h, {{ minGridDistance: 35 }});
        xR3.labels.template.setAll({{ fill: am5.color(0x94a3b8), fontSize: 11, fontFamily: 'JetBrains Mono, monospace' }});
        xAxis3 = chart3h.xAxes.push(am5xy.CategoryAxis.new(root3h, {{ categoryField: 'time_short', renderer: xR3 }}));

        var yR3 = am5xy.AxisRendererY.new(root3h, {{}});
        yR3.labels.template.setAll({{ fill: am5.color(0x94a3b8), fontSize: 11, fontFamily: 'JetBrains Mono, monospace' }});
        var yAxis3 = chart3h.yAxes.push(am5xy.ValueAxis.new(root3h, {{ renderer: yR3, numberFormat: \"#'°C'\" }}));

        var tip3 = am5.Tooltip.new(root3h, {{
          autoTextColor: false,
          getFillFromSprite: false,
          labelText: '[bold #38bdf8]{{stationName}} — {{time_short}}[/]\\n[#94a3b8]Температур:[/] [bold #fbbf24]{{temp_c}}°C[/] [#94a3b8](Мэдрэгдэх: {{feels_c}}°C)[/]\\n[#94a3b8]Төлөв / Салхи:[/] [bold #34d399]{{condition}} • {{wind_ms}} м/с[/]'
        }});
        tip3.get('background').setAll({{
          fill: am5.color(0x090d16),
          fillOpacity: 0.96,
          stroke: am5.color(0x38bdf8),
          strokeWidth: 1.5,
          cornerRadius: 8
        }});
        tip3.label.setAll({{ fill: am5.color(0xf8fafc), fontSize: 12, fontFamily: 'JetBrains Mono, monospace' }});

        sTemp = chart3h.series.push(
          am5xy.SmoothedXLineSeries.new(root3h, {{
            name: 'Агаарын хэм (°C)',
            xAxis: xAxis3,
            yAxis: yAxis3,
            valueYField: 'temp_c',
            categoryXField: 'time_short',
            stroke: am5.color(0xfbbf24),
            fill: am5.color(0xfbbf24),
            tooltip: tip3
          }})
        );
        sTemp.strokes.template.setAll({{ strokeWidth: 3 }});
        sTemp.fills.template.setAll({{ visible: true, fillOpacity: 0.14 }});
        sTemp.bullets.push(function() {{
          return am5.Bullet.new(root3h, {{
            sprite: am5.Circle.new(root3h, {{ radius: 4.5, fill: am5.color(0xfbbf24), stroke: am5.color(0x090d16), strokeWidth: 2 }})
          }});
        }});

        sFeels = chart3h.series.push(
          am5xy.SmoothedXLineSeries.new(root3h, {{
            name: 'Мэдрэгдэх хэм (°C)',
            xAxis: xAxis3,
            yAxis: yAxis3,
            valueYField: 'feels_c',
            categoryXField: 'time_short',
            stroke: am5.color(0x38bdf8)
          }})
        );
        sFeels.strokes.template.setAll({{ strokeWidth: 2, strokeDasharray: [4, 4] }});

        var cursor3 = chart3h.set('cursor', am5xy.XYCursor.new(root3h, {{ behavior: 'none', xAxis: xAxis3 }}));
        cursor3.lineY.set('visible', false);
      }}

      async function loadStationHeatmap(sid) {{
        var statusEl = document.getElementById('wx-hm-status');
        var apiLink = document.getElementById('wx-hm-api-link');
        var title3h = document.getElementById('wx-3h-title');
        if (apiLink) {{
          apiLink.href = '/forecast.json?sid=' + sid;
          apiLink.textContent = 'GET /forecast.json?sid=' + sid + ' →';
        }}
        if (statusEl) statusEl.textContent = 'Ачаалж байна (sid=' + sid + ')...';
        try {{
          var res = await fetch('/forecast.json?sid=' + encodeURIComponent(sid));
          var d = await res.json();
          var stName = (d.station || d.sum || 'Станц') + (d.aimag ? ' (' + d.aimag + ')' : '');
          var daysArr = d.heatmap_days || [];
          var hoursArr = d.heatmap_hours || [];
          var cellsArr = (d.heatmap_7d_24h || []).map(function(c) {{
            return Object.assign({{}}, c, {{ stationName: stName }});
          }});

          yAxis.data.setAll(daysArr.map(function(dy) {{ return {{ day: dy }}; }}));
          xAxis.data.setAll(hoursArr.map(function(hr) {{ return {{ hour: hr }}; }}));
          seriesHm.data.setAll(cellsArr);

          if (statusEl) {{
            statusEl.textContent = stName + ' • Сүүлийн 7 хоног × 24 цагийн (' + cellsArr.length + ' цагийн нүд) дулааны зураглал';
          }}

          if (xAxis3 && sTemp && sFeels) {{
            var h3 = (d.hourly_3h || []).map(function(item) {{
              var ts = item.time ? item.time.slice(5, 16) : '';
              return Object.assign({{}}, item, {{ time_short: ts, stationName: stName }});
            }});
            xAxis3.data.setAll(h3);
            sTemp.data.setAll(h3);
            sFeels.data.setAll(h3);
            if (title3h) {{
              title3h.textContent = '📈 ' + stName.toUpperCase() + ' — ИРЭХ 24–48 ЦАГИЙН ТЕМПЕРАТУР & МЭДРЭГДЭХ ХЭМИЙН МУРУЙ (amCharts 5)';
            }}
          }}
        }} catch (err) {{
          if (statusEl) statusEl.textContent = 'Өгөгдөл татахад алдаа гарлаа: ' + err;
        }}
      }}

      var hmSel = document.getElementById('wx-hm-sid-sel');
      if (hmSel) {{
        hmSel.addEventListener('change', function() {{
          loadStationHeatmap(hmSel.value);
        }});
      }}

      loadStationHeatmap('292');
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
    les_total = "272,869"
    les_llc = "207,923"
    les_fdi = "9,663"
    les_updated = "2026-11-04"
    try:
        async with wx_pool.acquire() as conn:
            lrow = await conn.fetchrow("SELECT total_entities, source_updated_at FROM les.metadata WHERE id = 1")
            if lrow and lrow["total_entities"]:
                les_total = f"{int(lrow['total_entities']):,}"
                if lrow["source_updated_at"]:
                    les_updated = str(lrow["source_updated_at"])[:10]
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
        <span class="stat-chip">STATUS: <strong style="color:#34d399;">5 MICROSERVICES OPERATIONAL</strong></span>
        <span class="stat-chip">USD/MNT: <strong>{html.escape(usd_rate)}</strong></span>
        <span class="stat-chip">ULAANBAATAR: <strong>{html.escape(ub_temp)} ({html.escape(ub_cond)})</strong></span>
        <span class="stat-chip">LEGAL_ENTITIES: <strong>{html.escape(les_total)} ААН</strong></span>
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

      <!-- 5. Legal Entities Open Data Registry (les.ulaach.cc) -->
      <div class="svc-card" style="grid-column: 1 / -1;">
        <div>
          <div class="svc-head">
            <span class="svc-domain" style="color:#f59e0b;">les.ulaach.cc</span>
            <span class="svc-live">● OPEN DATA API</span>
          </div>
          <h2 class="svc-title">Монгол Улсын Хуулийн Этгээдийн Улсын Бүртгэлийн Нээлттэй Өгөгдөл &amp; API</h2>
          <p class="svc-desc">Улсын бүртгэлийн <strong>{html.escape(les_total)}</strong> хуулийн этгээдийн регистрийн дугаар, оноосон нэр, бүртгэсэн огноо, хуулийн этгээдийн хэлбэр, үүсгэн байгуулагч / хувьцаа эзэмшигч, итгэмжлэлгүйгээр төлөөлөх захирал, эцсийн өмчлөгч (UBO), үйл ажиллагааны чиглэл ба өөрчлөн байгуулагдсан түүхийн бүх талбараар хайх систем (<code>/company.json</code>, <code>/search.json</code>, <code>/stats.json</code>).</p>
        </div>
        <div class="svc-metric" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px;">
          <div style="display:flex;justify-content:space-between;"><span class="muted">Нийт хуулийн этгээд:</span><strong style="color:#f59e0b;">{html.escape(les_total)} ААН</strong></div>
          <div style="display:flex;justify-content:space-between;"><span class="muted">ХХК / Гадаадын ХО:</span><strong style="color:#38bdf8;">{html.escape(les_llc)} / {html.escape(les_fdi)}</strong></div>
          <div style="display:flex;justify-content:space-between;"><span class="muted">Хайлтын индекс:</span><strong style="color:#34d399;">РД • Нэр • Хүн • Чиглэл</strong></div>
          <div style="display:flex;justify-content:space-between;"><span class="muted">Өгөгдлийн огноо:</span><span>{html.escape(les_updated)}</span></div>
        </div>
        <a class="svc-btn" style="background:rgba(245,158,11,0.14);color:#fbbf24;border-color:rgba(245,158,11,0.4);" href="https://les.ulaach.cc">les.ulaach.cc руу орох (Бүх талбараар хайх &amp; API) →</a>
      </div>
    </div>

    <div class="footer">ulaach.cc • Open Data &amp; Developer APIs Platform</div>
  </main>
</body>
</html>"""
    )
    return HTMLResponse(content=html_doc)


# ==============================================================================
# 5. LEGAL ENTITIES REGISTRY SERVICE (les.ulaach.cc)
# ==============================================================================
async def fetch_les_company(wx_pool: asyncpg.Pool, regno: str) -> dict[str, Any] | None:
    clean_reg = (regno or "").strip()
    if not clean_reg:
        return None
    async with wx_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT regno, name, registered_at, reg_year, legal_form, entity_type,
                   shareholders, executives, beneficial_owners, activities, reorganizations
            FROM les.entities
            WHERE regno = $1
            """,
            clean_reg,
        )
    if not row:
        return None
    return {
        "regno": row["regno"],
        "name": row["name"],
        "registered_at": row["registered_at"],
        "reg_year": row["reg_year"],
        "legal_form": row["legal_form"],
        "entity_type": row["entity_type"],
        "shareholders": json.loads(row["shareholders"]) if isinstance(row["shareholders"], str) else row["shareholders"],
        "executives": json.loads(row["executives"]) if isinstance(row["executives"], str) else row["executives"],
        "beneficial_owners": json.loads(row["beneficial_owners"]) if isinstance(row["beneficial_owners"], str) else row["beneficial_owners"],
        "activities": json.loads(row["activities"]) if isinstance(row["activities"], str) else row["activities"],
        "reorganizations": json.loads(row["reorganizations"]) if isinstance(row["reorganizations"], str) else row["reorganizations"],
    }


async def search_les_entities(
    wx_pool: asyncpg.Pool,
    q: str = "",
    field: str = "all",
    legal_form: str = "",
    entity_type: str = "",
    year: int | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    clean_q = (q or "").strip()
    clean_field = (field or "all").strip().lower()
    clean_form = (legal_form or "").strip()
    clean_type = (entity_type or "").strip()
    lim = max(1, min(int(limit or 25), 100))
    off = max(0, int(offset or 0))

    where_clauses: list[str] = []
    params: list[Any] = []

    if clean_q:
        if clean_field == "regno" or (clean_q.isdigit() and len(clean_q) == 7 and clean_field == "all"):
            params.append(clean_q)
            where_clauses.append(f"regno = ${len(params)}")
        elif clean_field == "name":
            params.append(f"%{clean_q}%")
            where_clauses.append(f"name ILIKE ${len(params)}")
        elif clean_field == "person":
            params.append(f"%{clean_q}%")
            where_clauses.append(f"people_text ILIKE ${len(params)}")
        elif clean_field == "activity":
            params.append(f"%{clean_q}%")
            where_clauses.append(f"activities_text ILIKE ${len(params)}")
        else:
            # All fields: if numeric prefix or general text, search name, people_text, activities_text, regno
            if clean_q.isdigit():
                params.append(f"{clean_q}%")
                where_clauses.append(f"regno LIKE ${len(params)}")
            else:
                params.append(f"%{clean_q}%")
                idx_p = len(params)
                where_clauses.append(
                    f"(name ILIKE ${idx_p} OR people_text ILIKE ${idx_p} OR activities_text ILIKE ${idx_p})"
                )

    if clean_form:
        params.append(clean_form)
        where_clauses.append(f"legal_form = ${len(params)}")
    if clean_type:
        params.append(clean_type)
        where_clauses.append(f"entity_type = ${len(params)}")
    if year is not None:
        params.append(int(year))
        where_clauses.append(f"reg_year = ${len(params)}")

    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # Order exact name match first when searching by name/all
    order_sql = "ORDER BY reg_year DESC NULLS LAST, registered_at DESC, regno DESC"
    if clean_q and not clean_q.isdigit() and clean_field in ("all", "name"):
        params.append(clean_q)
        exact_idx = len(params)
        order_sql = f"ORDER BY (LOWER(name) = LOWER(${exact_idx})) DESC, reg_year DESC NULLS LAST, registered_at DESC"

    params.append(lim)
    lim_idx = len(params)
    params.append(off)
    off_idx = len(params)

    query_sql = f"""
        SELECT regno, name, registered_at, reg_year, legal_form, entity_type,
               shareholders, executives, beneficial_owners, activities, reorganizations
        FROM les.entities
        {where_sql}
        {order_sql}
        LIMIT ${lim_idx} OFFSET ${off_idx}
    """

    async with wx_pool.acquire() as conn:
        rows = await conn.fetch(query_sql, *params)

    items = []
    for r in rows:
        sh = json.loads(r["shareholders"]) if isinstance(r["shareholders"], str) else r["shareholders"]
        ex = json.loads(r["executives"]) if isinstance(r["executives"], str) else r["executives"]
        ub = json.loads(r["beneficial_owners"]) if isinstance(r["beneficial_owners"], str) else r["beneficial_owners"]
        ac = json.loads(r["activities"]) if isinstance(r["activities"], str) else r["activities"]
        re_list = json.loads(r["reorganizations"]) if isinstance(r["reorganizations"], str) else r["reorganizations"]
        items.append(
            {
                "regno": r["regno"],
                "name": r["name"],
                "registered_at": r["registered_at"],
                "reg_year": r["reg_year"],
                "legal_form": r["legal_form"],
                "entity_type": r["entity_type"],
                "shareholders_count": len(sh),
                "executives_count": len(ex),
                "beneficial_owners_count": len(ub),
                "activities_count": len(ac),
                "shareholders": sh,
                "executives": ex,
                "beneficial_owners": ub,
                "activities": ac,
                "reorganizations": re_list,
            }
        )

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    return {
        "query": clean_q,
        "field": clean_field,
        "legal_form": clean_form or None,
        "entity_type": clean_type or None,
        "year": year,
        "limit": lim,
        "offset": off,
        "returned": len(items),
        "elapsed_ms": elapsed_ms,
        "items": items,
    }


async def fetch_les_stats(wx_pool: asyncpg.Pool) -> dict[str, Any]:
    async with wx_pool.acquire() as conn:
        mrow = await conn.fetchrow(
            """
            SELECT dataview_id, file_id, file_size_bytes, source_updated_at,
                   imported_at AT TIME ZONE 'Asia/Ulaanbaatar' AS imported_ub,
                   total_entities, summary_stats
            FROM les.metadata
            WHERE id = 1
            """
        )
    if not mrow:
        return {"total_entities": 0, "summary_stats": {}}
    st = json.loads(mrow["summary_stats"]) if isinstance(mrow["summary_stats"], str) else mrow["summary_stats"]
    return {
        "dataset": "Хуулийн этгээдийн улсын бүртгэлийн нээлттэй өгөгдөл",
        "dataview_id": mrow["dataview_id"],
        "file_id": mrow["file_id"],
        "file_size_mb": round(float(mrow["file_size_bytes"] or 0) / (1024 * 1024), 2),
        "source_updated_at": mrow["source_updated_at"],
        "imported_at_ub": mrow["imported_ub"].strftime("%Y-%m-%d %H:%M:%S") if mrow["imported_ub"] else None,
        "total_entities": mrow["total_entities"],
        "stats": st,
    }


async def render_les_page(wx_pool: asyncpg.Pool) -> HTMLResponse:
    stats_data = await fetch_les_stats(wx_pool)
    initial_search = await search_les_entities(wx_pool, q="", field="all", limit=15)

    st = stats_data.get("stats") or {}
    totals = st.get("totals") or {}
    by_form = st.get("by_legal_form") or []
    by_type = st.get("by_entity_type") or []
    by_year = st.get("by_year") or []

    total_ent = int(totals.get("total_entities") or stats_data.get("total_entities") or 272869)
    with_sh = int(totals.get("with_shareholders") or 0)
    with_ex = int(totals.get("with_executives") or 0)
    with_ubo = int(totals.get("with_ubo") or 0)
    with_act = int(totals.get("with_activities") or 0)
    with_reorg = int(totals.get("with_reorg") or 0)
    src_updated = str(stats_data.get("source_updated_at") or "2026-11-04")
    fsize_mb = stats_data.get("file_size_mb") or 440.15

    form_options_html = ['<option value="">Бүх хэлбэр (272,869)</option>']
    form_rows_html = []
    for idx_f, f_item in enumerate(by_form, start=1):
        lbl = str(f_item.get("label") or "")
        cnt = int(f_item.get("count") or 0)
        pct = (cnt * 100.0 / total_ent) if total_ent else 0.0
        form_options_html.append(f'<option value="{html.escape(lbl)}">{html.escape(lbl)} ({cnt:,})</option>')
        form_rows_html.append(
            f"<tr>"
            f"<td class='mono muted'>{idx_f:02d}</td>"
            f"<td class='bold'>{html.escape(lbl)}</td>"
            f"<td class='r mono bold' style='color:#38bdf8;'>{cnt:,}</td>"
            f"<td class='r mono' style='color:#34d399;'>{pct:.2f}%</td>"
            f"</tr>"
        )

    year_options_html = ['<option value="">Бүх он (1991–2026)</option>']
    for y_item in reversed(by_year):
        yr = y_item.get("year")
        cnt = int(y_item.get("count") or 0)
        if yr:
            year_options_html.append(f'<option value="{yr}">{yr} он ({cnt:,} ААН)</option>')

    by_year_json = json.dumps(by_year, ensure_ascii=False)
    initial_items_json = json.dumps(initial_search.get("items") or [], ensure_ascii=False)

    html_doc = (
        _common_head("les.ulaach.cc — Монгол Улсын Хуулийн Этгээдийн Бүртгэлийн Нээлттэй Өгөгдөл & API", "les")
        + f"""
  <main class="wrap">
    <section class="hero">
      <div class="mono" style="font-size:12px;color:#f59e0b;">LEGAL ENTITIES OPEN DATA REGISTRY // PG_TRGM FULL-FIELD INDEX</div>
      <h1>Монгол Улсын Хуулийн Этгээдийн Бүртгэлийн Нээлттэй Өгөгдөл &amp; Developer API</h1>
      <p>Монгол улсад бүртгэлтэй <strong>{total_ent:,}</strong> хуулийн этгээдийн регистрийн дугаар, оноосон нэр, бүртгэсэн огноо, хэлбэр, үүсгэн байгуулагч / хувьцаа эзэмшигч, итгэмжлэлгүйгээр төлөөлөх албан тушаалтан, эцсийн өмчлөгч (UBO), үйл ажиллагааны чиглэл ба өөрчлөн байгуулагдсан түүхийн лавлагаа.</p>
      <div class="status-strip">
        <span class="stat-chip">НИЙТ ААН: <strong style="color:#f59e0b;">{total_ent:,}</strong></span>
        <span class="stat-chip">УДИРДЛАГА: <strong style="color:#38bdf8;">{with_ex:,}</strong></span>
        <span class="stat-chip">ХУВЬЦАА ЭЗЭМШИГЧ: <strong style="color:#34d399;">{with_sh:,}</strong></span>
        <span class="stat-chip">ЭЦСИЙН ӨМЧЛӨГЧ: <strong>{with_ubo:,}</strong></span>
        <span class="stat-chip">ШИНЭЧЛЭГДСЭН: <strong>{html.escape(src_updated[:10])}</strong></span>
      </div>
    </section>

    <!-- 01. Interactive Full-Field Search Engine -->
    <div class="view-tabs-bar">
      <div class="view-tabs-list">
        <button type="button" class="view-tab-btn active" onclick="window.__switchViewTab('les', 'search', this)">🔍 Хуулийн Этгээд Хайх</button>
        <button type="button" class="view-tab-btn" onclick="window.__switchViewTab('les', 'stats', this)">📊 2005–2026 Оны График &amp; Хэлбэр</button>
        <button type="button" class="view-tab-btn" onclick="window.__switchViewTab('les', 'api', this)">⚡ Developer JSON API</button>
      </div>
      <button type="button" class="copy-btn" onclick="window.__switchViewTab('les', 'all', this)">Бүгдийг дэлгэх</button>
    </div>

    <section class="section-card" data-tab-group="les" data-tab-id="search">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">01</span> Бүх Талбараар Шууд Хайх Систем (РД, Нэр, Хувьцаа эзэмшигч, Захирал, Эцсийн өмчлөгч, Чиглэл)</h2>
        <span class="section-meta">PostgreSQL GIN Trigram Index • Хурд: ~1–5 ms</span>
      </div>
      <div class="section-body">
        <form id="les-search-form" style="display:grid;grid-template-columns:2fr 1.1fr 1.2fr 0.9fr auto;gap:10px;align-items:end;margin-bottom:14px;">
          <div>
            <label class="mono muted" style="display:block;font-size:11px;margin-bottom:5px;">ХАЙХ УТГА (РД, КОМПАНИЙН НЭР, ХҮНИЙ ОВОГ/НЭР, ҮЙЛ АЖИЛЛАГАА):</label>
            <input id="les-q" class="inp" type="text" placeholder="Жишээ: 5296722, Голомт, Чойжилсүрэн, програм хангамж..." style="width:100%;padding:9px 12px;font-size:13.5px;" />
          </div>
          <div>
            <label class="mono muted" style="display:block;font-size:11px;margin-bottom:5px;">ХАЙХ ТАЛБАР:</label>
            <select id="les-field" class="inp" style="width:100%;padding:9px 10px;">
              <option value="all">🔍 Бүх талбараар</option>
              <option value="name">🏢 Оноосон нэрээр</option>
              <option value="regno">🔢 Регистрийн дугаараар (РД)</option>
              <option value="person">👤 Захирал / Хувьцаа эзэмшигч / Эцсийн өмчлөгчөөр</option>
              <option value="activity">⚙️ Үйл ажиллагааны чиглэлээр</option>
            </select>
          </div>
          <div>
            <label class="mono muted" style="display:block;font-size:11px;margin-bottom:5px;">ХУУЛИЙН ЭТГЭЭДИЙН ХЭЛБЭР:</label>
            <select id="les-form" class="inp" style="width:100%;padding:9px 10px;">
              {''.join(form_options_html)}
            </select>
          </div>
          <div>
            <label class="mono muted" style="display:block;font-size:11px;margin-bottom:5px;">БҮРТГЭСЭН ОН:</label>
            <select id="les-year" class="inp" style="width:100%;padding:9px 10px;">
              {''.join(year_options_html)}
            </select>
          </div>
          <div>
            <button type="submit" class="run-btn" style="padding:9px 18px;font-size:13px;height:38px;">Хайх →</button>
          </div>
        </form>

        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:14px;">
          <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap;">
            <span class="mono muted" style="font-size:11.5px;">Жишээ хайлт:</span>
            <button type="button" class="copy-btn les-Preset" data-q="Голомт" data-field="name">🏢 Голомт</button>
            <button type="button" class="copy-btn les-Preset" data-q="Таван богд" data-field="all">🏢 Таван богд</button>
            <button type="button" class="copy-btn les-Preset" data-q="Оюу толгой" data-field="name">🏢 Оюу толгой</button>
            <button type="button" class="copy-btn les-Preset" data-q="Чойжилсүрэн" data-field="person">👤 Чойжилсүрэн (Хүнээр)</button>
            <button type="button" class="copy-btn les-Preset" data-q="Оджаргал" data-field="person">👤 Оджаргал (Хүнээр)</button>
            <button type="button" class="copy-btn les-Preset" data-q="програм хангамж" data-field="activity">⚙️ Програм хангамж (Чиглэлээр)</button>
          </div>
          <div id="les-search-meta" class="mono muted" style="font-size:12px;">Сүүлд бүртгэгдсэн 15 хуулийн этгээд</div>
        </div>

        <div id="les-detail-box" style="display:none;margin-bottom:18px;background:var(--bg-code);border:1px solid #f59e0b;border-radius:10px;padding:18px;"></div>

        <div class="table-wrap">
          <table style="table-layout:fixed;width:100%;">
            <thead>
              <tr>
                <th style="width:78px;">РД</th>
                <th style="width:17%;">ОНООСОН НЭР</th>
                <th style="width:92px;">БҮРТГЭСЭН</th>
                <th style="width:14%;">ХЭЛБЭР / ТӨРӨЛ</th>
                <th style="width:15%;">УДИРДЛАГА</th>
                <th style="width:15%;">ХУВЬЦАА / UBO</th>
                <th>ҮЙЛ АЖИЛЛАГАА</th>
                <th class="r" style="width:76px;">ЛАВЛАХ</th>
              </tr>
            </thead>
            <tbody id="les-results-tbody"></tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- 02. Шууд дуудах Developer JSON API Endpoint-ууд -->
    <section class="section-card" data-tab-group="les" data-tab-id="api" style="display:none;">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">02</span> Шууд дуудах JSON API Endpoint-ууд</h2>
        <span class="section-meta">Content-Type: application/json; charset=utf-8</span>
      </div>
      <div class="section-body">
        <div class="ep-list">
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/company.json?regno=2585065" target="_blank">/company.json?regno=2585065</a>
            </div>
            <span class="ep-desc">Регистрийн дугаараар (7 цифр) хуулийн этгээдийн бүх мэдээллийг (хувьцаа эзэмшигч, захирал, UBO, чиглэл, өөрчлөлт) шууд авах</span>
          </div>
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/search.json?q=Голомт&field=name&limit=10" target="_blank">/search.json?q=Голомт&amp;field=name&amp;limit=10</a>
            </div>
            <span class="ep-desc">Бүх талбараар хайх API (<code>field=all|name|regno|person|activity</code>, <code>legal_form</code>, <code>entity_type</code>, <code>year</code>, <code>limit</code>, <code>offset</code>)</span>
          </div>
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/search.json?q=Чойжилсүрэн&field=person&limit=10" target="_blank">/search.json?q=Чойжилсүрэн&amp;field=person&amp;limit=10</a>
            </div>
            <span class="ep-desc">Хувьцаа эзэмшигч, гүйцэтгэх удирдлага болон эцсийн өмчлөгчийн овог/нэрээр хамаарал бүхий компаниудыг шүүж олох</span>
          </div>
          <div class="ep-item">
            <div class="ep-left">
              <span class="method-get">GET</span>
              <a class="ep-url" href="/stats.json" target="_blank">/stats.json</a>
            </div>
            <span class="ep-desc">Улсын бүртгэлийн нийт хуулийн этгээдийн статистик (хэлбэр, төрөл, 1991–2026 оны бүртгэлийн динамик ба шинэчлэгдсэн огноо)</span>
          </div>
        </div>

        <div class="code-split">
          <div class="code-pane">
            <div class="code-pane-bar">
              <span>cURL &amp; JavaScript Fetch жишээ</span>
              <button class="copy-btn" data-copy="les-curl-code">Copy</button>
            </div>
            <pre id="les-curl-code"># 1. Регистрийн дугаараар компанийн бүрэн мэдээлэл авах
curl -s "https://les.ulaach.cc/company.json?regno=2585065" | jq .

# 2. Захирал / Хувьцаа эзэмшигч / Эцсийн өмчлөгчийн нэрээр хайх
curl -s "https://les.ulaach.cc/search.json?q=Чойжилсүрэн&amp;field=person&amp;limit=10" | jq .

# 3. Үйл ажиллагааны чиглэл ба оноор давхар шүүх
curl -s "https://les.ulaach.cc/search.json?q=програм&amp;field=activity&amp;year=2024"</pre>
          </div>
          <div class="code-pane">
            <div class="code-pane-bar">
              <span>200 OK — /company.json?regno=2585065</span>
            </div>
            <pre>{{
  "regno": "2585065",
  "name": "Хурдхүнс",
  "registered_at": "2005.12.21",
  "reg_year": 2005,
  "legal_form": "Хязгаарлагдмал хариуцлагатай компани",
  "entity_type": "Ашгийн төлөө",
  "shareholders": [
    {{
      "kind": "Иргэн",
      "country": "Монгол Улс",
      "last_name": "баттогтох",
      "first_name": "чойжилсүрэн",
      "gender": "Эр",
      "registered_at": "2005.12.21"
    }}
  ],
  "executives": [
    {{
      "title": "Гүйцэтгэх захирал",
      "country": "Монгол Улс",
      "last_name": "Баттогтох",
      "first_name": "Чойжилсүрэн",
      "gender": "Эр",
      "registered_at": "2005.12.21"
    }}
  ]
}}</pre>
          </div>
        </div>
      </div>
    </section>

    <!-- 03. Registration Trends Chart (amCharts 5) & Legal Form Breakdown -->
    <section class="section-card" data-tab-group="les" data-tab-id="stats" style="display:none;">
      <div class="section-head">
        <h2 class="section-title"><span class="step-badge">03</span> Жил Бүрийн Шинээр Бүртгэгдсэн Хуулийн Этгээдийн Тоо (2005–2026) &amp; Хэлбэрийн Ангилал</h2>
        <span class="section-meta">amCharts 5 Interactive Chart • GET /stats.json</span>
      </div>
      <div class="section-body">
        <div style="background:#0B0F17 !important;color:#F8FAFC !important;border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:20px;">
          <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:10px;">
            <div>
              <div class="mono bold" style="font-size:13px;color:#f8fafc !important;">📊 ОН ТУС БҮРЭЭР БҮРТГЭГДСЭН ХУУЛИЙН ЭТГЭЭДИЙН ТОО (2005–2026)</div>
              <div style="font-size:12px;color:#94a3b8 !important;">Багана дээр хулгана ойртуулж тухайн онд бүртгэгдсэн ААН-ийн тоог харна уу (багана дээр дарвал тухайн оноор шүүнэ)</div>
            </div>
          </div>
          <div id="les-year-chart" style="width:100%;height:290px;"></div>
        </div>

        <div class="mono bold" style="font-size:12.5px;color:#38bdf8;margin-bottom:8px;">📋 ХУУЛИЙН ЭТГЭЭДИЙН ХЭЛБЭРИЙН ЗАДАРГАА (ТОП 12 АНГИЛАЛ)</div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th style="width:60px;">#</th>
                <th>ХУУЛИЙН ЭТГЭЭДИЙН ХЭЛБЭР (LEGAL FORM)</th>
                <th class="r">НИЙТ ТОО</th>
                <th class="r">ЭЗЛЭХ ХУВЬ</th>
              </tr>
            </thead>
            <tbody>
              {''.join(form_rows_html)}
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <div class="footer">les.ulaach.cc • Legal Entities Registry Open Data API • Powered by ulaach.cc</div>
  </main>

  <script src="https://cdn.amcharts.com/lib/5/index.js"></script>
  <script src="https://cdn.amcharts.com/lib/5/xy.js"></script>
  <script src="https://cdn.amcharts.com/lib/5/themes/Animated.js"></script>
  <script>
    (function() {{
      var INITIAL_ITEMS = {initial_items_json};
      var BY_YEAR = {by_year_json}.filter(function(r) {{ return r.year >= 2005; }});

      function esc(s) {{
        var d = document.createElement('div');
        d.textContent = s == null ? '' : String(s);
        return d.innerHTML;
      }}

      function renderPeopleSummary(list, isExec) {{
        if (!list || !list.length) return '<span class="muted">—</span>';
        var parts = list.slice(0, 2).map(function(p) {{
          var full = ((p.last_name || '') + ' ' + (p.first_name || '')).trim();
          if (!full && p.country) full = p.country;
          var role = isExec ? (p.title ? '<span class="muted" style="font-size:11px;">[' + esc(p.title) + ']</span> ' : '') : '';
          return role + '<strong style="color:#f8fafc;">' + esc(full || '—') + '</strong>';
        }});
        if (list.length > 2) {{
          parts.push('<span class="mono muted" style="font-size:11px;">(+' + (list.length - 2) + ')</span>');
        }}
        return parts.join('<br>');
      }}

      function renderActivitiesSummary(list) {{
        if (!list || !list.length) return '<span class="muted">—</span>';
        var first = list[0].description || '';
        if (first.length > 44) first = first.slice(0, 42) + '…';
        var extra = list.length > 1 ? ' <span class="mono" style="color:#38bdf8;font-size:11px;">(+' + (list.length - 1) + ')</span>' : '';
        return esc(first) + extra;
      }}

      var currentItemsMap = {{}};

      function showEntityDetail(regno) {{
        var item = currentItemsMap[regno];
        var box = document.getElementById('les-detail-box');
        if (!item || !box) return;

        var shRows = (item.shareholders || []).map(function(p) {{
          return '<tr><td>' + esc(p.kind) + '</td><td>' + esc(p.country) + '</td><td class="bold">' + esc(p.last_name) + ' ' + esc(p.first_name) + '</td><td>' + esc(p.gender) + '</td><td class="mono">' + esc(p.registered_at) + '</td></tr>';
        }}).join('') || '<tr><td colspan="5" class="muted">Бүртгэлгүй</td></tr>';

        var exRows = (item.executives || []).map(function(p) {{
          return '<tr><td style="color:#38bdf8;">' + esc(p.title) + '</td><td>' + esc(p.country) + '</td><td class="bold">' + esc(p.last_name) + ' ' + esc(p.first_name) + '</td><td>' + esc(p.gender) + '</td><td class="mono">' + esc(p.registered_at) + '</td></tr>';
        }}).join('') || '<tr><td colspan="5" class="muted">Бүртгэлгүй</td></tr>';

        var uboRows = (item.beneficial_owners || []).map(function(p) {{
          return '<tr><td>' + esc(p.kind) + '</td><td class="bold">' + esc(p.last_name) + ' ' + esc(p.first_name) + '</td><td>' + esc(p.country) + '</td><td>' + esc(p.gender) + '</td></tr>';
        }}).join('') || '<tr><td colspan="4" class="muted">Бүртгэлгүй</td></tr>';

        var actBadges = (item.activities || []).map(function(a) {{
          return '<div style="background:#0f1523;border:1px solid #1e293b;border-radius:6px;padding:6px 10px;font-size:12px;"><span class="mono" style="color:#34d399;font-size:11px;">[' + esc(a.registered_at || '—') + ']</span> ' + esc(a.description) + '</div>';
        }}).join('') || '<div class="muted">Чиглэл бүртгэгдээгүй</div>';

        var reorgHtml = '';
        if (item.reorganizations && item.reorganizations.length) {{
          var rRows = item.reorganizations.map(function(r) {{
            return '<tr><td>' + esc(r.mode) + '</td><td class="mono">' + esc(r.registered_at) + '</td><td class="bold">' + esc(r.prev_name) + '</td><td style="white-space:normal;">' + esc(r.note) + '</td></tr>';
          }}).join('');
          reorgHtml = '<div style="margin-top:12px;"><div class="mono bold" style="font-size:12px;color:#f43f5e;margin-bottom:6px;">🔄 ӨӨРЧЛӨН БАЙГУУЛАГДСАН ТҮҮХ:</div><div class="table-wrap"><table><thead><tr><th>ХЭЛБЭР</th><th>ОГНОО</th><th>ӨМНӨХ НЭР</th><th>ТЭМДЭГЛЭЛ</th></tr></thead><tbody>' + rRows + '</tbody></table></div></div>';
        }}

        box.innerHTML =
          '<div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:12px;margin-bottom:14px;border-bottom:1px solid #1e293b;padding-bottom:12px;">' +
            '<div>' +
              '<div class="mono" style="font-size:12px;color:#f59e0b;">РЕГИСТРИЙН ДУГААР: <strong>' + esc(item.regno) + '</strong> • БҮРТГЭСЭН: <strong>' + esc(item.registered_at) + '</strong></div>' +
              '<h3 style="margin:4px 0;font-size:20px;color:#f8fafc;">' + esc(item.name) + '</h3>' +
              '<div style="font-size:12.5px;color:#94a3b8;">' + esc(item.legal_form) + ' • <span style="color:#34d399;">' + esc(item.entity_type) + '</span></div>' +
            '</div>' +
            '<div style="display:flex;gap:8px;">' +
              '<a class="run-btn" href="/company.json?regno=' + encodeURIComponent(item.regno) + '" target="_blank" style="text-decoration:none;">JSON API →</a>' +
              '<button type="button" class="copy-btn" onclick="document.getElementById(\\'les-detail-box\\').style.display=\\'none\\'">Хаах ✕</button>' +
            '</div>' +
          '</div>' +
          '<div style="display:grid;grid-template-columns:1fr;gap:12px;">' +
            '<div><div class="mono bold" style="font-size:12px;color:#38bdf8;margin-bottom:6px;">👔 ИТГЭМЖЛЭЛГҮЙГЭЭР ТӨЛӨӨЛӨХ АЛБАН ТУШААЛТАН (' + (item.executives || []).length + '):</div>' +
              '<div class="table-wrap"><table><thead><tr><th>АЛБАН ТУШААЛ</th><th>УЛС</th><th>ОВОГ НЭР</th><th>ХҮЙС</th><th>БҮРТГЭСЭН</th></tr></thead><tbody>' + exRows + '</tbody></table></div></div>' +
            '<div><div class="mono bold" style="font-size:12px;color:#34d399;margin-bottom:6px;">🤝 ХУВЬЦАА ЭЗЭМШИГЧ, ГИШҮҮНИЙ МЭДЭЭЛЭЛ (' + (item.shareholders || []).length + '):</div>' +
              '<div class="table-wrap"><table><thead><tr><th>АНГИЛАЛ</th><th>УЛС</th><th>ОВОГ НЭР</th><th>ХҮЙС</th><th>БҮРТГЭСЭН</th></tr></thead><tbody>' + shRows + '</tbody></table></div></div>' +
            '<div><div class="mono bold" style="font-size:12px;color:#fbbf24;margin-bottom:6px;">🛡️ ЭЦСИЙН ӨМЧЛӨГЧ / UBO (' + (item.beneficial_owners || []).length + '):</div>' +
              '<div class="table-wrap"><table><thead><tr><th>АНГИЛАЛ</th><th>ОВОГ НЭР</th><th>УЛС</th><th>ХҮЙС</th></tr></thead><tbody>' + uboRows + '</tbody></table></div></div>' +
            '<div><div class="mono bold" style="font-size:12px;color:#a78bfa;margin-bottom:6px;">⚙️ ҮЙЛ АЖИЛЛАГААНЫ ЧИГЛЭЛҮҮД (' + (item.activities || []).length + '):</div>' +
              '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px;">' + actBadges + '</div></div>' +
            reorgHtml +
          '</div>';
        box.style.display = 'block';
        box.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
      }}
      window.showEntityDetail = showEntityDetail;

      function renderTableRows(items) {{
        var tbody = document.getElementById('les-results-tbody');
        currentItemsMap = {{}};
        if (!items || !items.length) {{
          tbody.innerHTML = '<tr><td colspan="8" class="c muted" style="padding:24px;">Тохирох хуулийн этгээд олдсонгүй. Хайх үгээ өөрчилж үзнэ үү.</td></tr>';
          return;
        }}
        var htmlRows = items.map(function(it) {{
          currentItemsMap[it.regno] = it;
          var shOrUbo = (it.shareholders && it.shareholders.length) ? it.shareholders : (it.beneficial_owners || []);
          return '<tr style="cursor:pointer;" onclick="window.showEntityDetail(\\'' + esc(it.regno) + '\\')">' +
            '<td class="mono bold" style="color:#f59e0b;">' + esc(it.regno) + '</td>' +
            '<td class="bold" style="color:#f8fafc;white-space:normal;word-break:break-word;">' + esc(it.name) + '</td>' +
            '<td class="mono muted">' + esc(it.registered_at || '—') + '</td>' +
            '<td style="white-space:normal;word-break:break-word;font-size:12px;">' + esc(it.legal_form) + '<br><span class="mono muted" style="font-size:11px;">' + esc(it.entity_type) + '</span></td>' +
            '<td style="white-space:normal;word-break:break-word;font-size:12px;">' + renderPeopleSummary(it.executives, true) + '</td>' +
            '<td style="white-space:normal;word-break:break-word;font-size:12px;">' + renderPeopleSummary(shOrUbo, false) + '</td>' +
            '<td style="white-space:normal;word-break:break-word;font-size:12px;color:#cbd5e1;">' + renderActivitiesSummary(it.activities) + '</td>' +
            '<td class="r"><button type="button" class="copy-btn" style="padding:4px 8px;font-size:11px;" onclick="event.stopPropagation();window.showEntityDetail(\\'' + esc(it.regno) + '\\')">Харах</button></td>' +
          '</tr>';
        }}).join('');
        tbody.innerHTML = htmlRows;
      }}

      function runLesSearch() {{
        var q = document.getElementById('les-q').value.trim();
        var field = document.getElementById('les-field').value;
        var form = document.getElementById('les-form').value;
        var year = document.getElementById('les-year').value;
        var metaEl = document.getElementById('les-search-meta');
        metaEl.textContent = 'Хайж байна...';

        var params = new URLSearchParams();
        if (q) params.set('q', q);
        if (field) params.set('field', field);
        if (form) params.set('legal_form', form);
        if (year) params.set('year', year);
        params.set('limit', '30');

        fetch('/search.json?' + params.toString())
          .then(function(r) {{ return r.json(); }})
          .then(function(data) {{
            var items = data.items || [];
            renderTableRows(items);
            metaEl.textContent = 'Илэрц: ' + items.length + ' ААН • Хайлтын хугацаа: ' + (data.elapsed_ms || 0) + ' ms';
            if (items.length === 1) {{
              showEntityDetail(items[0].regno);
            }} else {{
              document.getElementById('les-detail-box').style.display = 'none';
            }}
          }})
          .catch(function() {{
            metaEl.textContent = 'Хайлт амжилтгүй боллоо.';
          }});
      }}

      document.getElementById('les-search-form').addEventListener('submit', function(e) {{
        e.preventDefault();
        runLesSearch();
      }});
      document.getElementById('les-form').addEventListener('change', runLesSearch);
      document.getElementById('les-year').addEventListener('change', runLesSearch);

      document.querySelectorAll('.les-Preset').forEach(function(btn) {{
        btn.addEventListener('click', function() {{
          document.getElementById('les-q').value = btn.getAttribute('data-q') || '';
          document.getElementById('les-field').value = btn.getAttribute('data-field') || 'all';
          document.getElementById('les-form').value = '';
          document.getElementById('les-year').value = '';
          runLesSearch();
        }});
      }});

      document.querySelectorAll('.copy-btn[data-copy]').forEach(function(btn) {{
        btn.addEventListener('click', function() {{
          var tid = btn.getAttribute('data-copy');
          var el = document.getElementById(tid);
          if (!el) return;
          navigator.clipboard.writeText(el.innerText.trim());
          var old = btn.textContent;
          btn.textContent = 'Copied!';
          setTimeout(function() {{ btn.textContent = old; }}, 1200);
        }});
      }});

      renderTableRows(INITIAL_ITEMS);

      // Render amCharts 5 Year Trend Chart
      if (window.am5 && window.am5xy && BY_YEAR.length) {{
        var root = am5.Root.new('les-year-chart');
        root.setThemes([am5themes_Animated.new(root)]);
        var chart = root.container.children.push(
          am5xy.XYChart.new(root, {{
            panX: false,
            panY: false,
            layout: root.verticalLayout
          }})
        );
        var xRenderer = am5xy.AxisRendererX.new(root, {{ minGridDistance: 28 }});
        xRenderer.labels.template.setAll({{
          fill: am5.color(0x94a3b8),
          fontSize: 11,
          fontFamily: 'JetBrains Mono, monospace'
        }});
        xRenderer.grid.template.setAll({{ stroke: am5.color(0x1e293b), strokeOpacity: 0.5 }});
        var xAxis = chart.xAxes.push(
          am5xy.CategoryAxis.new(root, {{
            categoryField: 'yearStr',
            renderer: xRenderer
          }})
        );
        var yRenderer = am5xy.AxisRendererY.new(root, {{}});
        yRenderer.labels.template.setAll({{
          fill: am5.color(0x94a3b8),
          fontSize: 11,
          fontFamily: 'JetBrains Mono, monospace'
        }});
        yRenderer.grid.template.setAll({{ stroke: am5.color(0x1e293b), strokeOpacity: 0.6 }});
        var yAxis = chart.yAxes.push(
          am5xy.ValueAxis.new(root, {{ renderer: yRenderer, min: 0 }})
        );

        var series = chart.series.push(
          am5xy.ColumnSeries.new(root, {{
            name: 'Бүртгэгдсэн ААН',
            xAxis: xAxis,
            yAxis: yAxis,
            valueYField: 'count',
            categoryXField: 'yearStr',
            tooltip: am5.Tooltip.new(root, {{
              autoTextColor: false,
              labelText: '[bold #ffffff]{{categoryX}} он: {{valueY}} ААН[/]'
            }})
          }})
        );
        series.columns.template.setAll({{
          cornerRadiusTL: 5,
          cornerRadiusTR: 5,
          fill: am5.color(0xf59e0b),
          strokeOpacity: 0,
          cursorOverStyle: 'pointer'
        }});
        series.columns.template.states.create('hover', {{
          fill: am5.color(0x38bdf8)
        }});
        series.columns.template.events.on('click', function(ev) {{
          var ctx = ev.target.dataItem && ev.target.dataItem.dataContext;
          if (ctx && ctx.year) {{
            document.getElementById('les-year').value = String(ctx.year);
            runLesSearch();
          }}
        }});

        var chartData = BY_YEAR.map(function(d) {{
          return {{ year: d.year, yearStr: String(d.year), count: d.count }};
        }});
        xAxis.data.setAll(chartData);
        series.data.setAll(chartData);
        chart.set('cursor', am5xy.XYCursor.new(root, {{ behavior: 'none' }}));
      }}
    }})();
  </script>
</body>
</html>"""
    )
    return HTMLResponse(content=html_doc)
