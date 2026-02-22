import os
import sys
import unittest
from unittest import mock


"""单元测试聚焦导出格式与交集计算等核心工具函数。"""

SCRIPT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", ".github", "skills", "map-story", "script")
)
sys.path.insert(0, SCRIPT_DIR)

try:
    import story_map
except Exception as exc:
    story_map = None
    _IMPORT_ERROR = exc


def _sample_profile():
    return {
        "person": {"name": "李白"},
        "locations": [
            {
                "name": "长安",
                "modernName": "西安",
                "ancientName": "长安",
                "lat": 34.34,
                "lng": 108.94,
                "type": "normal",
                "time": "742年",
            },
            {
                "name": "成都",
                "modernName": "成都",
                "ancientName": "成都",
                "lat": 30.67,
                "lng": 104.07,
                "type": "normal",
                "time": "744年",
            },
        ],
    }


def _sample_markdown():
    return """# 李白
## 人物档案
### 基本信息
- **姓名**：李白
- **朝代**：唐
- **身份**：诗人
### 生平概述
盛唐浪漫主义诗人。
## 人生历程
### 重要地点：长安
- **时间**：742年
- **地点**：长安
- **事件**：入京求仕。
### 重要地点：成都
- **时间**：744年
- **地点**：成都
- **事件**：寓居蜀中。
## 地点坐标
| 地点 | 纬度 | 经度 |
| --- | --- | --- |
| 西安 | 34.34 | 108.94 |
| 成都 | 30.67 | 104.07 |
"""


@unittest.skipIf(story_map is None, "story_map import failed")
class StoryMapUtilsTest(unittest.TestCase):
    def test_build_geojson_for_profile(self):
        # 验证轨迹点与路径线是否能生成合法 GeoJSON。
        profile = _sample_profile()
        geo = story_map._build_geojson_for_profile(profile)
        self.assertEqual(geo.get("type"), "FeatureCollection")
        features = geo.get("features") or []
        self.assertEqual(len(features), 3)
        line = features[-1]
        self.assertEqual(line.get("geometry", {}).get("type"), "LineString")

    def test_build_csv_for_profile(self):
        # 验证导出 CSV 的基本结构与人物字段输出。
        profile = _sample_profile()
        csv_text = story_map._build_csv_for_profile(profile)
        lines = [line for line in csv_text.splitlines() if line.strip()]
        self.assertGreaterEqual(len(lines), 3)
        self.assertIn("person", lines[0])
        self.assertIn("李白", csv_text)

    def test_compute_overlaps(self):
        # 验证多人物轨迹的交集地点计算。
        people = [
            {
                "person": {"name": "甲"},
                "locations": [
                    {"modernName": "西安", "name": "长安"},
                    {"modernName": "洛阳", "name": "洛阳"},
                ],
            },
            {
                "person": {"name": "乙"},
                "locations": [
                    {"modernName": "西安", "name": "长安"},
                    {"modernName": "开封", "name": "开封"},
                ],
            },
        ]
        overlaps = story_map._compute_overlaps(people)
        names = [item.get("name") for item in overlaps]
        self.assertIn("西安", names)

    def test_render_profile_html_output(self):
        def fake_batch(texts: list[str], event_callback=None):
            _ = event_callback
            mapping = {}
            for text in texts:
                if "长安" in text:
                    mapping[text] = ("长安", "西安")
                elif "成都" in text:
                    mapping[text] = ("成都", "成都")
                else:
                    mapping[text] = ("", "")
            for key, value in mapping.items():
                story_map._SPLIT_CACHE[key] = value
            return mapping

        md = _sample_markdown()
        with mock.patch.object(story_map, "_batch_split_ancient_modern", side_effect=fake_batch):
            html = story_map.render_html("李白", [], md=md)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn('id="root"', html)
        self.assertIn('id="map"', html)
        self.assertIn("window.__EXPORT_DATA__", html)
        self.assertIn("李白", html)
        self.assertNotIn("__DATA__", html)


if __name__ == "__main__":
    unittest.main()
