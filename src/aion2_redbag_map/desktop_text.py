"""Management-window text, in the same four languages as the maps."""
from .localization import SUPPORTED_LANGUAGES, restored_language

LANGUAGE_NAMES = ("简体中文", "English", "日本語", "한국어")

TEXT = {
    "title": ("AION2隐藏背包雷达", "AION2 Hidden Cube Radar", "AION2 ヒドゥンキューブレーダー", "AION2 히든 큐브 레이더"),
    "receive_status": ("接收状态", "RECEPTION", "受信状況", "수신 상태"),
    "points": ("观测点位", "Recorded locations", "観測地点", "관측 지점"),
    "start": ("开始实时接收", "Start receiving", "リアルタイム受信を開始", "실시간 수신 시작"),
    "stop": ("停止接收", "Stop receiving", "受信を停止", "수신 중지"),
    "map_views": ("选择地图视图", "Choose a map view", "マップ表示を選択", "지도 보기 선택"),
    "overlay": ("显示悬浮地图  ↗", "Floating map  ↗", "フローティングマップ  ↗", "플로팅 지도  ↗"),
    "overlay_description": ("置顶小窗，边跑图边查看", "Stay on top while you explore", "最前面の小窓で探索中も確認", "탐험 중에도 항상 위에 표시"),
    "full_map": ("打开完整地图  ↗", "Full map  ↗", "全体マップ  ↗", "전체 지도  ↗"),
    "map_description": ("全图浏览，查看点位与记录", "Explore locations and history", "全体マップで地点と履歴を確認", "전체 지도의 지점과 기록 확인"),
    "history": ("历史记录", "History", "履歴", "기록"),
    "data_folder": ("数据目录", "Data folder", "データフォルダー", "데이터 폴더"),
    "import": ("导入地图资源", "Import map assets", "マップ素材を取り込む", "지도 리소스 가져오기"),
    "alert_settings": ("提醒设置", "Alert settings", "通知設定", "알림 설정"),
    "alerts_subtitle": ("选择喜欢的声音，调整到舒适的音量。", "Choose a cue and a comfortable volume.", "好きな音と聞きやすい音量を選びます。", "원하는 소리와 편안한 음량을 선택하세요."),
    "alerts_sound": ("提示音", "Sound", "通知音", "알림음"),
    "alerts_volume": ("音量", "Volume", "音量", "음량"),
    "alerts_volume_note": ("只调整本工具的提示音，不改变游戏或系统音量。", "Changes only this tool's cue, not game or system volume.", "このツールの通知音のみ変更します。ゲームやシステムの音量は変わりません。", "이 도구의 알림음만 조절하며 게임과 시스템 음량은 바꾸지 않습니다."),
    "alerts_overlay": ("仅在收到新背包提醒时弹出悬浮窗", "Show the floating map only for new Hidden Cube alerts", "新しいバッグの通知時だけマップを表示", "새 Hidden Cube 알림이 있을 때만 플로팅 지도 표시"),
    "alerts_overlay_note": ("收到新提醒前保持隐藏；历史记录和试听不会弹出悬浮窗。", "Stay hidden until a new alert. History and sound previews do not open the map.", "新しい通知まで非表示にします。履歴や試聴では表示しません。", "새 알림 전에는 숨겨집니다. 과거 기록이나 미리듣기로 지도가 열리지 않습니다."),
    "alerts_preview": ("试听", "Preview", "試聴", "미리듣기"),
    "alerts_save": ("保存设置", "Save settings", "設定を保存", "설정 저장"),
    "alerts_close": ("关闭", "Close", "閉じる", "닫기"),
    "alerts_draft": ("更改在保存后生效；试听不会保存设置。", "Changes apply after saving. Preview does not save them.", "変更は保存後に反映されます。試聴では保存しません。", "변경 사항은 저장 후 적용됩니다. 미리듣기로는 저장되지 않습니다."),
    "alerts_saving": ("正在保存设置…", "Saving settings…", "設定を保存中…", "설정 저장 중…"),
    "alerts_previewing": ("正在发送试听请求…", "Sending preview request…", "試聴を要求中…", "미리듣기 요청 중…"),
    "alerts_saved": ("设置已保存。", "Settings saved.", "設定を保存しました。", "설정을 저장했습니다."),
    "alerts_preview_sent": ("试听请求已发送，请按听感调整音量。", "Preview requested. Adjust the volume to your comfort.", "試聴を要求しました。聞こえ方に合わせて音量を調整してください。", "미리듣기를 요청했습니다. 편안한 음량으로 조절하세요."),
    "alerts_muted": ("音量为 0%，本次试听静音。", "Volume is 0%; this preview is silent.", "音量が0%のため、試聴は無音です。", "음량이 0%이므로 이번 미리듣기는 무음입니다."),
    "alerts_failed": ("操作未完成，设置草稿已保留，请重试。", "The action failed. Your draft is kept; please retry.", "操作に失敗しました。変更は保持されています。再試行してください。", "작업에 실패했습니다. 작성 중인 설정은 유지됩니다. 다시 시도하세요."),
    "sound_chime": ("柔和三音", "Soft chime", "やさしいチャイム", "부드러운 차임"),
    "sound_bell": ("轻铃", "Light bell", "軽いベル", "가벼운 종소리"),
    "sound_bubble": ("气泡", "Bubbles", "バブル", "물방울"),
    "sound_arcade": ("像素音", "Arcade", "アーケード", "아케이드"),
    "sound_sonar": ("声呐", "Sonar", "ソナー", "소나"),
    "sound_alarm": ("原版警铃", "Original alarm", "従来の警報音", "기존 경보음"),
    "idle": ("尚未开始接收", "Ready when you are", "受信を開始できます", "수신 준비 완료"),
    "idle_detail": ("先浏览地图，或开启实时接收。", "Browse the map or start live reception.", "マップを閲覧するか、リアルタイム受信を開始します。", "지도를 보거나 실시간 수신을 시작하세요."),
    "browsing": ("正在浏览历史", "Browsing history", "履歴を表示中", "기록 확인 중"),
    "browsing_detail": ("查看已保存的背包点位与记录。", "View saved Hidden Cube locations and records.", "保存済みのバッグの地点と記録を確認できます。", "저장된 가방 위치와 기록을 확인합니다."),
    "paused": ("接收已停止", "Reception stopped", "受信停止中", "수신 중지됨"),
    "paused_detail": ("记录已保留，可随时继续接收。", "Your records are saved. Resume at any time.", "記録は保持されています。いつでも受信を再開できます。", "기록이 보존되어 있습니다. 언제든 다시 수신할 수 있습니다."),
    "unavailable": ("接收暂不可用", "Reception unavailable", "受信できません", "수신할 수 없음"),
    "unavailable_detail": ("请查看数据目录中的接收日志。", "Check the reception log in the data folder.", "データフォルダー内の受信ログを確認してください。", "데이터 폴더에서 수신 로그를 확인하세요."),
    "receiving": ("正在接收", "Receiving", "受信中", "수신 중"),
    "receiving_detail": ("新背包与状态变化会自动更新到地图。", "New Hidden Cubes and status changes appear on the map.", "新しいバッグと状態の変化をマップに自動反映します。", "새 가방과 상태 변경이 지도에 자동으로 반영됩니다."),
    "waiting": ("正在等待记录", "Waiting for records", "記録を待機中", "기록 대기 중"),
    "waiting_detail": ("等待本机收到游戏记录。", "Waiting for game records to reach this computer.", "この端末がゲームの記録を受信するのを待っています。", "이 컴퓨터에 게임 기록이 수신되기를 기다립니다."),
    "storage_error": ("存档暂未保存，请检查数据目录。", "Records could not be saved. Check the data folder.", "記録を保存できません。データフォルダーを確認してください。", "기록을 저장하지 못했습니다. 데이터 폴더를 확인하세요."),
    "driver_check": ("正在检查接收组件…", "Checking reception component…", "受信コンポーネントを確認中…", "수신 구성 요소 확인 중…"),
    "driver_ready": ("接收组件已找到", "Reception component ready", "受信コンポーネントを確認済み", "수신 구성 요소 준비 완료"),
    "driver_missing": ("未找到接收组件，仍可浏览地图和历史。", "Reception component missing. Maps and history remain available.", "受信コンポーネントがありません。マップと履歴は閲覧できます。", "수신 구성 요소가 없습니다. 지도와 기록은 볼 수 있습니다."),
    "starting": ("正在开始接收…", "Starting reception…", "受信を開始中…", "수신 시작 중…"),
    "starting_overlay": ("正在开始接收并打开悬浮地图…", "Starting reception and floating map…", "受信とフローティングマップを開始中…", "수신 및 플로팅 지도 시작 중…"),
    "stopping": ("正在停止接收…", "Stopping reception…", "受信を停止中…", "수신 중지 중…"),
    "opening_overlay": ("正在打开悬浮地图…", "Opening floating map…", "フローティングマップを開いています…", "플로팅 지도 여는 중…"),
    "opening_map": ("正在打开完整地图…", "Opening full map…", "全体マップを開いています…", "전체 지도 여는 중…"),
    "opening_history": ("正在打开历史记录…", "Opening history…", "履歴を開いています…", "기록 여는 중…"),
    "opening_data": ("正在打开数据目录…", "Opening data folder…", "データフォルダーを開いています…", "데이터 폴더 여는 중…"),
    "import_title": ("选择有权使用的地图资源目录", "Choose a folder of map assets you may use", "使用権限のあるマップ素材フォルダーを選択", "사용 권한이 있는 지도 리소스 폴더 선택"),
    "importing": ("正在导入地图资源…", "Importing map assets…", "マップ素材を取り込み中…", "지도 리소스 가져오는 중…"),
    "imported": ("地图资源已导入，重新打开地图窗口后生效。", "Map assets imported. Reopen the map window to use them.", "マップ素材を取り込みました。マップを開き直すと反映されます。", "지도 리소스를 가져왔습니다. 지도 창을 다시 열면 적용됩니다."),
    "changing_language": ("正在切换语言…", "Changing language…", "言語を切り替え中…", "언어 변경 중…"),
    "language_changed": ("语言已保存，地图与提醒会同步切换。", "Language saved. Maps and alerts use this language.", "言語を保存しました。マップと通知にも反映されます。", "언어를 저장했습니다. 지도와 알림에도 적용됩니다."),
    "done": ("操作已完成。", "Done.", "完了しました。", "완료했습니다."),
    "action_error": ("操作未完成，请重试或检查数据目录。", "The action failed. Retry or check the data folder.", "操作に失敗しました。再試行するか、データフォルダーを確認してください。", "작업에 실패했습니다. 다시 시도하거나 데이터 폴더를 확인하세요."),
    "status_error": ("暂时无法读取本机地图状态。", "Unable to read the local map status.", "ローカルマップの状態を読み取れません。", "로컬 지도 상태를 읽을 수 없습니다."),
    "closing": ("正在保存并退出…", "Saving and closing…", "保存して終了中…", "저장 및 종료 중…"),
    "exit_title": ("退出提示", "Closing the app", "終了のお知らせ", "종료 안내"),
    "close_error": ("部分本机窗口未能正常退出，请关闭仍显示的地图窗口。", "Some windows could not close. Close any remaining map windows.", "一部のウィンドウを終了できませんでした。残っているマップを閉じてください。", "일부 창을 닫지 못했습니다. 남아 있는 지도 창을 닫아 주세요."),
    "service_collision": ("已有连接或窗口与当前地图不匹配，请关闭旧版地图后重试。", "An existing connection or window does not match this map. Close the older map and retry.", "既存の接続またはウィンドウが一致しません。以前のマップを閉じて再試行してください。", "기존 연결 또는 창이 이 지도와 일치하지 않습니다. 이전 지도를 닫고 다시 시도하세요."),
    "npcap_missing": ("未找到接收组件，暂时不能实时接收，仍可浏览地图和历史。", "The reception component is missing. You can still browse maps and history.", "受信コンポーネントがないため受信できません。マップと履歴は閲覧できます。", "수신 구성 요소가 없어 실시간 수신이 불가능합니다. 지도와 기록은 볼 수 있습니다."),
    "language_save_error": ("语言设置未能保存，请检查数据目录是否可写。", "Language could not be saved. Check that the data folder is writable.", "言語設定を保存できません。データフォルダーの書き込み権限を確認してください。", "언어 설정을 저장하지 못했습니다. 데이터 폴더에 쓸 수 있는지 확인하세요."),
}


def text(key, language="zh-CN"):
    return TEXT[key][SUPPORTED_LANGUAGES.index(restored_language(language))]


def error_text(error, language="zh-CN", fallback="action_error"):
    from .runtime import DesktopError, NpcapMissing, ServiceCollision
    if isinstance(error, NpcapMissing):
        return text("npcap_missing", language)
    if isinstance(error, ServiceCollision):
        return text("service_collision", language)
    if isinstance(error, DesktopError):
        if getattr(error, "key", None) in TEXT:
            return text(error.key, language)
        if language == "zh-CN":
            return str(error)
    return text(fallback, language)
