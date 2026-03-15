# Compliance Radar — 功能清單與測試紀錄

> 最後更新：2026-03-15

## 現有功能總覽

### 核心模組 (`scraper.py`)

| # | 功能 | 函數 | 說明 |
| --- | --- | --- | --- |
| 1 | RSS 抓取 | `main()` | 解析金管會 2 個 RSS 來源（函釋 + 草案），使用 `requests` + `feedparser`，SSL 自動降級 |
| 2 | 去重機制 | `load_state()` / `save_state()` | 以 `processed_announcements.json` 記錄已處理 URL，最多保留 1000 筆 |
| 3 | 附件偵測 | `check_for_attachments()` | 爬取原文頁面，偵測 PDF/DOC/XLS/ZIP 連結，SSL 容錯 |
| 4 | AI 分析 | `process_with_claude()` | Claude Sonnet 4.6 產生結構化 JSON（摘要 + 草稿 + 日期資訊），含 3 層 JSON 解析容錯 |
| 5 | 日期解析 | `parse_date_string()` | 支援西元年 (`2026.3.27`)、民國年 (`115年1月1日`)、會計年度 (`115會計年度`) |
| 6 | 日期計算 | `calculate_effective_date()` | 處理 AI 回傳的 exact/relative/unknown 日期類型 |
| 7 | Google 日曆連結 | `build_gcal_url()` | 產生 Google Calendar Template URL，一鍵加入全天行程 |
| 8 | 日曆注入 | `inject_calendar_links()` | 在 Email HTML 中自動嵌入藍色（生效日）/橘色（意見截止）日曆按鈕 |
| 9 | ICS 生成 | `create_ics_attachment()` | 產生 `.ics` 檔案（用於 `.eml` 草稿附件，非 Email 內嵌） |
| 10 | Email 單篇發送 | `dispatch_single_emails()` | 策略一：每則法規獨立一封信，含 AI 摘要 + 草稿 + 日曆連結 |
| 11 | Email 彙整發送 | `dispatch_digest_with_eml()` | 策略二：一封彙整信 + `.eml` 草稿附件 + 日曆連結 |
| 12 | HTML 月報 | `append_to_html_report()` | 按月產生 HTML 報告，自動排序、分類過濾（函釋/草案）、附件警示 |
| 13 | 報告索引 | `update_index_html()` | 自動產生 `reports/index.html` 列出所有月份連結 |
| 14 | 保留提醒 | `check_retention_reminder()` | 超過 5 年的報告檔案提醒歸檔 |
| 15 | 運行紀錄 | `log_run()` | 每次執行追加一行 JSON 到 `run_history.jsonl`（時間、筆數、成功/失敗） |

### 設定與部署 (`setup.py`)

| # | 功能 | 說明 |
| --- | --- | --- |
| 1 | 設定檔生成 | 建立 `config.json` 範本 |
| 2 | 排程註冊 | 自動向 Windows Task Scheduler 註冊每小時執行 |

### 檔案結構

```text
compliance-radar/
├── scraper.py                       # 核心腳本（15 個功能函數）
├── setup.py                         # 初始化與排程
├── config.json                      # 執行設定（不入版控）
├── processed_announcements.json     # 已處理 URL 狀態檔
├── run_history.jsonl                # 運行紀錄（每次執行追加一行）
├── requirements.txt                 # Python 依賴
├── reports/
│   ├── index.html                   # 月份索引頁
│   └── YYYY-MM.html                 # 各月份報告
└── README.md
```

---

## 測試架構：三層驗證

### 第一層：單元驗證（不花 API 額度）

| # | 測試項目 | 方法 | 通過標準 |
| --- | --- | --- | --- |
| 1 | RSS 抓取 | 直接呼叫 `feedparser.parse(url)` 解析金管會 RSS | 能取得 entries，標題非空 |
| 2 | SSL 容錯 | 對金管會網址呼叫 `check_for_attachments()` | 不 crash，回傳 True 或 False |
| 3 | 狀態檔讀寫 | `save_state()` 寫入 → `load_state()` 讀取 → 比對 | 資料一致 |
| 4 | HTML 報告產生 | 傳入假資料呼叫 `append_to_html_report()` | 檔案存在、能用瀏覽器開啟、內容正確 |
| 5 | HTML 追加模式 | 第二次呼叫 `append_to_html_report()` | 新資料在舊資料上方，不覆蓋 |
| 6 | index.html 產生 | 呼叫 `update_index_html()` | 索引頁列出所有月份連結 |
| 7 | 5 年保留提醒 | 手動建立 `reports/2020-01.html` 後呼叫 `check_retention_reminder()` | 印出提醒訊息 |

### 第二層：整合驗證（花少量 API 額度）

| # | 測試項目 | 方法 | 通過標準 |
| --- | --- | --- | --- |
| 1 | AI 分析 | 取一則真實 RSS entry 呼叫 `process_with_claude()` | 回傳合法 JSON，五個欄位都有值 |
| 2 | Email 發送 | 用 `dispatch_single_emails()` 寄一封給自己 | 收到信、HTML 格式正確、中文不亂碼 |

### 第三層：端對端驗證（完整流程）

