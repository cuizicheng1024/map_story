"""
简要说明：
- 读取人物生平 Markdown，解析“年份”表中的地点与事件列
- 调用 geocode_city 获取 GCJ-02 坐标
- 生成可交互 HTML 地图：支持行政/地形/Esri 多种底图，连线展示顺序，Markdown 弹窗显示大事
"""
import argparse
import os
import re
import time
from typing import Dict, List, Optional

from dotenv import load_dotenv
from map_client import (
    append_coords_section,
    compute_total_distance_km,
    geocode_city,
    insert_distance_intro,
)
from map_html_renderer import (
    build_info_panel_html,
    render_osm_html,
    render_profile_html,
)
from story_agents import (
    StoryAgentLLM,
    extract_historical_figures,
    generate_historical_markdown,
    save_markdown,
)


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))


local_env = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(dotenv_path=local_env)
load_dotenv(dotenv_path=os.path.join(_project_root(), ".env"))


def _parse_timeline_table(md: str) -> tuple[List[str], List[List[str]]]:
    """
    解析“年份”表，返回表头与行数据。
    """
    lines = md.splitlines()
    in_sec = False
    header: List[str] = []
    rows: List[List[str]] = []
    table_started = False
    header_seen = False
    for line in lines:
        if line.strip().startswith("## "):
            title = line.strip().lstrip("#").strip()
            in_sec = title.startswith("年份")
            table_started = False
            header_seen = False
            header = []
            continue
        if not in_sec:
            continue
        if line.strip().startswith("|") and not table_started:
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            table_started = True
            continue
        if table_started:
            if re.match(r"^\|\s*-{3,}\s*\|", line.strip()):
                header_seen = True
                continue
            if header_seen and line.strip().startswith("|"):
                rows.append([c.strip() for c in line.strip().strip("|").split("|")])
            else:
                break
    return header, rows


def _parse_basic_info(md: str) -> Dict[str, str]:
    """
    解析“人物档案/基本信息”小节，提取键值对（如姓名、朝代、出生等）。
    """
    lines = md.splitlines()
    in_profile = False
    in_basic = False
    info: Dict[str, str] = {}
    for line in lines:
        if line.strip().startswith("## "):
            title = line.strip().lstrip("#").strip()
            in_profile = "人物档案" in title
            in_basic = False
            continue
        if not in_profile:
            continue
        if line.strip().startswith("### "):
            title = line.strip().lstrip("#").strip()
            in_basic = "基本信息" in title
            continue
        if in_basic:
            m = re.match(r"-\s*\*\*(.+?)\*\*：\s*(.+)", line.strip())
            if m:
                info[m.group(1).strip()] = m.group(2).strip()
    return info


def _parse_overview(md: str) -> str:
    """
    解析“人物档案/生平概述”内容，用于生成简介文本。
    """
    lines = md.splitlines()
    in_profile = False
    in_overview = False
    buf: List[str] = []
    for line in lines:
        if line.strip().startswith("## "):
            title = line.strip().lstrip("#").strip()
            in_profile = "人物档案" in title
            if not in_profile:
                in_overview = False
            continue
        if not in_profile:
            continue
        if line.strip().startswith("### "):
            title = line.strip().lstrip("#").strip()
            in_overview = "生平概述" in title
            continue
        if in_overview:
            t = line.strip()
            if not t or re.match(r"^-{3,}$", t):
                continue
            buf.append(t)
    return "".join(buf).strip()


def _extract_works(text: str) -> List[str]:
    if not text:
        return []
    items = re.findall(r"《([^》]+)》", text)
    seen = set()
    works: List[str] = []
    for item in items:
        name = item.strip()
        if name and name not in seen:
            seen.add(name)
            works.append(name)
    return works


def _split_quote_lines(text: str) -> List[str]:
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"[；;]\s*", text) if p.strip()]
    return parts


