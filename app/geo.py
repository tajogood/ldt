"""좌표 변환과 거리 계산."""
from __future__ import annotations

import math
from functools import lru_cache

from pyproj import Transformer

# 행정안전부 지방행정인허가 데이터: 보정계수 없는 Bessel 중부원점TM
# EPSG:5174 정의는 pyproj 기본값에 towgs84가 들어 있어 그대로 쓰면 된다.
SRC_CRS = {
    "5174": "EPSG:5174",
    "5181": "EPSG:5181",   # 카카오 내부 좌표(WCONGNAMUL 아님)
    "5186": "EPSG:5186",
    "4326": "EPSG:4326",
}

KOREA_BBOX = (32.8, 39.0, 124.5, 132.0)  # lat_min, lat_max, lon_min, lon_max

EARTH_R_KM = 6371.0088


@lru_cache(maxsize=8)
def _tf(src: str) -> Transformer:
    return Transformer.from_crs(SRC_CRS.get(src, src), "EPSG:4326", always_xy=True)


def to_wgs84(x: float, y: float, src: str = "5174") -> tuple[float, float] | None:
    """(x, y) → (lat, lon). 한국 범위를 벗어나면 None."""
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError):
        return None
    if src == "4326":
        lon, lat = x, y
    else:
        lon, lat = _tf(src).transform(x, y)
    if not in_korea(lat, lon):
        return None
    return round(lat, 6), round(lon, 6)


def in_korea(lat: float | None, lon: float | None) -> bool:
    if lat is None or lon is None:
        return False
    a, b, c, d = KOREA_BBOX
    return a <= lat <= b and c <= lon <= d


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R_KM * math.asin(math.sqrt(h))


def bbox(lat: float, lon: float, km: float) -> tuple[float, float, float, float]:
    """반경 km를 덮는 위경도 사각형 (lat_min, lat_max, lon_min, lon_max)."""
    dlat = km / 111.0
    dlon = km / (111.0 * max(math.cos(math.radians(lat)), 1e-6))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon
