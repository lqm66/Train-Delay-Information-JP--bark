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

# 提取原因时用到的一些关键词
REASON_KEYWORDS = [
    "人身事故", "脱線事故", "脱線", "車両故障", "車両点検",
    "信号トラブル", "信号関係の故障",
    "踏切内での事故", "踏切での事故",
    "線路内立ち入り", "線路内への立ち入り",
    "強風", "大雨", "大雪", "落雷", "停電",
    "安全確認", "ポイント故障", "工事", "影響", "代行輸送", "見合わせ"
]

# Twemoji 图标（emoji 风 PNG）
ICON_OK = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/2705.png"   # ✅
ICON_WARN = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/26a0.png"  # ⚠
ICON_ERROR = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/274c.png" # ❌


def fetch_page_info(url: str):
    """
    从 Yahoo diainfo 页面解析：
    - updated: '11月27日 15時47分更新'
    - status:  '平常運転' / '遅延' / '運転見合わせ' / 'その他' 等
    - detail_text: 状態行下面的一段说明，用来抽取原因
    """
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    strings = list(soup.stripped_strings)

    updated = "更新時刻不明"
    status = "状態不明"

    # 1) 找更新时间：包含 月/日/時/分 和 “更新”
    updated_idx = None
    for i, s in enumerate(strings):
        if "更新" in s and "月" in s and "日" in s and "時" in s and "分" in s:
            updated = s.strip()
            updated_idx = i
            break

    # 2) 找状态：优先在“更新”后面的几行找一个短字符串
    status_candidates = []
    if updated_idx is not None:
        status_candidates.extend(strings[updated_idx + 1 : updated_idx + 6])
    status_candidates.extend(strings)  # 兜底再全局扫

    # 常见状态文字，加上“その他”
    status_words = ["平常運転", "遅延", "運転見合わせ", "運休", "ダイヤ乱れ", "その他"]
    for s in status_candidates:
        text = s.strip()
        if len(text) <= 10 and any(w == text or w in text for w in status_words):
            status = text
            break

    # 3) 详细文本：从状态行下面开始抓几行，用于提取“红字说明”
    detail_start = 0
    if status != "状態不明":
        try:
            idx = strings.index(status)
            detail_start = idx + 1
        except ValueError:
            detail_start = (updated_idx + 1) if updated_idx is not None else 0
    elif updated_idx is not None:
        detail_start = updated_idx + 1

    detail_text = " ".join(strings[detail_start : detail_start + 30])
    return updated, status, detail_text


def extract_reason_and_delay(detail_text: str, status: str):
    """
    从 detail 文本中提取原因和延迟分钟数：
    - 平常運転：直接返回 (None, None)
    - 其他状态：如果没有事故/遅延相关词，也返回 None，避免把标题当原因
    """
    # 平常運転就完全不显示原因
    if "平常運転" in status:
        return None, None

    # 有些页面会写“現在、事故・遅延に関する情報はありません。”，这种也不用当原因
    if "事故・遅延に関する情報はありません" in detail_text:
        return None, None

    reason_text = None
    delay_minutes = None

    # 先看看文本里有没有“○分遅れ”
    m = re.search(r"(\d+)\s*分(?:程度|前後)?(?:の)?遅れ", detail_text)
    if m:
        try:
            delay_minutes = int(m.group(1))
        except ValueError:
            delay_minutes = None

    # 只有在包含“事故/遅延/見合わせ/運休/故障”等字眼时，才认为有“原因”
    if not any(kw in detail_text for kw in ["事故", "遅延", "見合わせ", "運休", "故障", "脱線", "影響"]):
        return None, delay_minutes

    # 优先用我们定义的关键词附近的一段话
    for kw in REASON_KEYWORDS:
        if kw in detail_text:
            idx = detail_text.index(kw)
            start = max(0, idx - 20)
            snippet = detail_text[start : idx + len(kw) + 40]
            reason_text = snippet.strip()
            break

    # 如果没匹配到具体关键词，就取第一句话
    if reason_text is None:
        sentence_end = re.search(r"[。！!？?]", detail_text)
        if sentence_end:
            reason_text = detail_text[: sentence_end.end()]
        else:
            reason_text = detail_text[:60] + ("…" if len(detail_text) > 60 else "")

    return reason_text, delay_minutes


def collect_all_lines():
    results = []
    for name, url in LINES:
        try:
            updated, status, detail = fetch_page_info(url)
            reason, delay_minutes = extract_reason_and_delay(detail, status)
            results.append(
                {
                    "name": name,
                    "updated": updated,
                    "status": status,
                    "reason": reason,
                    "delay_minutes": delay_minutes,
                }
            )
        except Exception as e:
            results.append(
                {
                    "name": name,
                    "updated": "取得失敗",
                    "status": "情報取得エラー",
                    "reason": str(e),
                    "delay_minutes": None,
                }
            )
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

        # 只要不是平常運転，就算“有异常”
        if "平常運転" not in status and "情報取得エラー" not in status:
            has_abnormal = True
        if any(x in status for x in ["運転見合わせ", "運休", "脱線"]):
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
        raise RuntimeError(
            "環境変数 BARK_KEY が設定されていません。GitHub Secrets に BARK_KEY を設定してください。"
        )

    base = f"https://api.day.app/{bark_key}/{quote(title)}/{quote(body)}"
    if icon_url:
        base = base + "?icon=" + quote(icon_url, safe=":/")
    requests.get(base, timeout=10)


def main():
    results = collect_all_lines()
    has_abnormal, has_severe, body = build_grouped_message(results)
    title = "常磐線運行情報"
    icon = choose_icon(has_abnormal, has_severe)

    # 想只在有异常时推送的话改成 if has_abnormal: send_bark(...)
    send_bark(title, body, icon)


if __name__ == "__main__":
    main()