def _parse_location_sections(md: str) -> List[Dict[str, str]]:
    """
    解析“人生历程/重要地点”段落为结构化地点事件列表。
    """
    lines = md.splitlines()
    in_section = False
    current: Dict[str, str] | None = None
    locations: List[Dict[str, str]] = []
    for line in lines:
        if line.strip().startswith("## "):
            title = line.strip().lstrip("#").strip()
            if "人生历程" in title or "重要地点" in title:
                in_section = True
                current = None
                continue
            if in_section:
                break
        if not in_section:
            continue
        if line.strip().startswith("### "):
            if current:
                locations.append(current)
            raw_title = line.strip().lstrip("#").strip()
            loc_type = "normal"
            if "出生地" in raw_title:
                loc_type = "birth"
            elif "去世地" in raw_title:
                loc_type = "death"
            if "：" in raw_title:
                name = raw_title.split("：", 1)[-1].strip()
            else:
                name = raw_title
            name = re.sub(r"^[^0-9A-Za-z\u4e00-\u9fff]+", "", name).strip()
            current = {
                "name": name,
                "type": loc_type,
                "time": "",
                "location": "",
                "event": "",
                "significance": "",
                "duration": "",
                "quotes": "",
            }
            continue
        if current:
            m = re.match(r"-\s*\*\*(.+?)\*\*：\s*(.+)", line.strip())
            if m:
                key = m.group(1).strip()
                val = m.group(2).strip()
                if key in {"时间", "时段", "时期", "年代", "公元纪年", "年号纪年"}:
                    current["time"] = val
                elif key in {"位置", "地点"}:
                    current["location"] = val
                elif key in {"事迹", "背景", "经过", "事件"}:
                    current["event"] = (current["event"] + " " + val).strip()
                elif key in {"意义", "影响"}:
                    current["significance"] = val
                elif key in {"停留", "停留时间", "停留时长", "居留", "驻留", "逗留", "在此时间", "在此时长"}:
                    current["duration"] = val
                elif key in {"名篇名句", "代表名句", "名句", "诗句"}:
                    current["quotes"] = (current["quotes"] + "；" + val).strip("；")
    if current:
        locations.append(current)
    return locations


def _split_ancient_modern(loc_text: str) -> tuple[str, str]:
    """
    将“古称（今 XX）”文本拆分为古称/现代地名。
    """
    if not loc_text:
        return "", ""
    modern_parts = re.findall(r"[（(]今([^）)]+)[）)]", loc_text)
    modern = " / ".join([p.strip() for p in modern_parts if p.strip()])
    ancient = re.sub(r"[（(]今[^）)]+[）)]", "", loc_text)
    ancient = re.sub(r"\s+", " ", ancient).strip(" /")
    return ancient.strip(), modern.strip()


def _pick_geocode_name(text: str) -> str:
    """
    为地理编码选取最稳妥的候选名称。
    """
    if not text:
        return ""
    for sep in [" / ", "/", "或", "、"]:
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    text = re.sub(r"[（(].*?[）)]", "", text).strip()
    return text


def _extract_title_from_text(text: str) -> str:
    m = re.search(r"“([^”]+)”", text)
    if m:
        return m.group(1).strip()
    return ""


def _parse_date_location(text: str, keys: List[str]) -> tuple[str, str]:
    date = ""
    m = re.search(r"\d{3,4}年", text)
    if m:
        date = m.group(0)
    loc = ""
    for k in keys:
        if k in text:
            loc = text.split(k, 1)[-1].strip("。；; ")
            break
    if not loc:
        parts = re.split(r"[，,]", text, maxsplit=1)
        if len(parts) > 1:
            loc = parts[1].strip("。；; ")
    return date, loc


