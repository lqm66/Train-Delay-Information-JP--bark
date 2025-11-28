import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote

# 监控的 4 段常磐線（名字写死在这里，方便合并&显示）
LINES = [
    ("常磐線(快速)[品川～取手]", "https://transit.yahoo.co.jp/diainfo/57/0"),
    ("常磐線(各停)",             "https://transit.yahoo.co.jp/diainfo/58/0"),
    ("常磐線[品川～水戸]",       "https://transit.yahoo.co.jp/diainfo/59/59"),
    ("常磐線[水戸～いわき]",     "https://transit.yahoo.co.jp/diainfo/59/60"),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

ICON_OK   = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/2705.png"   # ✅
ICON_WARN = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/26a0.png"  # ⚠
ICON_ERR  = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/274c.png"  # ❌


def pick_title_index(name, strings):
    """
    在所有等于 name 的行中，找到“下一行是日期”的那个索引。
    例：
      [19] 常磐線(快速)[品川～取手]         ← 面包屑
      [20] じょうばんせん(かいそく)…
      [21] 常磐線(快速)[品川～取手]         ← 正文标题
      [22] 11月28日 8時59分
      [23] 更新
    """
    date_re = re.compile(r"\d{1,2}月\d{1,2}日\s+\d{1,2}時\d{1,2}分")
    candidates = [i for i, t in enumerate(strings) if t == name]
    for i in candidates:
        if i + 1 < len(strings) and date_re.fullmatch(strings[i + 1]):
            return i
    return candidates[0] if candidates else None


def fetch_page_info(name, url):
    """
    - 更新：标题下一行(日期) + 再下一行“更新”
    - 状態：标题后面往下扫，取第一个是状态词的那一行
    - 原因：只有非「平常運転」时，从状态下一两行开始取直到“迂回ルート検索/路線を登録…”为止
    """
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    strings = [s.strip() for s in soup.stripped_strings if s.strip()]

    updated = "更新時刻不明"
    status = "状態不明"
    reason = None

    # 1) 找真正标题行
    title_idx = pick_title_index(name, strings)

    # 2) 更新：22 行日期 + 23 行“更新”
    if title_idx is not None and title_idx + 1 < len(strings):
        updated = strings[title_idx + 1]
        if title_idx + 2 < len(strings) and "更新" in strings[title_idx + 2]:
            updated += strings[title_idx + 2]
        elif "更新" not in updated:
            updated += "更新"

    # 兜底：用 “xx月xx日 xx時xx分”
    if updated == "更新時刻不明":
        dt_re = re.compile(r"\d{1,2}月\d{1,2}日\s+\d{1,2}時\d{1,2}分")
        for t in strings:
            if dt_re.fullmatch(t):
                updated = t + "更新"
                break

    # 3) 状態：标题后面往下扫
    status_words = [
        "平常運転", "遅延", "運転見合わせ", "運休",
        "ダイヤ乱れ", "運転状況", "列車遅延", "その他",
    ]
    status_idx = None
    if title_idx is not None:
        search_range = range(title_idx + 1, min(title_idx + 15, len(strings)))
    else:
        search_range = range(len(strings))

    for j in search_range:
        t = strings[j]
        if t in status_words:   # 避免匹配到假名那行
            status = t
            status_idx = j
            break

    # 4) 原因：只有非平常運転时才取
    if status_idx is not None and "平常運転" not in status:
        stop_words = [
            "迂回ルート検索",
            "路線を登録",
            "路線を登録すると",
            "に関するつぶやき",
            "ツイート",
        ]
        detail_lines = []
        for j in range(status_idx + 1, min(status_idx + 10, len(strings))):
            t = strings[j]
            if any(sw in t for sw in stop_words):
                break
            detail_lines.append(t)

        if detail_lines:
            reason_text = " ".join(detail_lines)
            if "事故･遅延に関する情報はありません" not in reason_text:
                reason = reason_text

    return updated, status, reason


def collect_all_lines():
    results = []
    for name, url in LINES:
        try:
            updated, status, reason = fetch_page_info(name, url)
            results.append(
                {
                    "name": name,       # 用作“路線名”
                    "updated": updated,
                    "status": status,
                    "reason": reason,
                }
            )
        except Exception as e:
            results.append(
                {
                    "name": name,
                    "updated": "取得失敗",
                    "status": "情報取得エラー",
                    "reason": str(e),
                }
            )
    return results


def build_grouped_message(results):
    """
    合并逻辑：
      key = (status, reason_text)

    输出格式：
      路線：常磐線(快速)… / 常磐線(各停) / …
      状態：平常運転 / 列車遅延 / …
      更新：11月28日 9時52分更新
      原因：……（有事故时才有）
    """
    groups = {}
    for r in results:
        key = (r["status"] or "", r["reason"] or "")
        groups.setdefault(key, []).append(r)

    blocks = []
    has_abnormal = False
    has_severe = False

    for (status, reason), items in groups.items():
        names = " / ".join(i["name"] for i in items)
        updated = items[0]["updated"]

        lines = [
            f"路線：{names}",
            f"状態：{status}",
            f"更新：{updated}",
        ]

        if "平常運転" not in status and "情報取得エラー" not in status:
            has_abnormal = True
        if any(w in status for w in ["運転見合わせ", "運休", "脱線"]):
            has_severe = True

        if reason and "情報取得エラー" not in status:
            lines.append(f"原因：{reason}")

        blocks.append("\n".join(lines))

    body = "\n\n".join(blocks)
    return has_abnormal, has_severe, body


def choose_icon(has_abnormal, has_severe):
    if not has_abnormal:
        return ICON_OK
    if has_severe:
        return ICON_ERR
    return ICON_WARN


def send_bark(title, body, icon_url=None):
    bark_key = os.environ.get("BARK_KEY")
    if not bark_key:
        raise RuntimeError("環境変数 BARK_KEY が設定されていません。")

    url = f"https://api.day.app/{bark_key}/{quote(title)}/{quote(body)}"
    if icon_url:
        url += "?icon=" + quote(icon_url, safe=":/")

    # 在 GitHub Actions 日志里也打印一下，方便以后 debug
    print("Bark URL:", url)
    requests.get(url, timeout=10)


def main():
    results = collect_all_lines()
    has_abnormal, has_severe, body = build_grouped_message(results)

    title = "常磐線運行情報"
    icon = choose_icon(has_abnormal, has_severe)
    send_bark(title, body, icon)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 万一脚本报错，也给你推一条错误通知
        try:
            err_msg = f"スクリプトエラー: {repr(e)}"
            print(err_msg)
            send_bark("常磐線運行情報（エラー）", err_msg, ICON_ERR)
        except Exception as _:
            # 如果连 Bark 都发不出去，只能让 Actions 变红了
            raise
