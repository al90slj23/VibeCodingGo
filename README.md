# VibeCue — AI Coding Companion

AI 開發提醒助手：在 macOS 用螢幕四周泛光、不同提示音與對話卡，提醒你回到需要注意的 AI coding 工作。目前先支援 Codex，事件接入層與提醒介面分開，之後可擴充其他開發工具。

| 事件 | 畫面 | 提示音 |
| --- | --- | --- |
| 本輪執行完成 | 綠色泛光 | 上行和弦 |
| 提問或要求確認／選擇 | 琥珀色泛光 | 三段提問音 |
| 權限請求 | 珊瑚色泛光 | 兩短一長 |

提示卡顯示 Codex 的對話名稱。邊緣視窗不攔截滑鼠、不搶鍵盤焦點，涵蓋所有螢幕；提示卡顯示在滑鼠所在螢幕。完成提醒約 8 秒，其他提醒約 14 秒。需要回答的提醒會優先顯示。遵循 macOS 減少動態效果設定。

## 安裝與啟動

需要 macOS 13 以上、Xcode Command Line Tools 中的 Swift 與 `/usr/bin/python3`。無第三方套件、無 API key。

```sh
./scripts/build.sh
/usr/bin/python3 scripts/install.py
```

安裝位置：

- `/Applications/VibeCue.app`：原生選單列應用程式。
- `~/Library/Application Support/VibeCue/`：程式、事件佇列與私有狀態。
- `~/Library/LaunchAgents/local.vibecue.plist`：登入後啟動應用程式，由 App 管理監看子程序。
- `~/.codex/hooks.json`：加入 VibeCue 的兩個被動提醒 hooks，保留其他 hooks。

**權限請求的 hook 需要額外啟用。** Codex 官方要求先審閱並信任非託管 hook 的確切定義。請在可用的 Codex CLI 中輸入 `/hooks`，檢查並信任命令指向 `VibeCue/vibecuectl hook` 的兩個項目。桌面版是否即時重載 hooks 依版本而異，必要時在目前工作結束後重新開啟 Codex。安裝本身不會更改信任紀錄或略過 hook 審閱。完成與提問的本機紀錄監看不依賴 hooks。

官方依據：[Hooks](https://learn.chatgpt.com/docs/hooks)、[Notifications](https://learn.chatgpt.com/docs/notifications)。

若全域 `codex` 命令不可用，可使用官方 App 內建 CLI 開啟審閱：

```sh
./scripts/review-hooks.sh
# 進入 CLI 後輸入 /hooks
```

此腳本只啟動官方 CLI，不會替你信任 hooks。

## 操作

點選 Mac 頂部選單列的 VibeCue 圖示，可以打開設定面板、測試三種提醒、關閉聲音、暫停 30 分鐘，或查看最近提醒。雙擊 App 也會打開設定面板。面板中的音量、泛光強度、顯示時間與提醒類型開關會儲存並立即生效。

聲音使用 AVAudioPlayer 播放本機合成 WAV，走一般媒體音訊輸出，不改動系統音量或靜音設定。提醒卡中的「開啟對話」使用 `codex://threads/<id>`，需要系統已註冊 Codex URL handler。

```sh
# 專案內的控制命令
./scripts/vibecuectl status
./scripts/vibecuectl test complete
./scripts/vibecuectl test input
./scripts/vibecuectl test permission

# 完全停止或恢復 App 及其監看子程序
./scripts/vibecuectl stop
./scripts/vibecuectl start

# 移除登入啟動與 VibeCue 自己的 hook，保留程式與資料
/usr/bin/python3 scripts/uninstall.py
```

也可使用安裝後的 `~/Library/Application Support/VibeCue/vibecuectl`。選單列結束功能會連同監看程序一起停止，再次開啟 App 恢復。

若 Codex 記錄在外接磁碟，macOS 可能要求 VibeCue 的讀取授權。請使用面板「集成與檔案」中的記錄目錄授權，或接受系統針對該磁碟的讀取提示；不需要授予完整磁碟存取。讀取被拒絕時，面板會顯示異常，不能只以程序仍在執行判斷監看健康。

## 事件來源與準確性

監看程序只讀本機 `~/.codex/state_*.sqlite` 對話中繼資料與 session JSONL 的新增事件，不修改 Codex 資料庫或對話，不讀取登入憑證，不連網。對話名稱優先取本機正式名稱；啟動時從既有檔案尾端開始，不重播歷史提醒。跨程序以工具自己的 SQLite 去重，佇列有數量與過期上限。

- `task_complete` 表示**本輪結束**，並非保證整個專案已完成。中斷及子代理結束不提醒。
- 結構化 `request_user_input`／`request_user_input_async` 呼叫產生提問提醒。Code Mode 的巢狀呼叫依執行記錄辨識，無法證明未執行分支，提醒文字會註明記錄判斷。
- 如果最後一段回覆明確要求使用者確認或選擇，也會標為提問；這是保守文字規則，提示卡會標示「依回覆文字判斷」，可能有漏判或誤判。
- `PermissionRequest` hook 只表示發生權限請求。Codex 的自動審查可能迅速處理，提示會請你查看 Codex 是否仍需要批准。VibeCue 不會替你批准、拒絕或回覆。
- 本機紀錄格式是內部介面，Codex 更新後可能需要調整。監看狀態顯示 `local-transcripts`，不能視為官方穩定事件 API。
- 沒有落在本機 session 紀錄中的雲端或其他電腦對話，不在此監看範圍。雲端協調的對話也可能不支援本機 command hooks。

原有 `config.toml` 及 computer-use 的 `notify` 設定維持原樣。安裝器會備份被修改的既有 `hooks.json`；解除安裝只移除 VibeCue 自己的項目。

## 開發與驗證

```sh
/usr/bin/python3 -m unittest discover -s tests -v
# 在隔離目錄驗證安裝，不啟動程序
/usr/bin/python3 scripts/install.py --no-start --user-home /private/tmp/vibecue-install-qa
# 單獨顯示測試，擷取應用程式自己的卡片與泛光畫面
VIBECUE_HOME=/private/tmp/vibecue-qa \
  'build/VibeCue.app/Contents/MacOS/VibeCue' \
  --demo input --capture /private/tmp/vibecue-banner.png \
  --capture-glow /private/tmp/vibecue-glow.png
```

測試涵蓋啟動不重播、部分寫入、檔案替換／截斷、子代理與中斷過濾、事件去重、暫時錯誤重試、提問規則、停用標記、佇列上限、程序單例、hook 不產生批准決策，以及安裝保留原設定。實際播放狀態、播放進度與音訊電平可在 `app-status.json` 查看；該狀態不能代替人耳確認。

## 接入其他 AI 開發工具

`vibecue/providers/codex.py` 是目前實際運作的 Codex adapter。它輸出統一的本機事件給 `vibecue/events.py`，原生 App 只負責顯示、聲音與設定。新增工具時應提供自己的事件與對話名稱來源，不沿用 Codex 的內部紀錄格式。

```json
{"provider":"codex","id":"stable-event-id","kind":"input","thread_id":"thread-id","title":"對話名稱","message":"需要回答","created_at":"2026-10-27T12:00:00Z"}
```

此版本尚未接入 Claude Desktop 或其他工具。VibeCue 是獨立專案；安裝後執行不依賴原始碼所在目錄。