def _parse_coords_table(md: str) -> Dict[str, tuple[float, float]]:
    """
    解析“地点坐标”表，提供名称到经纬度的缓存映射。
    """
    lines = md.splitlines()
    in_section = False
    table_started = False
    header_seen = False
    idx_name = None
    idx_lat = None
    idx_lon = None
    coords: Dict[str, tuple[float, float]] = {}
    for line in lines:
        if line.strip().startswith("## "):
            title = line.strip().lstrip("#").strip()
            in_section = "地点坐标" in title
            table_started = False
            header_seen = False
            idx_name = None
            idx_lat = None
            idx_lon = None
            continue
        if not in_section:
            continue
        if line.strip().startswith("|") and not table_started:
            table_started = True
            header = [c.strip() for c in line.strip().strip("|").split("|")]
            for i, c in enumerate(header):
                if "现称" in c or "地点" in c:
                    idx_name = i
                if "纬度" in c or "lat" in c.lower():
                    idx_lat = i
                if "经度" in c or "lon" in c.lower() or "lng" in c.lower():
                    idx_lon = i
            continue
        if table_started:
            if re.match(r"^\|\s*-{3,}\s*\|", line.strip()):
                header_seen = True
                continue
            if header_seen and line.strip().startswith("|"):
                row = [c.strip() for c in line.strip().strip("|").split("|")]
                if idx_name is None or idx_lat is None or idx_lon is None:
                    continue
                if idx_name >= len(row) or idx_lat >= len(row) or idx_lon >= len(row):
                    continue
                name = _pick_geocode_name(row[idx_name])
                try:
                    lat = float(row[idx_lat])
                    lon = float(row[idx_lon])
                except Exception:
                    continue
                if name:
                    coords[name] = (lat, lon)
            else:
                break
    return coords


def _build_profile_data(md: str) -> Optional[Dict[str, object]]:
    """
    汇总人物档案与地点数据，形成完整人物页渲染所需结构。
    """
    info = _parse_basic_info(md)
    locations = _parse_location_sections(md)
    if not info or not locations:
        return None
    name_raw = info.get("姓名", "")
    name = name_raw.split("（", 1)[0].strip() or name_raw.strip()
    title = (
        _extract_title_from_text(info.get("历史地位", ""))
        or _extract_title_from_text(name_raw)
        or ""
    )
    description = _parse_overview(md)
    if not description:
        description = "；".join(
            [t for t in [info.get("历史地位", ""), info.get("主要成就", "")] if t]
        )
    description = re.sub(r"-{3,}$", "", description).strip()
    works = _extract_works(" ".join([description, info.get("主要成就", ""), info.get("历史地位", "")]))
    birth_text = info.get("出生", "")
    death_text = info.get("去世", "")
    birth_date, birth_loc = _parse_date_location(birth_text, ["出生于", "生于"])
    death_date, death_loc = _parse_date_location(death_text, ["卒于", "去世于", "卒"])
    lifespan = info.get("享年", "")
    birth_ancient, birth_modern = _split_ancient_modern(birth_loc)
    death_ancient, death_modern = _split_ancient_modern(death_loc)
    birth_geo = _pick_geocode_name(birth_modern or birth_loc)
    death_geo = _pick_geocode_name(death_modern or death_loc)
    birth_coord = geocode_city(birth_geo) if birth_geo else None
    death_coord = geocode_city(death_geo) if death_geo else None
    dynasty = (info.get("时代", "") or info.get("朝代", "")).strip()
    avatar = ""
    person = {
        "name": name or "人物",
        "title": title,
        "description": description,
        "quote": title,
        "dynasty": dynasty,
        "birthplace": birth_loc,
        "avatar": avatar,
        "birth": {
            "date": birth_date,
            "location": birth_loc,
            "lat": birth_coord[0] if birth_coord else None,
            "lng": birth_coord[1] if birth_coord else None,
        },
        "death": {
            "date": death_date,
            "location": death_loc,
            "lat": death_coord[0] if death_coord else None,
            "lng": death_coord[1] if death_coord else None,
        },
        "lifespan": lifespan,
    }
    coords_cache = _parse_coords_table(md)
    loc_items: List[Dict[str, object]] = []
    for loc in locations:
        loc_text = loc.get("location") or loc.get("name") or ""
        ancient, modern = _split_ancient_modern(loc_text)
        geo_name = _pick_geocode_name(modern or loc.get("name") or ancient)
        coord = None
        # 优先使用 Markdown 中自动写入的坐标表，降低地理编码调用次数
        if geo_name:
            coord = coords_cache.get(geo_name)
        if not coord and modern:
            coord = coords_cache.get(_pick_geocode_name(modern))
        if not coord and loc.get("name"):
            coord = coords_cache.get(_pick_geocode_name(loc.get("name") or ""))
        if not coord and geo_name:
            # 坐标表缺失时才触发在线地理编码
            coord = geocode_city(geo_name)
        if not coord:
            continue
        works = _extract_works(" ".join([loc.get("event", ""), loc.get("significance", "")]))
        quote_lines = _split_quote_lines(loc.get("quotes", ""))
        loc_items.append(
            {
                "name": loc.get("name") or geo_name,
                "ancientName": ancient or loc.get("name") or "",
                "modernName": modern or loc_text,
                "lat": coord[0],
                "lng": coord[1],
                "type": loc.get("type", "normal"),
                "event": loc.get("event", ""),
                "time": loc.get("time", ""),
                "duration": loc.get("duration", ""),
                "significance": loc.get("significance", ""),
                "works": works,
                "quoteLines": quote_lines,
            }
        )
    if not loc_items:
        return None
    for loc in loc_items:
        quote_lines = loc.get("quoteLines") or []
        if quote_lines:
            person["quote"] = quote_lines[0]
            break
    map_style = {
        "pathColor": "#1e40af",
        "markers": {
            "normal": {
                "iconUrl": "https://a.amap.com/jsapi_demos/static/demo-center/icons/poi-marker-default.png",
                "color": "#3498db",
            },
            "birth": {
                "iconUrl": "https://a.amap.com/jsapi_demos/static/demo-center/icons/poi-marker-green.png",
                "color": "#2ecc71",
            },
            "death": {
                "iconUrl": "https://a.amap.com/jsapi_demos/static/demo-center/icons/poi-marker-red.png",
                "color": "#e74c3c",
            },
        },
    }
    return {"person": person, "locations": loc_items, "mapStyle": map_style}


