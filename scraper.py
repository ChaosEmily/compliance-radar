"""
scraper.py — 金融合規雷達核心抓取腳本 (RSS-to-Gmail 版)

執行內容：
1. 讀取 config.json 與 processed_announcements.json
2. 解析金管會 RSS feeds，找出尚未處理的新公告
3. 針對每則新公告，爬取原文網頁偵測是否有 PDF/DOC 附件
4. 呼叫 Ollama 產生合規重點摘要與草稿 (以 JSON 格式回傳)
5. 依照 config.json 設定的 email_strategy 寄送 Email 給法遵
   - "single_emails": 每則公告獨立寄出一封含有草稿的 Email。
   - "digest_with_eml": 所有公告彙整成一封信，草稿以 .eml 附件形式夾帶。
6. 寄送成功後，更新 processed_announcements.json 確保不重複發送。
"""

import json
import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta

import io
import feedparser
import requests
import urllib3
from bs4 import BeautifulSoup
from openai import OpenAI

# 金管會 SSL 憑證缺少 Subject Key Identifier，verify=False fallback 時會產生警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Windows 終端預設 cp950，強制 UTF-8 以正確顯示中文 log
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import smtplib
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.json"
STATE_FILE = SCRIPT_DIR / "processed_announcements.json"
REPORTS_DIR = SCRIPT_DIR / "reports"

# ============================================================================
# Core Utilities
# ============================================================================

def load_config() -> dict:
    if not CONFIG_FILE.exists():
        print("[ERROR] 找不到 config.json，請先執行 python setup.py 建立設定檔。")
        sys.exit(1)
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))

def load_state() -> list:
    if not STATE_FILE.exists():
        return []
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except:
        return []

def save_state(processed_urls: list):
    STATE_FILE.write_text(json.dumps(processed_urls, indent=4, ensure_ascii=False), encoding="utf-8")

def check_for_attachments(url: str) -> bool:
    """爬取原始網頁，檢查是否含有特定副檔名的連結"""
    try:
        # 金管會網站偶有 SSL 憑證問題，先嘗試正常驗證，失敗再跳過
        try:
            resp = requests.get(url, timeout=10)
        except requests.exceptions.SSLError:
            print(f"  [WARN] SSL 驗證失敗，改用不驗證模式：{url}")
            resp = requests.get(url, timeout=10, verify=False)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        target_exts = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.zip']
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href'].lower()
            if any(href.endswith(ext) for ext in target_exts):
                return True
        return False
    except Exception as e:
        print(f"  [WARN] 檢查附件失敗 ({url}): {e}")
        return False

# ============================================================================
# Sanitize AI Output
# ============================================================================

