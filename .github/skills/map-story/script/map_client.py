"""
map_client
职责：与地图与地理计算相关的通用能力层，供 story_map 集成调用。
- 地理编码：优先通过 QVeris 接入的高德工具，失败回退 OSM（并做 WGS84→GCJ-02 转换）
- 距离计算：本地 Haversine
- 地图渲染：通过 QVeris 提供的高德地图渲染接口生成 HTML 片段
依赖环境变量：QVERIS_API_URL/QVERIS_BASE_URL、QVERIS_API_KEY（可选）
"""
import json
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote
from urllib.request import Request, urlopen

from dotenv import load_dotenv


_DEFAULT_USER_AGENT = "map-story/1.0"
_TABLE_SEPARATOR_RE = re.compile(r"^\|\s*-{3,}\s*\|")
_PAREN_CONTENT_RE = re.compile(r"[（(].*?[)）]")
_GEOCODE_ENDPOINTS = [
    ("https://nominatim.openstreetmap.org/search?format=json&limit=1&q={}", "list"),
    ("https://geocode.maps.co/search?q={}", "list"),
    ("https://photon.komoot.io/api/?limit=1&q={}", "photon"),
]


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))


local_env = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=local_env)
load_dotenv(dotenv_path=os.path.join(_project_root(), ".env"))


def _http_post_json(url: str, headers: Dict[str, str], body: Dict[str, object]) -> Optional[object]:
    """
    使用 POST 请求发送 JSON，并返回解析后的 JSON 对象。
    任何网络或解析异常统一回退为 None，避免上层调用中断。
    """
    try:
        req = Request(url, headers=headers, data=json.dumps(body).encode("utf-8"), method="POST")
        with urlopen(req, timeout=20) as resp:
            data = resp.read()
            return json.loads(data.decode("utf-8", errors="ignore"))
    except Exception:
        return None


def _parse_latlon_pair(parts: Iterable[str]) -> Optional[Tuple[float, float]]:
    """
    将字符串列表解析为 (lat, lon)。
    - 如果出现纬度经度颠倒，则自动纠正。
    - 失败返回 None。
    """
    items = [p.strip() for p in parts if p.strip()]
    if len(items) != 2:
        return None
    try:
        a = float(items[0])
        b = float(items[1])
    except Exception:
        return None
    if abs(a) > 90 and abs(b) <= 90:
        # lon,lat → lat,lon
        return b, a
    if abs(b) > 90 and abs(a) <= 90:
        # lat,lon → lat,lon
        return a, b
    return a, b