def parse_places(md: str) -> List[Dict[str, str]]:
    """
    从 Markdown 中解析“年份”表，提取古称/现称列。
    返回每行字典：{"ancient": 古称, "modern": 现称}
    """
    header, rows = _parse_timeline_table(md)
    if not header or not rows:
        return []
    idx_ancient = None
    idx_modern = None
    for i, c in enumerate(header):
        if "古称" in c:
            idx_ancient = i
        if "现称" in c:
            idx_modern = i
    if idx_ancient is None and idx_modern is None:
        return []
    res: List[Dict[str, str]] = []
    for row in rows:
        a = row[idx_ancient] if idx_ancient is not None and idx_ancient < len(row) else ""
        b = row[idx_modern] if idx_modern is not None and idx_modern < len(row) else ""
        if "：" in a:
            a = a.split("：", 1)[-1].strip()
        if "：" in b:
            b = b.split("：", 1)[-1].strip()
        a = re.sub(r"[（）()].*?[）)]", "", a).strip()
        b = re.sub(r"[（）()].*?[）)]", "", b).strip()
        if a or b:
            res.append({"ancient": a, "modern": b})
    return res


def parse_events(md: str) -> List[Dict[str, str]]:
    """
    从 Markdown 中解析“年份”表，提取 年号纪年/公元纪年/事件简述 三列。
    返回每行字典：{"era": ..., "ad": ..., "desc": ...}
    """
    header, rows = _parse_timeline_table(md)
    if not header or not rows:
        return []
    idx_era = None
    idx_ad = None
    idx_desc = None
    for i, c in enumerate(header):
        if "年号" in c:
            idx_era = i
        if "公元" in c:
            idx_ad = i
        if "事件" in c:
            idx_desc = i
    if idx_era is None and idx_ad is None and idx_desc is None:
        return []
    res: List[Dict[str, str]] = []
    for row in rows:
        era = row[idx_era] if idx_era is not None and idx_era < len(row) else ""
        ad = row[idx_ad] if idx_ad is not None and idx_ad < len(row) else ""
        desc = row[idx_desc] if idx_desc is not None and idx_desc < len(row) else ""
        if era or ad or desc:
            res.append({"era": era, "ad": ad, "desc": desc})
    return res