def strip_fake_emails(html: str) -> str:
    """移除 AI 產出的 mailto 連結和假 email 地址"""
    html = re.sub(r'<a\s+href=["\']mailto:[^"\']*["\'][^>]*>(.*?)</a>', r'\1', html, flags=re.IGNORECASE)
    html = re.sub(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', '', html)
    return html

# ============================================================================
# Date Utilities
# ============================================================================

def parse_date_string(date_str: str) -> str:
    """將日期字串轉為 YYYY-MM-DD 格式。支援民國年與西元年。"""
    if not date_str:
        return None
    try:
        import re
        # 模式 1: 西元年完整日期 (如 2026.3.27, 2026-03-27, 2026/3/27)
        match_western = re.search(r'(20\d{2})[./-](\d{1,2})[./-](\d{1,2})', date_str)
        if match_western:
            year = int(match_western.group(1))
            month = int(match_western.group(2))
            day = int(match_western.group(3))
            return f"{year:04d}-{month:02d}-{day:02d}"

        # 模式 2: 民國年完整年月日 (如 115年1月1日, 115.1.1)
        match_minguo = re.search(r'(\d{2,3})[年./-](\d{1,2})[月./-](\d{1,2})', date_str)
        if match_minguo:
            minguo_year = int(match_minguo.group(1))
            month = int(match_minguo.group(2))
            day = int(match_minguo.group(3))
            western_year = minguo_year + 1911
            return f"{western_year:04d}-{month:02d}-{day:02d}"

        # 模式 3: 僅有民國年份 (如 115年, 115會計年度)
        match_year = re.search(r'(\d{2,3})(?:年|會計年度|年度)', date_str)
        if match_year:
            minguo_year = int(match_year.group(1))
            western_year = minguo_year + 1911
            return f"{western_year:04d}-01-01"

        return None
    except:
        return None

def calculate_effective_date(pub_date_str: str, info: dict) -> str:
    """根據 AI 提供的 info 計算日期"""
    if not info or info.get("type") == "unknown" or not info.get("value"):
        return None
    
    if info.get("type") == "exact":
        return parse_date_string(info.get("value"))
    
    if info.get("type") == "relative":
        try:
            from datetime import datetime, timedelta
            # 嘗試多種發布日期格式
            base_date = None
            date_seeds = [
                pub_date_str[:10], # YYYY-MM-DD
                datetime.now().strftime("%Y-%m-%d") # Fallback to today
            ]
            
            # 嘗試使用 dateparser 或簡單解析
            for ds in date_seeds:
                try:
                    # 嘗試 YYYY-MM-DD
                    if "-" in ds and len(ds) == 10:
                        base_date = datetime.strptime(ds, "%Y-%m-%d")
                        break
                except:
                    continue
            
            if not base_date:
                base_date = datetime.now()
                
            days = int(info.get("value", 0))
            result_date = base_date + timedelta(days=days)
            return result_date.strftime("%Y-%m-%d")
        except Exception as e:
            print(f"    [DEBUG] 相對日期計算失敗: {e}")
            return None
    return None

def build_gcal_url(title: str, date_str: str, description: str) -> str:
    """產生 Google Calendar 一鍵加入連結（全天行程）"""
    from urllib.parse import quote
    from datetime import datetime, timedelta
    
    try:
        # 開始日期 (YYYYMMDD)
        start_dt = datetime.strptime(date_str, "%Y-%m-%d")
        start_str = start_dt.strftime("%Y%m%d")
        
        # 結束日期 (Google 全天行程需加一天)
        end_dt = start_dt + timedelta(days=1)
        end_str = end_dt.strftime("%Y%m%d")
        
        dates = f"{start_str}/{end_str}"
        
        base_url = "https://calendar.google.com/calendar/render?action=TEMPLATE"
        url = f"{base_url}&text={quote(title)}&dates={dates}&details={quote(description)}"
        return url
    except:
        return "#"

def inject_calendar_links(html_content: str, res: dict) -> str:
    """在 HTML 內容中注入日曆連結區塊"""
    if not res.get('effective_date') and not res.get('opinion_deadline'):
        return html_content
        
    links_html = '<div style="background: #e8f0fe; border: 1px solid #4285f4; border-radius: 8px; padding: 12px 16px; margin: 16px 0;">'
    links_html += '<strong style="color: #1a73e8;">📅 加入 Google 日曆</strong><br/>'
    
    if res.get('effective_date'):
        gcal_url_eff = build_gcal_url(
            f"📌 生效日: {res['title'][:40]}",
            res['effective_date'],
            f"公告類型: {res['type']}\n摘要: {res['ai_output'].get('digest_summary', '')}\n原文: {res['link']}"
        )
        links_html += f'<a href="{gcal_url_eff}" target="_blank" style="display:inline-block; margin:6px 8px 4px 0; padding:8px 16px; background:#1a73e8; color:#fff; text-decoration:none; border-radius:4px; font-size:14px;">📌 生效日 {res["effective_date"]}</a>'
        
    if res.get('opinion_deadline'):
        gcal_url_op = build_gcal_url(
            f"🕙 意見截止: {res['title'][:40]}",
            res['opinion_deadline'],
            f"【意見徵詢截止】此公告為草案階段，請於此日期前彙整意見。\n原文: {res['link']}"
        )
        links_html += f'<a href="{gcal_url_op}" target="_blank" style="display:inline-block; margin:6px 8px 4px 0; padding:8px 16px; background:#ea8600; color:#fff; text-decoration:none; border-radius:4px; font-size:14px;">🕙 意見截止 {res["opinion_deadline"]}</a>'
    
    links_html += '</div>'
    
    # 注入到 HTML 中 (若有 📝 標題則插入其上方，否則附加於末尾)
    import re
    if re.search(r'<h3>📝\s*內部通知草稿', html_content):
        return re.sub(r'(<h3>📝\s*內部通知草稿)', links_html + r'\1', html_content)
    return html_content + links_html

def create_ics_attachment(res: dict) -> bytes:
    """為法規公告產生 Google Calendar 邀請檔 (.ics)，支援多重行程"""
    try:
        from icalendar import Calendar, Event
        from datetime import datetime, timedelta
        import hashlib

        cal = Calendar()
        cal.add('prodid', '-//Compliance Radar//mxm.dk//')
        cal.add('version', '2.0')
        cal.add('method', 'PUBLISH') # 標註為發佈模式，讓郵件客戶端顯示「加入日曆」

        # 1. 生效日行程
        if res.get('effective_date'):
            event_eff = Event()
            event_eff.add('summary', f"📌 生效日: {res['title']}")
            dt_start = datetime.strptime(res['effective_date'], "%Y-%m-%d").date()
            # Google Calendar 全天行程的 DTEND 必須是隔天 (Exclusive)
            dt_end = dt_start + timedelta(days=1)
            
            event_eff.add('dtstart', dt_start)
            event_eff.add('dtend', dt_end)
            event_eff.add('description', f"公告類型: {res['type']}\n白話摘要: {res['ai_output']['digest_summary']}\n\n原文連結: {res['link']}")
            event_eff.add('location', '金管會公告')
            event_eff.add('transp', 'TRANSPARENT') # 不佔用忙碌時間
            
            uid_eff = hashlib.md5((res['link'] + "_eff").encode()).hexdigest() + "@compliance.radar"
            event_eff.add('uid', uid_eff)
            cal.add_component(event_eff)

        # 2. 意見截止日行程 (僅針對草案或有截止資訊時)
        if res.get('opinion_deadline'):
            event_op = Event()
            event_op.add('summary', f"🕙 意見截止: {res['title']}")
            dt_op_start = datetime.strptime(res['opinion_deadline'], "%Y-%m-%d").date()
            dt_op_end = dt_op_start + timedelta(days=1)
            
            event_op.add('dtstart', dt_op_start)
            event_op.add('dtend', dt_op_end)
            event_op.add('description', f"【意見徵詢截止】\n此公告為草案階段，請於此日期前彙整意見。\n\n原文連結: {res['link']}")
            event_op.add('location', '金管會公告')
            event_op.add('transp', 'TRANSPARENT')
            
            uid_op = hashlib.md5((res['link'] + "_op").encode()).hexdigest() + "@compliance.radar"
            event_op.add('uid', uid_op)
            cal.add_component(event_op)
        
        if len(cal.subcomponents) == 0:
            return None
            
        return cal.to_ical()
    except Exception as e:
        print(f"    [WARNING] 無法產生 ICS 檔案: {e}")
        return None

# ============================================================================
# AI Processing
# ============================================================================

def process_with_ollama(item: dict, client: OpenAI, model: str ) -> dict:
    f"""呼叫 Ollama {model} 將法規編寫為 JSON 格式的摘要與草稿"""
    print(f"  🤖 正在讓 AI 分析：{item['title']}")

    prompt = f"""請扮演專業的台灣金控法遵助理。
我收到一則金管會的新公告，請幫我產生「法規重點摘要」與「要發送給內部業務部門的通知草稿」。
公告類型：{item['type']}
標題：{item['title']}
發布時間：{item['published']}
原文連結：{item['link']}
公告內容：
{item['summary']}

請直接輸出一個格式正確的 JSON Object，不要包含任何其他文字或 Markdown 標記，直接輸出 JSON 即可：
{{
    "digest_summary": "對這篇公告的白話文一句話摘要。",
    "draft_subject": "【{item['type']}】AI 合規建議 - {item['title'][:50]}...",
    "draft_body": "一段 HTML 格式的草稿內文。請確保內容【僅限】通知電子郵件的內文。請使用簡單的 <br> 和 <ul><li> 標籤排版。不要包含 <html> 或 <body> 標籤。【不要在草稿中放入原文連結，系統會自動附上】。【嚴禁放入任何虛構的 email 地址、電話號碼、會議連結或聯絡資訊，只寫法規內容與合規建議即可】。",
    "effective_date_info": {{
        "type": "exact 或 relative 或 unknown",
        "value": "搜尋標題與內文中的『施行』、『生效』、『會計年度』等關鍵字。exact 範例：'2026.3.27' 或 '115年1月1日'（直接照抄原文日期字串）；relative 範例：'0'（自發布日施行）或 '30'（30日後）；找不到就填 null"
    }},
    "opinion_deadline_info": {{
        "type": "exact 或 relative 或 unknown",
        "value": "搜尋標題與內文中的『預告期間』、『日內』、『提出意見』、『截止』。若標題含 '預告期間:2026.02.26~2026.3.27' 則 type=exact, value='2026.3.27'（取結束日期）；若為 '60日內可提出意見' 則 type=relative, value='60'；非草案或找不到就填 null"
    }}
}}
"""
    try:
        response = client.chat.completions.create(
            model=model,
            max_tokens=8000,
            temperature=0.2,
            messages=[{"role": "user", "content": prompt}]
        )
        raw_text = response.choices[0].message.content
        print(f"  [DEBUG] finish_reason: {response.choices[0].finish_reason}")
        print(f"  [DEBUG] content type: {type(raw_text)}, value: {repr(raw_text)}")
        print(f"  [DEBUG] usage: {response.usage}")
        # 尋找第一個 { 和最後一個 } 之間的內容，解決 Ollama 偶爾加入額外說明的問題
        # 修正：更精確地抓取大括號內容，避免抓到結尾說明的干擾
        import re
        json_match = re.search(r'(\{.*\})', raw_text, re.DOTALL | re.MULTILINE)
        if json_match:
            # 找到最後一個大括號，防止 AI 在後面碎碎念
            full_content = json_match.group(1)
            last_brace = full_content.rfind('}')
            json_text = full_content[:last_brace+1]
        else:
            json_text = raw_text

        try:
            return json.loads(json_text)
        except json.JSONDecodeError:
            # 若正規解析失敗，嘗試清理常見的導致斷裂的標籤
            cleaned_text = json_text.replace('\n', ' ').replace('\r', '')
            try:
                return json.loads(cleaned_text)
            except:
                # 使用非貪婪匹配抓取各個欄位，避免資料相互吞噬
                import re
                summary_m = re.search(r'"digest_summary"\s*:\s*"(.*?)"(?=\s*[,}])', json_text, re.DOTALL)
                subject_m = re.search(r'"draft_subject"\s*:\s*"(.*?)"(?=\s*[,}])', json_text, re.DOTALL)
                body_m = re.search(r'"draft_body"\s*:\s*"(.*?)"(?=\s*,\s*"effective_date_info"|\s*\})', json_text, re.DOTALL)
                
                eff_type_m = re.search(r'"effective_date_info".*?"type"\s*:\s*"(.*?)"', json_text, re.DOTALL)
                eff_val_m = re.search(r'"effective_date_info".*?"value"\s*:\s*"(.*?)"', json_text, re.DOTALL)
                
                op_type_m = re.search(r'"opinion_deadline_info".*?"type"\s*:\s*"(.*?)"', json_text, re.DOTALL)
                op_val_m = re.search(r'"opinion_deadline_info".*?"value"\s*:\s*"(.*?)"', json_text, re.DOTALL)

                if summary_m and subject_m and body_m:
                    return {
                        "digest_summary": summary_m.group(1).strip(),
                        "draft_subject": subject_m.group(1).strip(),
                        "draft_body": strip_fake_emails(body_m.group(1).strip()),
                        "effective_date_info": {
                            "type": eff_type_m.group(1) if eff_type_m else "unknown",
                            "value": eff_val_m.group(1) if eff_val_m and eff_val_m.group(1) != "null" else None
                        },
                        "opinion_deadline_info": {
                            "type": op_type_m.group(1) if op_type_m else "unknown",
                            "value": op_val_m.group(1) if op_val_m and op_val_m.group(1) != "null" else None
                        }
                    }
                raise
    except Exception as e:
        print(f"  [ERROR] Ollama API 呼叫失敗或 JSON 解析錯誤：{e}")
        return None

# ============================================================================
# Email Dispatchers
# ============================================================================

def smtp_connection(config: dict):
    """建立共用的 SMTP 連線"""
    server = smtplib.SMTP_SSL("smtp.gmail.com", 465)
    server.login(config["gmail_user"], config["gmail_app_password"])
    return server

def send_smtp_email(config: dict, msg: EmailMessage, server=None):
    """底層共用的 SMTP 寄信邏輯。可接受既有連線或自行建立。"""
    try:
        if server:
            server.send_message(msg)
        else:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
                s.login(config["gmail_user"], config["gmail_app_password"])
                s.send_message(msg)
        return True
    except Exception as e:
        print(f"  [ERROR] 發送 Email 失敗：{e}")
        return False

def dispatch_single_emails(config: dict, results: list) -> bool:
    """策略一：每則法規獨立發送一封 Email（共用 SMTP 連線）"""
    all_success = True
    try:
        server = smtp_connection(config)
    except Exception as e:
        print(f"  [ERROR] SMTP 連線失敗：{e}")
        return False

    try:
        for res in results:
            print(f"  📧 正在獨立發送 Email: {res['ai_output']['draft_subject']}")

            emoji = "📜" if res['type'] == "函釋" else "📝"
            type_style = "background-color: #e6f4ea; border-left: 4px solid #34a853;" if res['type'] == "函釋" else "background-color: #fff4e5; border-left: 4px solid #fb8c00;"
            attachment_notice = ""
            if res.get('has_attachments'):
                attachment_notice = '<div style="background-color: #fff3cd; color: #856404; padding: 10px; border: 1px solid #ffeeba; border-radius: 4px; margin: 10px 0; font-weight: bold;">⚠️ 包含附件，請務必點擊原文連結詳閱。</div>'

            # 組裝內文
            main_content = f"""
        <div style="{type_style} padding: 15px; margin-bottom: 20px;">
            <h3 style="margin-top: 0;">📊 AI 綜合摘要 ({res['type']})</h3>
            <p><strong>{res['title']}</strong><br/>{res['ai_output']['digest_summary']}</p>
            {attachment_notice}
            <p><a href="{res['link']}" target="_blank" style="color: #1a73e8;">🔗 查看原文公告</a></p>
        </div>
        <hr/>
        <h3>📝 內部通知草稿 (請確認後直接轉寄本信)</h3>
        <p style="color: #666;">建議主旨: {res['ai_output']['draft_subject']}</p>
        <div style="border: 1px solid #ddd; padding: 15px; border-radius: 5px;">
            {res['ai_output']['draft_body']}
        </div>
        """

            # 統一注入日曆連結
            final_html = inject_calendar_links(main_content, res)

            html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            {final_html}
        </body>
        </html>
        """

            msg = EmailMessage()
            msg["Subject"] = f"{emoji}【{res['type']}】AI 合規建議 - {res['title']}"
            msg["From"] = config["gmail_user"]
            msg["To"] = config["recipient_email"]
            msg.set_content(html_content, subtype='html')

            if not send_smtp_email(config, msg, server):
                all_success = False
                break # 若其中一封發送失敗，中斷發送以避免漏失記錄
    finally:
        server.quit()

    return all_success

def dispatch_digest_with_eml(config: dict, results: list) -> bool:
    """策略二：彙整長信 + .eml 草稿附件檔"""
    print(f"  📧 正在發送彙整長信含 {len(results)} 個草稿附件...")
    
    msg = MIMEMultipart()
    msg["Subject"] = f"【本日合規情報總覽】共有 {len(results)} 則新法規異動"
    msg["From"] = config["gmail_user"]
    msg["To"] = config["recipient_email"]
    
    # 建立信件本文
    html_body = "<html><body style='font-family: Arial, sans-serif;'>"
    html_body += "<h2>🏦 本日金管會合規情報總覽</h2>"
    html_body += "<p>法遵同仁您好，這是今日的合規情報。若需轉發部門，請直接下載並開啟信件附屬的 .eml 檔，即可一鍵發送草稿！</p>"
    
    for idx, res in enumerate(results, 1):
        item_summary = f"""
        <div style='margin-bottom: 15px; padding: 10px; background: #f0f7ff; border-radius: 5px;'>
            <strong>{idx}. [{res['type']}] {res['title']}</strong><br/>
            <span style='color: #444;'>{res['ai_output']['digest_summary']}</span><br/>
            <a href="{res['link']}" target="_blank" style="color: #1a73e8;">🔗 查看原文公告</a>
        </div>
        """
        # 彙整長信的摘要區塊也加入日曆連結
        html_body += inject_calendar_links(item_summary, res)
        
    html_body += "</body></html>"
    msg.attach(MIMEText(html_body, 'html'))
    
    # 建立 .eml 附件
    for res in results:
        draft = EmailMessage()
        emoji = "📜" if res['type'] == "函釋" else "📝"
        # 確保附件檔主旨也具備清楚標示
        subject = res['ai_output']['draft_subject']
        if f"【{res['type']}】" not in subject:
            subject = f"{emoji}【{res['type']}】" + subject
            
        draft["Subject"] = subject
        # 確保 .eml 附件內的草稿也包含日曆連結
        eml_body = inject_calendar_links(res['ai_output']['draft_body'], res)
        draft.set_content(eml_body, subtype='html')
        
        # 加入日曆邀請附件到 .eml 草稿中
        if res.get('effective_date') or res.get('opinion_deadline'):
            ics_data = create_ics_attachment(res)
            if ics_data:
                draft.add_attachment(
                    ics_data,
                    maintype='text',
                    subtype='calendar',
                    filename=f"regulation_deadline.ics"
                )
        
        # 轉換為位元組形式附加到外層郵件
        eml_data = draft.as_bytes()
        part = MIMEApplication(eml_data, _subtype='rfc822')
        # 防止檔名有怪字元導致錯誤
        safe_filename = "".join(c for c in res['title'] if c.isalnum() or c in " _-")[:30]
        part.add_header('Content-Disposition', 'attachment', filename=f"草稿-{safe_filename}.eml")
        msg.attach(part)

    return send_smtp_email(config, msg)

# ============================================================================
# HTML Report
# ============================================================================

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<title>金融合規雷達 — {month_label}</title>
<style>
  body {{ font-family: "Microsoft JhengHei", Arial, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; color: #333; }}
  h1 {{ color: #0056b3; border-bottom: 2px solid #0056b3; padding-bottom: 10px; }}
  .filter-bar {{ margin: 20px 0; padding: 10px; background: #eee; border-radius: 8px; position: sticky; top: 0; z-index: 100; }}
  .filter-btn {{ padding: 8px 16px; margin-right: 8px; border: none; border-radius: 4px; cursor: pointer; background: #fff; transition: background 0.2s; }}
  .filter-btn.active {{ background: #0056b3; color: #fff; }}
  .filter-btn:hover {{ background: #d0d0d0; }}
  .entry {{ padding: 12px 16px; margin: 16px 0; border-radius: 4px; display: block; }}
  .type-函釋 {{ background-color: #e6f4ea; border-left: 5px solid #34a853; }}
  .type-草案 {{ background-color: #fff4e5; border-left: 5px solid #fb8c00; }}
  .entry h3 {{ margin: 0 0 8px 0; }}
  .attachment-warn {{ background-color: #fff3cd; color: #856404; padding: 8px 12px; border: 1px solid #ffeeba; border-radius: 4px; margin: 10px 0; font-weight: bold; font-size: 0.9em; }}
  .meta {{ color: #666; font-size: 0.9em; margin-bottom: 8px; }}
  .summary {{ margin: 8px 0; }}
  .disclaimer {{ color: #999; font-size: 0.85em; margin-top: 40px; border-top: 1px solid #ddd; padding-top: 10px; }}
  a {{ color: #0056b3; }}
</style>
<script>
function filterEntries(type) {{
    const entries = document.querySelectorAll('.entry');
    const buttons = document.querySelectorAll('.filter-btn');
    
    buttons.forEach(btn => btn.classList.remove('active'));
    event.target.classList.add('active');

    entries.forEach(entry => {{
        if (type === 'all' || entry.classList.contains('type-' + type)) {{
            entry.style.display = 'block';
        }} else {{
            entry.style.display = 'none';
        }}
    }});
}}
</script>
</head>
<body>
<h1>金融合規雷達 — {month_label}</h1>
<p>本報告由系統自動產生，最後更新：{updated_at}</p>

<div class="filter-bar">
    <button class="filter-btn active" onclick="filterEntries('all')">顯示全部</button>
    <button class="filter-btn" onclick="filterEntries('函釋')">只看函釋</button>
    <button class="filter-btn" onclick="filterEntries('草案')">只看草案</button>
</div>

<div id="entries-container">
<!-- ENTRIES -->
{entries_html}
<!-- /ENTRIES -->
</div>

<div class="disclaimer">本報告為 AI 輔助分析，不構成合規結論。請由法令遵循人員逐項確認。</div>
</body>
</html>"""

ENTRY_TEMPLATE = """<div class="entry type-{entry_type}" data-published="{published}">
<h3>[{entry_type}] {title}</h3>
<div class="meta">發布時間：{published} ｜ 抓取時間：{scraped_at} ｜ <a href="{link}" target="_blank">原文連結</a></div>
{attachment_html}
<div class="summary"><strong>AI 摘要：</strong>{digest_summary}</div>
</div>"""


def append_to_html_report(results: list):
    """將本次結果追加到當月 HTML 報告，並進行全域排序與更新。"""
    REPORTS_DIR.mkdir(exist_ok=True)
    now = datetime.now()
    month_key = now.strftime("%Y-%m")
    month_label = now.strftime("%Y 年 %m 月")
    report_file = REPORTS_DIR / f"{month_key}.html"

    all_entries = []

    # 1. 讀取並解析既有 entry (若檔案存在)
    if report_file.exists():
        try:
            old_html = report_file.read_text(encoding="utf-8")
            soup = BeautifulSoup(old_html, "html.parser")
            existing_divs = soup.find_all("div", class_="entry")
            for div in existing_divs:
                # 嘗試從 class 或 data 屬性還原資料
                e_type = "函釋" if "type-函釋" in div.get("class", []) else "草案"
                # 這裡我們需要內容。為了簡單起見，我們直接保留整個 HTML 字串，
                # 但要提取發布時間以便排序。
                meta_text = div.find("div", class_="meta").get_text()
                # Meta 格式: 發布時間：2026-03-06 ｜ ...
                import re
                date_match = re.search(r"發布時間：(\d{4}-\d{2}-\d{2})", meta_text)
                pub_date = date_match.group(1) if date_match else "0000-00-00"
                
                all_entries.append({
                    "html": str(div),
                    "pub_date": pub_date
                })
        except Exception as e:
            print(f"  [WARNING] 解析舊報告失敗，將建立新檔：{e}")

    # 2. 加入本次新抓取的 entry (轉換成 HTML 字串形式)
    for res in results:
        attachment_html = ""
        if res.get("has_attachments"):
            attachment_html = '<div class="attachment-warn">⚠️ 包含附件，請務必點擊原文連結詳閱。</div>'

        pub_date = res.get("published", "0000-00-00")
        # 統一日期格式以便比較 (處理 RSS 可能帶有的時區或不精確時間)
        if len(pub_date) > 10: pub_date = pub_date[:10]

        entry_html = ENTRY_TEMPLATE.format(
            entry_type=res["type"],
            title=res["title"],
            published=pub_date,
            scraped_at=now.strftime("%Y-%m-%d %H:%M"),
            link=res["link"],
            attachment_html=attachment_html,
            digest_summary=res["ai_output"]["digest_summary"],
        )
        
        all_entries.append({
            "html": entry_html,
            "pub_date": pub_date
        })

    # 3. 全域排序：依照發布時間 Ascending (由舊到新)
    all_entries.sort(key=lambda x: x["pub_date"])

    # 4. 組合最終 HTML
    final_entries_html = "\n".join([e["html"] for e in all_entries])
    
    html = HTML_TEMPLATE.format(
        month_label=month_label,
        updated_at=now.strftime("%Y-%m-%d %H:%M"),
        entries_html=final_entries_html,
    )

    report_file.write_text(html, encoding="utf-8")
    print(f"  📄 HTML 報告已更新 (已完成排序與過濾功能)：{report_file}")

    # 更新 index.html
    update_index_html()


def update_index_html():
    """重新產生 index.html，列出所有月份報告連結。"""
    report_files = sorted(REPORTS_DIR.glob("2*.html"), reverse=True)
    if not report_files:
        return

    links = ""
    for f in report_files:
        month_key = f.stem  # e.g. "2026-03"
        links += f'<li><a href="{f.name}">{month_key}</a></li>\n'

    index_html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<title>金融合規雷達 — 歷史報告索引</title>
<style>
  body {{ font-family: "Microsoft JhengHei", Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; }}
  h1 {{ color: #0056b3; }}
  li {{ margin: 8px 0; font-size: 1.1em; }}
  a {{ color: #0056b3; }}
</style>
</head>
<body>
<h1>金融合規雷達 — 歷史報告索引</h1>
<ul>
{links}</ul>
</body>
</html>"""

    (REPORTS_DIR / "index.html").write_text(index_html, encoding="utf-8")


def check_retention_reminder():
    """檢查是否有超過 5 年保存期限的報告檔案，僅提醒不刪除。"""
    if not REPORTS_DIR.exists():
        return

    cutoff = datetime.now() - timedelta(days=5 * 365)
    expired = []
    for f in REPORTS_DIR.glob("2*.html"):
        try:
            file_month = datetime.strptime(f.stem, "%Y-%m")
            if file_month < cutoff:
                expired.append(f.name)
        except ValueError:
            continue

    if expired:
        print(f"\n  ⚠️ 以下 {len(expired)} 份報告已超過 5 年保存期限，請法遵人員評估是否歸檔：")
        for name in expired:
            print(f"     - {name}")


# ============================================================================
# Run History Logging
# ============================================================================

RUN_HISTORY_FILE = SCRIPT_DIR / "run_history.jsonl"

def log_run(rss_new: int = 0, ai_processed: int = 0, email_sent: bool = False, error: str = None):
    """每次執行結束時追加一行 JSON 到 run_history.jsonl"""
    record = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rss_new": rss_new,
        "ai_processed": ai_processed,
        "email_sent": email_sent,
        "error": error,
    }
    try:
        with open(RUN_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"  [WARN] 無法寫入運行紀錄: {e}")

# ============================================================================
# Main Wrapper
# ============================================================================

def main():
    print("=" * 60)
    print(f"  Compliance Radar 啟動時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    config = load_config()
    processed_list = load_state()
    processed_urls = set(processed_list)

    # 檢查報告保存年限
    check_retention_reminder()

    api_key = config.get("ollama_api_key")
    ollama_model = config.get("ollama_model")
    if not api_key:
        log_run(error="Ollama API Key 未設定")
        print("[ERROR] Ollama API Key 未設定，請檢查 config.json。")
        sys.exit(1)
    if not ollama_model:
        log_run(error="Ollama model 未設定")
        print("[ERROR] Ollama model 未設定，請檢查 config.json。")
    new_items = []
    rss_errors = []

    # 1. 爬取 RSS
    print("[1/3] 正在檢查最新的法令更新...")
    for source in config.get("rss_sources", []):
        try:
            try:
                response = requests.get(source["url"], timeout=15)
            except requests.exceptions.SSLError:
                response = requests.get(source["url"], timeout=15, verify=False)
            feed = feedparser.parse(response.text)
            max_match = source['max_batch']
            feed.entries = feed.entries[:max_match]
            print(f"  檢查來源: {source['name']} (取得 {len(feed.entries)} 筆)")

            for entry in feed.entries:
                link = entry.link
                if link in processed_urls:
                    continue # 已經處理過

                new_items.append({
                    "title": entry.title,
                    "link": link,
                    "published": getattr(entry, 'published', '未知時間'),
                    "summary": getattr(entry, 'summary', ''),
                    "type": source["type"]
                })
        except Exception as e:
            print(f"  [ERROR] 讀取 RSS 失敗 ({source['name']}): {e}")
            rss_errors.append(f"{source['name']}: {e}")

    if not new_items:
        if rss_errors:
            log_run(error=f"RSS 讀取失敗: {'; '.join(rss_errors)}")
            print("  [ERROR] 所有 RSS 來源讀取失敗，無法判斷是否有新法規。")
            sys.exit(1)
        log_run(rss_new=0)
        print("  ✅ 目前沒有新法規需要處理。")
        sys.exit(0)
        
    print(f"\n[2/3] 發現 {len(new_items)} 則新發布法規，開始進行 AI 分析...")
    client = OpenAI(
        base_url="https://ollama.com/v1",
        api_key=api_key
    )
    results = []
    for item in new_items:
        # 深度掃描附件
        item['has_attachments'] = check_for_attachments(item['link'])

        # 呼叫 Ollama
        ai_data = process_with_ollama(item, client, ollama_model)
        if ai_data:
            # 計算生效日期與意見截止日
            effective_date = calculate_effective_date(item['published'], ai_data.get("effective_date_info"))
            opinion_deadline = calculate_effective_date(item['published'], ai_data.get("opinion_deadline_info"))
            
            if effective_date:
                print(f"    📅 計算生效日：{effective_date}")
            if opinion_deadline:
                print(f"    🕙 計算意見截止日：{opinion_deadline}")
            
            item['ai_output'] = ai_data
            item['effective_date'] = effective_date
            item['opinion_deadline'] = opinion_deadline # 新增欄位
            results.append(item)
        
        time.sleep(1) # 簡單的速率限制

    if not results:
        log_run(rss_new=len(new_items), ai_processed=0, error="AI 分析全部失敗")
        print("  ❌ 未能成功產生任何 AI 摘要，跳過發信。")
        sys.exit(1)

    print("\n[3/3] 開始配送 Email 報告...")
    strategy = config.get("email_strategy", "single_emails")
    
    success = False
    if strategy == "digest_with_eml":
        success = dispatch_digest_with_eml(config, results)
    else:
        success = dispatch_single_emails(config, results)

    # 如果寄信成功，才把網址寫入 processed_announcements.json
    if success:
        print("\n  ✅ 全部發送成功！正在更新已處理清單...")
        for r in results:
            processed_urls.add(r['link'])
        # 若清單過長，可視情況截斷，例如保留最近 1000 筆
        save_state(list(processed_urls)[-1000:])

        # 同步寫入 HTML 歷史報告
        append_to_html_report(results)
        log_run(rss_new=len(new_items), ai_processed=len(results), email_sent=True)
    else:
        log_run(rss_new=len(new_items), ai_processed=len(results), error="Email 發送失敗")
        print("\n  ❌ 發送過程中發生錯誤，狀態將不會更新，下次將重試。")
        sys.exit(1)

if __name__ == "__main__":
    main()
