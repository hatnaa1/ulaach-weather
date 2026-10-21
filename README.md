# ulaach-weather (`weather.ulaach.cc`)

Монгол орны 21 аймаг, 321 сум/станцын цаг агаар, нар мандах/жаргах цаг, агаарын чанарын индекс (AQI), ой хээрийн түймрийн эрсдэлийн мэдээ болон олон хэлний (`MN`, `EN`, `CN`, `JP`, `KR`) IFrame виджет сервис.

- **Live Web & Docs:** [https://weather.ulaach.cc](https://weather.ulaach.cc)
- **Ecosystem Portal:** [https://ulaach.cc](https://ulaach.cc)

## JSON Endpoints
- `GET /weather.json` — 21 аймгийн төв ба Улаанбаатар хотын (28 гол станц, эсвэл `?all_stations=true` бүх 321 станц) бодит ажиглалт + нар мандах/жаргах цаг
- `GET /aqi.json` — Улаанбаатар болон орон нутгийн агаарын чанарын индекс (`AQI`, `PM2.5`, `PM10`, `SO2`, `NO2`, `CO`, `O3`)
- `GET /forecast.json?sid=292` — 3 цаг тутмын болон 5 хоногийн урьдчилсан мэдээ
- `GET /fire-risk.json` — Ой, хээрийн түймрийн эрсдэлийн зэрэглэл ба сэрэмжлүүлэг мэдээ
- `GET /weather.html?sid=292&lang=MN&theme=dark` — Олон хэлний (`MN`, `EN`, `CN`, `JP`, `KR`) Dark/Light IFrame виджет
