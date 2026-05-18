"""一次性脚本:基于 lens.db 生成"去过的城市"地图 HTML。

数据:photos.derived.location_bucket.city + exif.gps 经纬度。
坐标:db 里是 WGS-84,转 GCJ-02 后画在高德 tile 上(国内显示对齐)。
输出:~/.life_lens/reports/cities_map_<timestamp>.html,自动 open。
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# 复用项目里现成的 WGS-84 → GCJ-02 转换
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from life_lens.geocode.amap import wgs84_to_gcj02


def main():
    db_path = Path.home() / ".life_lens" / "lens.db"
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """
        SELECT
            json_extract(derived, '$.location_bucket.province') AS province,
            json_extract(derived, '$.location_bucket.city') AS city,
            COUNT(*) AS photos,
            AVG(json_extract(exif, '$.gps.lat')) AS lat,
            AVG(json_extract(exif, '$.gps.lng')) AS lng
        FROM photos
        WHERE source != 'seed'
          AND json_extract(derived, '$.location_bucket.city') IS NOT NULL
          AND json_extract(exif, '$.gps.lat') IS NOT NULL
        GROUP BY province, city
        ORDER BY photos DESC
        """
    ).fetchall()
    conn.close()

    data = []
    for province, city, photos, lat, lng in rows:
        glat, glng = wgs84_to_gcj02(lat, lng)
        label = city if province == city else f"{province} · {city}"
        data.append({
            "label": label,
            "count": photos,
            "lat": glat,
            "lng": glng,
        })

    total_cities = len(data)
    total_photos = sum(d["count"] for d in data)

    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    html = html.replace("__CITIES__", str(total_cities))
    html = html.replace("__PHOTOS__", str(total_photos))
    html = html.replace("__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M"))

    out_dir = Path.home() / ".life_lens" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    fp = out_dir / f"cities_map_{ts}.html"
    fp.write_text(html, encoding="utf-8")

    print(f"已生成: {fp}")
    print(f"  {total_cities} 个城市 / {total_photos} 张照片")
    subprocess.run(["open", str(fp)])


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>life_lens · 去过的城市</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html, body, #map { margin: 0; height: 100%; font-family: -apple-system, "PingFang SC", sans-serif; }
  .title-bar {
    position: absolute; top: 14px; left: 56px; z-index: 1000;
    background: rgba(255,255,255,0.96); padding: 8px 14px;
    border-radius: 6px; box-shadow: 0 1px 6px rgba(0,0,0,0.2);
    font-size: 13px; color: #333;
  }
  .title-bar b { font-size: 14px; color: #1a73e8; }
  .meta { font-size: 11px; color: #888; margin-top: 2px; }
  .city-popup { font-size: 13px; }
  .city-popup b { font-size: 14px; color: #1a73e8; }
  .city-popup .count { color: #555; }
</style>
</head>
<body>
<div id="map"></div>
<div class="title-bar">
  <b>life_lens</b> · 去过的城市
  <div class="meta">__CITIES__ 个城市 · __PHOTOS__ 张有 GPS 的照片 · 生成于 __GENERATED__</div>
</div>
<script>
const data = __DATA__;

const map = L.map('map', { zoomControl: true }).setView([35, 105], 4);

// 高德矢量地图 tile(中文,GCJ-02 坐标系)
L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}', {
  subdomains: ['1', '2', '3', '4'],
  maxZoom: 18,
  attribution: '&copy; 高德地图'
}).addTo(map);

const bounds = [];
data.forEach(d => {
  // 圆 marker 大小按 sqrt(count) 缩放(线性会让北京一家独大其他全是小点)
  const radius = Math.min(45, 9 + Math.sqrt(d.count) * 2.4);
  const m = L.circleMarker([d.lat, d.lng], {
    radius: radius,
    color: '#1a73e8',
    fillColor: '#1a73e8',
    fillOpacity: 0.55,
    weight: 2
  }).addTo(map);
  m.bindPopup(
    '<div class="city-popup"><b>' + d.label + '</b><br>' +
    '<span class="count">' + d.count + ' 张照片</span></div>'
  );
  m.bindTooltip(d.label + ' (' + d.count + ')', { permanent: false, direction: 'top', offset: [0, -radius - 4] });
  bounds.push([d.lat, d.lng]);
});

if (bounds.length) {
  map.fitBounds(bounds, { padding: [60, 60], maxZoom: 8 });
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