| 步驟 | 操作 | 驗證標準 |
| --- | --- | --- |
| 1 | 清空 `processed_announcements.json` 為 `[]` | 檔案內容為 `[]` |
| 2 | 手動執行 `python scraper.py` | 印出抓取筆數、AI 分析進度 |
| 3 | 檢查 Email 信箱 | 收到通知信，格式正確，日曆連結可點擊 |
| 4 | 開啟 `reports/index.html` | 看到當月連結 |
| 5 | 點進當月報告 | 看到 AI 摘要、原文連結、免責聲明 |
| 6 | 再跑一次 `python scraper.py` | 印出「目前沒有新法規需要處理」（去重正常） |
| 7 | 檢查 `processed_announcements.json` | 包含已處理的 URL |
| 8 | 檢查 `run_history.jsonl` | 包含本次執行紀錄 |

---

## 測試結果紀錄

### 第一層單元驗證 — 2026-03-10 執行

| # | 測試項目 | 結果 | 備註 |
| --- | --- | --- | --- |
| 1 | RSS 抓取 | PASS | 兩個來源各取得 20 筆 entries，標題非空 |
| 2 | SSL 容錯 | PASS | SSL 驗證失敗後自動降級為不驗證模式，回傳 `True`（偵測到附件） |
| 3 | 狀態檔讀寫 | PASS | 寫入/讀取資料一致；損壞 JSON 檔回傳空 list，不 crash |
| 4 | HTML 報告產生 | PASS | 檔案存在，含標題、摘要、免責聲明 |
| 5 | HTML 追加模式 | PASS | 新資料出現在舊資料上方，兩筆均保留 |
| 6 | index.html 產生 | PASS | 索引頁存在，含當月連結 |
| 7 | 5 年保留提醒 | PASS | 正確印出「2020-01.html 已超過 5 年保存期限」提醒 |

### 第二層整合驗證 — 2026-03-10 執行

| # | 測試項目 | 結果 | 備註 |
| --- | --- | --- | --- |
| 1 | AI 分析 | PASS | 已修正 `max_tokens` 與 JSON 解析容錯，可穩定產出結構化草稿 |
| 2 | Email 發送 | PASS | 配合 Google 應用程式密碼 (App Password) 成功寄出 SMTP 信件 |

### 第三層端對端驗證 — 2026-03-10 執行

| # | 測試項目 | 結果 | 備註 |
| --- | --- | --- | --- |
| 1 | 完整流程 (RSS->AI->Email) | PASS | 成功處理積壓法規，並發送彙整信件 |
| 2 | 去重機制 | PASS | 第二次執行正確跳過已處理 URL |
| 3 | HTML 歷史報告同步 | PASS | 歷史報告同步更新，且索引頁導覽正常 |

### Google Calendar 功能驗證 — 2026-03-10 執行

| # | 測試項目 | 結果 | 備註 |
| --- | --- | --- | --- |
| 1 | AI 提取生效日 | PASS | 「115會計年度」→ `2026-01-01` |
| 2 | AI 提取意見截止日 | PASS | 「預告期間:2026.02.26~2026.3.27」→ `2026-03-27` |
| 3 | 民國年轉換 | PASS | 支援民國年與西元年格式 |
| 4 | `.ics` 附件 | FAILED | Gmail 僅顯示為附件，無「加入日曆」按鈕 |
| 5 | `text/calendar` 內嵌 | FAILED | Gmail 要求完整 iTIP 會議邀請協議（不適用事件發佈場景） |
| 6 | **Google Calendar URL 連結** | **PASS** | 郵件嵌入預填連結，點擊即開啟 Google Calendar 預填畫面 |

### 運行紀錄功能驗證 — 2026-03-15 新增

| # | 測試項目 | 結果 | 備註 |
| --- | --- | --- | --- |
| 1 | `log_run()` 寫入 JSONL | PASS | 正確追加 JSON 行到 `run_history.jsonl` |
| 2 | 覆蓋所有退出點 | PASS | API Key 缺失、無新法規、AI 失敗、Email 失敗、成功 — 5 個情境均記錄 |

---

## POC 驗證進度

**結論：POC 開發驗證 100% 完成。**

| # | 監測項目 | 目標 | 狀態 |
| --- | --- | --- | --- |
| 1 | 排程穩定性 | 確認 Windows Task Scheduler 每小時啟動一次正常 | PASS (2026-03-15 確認) |
| 2 | 真實數據追蹤 | 確認有新的金管會公告能被成功抓取並寫入報告 | PASS — RSS 推送正常觸發 |
| 3 | 持續運行驗證 | 確認 `2026-03.html` 在多次自動寫入後格式依然正確 | PASS |
| 4 | 日曆連結端對端 | 確認信件中的日期成功產生 Google Calendar 連結 | PASS — 實際公告日期正確設置 |

## 邊界情況

| 情境 | 預期行為 |
| --- | --- |
| `processed_announcements.json` 損壞 | `load_state()` 回傳空 list，不 crash |
| Claude API JSON 回應截斷 | 3 層解析容錯：`json.loads` → 清理換行重試 → 逐欄位 regex 提取 |
| 金管會 SSL 憑證失效 | RSS 抓取與附件偵測均自動降級為 `verify=False` |
| 無日期資訊的公告 | 正常發送 Email，不顯示日曆按鈕區塊 |
| `run_history.jsonl` 寫入失敗 | 僅印出 WARN，不影響主流程 |

---

> V2.0 規劃與系統設計文件：[`金融合規AI工具-系統設計思路.md`](D:\Obsidian Vault\1-Rough Notes\金融合規AI工具-系統設計思路.md)
