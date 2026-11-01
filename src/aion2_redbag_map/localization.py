"""UI notification text only; transport records and observation states stay unchanged."""

DEFAULT_LANGUAGE = "zh-CN"
SUPPORTED_LANGUAGES = ("zh-CN", "en", "ja", "ko")

TEXT = {
    "zh-CN": {
        "new_bag": "发现红色背包",
        "test_title": "提醒测试 · 非背包刷新",
        "test_text": "测试提醒已触发。实际发现红色背包时，这里会显示地图与坐标。",
        "tray": "AION2 红色背包地图 · 点击打开",
        "view_map": "请查看地图",
        "map": "地图",
    },
    "en": {
        "new_bag": "Red bag spotted",
        "test_title": "Alert test · No bag spawn",
        "test_text": "This is a test alert. When a red bag is detected, its map and coordinates will appear here.",
        "tray": "AION2 Red Bag Map · Click to open",
        "view_map": "Check the map",
        "map": "Map",
    },
    "ja": {
        "new_bag": "赤いバッグを発見",
        "test_title": "通知テスト · バッグ出現ではありません",
        "test_text": "テスト通知です。赤いバッグを検出すると、ここにマップと座標が表示されます。",
        "tray": "AION2 赤いバッグマップ · クリックして開く",
        "view_map": "マップを確認してください",
        "map": "マップ",
    },
    "ko": {
        "new_bag": "빨간 가방 발견",
        "test_title": "알림 테스트 · 가방 출현 아님",
        "test_text": "테스트 알림입니다. 빨간 가방이 감지되면 지도와 좌표가 여기에 표시됩니다.",
        "tray": "AION2 빨간 가방 지도 · 클릭하여 열기",
        "view_map": "지도를 확인하세요",
        "map": "지도",
    },
}

# Keep map IDs and observations unchanged; only localize their display names.
MAP_NAMES = {20: {'zh-CN': '艾雷修蓝塔下层', 'en': 'Lower Reshanta', 'ja': 'エレシュランタ下層', 'ko': '에레슈란타 하층'},
 22: {'zh-CN': '艾雷修蓝塔中层', 'en': 'Middle Reshanta', 'ja': 'エレシュランタ中層', 'ko': '에레슈란타 중층'},
 23: {'zh-CN': '艾雷修蓝塔上层', 'en': 'Upper Reshanta', 'ja': 'エレシュランタ上層', 'ko': '에레슈란타 상층'},
 1000: {'zh-CN': '波伊塔', 'en': 'Poeta', 'ja': 'ポエタ', 'ko': '포에타'},
 1010: {'zh-CN': '斐尔特朗', 'en': 'Verteron', 'ja': 'ベルテロン', 'ko': '베르테론'},
 1011: {'zh-CN': '耶尔特奈', 'en': 'Eltnen', 'ja': 'エルテネン', 'ko': '엘테넨'},
 1100: {'zh-CN': '伊斯夏尔肯', 'en': 'Ishalgen', 'ja': 'イスハルゲン', 'ko': '이스할겐'},
 1110: {'zh-CN': '阿尔特盖德', 'en': 'Altgard', 'ja': 'アルトガルド', 'ko': '알트가르드'},
 1111: {'zh-CN': '莫尔海姆', 'en': 'Morheim', 'ja': 'モルヘイム', 'ko': '모르헤임'},
 600001: {'zh-CN': '克劳洞穴', 'en': 'Krao Cave', 'ja': 'クラオ洞窟', 'ko': '크라오 동굴'},
 600002: {'zh-CN': '克劳洞穴', 'en': 'Krao Cave', 'ja': 'クラオ洞窟', 'ko': '크라오 동굴'},
 600004: {'zh-CN': '克劳洞穴', 'en': 'Krao Cave', 'ja': 'クラオ洞窟', 'ko': '크라오 동굴'},
 600021: {'zh-CN': '火之神殿', 'en': 'Fire Temple', 'ja': '炎の神殿', 'ko': '불의 신전'},
 600022: {'zh-CN': '火之神殿', 'en': 'Fire Temple', 'ja': '炎の神殿', 'ko': '불의 신전'},
 600023: {'zh-CN': '火之神殿', 'en': 'Fire Temple', 'ja': '炎の神殿', 'ko': '불의 신전'},
 600031: {'zh-CN': '德拉乌伯尼尔', 'en': 'Draupnir', 'ja': 'ドラウプニル', 'ko': '드라웁니르'},
 600032: {'zh-CN': '德拉乌伯尼尔', 'en': 'Draupnir', 'ja': 'ドラウプニル', 'ko': '드라웁니르'},
 600033: {'zh-CN': '德拉乌伯尼尔', 'en': 'Draupnir', 'ja': 'ドラウプニル', 'ko': '드라웁니르'}}
ENGLISH_MAP_NAMES = {
    "Reshanta Inferior": "Lower Reshanta", "Reshanta Média": "Middle Reshanta",
    "Reshanta Superior": "Upper Reshanta", "Caverna de Krao": "Krao Cave",
    "Templo Divino do Fogo": "Fire Temple",
}


def validate_language(value):
    if not isinstance(value, str) or value not in SUPPORTED_LANGUAGES:
        raise ValueError("不支持的语言")
    return value


def restored_language(value):
    return value if isinstance(value, str) and value in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def text(key, language=DEFAULT_LANGUAGE):
    return TEXT[restored_language(language)][key]


def map_name(maps, map_id, language=DEFAULT_LANGUAGE):
    language = restored_language(language)
    if map_id in MAP_NAMES:
        return MAP_NAMES[map_id][language]
    item = next((item for item in maps if item["id"] == map_id), None)
    if item:
        if language == DEFAULT_LANGUAGE:
            return item["name"]
        name = item.get("english_name") or item["name"]
        return ENGLISH_MAP_NAMES.get(name, name)
    return f"{text('map', language)} {map_id}"


def test_alert(language=DEFAULT_LANGUAGE):
    return {"title": text("test_title", language), "text": text("test_text", language),
            "language": restored_language(language)}