def _summarize_samples(items: List[str], limit: int = 3) -> str:
    if not items:
        return ""
    samples = items[:limit]
    more = len(items) - len(samples)
    sample_text = "、".join(samples)
    if more > 0:
        return f"{sample_text} 等 {more} 个"
    return sample_text


def _collect_quality_metrics(md: str) -> Dict[str, int]:
    header, rows = _parse_timeline_table(md)
    places = parse_places(md)
    locations = _parse_location_sections(md)
    coords = _parse_coords_table(md)
    return {
        "timeline_rows": len(rows),
        "places": len(places),
        "locations": len(locations),
        "coords": len(coords),
    }


def _validate_data_quality(md: str) -> List[str]:
    issues: List[str] = []
    header, rows = _parse_timeline_table(md)
    if not header or not rows:
        issues.append("年份表缺失或为空")
    else:
        if not any("现称" in c for c in header):
            issues.append("年份表缺少现称列")
        if not any("事件" in c for c in header):
            issues.append("年份表缺少事件列")
    locations = _parse_location_sections(md)
    if not locations:
        issues.append("重要地点段落缺失或为空")
    else:
        missing_event = [l for l in locations if not (l.get("event") or "").strip()]
        if missing_event and len(missing_event) >= max(1, len(locations) // 2):
            issues.append(f"重要地点事迹缺失较多（{len(missing_event)} / {len(locations)}）")
    places = parse_places(md)
    place_names = []
    for p in places:
        name = p.get("modern") or p.get("ancient") or ""
        name = _pick_geocode_name(name)
        if name:
            place_names.append(name)
    coords = _parse_coords_table(md)
    if place_names and not coords:
        issues.append("地点坐标表缺失或为空")
    if coords:
        invalid = []
        for name, coord in coords.items():
            lat, lon = coord
            if abs(lat) > 90 or abs(lon) > 180:
                invalid.append(name)
        if invalid:
            issues.append(f"地点坐标存在异常范围：{_summarize_samples(invalid)}")
        missing = []
        for name in place_names:
            if name not in coords:
                missing.append(name)
        if missing:
            issues.append(f"地点坐标缺失：{_summarize_samples(missing)}")
    return issues


def _print_quality_report(md: str) -> None:
    metrics = _collect_quality_metrics(md)
    issues = _validate_data_quality(md)
    print("数据质量检查：")
    print(f"- 年份表行数：{metrics['timeline_rows']}")
    print(f"- 地点条目：{metrics['places']}")
    print(f"- 坐标条目：{metrics['coords']}")
    print(f"- 结构化地点：{metrics['locations']}")
    if issues:
        for item in issues:
            print(f"- {item}")
    else:
        print("- 未发现明显问题")


def _format_seconds(sec: float) -> str:
    return f"{sec:.2f}s"


def build_points(places: List[Dict[str, str]], events: List[Dict[str, str]]) -> List[Dict[str, object]]:
    """
    将地点列表转为带坐标与弹窗内容的点位：
    - 对每个地点进行地理编码
    - 优先收集包含该地名的事件；无匹配则取前若干条
    - 弹窗内容使用 Markdown 列表
    """
    pts: List[Dict[str, object]] = []
    for p in places:
        name = p.get("modern") or p.get("ancient") or ""
        if not name:
            continue
        coord = geocode_city(name)
        if not coord:
            continue
        lat, lon = coord
        matched = []
        for e in events:
            d = e.get("desc") or ""
            if name and name in d:
                matched.append(e)
        lines = [f"**{name}**", ""]
        items = matched[:6] if matched else events[:3]
        for e in items:
            era = e.get("era", "")
            ad = e.get("ad", "")
            desc = e.get("desc", "")
            lines.append(f"- {era} / {ad}：{desc}")
        md = "\n".join(lines)
        pts.append({"name": name, "lat": lat, "lon": lon, "md": md})
    return pts


def _extract_intro_fields(md: str) -> Dict[str, str]:
    """
    从“简介”版块提取字段，用于信息面板展示。
    """
    lines = md.splitlines()
    in_intro = False
    fields = {"朝代": "", "身份": "", "生卒年": "", "主要事件": "", "主要作品": "", "历史地位": "", "一生行程": ""}
    for line in lines:
        if line.strip().startswith("## "):
            title = line.strip().lstrip("#").strip()
            in_intro = (title == "简介")
            continue
        if not in_intro:
            continue
        if line.strip().startswith("## "):
            break
        t = line.strip()
        if "：" in t:
            k, v = t.split("：", 1)
            k = k.strip()
            v = v.strip()
            if k in fields:
                fields[k] = v
    if any(fields.values()):
        return fields
    info = _parse_basic_info(md)
    if info:
        if not fields["朝代"]:
            fields["朝代"] = info.get("时代", "") or info.get("朝代", "")
        if not fields["身份"]:
            fields["身份"] = info.get("主要身份", "")
        if not fields["历史地位"]:
            fields["历史地位"] = info.get("历史地位", "")
        if not fields["主要事件"]:
            fields["主要事件"] = info.get("主要成就", "")
        if not fields["生卒年"]:
            birth_text = info.get("出生", "")
            death_text = info.get("去世", "")
            birth_date, _ = _parse_date_location(birth_text, ["出生于", "生于"])
            death_date, _ = _parse_date_location(death_text, ["卒于", "去世于", "卒"])
            if birth_date or death_date:
                fields["生卒年"] = f"{birth_date}-{death_date}".strip("-")
            else:
                merged = " / ".join([t for t in [birth_text, death_text] if t])
                fields["生卒年"] = merged
    in_section = False
    for line in lines:
        if line.strip().startswith("## "):
            title = line.strip().lstrip("#").strip()
            if "人生足迹地图说明" in title:
                in_section = True
                continue
            if in_section:
                break
        if not in_section:
            continue
        if "：" not in line:
            continue
        label = ""
        m = re.search(r"\*\*(.+?)\*\*", line)
        if m:
            label = m.group(1).strip()
        val = line.split("：", 1)[-1].strip()
        if label == "行程概览":
            fields["一生行程"] = val
            break
        if not fields["一生行程"] and label in {"时间跨度", "地理范围"}:
            fields["一生行程"] = val
    return fields


def render_html(title: str, points: List[Dict[str, object]], md: str = "") -> str:
    """
    优先输出完整人物页；若缺少结构化信息则回退为基础地图页。
    """
    if md:
        profile = _build_profile_data(md)
        if profile:
            return render_profile_html(profile)
        fields = _extract_intro_fields(md)
        if any(fields.values()):
            info_panel_html = build_info_panel_html(title, fields)
            return render_osm_html(title, points, info_panel_html)
    return render_osm_html(title, points, "")


def save_html(person: str, content: str) -> str:
    """
    将 HTML 内容写入 story_map/目录，文件名取人物名。
    """
    root = _project_root()
    folder = os.path.join(root, "story_map")
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r'[\\\\/:*?"<>|]', "_", person).strip() or "map"
    path = os.path.join(folder, f"{safe}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def run_interactive() -> None:
    """
    交互模式：
    - 输入人物或一句包含人物的句子
    - 生成并输出地图文件路径
    """
    client = StoryAgentLLM()
    while True:
        try:
            text = input("请输入人物或一句包含人物的句子（q 退出）：").strip()
        except EOFError:
            break
        if not text:
            continue
        if text.lower() in {"q", "quit", "exit"}:
            print("已退出。")
            break
        targets = extract_historical_figures(client, text)
        if not targets:
            print("未识别到历史人物")
            continue
        print(f"识别到人物数量：{len(targets)}")
        stats = {"markdown": 0, "html": 0, "failed": 0}
        for person in targets:
            print(f"正在生成 {person} 生平文档，可能需要一些时间...")
            t0 = time.perf_counter()
            t_step = time.perf_counter()
            md = generate_historical_markdown(client, person)
            t_md = time.perf_counter() - t_step
            if not md:
                print(f"未取得：{person}")
                stats["failed"] += 1
                continue
            km = compute_total_distance_km(md)
            if isinstance(km, float):
                md = insert_distance_intro(md, km)
            print("正在进行地点地理编码，可能需要一些时间...")
            t_step = time.perf_counter()
            md = append_coords_section(md)
            t_geo = time.perf_counter() - t_step
            _print_quality_report(md)
            saved = save_markdown(person, md)
            print(f"已生成：{saved}")
            t_step = time.perf_counter()
            try:
                places = parse_places(md)
                events = parse_events(md)
                pts = build_points(places, events)
                html = render_html(person, pts, md=md)
            except Exception:
                html = render_osm_html(person, [], "")
            t_render = time.perf_counter() - t_step
            out = save_html(person, html)
            print(out)
            total = time.perf_counter() - t0
            print(
                f"耗时：生平生成 {_format_seconds(t_md)}，地理编码 {_format_seconds(t_geo)}，"
                f"地图渲染 {_format_seconds(t_render)}，总计 {_format_seconds(total)}"
            )
            stats["markdown"] += 1
            stats["html"] += 1
        print(
            f"本次完成：人物 {len(targets)}，文档 {stats['markdown']}，地图 {stats['html']}，失败 {stats['failed']}"
        )


def main():
    """
    命令行入口：
    - 可指定人物与底图
    - 未指定人物时进入交互模式
    """
    parser = argparse.ArgumentParser(
        description="生成人物生平 Markdown，并导出可交互地图 HTML"
    )
    parser.add_argument("-p", "--person", help="历史人物姓名或一句包含人物的句子", required=False)
    args = parser.parse_args()
    if not args.person:
        return run_interactive()
    client = StoryAgentLLM()
    targets = extract_historical_figures(client, args.person)
    if not targets:
        print("未识别到历史人物")
        return
    stats = {"markdown": 0, "html": 0, "failed": 0}
    for person in targets:
        print(f"正在生成 {person} 生平文档，可能需要一些时间...")
        t0 = time.perf_counter()
        t_step = time.perf_counter()
        md = generate_historical_markdown(client, person)
        t_md = time.perf_counter() - t_step
        if not md:
            print(f"未取得：{person}")
            stats["failed"] += 1
            continue
        km = compute_total_distance_km(md)
        if isinstance(km, float):
            md = insert_distance_intro(md, km)
        print("正在进行地点地理编码，可能需要一些时间...")
        t_step = time.perf_counter()
        md = append_coords_section(md)
        t_geo = time.perf_counter() - t_step
        _print_quality_report(md)
        saved = save_markdown(person, md)
        print(f"已生成：{saved}")
        t_step = time.perf_counter()
        try:
            places = parse_places(md)
            events = parse_events(md)
            pts = build_points(places, events)
            html = render_html(person, pts, md=md)
        except Exception:
            html = render_osm_html(person, [], "")
        t_render = time.perf_counter() - t_step
        out = save_html(person, html)
        print(out)
        total = time.perf_counter() - t0
        print(
            f"耗时：生平生成 {_format_seconds(t_md)}，地理编码 {_format_seconds(t_geo)}，"
            f"地图渲染 {_format_seconds(t_render)}，总计 {_format_seconds(total)}"
        )
        stats["markdown"] += 1
        stats["html"] += 1
    print(
        f"运行完成：人物 {len(targets)}，文档 {stats['markdown']}，地图 {stats['html']}，失败 {stats['failed']}"
    )


if __name__ == "__main__":
    main()
