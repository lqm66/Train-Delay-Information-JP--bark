import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote

# JR 常磐線 4 区间（你前面整理的那四个）
LINES = [
    ("常磐線(快速)[品川～取手]", "https://transit.yahoo.co.jp/diainfo/57/0"),
    ("常磐線(各停)",             "https://transit.yahoo.co.jp/diainfo/58/0"),
    ("常磐線[品川～水戸]",       "https://transit.yahoo.co.jp/diainfo/59/59"),
    ("常磐線[水戸～いわき]",     "https://transit.yahoo.co.jp/diainfo/59/60"),
]

# 一些常见的原因关键词（简单提取一下“为什么延迟”）
REASON_KEYWORDS = [
    "人身事故", "車両故障", "車両点検", "信号トラブル", "信号関係の故障",
    "踏切内での事故", "踏切での事故", "線路内立ち入り", "線路内への立ち入り",
    "強風", "大雨", "大雪", "落雷", "停電", "安全確認", "ポイント故障", "工事"
]

# Twemoji 图标（emoji 风 PNG）
ICON_OK = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/2705.png"  # ✅
ICON_WARN = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/26a0.png"  # ⚠
ICON_ERROR = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/274c.png"  # ❌


def fetch_page_info(url: str):
    """
    从 Yahoo diainfo 页面解析：
    - 更新时刻（字符串）
    - 状态（平常運転/遅延/運転見合わせ…）
    - detail_text（用来提取原因和延迟）
    """
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    strings = list(soup.stripped_strings)

    updated = "更新時刻不明"
    status = "状態不明"
    detail_text = ""

    idx = None
    for i, s in enumerate(strings):
        if s.endswith("更新"):
            updated = s
            idx = i
            break

    if idx is not None:
        if idx + 1 < len(strings):
            status = strings[idx + 1]
        detail_candidates = strings[idx + 2: idx + 12]
        detail_text = " ".join(detail_candidates)
    else:
        detail_text = " ".join(strings)

    return updated, status, detail_text


def extract_reason_and_delay(detail_text: str):
    """
    从 detail 文本中尽量提取：
    - reason_text: 例如“○○駅での人身事故”
    - delay_minutes: int 或 None
    """
    reason_text = None
    delay_minutes = None

    # 提取“20分遅れ”“20分程度の遅れ”等
    m = re.search(r"(\d+)\s*分(?:程度|前後)?(?:の)?遅れ", detail_text)
    if m:
        try:
            delay_minutes = int(m.group(1))
        except ValueError:
            delay_minutes = None

    # 提取原因关键词
    for kw in REASON_KEYWORDS:
        if kw in detail_text:
            idx = detail_text.index(kw)
            start = max(0, idx - 15)
            snippet = detail_text[start: idx + len(kw) + 15]
            reason_text = snippet.strip()
            break

    # 如果完全没有关键词，但有文本，就截一小段当原因
    if reason_text is None and detail_text:
        sentence_end = re.search(r"[。！!？?]", detail_text)
        if sentence_end:
            reason_text = detail_text[:sentence_end.end()]
        else:
            reason_text = detail_text[:40] + ("…" if len(detail_text) > 40 else "")

    return reason_text, delay_minutes


def collect_all_lines():
    """
    返回每一段线区的解析结果列表：
    [
      {
        "name": "常磐線(快速)[品川～取手]",
        "updated": "... 更新",
        "status": "遅延",
        "reason": "...",
        "delay_minutes": 20
      },
      ...
    ]
    """
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
    - 完全一样的就合并显示线名，避免四条内容重复

    返回：
    - has_abnormal: 是否存在非“平常運転”状态
    - has_severe: 是否存在“運転見合わせ / 運休”等严重状态
    - body_text: 发送到 Bark 的正文
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
    """
    根据整体情况选图标：
    - 全部平常運転 → ✅
    - 有异常但无严重停运 → ⚠
    - 有運転見合わせ / 運休 → ❌
    """
    if not has_abnormal:
        return ICON_OK
    if has_severe:
        return ICON_ERROR
    return ICON_WARN


def send_bark(title: str, body: str, icon_url: str | None = None):
    bark_key = os.environ.get("BARK_KEY")
    if not bark_key:
        raise RuntimeError("環境変数 BARK_KEY が設定されていません。GitHub Secrets に BARK_KEY を設定してください。")

    # Path 中的日文标题 & 正文要 URL 编码
    base = f"https://api.day.app/{bark_key}/{quote(title)}/{quote(body)}"
    if icon_url:
        # icon 是 URL，需要保留 :/ 不被转义
        base = base + "?icon=" + quote(icon_url, safe=":/")

    requests.get(base, timeout=10)


def main():
    results = collect_all_lines()
    has_abnormal, has_severe, body = build_grouped_message(results)

    # 标题可按需改，比如加「朝」「夕」
    title = "常磐線運行情報"

    icon = choose_icon(has_abnormal, has_severe)

    # 你可以选择：
    # 1）只在有异常时推送，在 GitHub Actions 里跑脚本不会白吵你：
    # if has_abnormal:
    #     send_bark(title, body, icon)

    # 2）无论是否异常都推送（早晚电车日报）：
    send_bark(title, body, icon)


if __name__ == "__main__":
    main()