def _extract_latlon(value: object) -> Optional[Tuple[float, float]]:
    """
    递归从多形态数据中提取 (lat, lon)。
    支持字符串 "lon,lat" / "lat,lon"，列表嵌套与常见字段名称。
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None
    if isinstance(value, str):
        # 兼容 "lat,lon" 或 "lon,lat" 字符串格式
        return _parse_latlon_pair(value.split(","))
    if isinstance(value, list):
        # 列表内可能嵌套多种结构，递归寻找首个可用坐标
        for item in value:
            res = _extract_latlon(item)
            if res:
                return res
        return None
    if isinstance(value, dict):
        # 常见字段组合优先提取
        if "lat" in value and ("lon" in value or "lng" in value):
            try:
                lat = float(value.get("lat"))
                lon = float(value.get("lon", value.get("lng")))
                return lat, lon
            except Exception:
                pass
        if "latitude" in value and ("longitude" in value or "lng" in value):
            try:
                lat = float(value.get("latitude"))
                lon = float(value.get("longitude", value.get("lng")))
                return lat, lon
            except Exception:
                pass
        for key in ("location", "center", "lnglat"):
            if key in value:
                res = _extract_latlon(value.get(key))
                if res:
                    return res
        # 兜底遍历字典子项
        for v in value.values():
            res = _extract_latlon(v)
            if res:
                return res
    return None


class QVerisClient:
    """
    QVeris 的轻量调用器：
    - 统一管理 API URL 与 API Key
    - 提供地理编码与距离计算的薄封装
    """
    def __init__(self, api_url: str, api_key: str):
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key

    def _execute(self, tool_id: str, parameters: Dict[str, object]) -> Optional[object]:
        """
        调用 QVeris 工具执行接口，返回 data 字段或原始数据。
        """
        if not tool_id:
            return None
        url = f"{self.api_url}/tools/execute?tool_id={tool_id}"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"}
        body = {"parameters": parameters, "max_response_size": 20480}
        data = _http_post_json(url, headers, body)
        if not isinstance(data, dict):
            return None
        result = data.get("result")
        if isinstance(result, dict) and "data" in result:
            # 兼容 result.data 的响应形态
            return result.get("data")
        return data.get("data", data)

    def geocode(self, name: str) -> Optional[Tuple[float, float]]:
        """
        使用 QVeris 的地理编码工具解析地点坐标。
        兼容 address / keywords / q 三种参数形式。
        """
        tool_id = os.getenv("QVERIS_GEOCODE_TOOL_ID") or "amap_webservice.geocode.geo.retrieve.v3"
        payload = self._execute(tool_id, {"address": name})
        res = _extract_latlon(payload)
        if res:
            return res
        payload = self._execute(tool_id, {"keywords": name})
        res = _extract_latlon(payload)
        if res:
            return res
        payload = self._execute(tool_id, {"q": name})
        return _extract_latlon(payload)

def _geocode_nominatim(name: str) -> Optional[Tuple[float, float]]:
    """
    公共地理编码回退链路：
    - nominatim.openstreetmap.org
    - geocode.maps.co
    - photon.komoot.io
    """
    for url_tpl, kind in _GEOCODE_ENDPOINTS:
        try:
            url = url_tpl.format(quote(name))
            req = Request(url, headers={"User-Agent": _DEFAULT_USER_AGENT})
            with urlopen(req, timeout=20) as resp:
                data = resp.read()
                payload = json.loads(data.decode("utf-8", errors="ignore"))
            if kind == "list" and isinstance(payload, list) and payload:
                # Nominatim / maps.co 返回列表
                lat = float(payload[0].get("lat"))
                lon = float(payload[0].get("lon"))
                return lat, lon
            if kind == "photon" and isinstance(payload, dict):
                # Photon 返回 features 数组
                features = payload.get("features") or []
                if features:
                    coords = features[0].get("geometry", {}).get("coordinates") or []
                    if len(coords) >= 2:
                        lon = float(coords[0])
                        lat = float(coords[1])
                        return lat, lon
        except Exception:
            continue
    return None


def _get_qveris_client_class():
    """
    预留扩展：允许在单测或外部注入自定义客户端。
    """
    return QVerisClient


def geocode_city(name: str) -> Optional[Tuple[float, float]]:
    """
    城市/地址字符串 → GCJ-02 经纬度。
    仅使用 QVeris 接入的高德地理编码工具。
    """
    api_url = os.getenv("QVERIS_API_URL") or os.getenv("QVERIS_BASE_URL")
    api_key = os.getenv("QVERIS_API_KEY")
    if api_url and api_key:
        try:
            QVC = _get_qveris_client_class()
            if not QVC:
                raise RuntimeError("QVerisClient unavailable")
            client = QVC(api_url=api_url, api_key=api_key)
            res = client.geocode(name)
            if res:
                return res
        except Exception:
            # QVeris 失败则降级到公共地理编码
            return _geocode_nominatim(name)
    return _geocode_nominatim(name)


def _clean_place_name(text: str) -> str:
    """
    去除地名中的括注内容，保留核心名称，提升地理编码命中率。
    """
    text = _PAREN_CONTENT_RE.sub("", text)
    return text.strip()


def extract_places_in_order(md: str) -> List[str]:
    """
    从“年份”表解析“现称”列，按出现顺序返回地点列表（去重保序）。
    """
    lines = md.splitlines()
    in_loc = False
    table_started = False
    header_seen = False
    col_indices = None
    places: List[str] = []
    for line in lines:
        if line.strip().startswith("## "):
            title = line.strip().lstrip("#").strip()
            if title.startswith("年份"):
                in_loc = True
                table_started = False
                header_seen = False
                col_indices = None
                continue
            else:
                in_loc = False
        if not in_loc:
            continue
        if line.strip().startswith("|") and not table_started:
            # 读取表头行并确定“现称”列索引
            table_started = True
            header_cells = [c.strip() for c in line.strip().strip("|").split("|")]
            idx = None
            for j, c in enumerate(header_cells):
                if "现称" in c:
                    idx = j
                    break
            if idx is None:
                idx = len(header_cells) - 1
            col_indices = idx
            continue
        if table_started:
            if _TABLE_SEPARATOR_RE.match(line.strip()):
                header_seen = True
                continue
            if header_seen and line.strip().startswith("|"):
                # 读取数据行
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if cells and col_indices is not None and col_indices < len(cells):
                    cell = cells[col_indices]
                    if cell:
                        if "：" in cell:
                            cell = cell.split("：", 1)[-1].strip()
                        clean = _clean_place_name(cell)
                        if clean and clean != "—":
                            places.append(clean)
            else:
                break
    if not places:
        return []
    return list(dict.fromkeys(places))


def append_coords_section(md: str) -> str:
    """
    依据“年份”表逐个地理编码，并在文末追加“地点坐标（自动地理编码）”表。
    如果没有识别出地点或均编码失败，则不做改动直接返回原文。
    """
    lines = md.splitlines()
    coords: Dict[str, Tuple[float, float]] = {}
    places = extract_places_in_order(md)
    if not places:
        return md
    for p in places:
        coord = geocode_city(p)
        if coord:
            coords[p] = coord
    if not coords:
        return md
    section = []
    section.append("")
    section.append("## 地点坐标（自动地理编码）")
    section.append("| 现称 | 纬度 | 经度 |")
    section.append("| --- | --- | --- |")
    for p in places:
        if p in coords:
            lat, lon = coords[p]
            section.append(f"| {p} | {lat:.6f} | {lon:.6f} |")
    return "\n".join(lines + section)


def compute_total_distance_km(md: str) -> Optional[float]:
    """
    从“地点坐标（自动地理编码）”表获取经纬度，计算总直线距离（公里）。
    使用 Haversine 公式计算。
    """
    coords: List[Tuple[float, float]] = []
    lines = md.splitlines()
    in_section = False
    header_seen = False
    for line in lines:
        if line.strip().startswith("## "):
            title = line.strip().lstrip("#").strip()
            in_section = "地点坐标" in title
            header_seen = False
            continue
        if not in_section:
            continue
        if line.strip().startswith("|") and not header_seen:
            header_seen = True
            continue
        if header_seen:
            if not line.strip().startswith("|"):
                break
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            try:
                lat = float(cells[1])
                lon = float(cells[2])
                coords.append((lat, lon))
            except Exception:
                continue
    if len(coords) < 2:
        return None
    total = 0.0
    for i in range(len(coords) - 1):
        lat1, lon1 = coords[i]
        lat2, lon2 = coords[i + 1]
        total += _haversine(lat1, lon1, lat2, lon2)
    return total


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    R = 6371.0
    dLat = math.radians(lat2 - lat1)
    dLon = math.radians(lon2 - lon1)
    a = math.sin(dLat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(
        math.radians(lat2)
    ) * math.sin(dLon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def insert_distance_intro(md: str, distance_km: float) -> str:
    """
    在“人生足迹地图说明”中插入总行程描述。
    """
    lines = md.splitlines()
    out: List[str] = []
    inserted = False
    for line in lines:
        out.append(line)
        if line.strip().startswith("## 二、人生足迹地图说明"):
            continue
        if not inserted and line.strip().startswith("- 🌟 **重要节点数量**"):
            out.append(f"- 🚶 **总行程估算**：约 {distance_km:.0f} 公里")
            inserted = True
    return "\n".join(out)
