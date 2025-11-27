import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote

# JR 常磐線 4 区间
LINES = [
    ("常磐線(快速)[品川～取手]", "https://transit.yahoo.co.jp/diainfo/57/0"),
    ("常磐線(各停)",             "https://transit.yahoo.co.jp/diainfo/58/0"),
    ("常磐線[品川～水戸]",       "https://transit.yahoo.co.jp/diainfo/59/59"),
    ("常磐線[水戸～いわき]",     "https://transit.yahoo.co.jp/diainfo/59/60"),
]

REASON_KEYWORDS = [
    "人身事故", "車両故障", "車両点検", "信号トラブル", "信号関係の故障",
    "踏切内での事故", "踏切での事故", "線路内立ち入り", "線路内への立ち入り",
    "強風", "大雨", "大雪", "落雷", "停電", "安全確認", "ポイント故障", "工事"
]

# Twemoji 图标（emoji 风 PNG）
ICON_OK = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/2705.png"   # ✅
ICON_WARN = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/26a0.png"  # ⚠
ICON_ERROR = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/274c.png" # ❌


def fetch_page_info(url: str):
    """
    从 Yahoo diainfo 页面解析：
    - 更新时刻
    - 状态（平常運転/遅延/運転見合わせ…）
    - detail_text（后续用来提取原因和延迟）
    """
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    strings = list(soup.stripped_strings)

    updated = "更新時刻不明"
    status = "状態不明"

    # 更宽松地找“xx月xx日 xx時xx分 更新”
    for s in strings:
        if "更新" in s and "時" in s and re.search(r"更新$", s):
            updated = s
            break

    # 更宽松地找状态：平常運転 / 遅延 / 運転見合わせ / 運休 / ダイヤ乱れ 等
    for s in strings:
        if len(s) <= 10 and any(
            kw in s for kw in ["平常運転", "遅延", "運転見合わせ", "運休", "ダイヤ乱れ"]
        ):
            status = s
            break

    # 详细文本：从“更新”那行之后开始，多抓几行
    detail_start = 0
    if updated != "更新時刻不明":
        try:
            idx = strings.index(updated)
            detail_start = idx + 1
        except ValueError:
            pass

    detail_text = " ".join(strings[detail_start: detail_start + 20])
    return updated, status, detail_text


def extract_reason_and_delay(detail_text: str):
    """从 detail 文本中尽量提取原因和大致延迟分钟数"""
    reason_text = None
    delay_minutes = None

    m = re.search(r"(\d+)\s*分(?:程度|前後)?(?:の)?遅れ", detail_text)
    if m:
        try:
            delay_minutes = int(m.group(1))
        except ValueError:
            delay_minutes = None

    for kw in REASON_KEYWORDS:
        if kw in detail_text:
            idx = detail_text.index(kw)
            start = max(0, idx - 15)
            snippet = detail_text[start: idx + len(kw) + 15]
            reason_text = snippet.strip()
            break

    if reason_text is None and detail_text:
        sentence_end = re.search(r"[。！!？?]", detail_text)
        if sentence_end:
            reason_text = detail_text[:sentence_end.end()]
        else:
            reason_text = detail_text[:40] + ("…" if len(detail_text) > 40 else "")

    return reason_text, delay_minutes


def collect_all_lines():
    results = []
    for name, url in LINES:
        try:
            updated, status, detail = fetch_page_info(url)
            reason, delay_minutes = extract_reason_and_delay(detail)
            results.append({
                "name": name,
                "updated": updated,
                "status": status,
                "reason": reason,
                "delay_minutes": delay_minutes,
            })
        except Exception as e:
            results.append({
                "name": name,
                "updated": "取得失敗",
                "status": "情報取得エラー",
                "reason": str(e),
                "delay_minutes": None,
            })
    return results


def build_grouped_message(results):
    """
    按 (status, reason, delay_minutes) 分组：
    - 完全一样的分一组显示，避免四条重复
    """
    groups = {}
    for r in results:
        key = (r["status"] or "", r["reason"] or "", r["delay_minutes"] or 0)
        groups.setdefault(key, []).append(r)

    lines = []
    has_abnormal = False
    has_severe = False

    for (status, reason, delay_minutes), items in groups.items():
        names = " / ".join(i["name"] for i in items)
        updated = items[0]["updated"]

        block_lines = [f"", f"状態：{status}", f"更新：{updated}"]

        if "平常運転" not in status and "情報取得エラー" not in status:
            has_abnormal = True
        if any(x in status for x in ["運転見合わせ", "運休", "運転を見合わせ"]):
            has_severe = True

        if reason and "情報取得エラー" not in status:
            block_lines.append(f"原因：{reason}")
        if delay_minutes and "情報取得エラー" not in status:
            block_lines.append(f"遅延：最大およそ{delay_minutes}分")

        lines.append("\n".join(block_lines))

    body = "\n\n".join(lines)
    return has_abnormal, has_severe, body


def choose_icon(has_abnormal: bool, has_severe: bool) -> str:
    """根据整体情况选图标：✅ / ⚠ / ❌"""
    if not has_abnormal:
        return ICON_OK
    if has_severe:
        return ICON_ERROR
    return ICON_WARN


def send_bark(title: str, body: str, icon_url: str | None = None):
    bark_key = os.environ.get("BARK_KEY")
    if not bark_key:
        raise RuntimeError("環境変数 BARK_KEY が設定されていません。GitHub Secrets に BARK_KEY を設定してください。")

    base = f"https://api.day.app/{bark_key}/{quote(title)}/{quote(body)}"
    if icon_url:
        base = base + "?icon=" + quote(icon_url, safe=":/")
    requests.get(base, timeout=10)


def main():
    results = collect_all_lines()
    has_abnormal, has_severe, body = build_grouped_message(results)
    title = "常磐線運行情報"
    icon = choose_icon(has_abnormal, has_severe)

    # 想只在有异常时推送就把下面两行改成 if has_abnormal: send_bark(...)
    send_bark(title, body, icon)


if __name__ == "__main__":
    main()
