import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote

# 想监控的 4 段常磐線（标题写死在这里，方便合并显示）
LINES = [
    ("常磐線(快速)[品川～取手]", "https://transit.yahoo.co.jp/diainfo/57/0"),
    ("常磐線(各停)",             "https://transit.yahoo.co.jp/diainfo/58/0"),
    ("常磐線[品川～水戸]",       "https://transit.yahoo.co.jp/diainfo/59/59"),
    ("常磐線[水戸～いわき]",     "https://transit.yahoo.co.jp/diainfo/59/60"),
]

# 防止被当成机器人，带一个正常 UA
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# 用来判断是否“有事”的关键词
REASON_KEYWORDS = [
    "人身事故", "脱線事故", "脱線", "車両故障", "車両点検",
    "信号トラブル", "信号関係の故障",
    "踏切内での事故", "踏切での事故",
    "線路内立ち入り", "線路内への立ち入り",
    "強風", "大雨", "大雪", "落雷", "停電",
    "安全確認", "ポイント故障", "工事", "影響",
    "代行輸送", "見合わせ",
]

# Bark 图标（Twemoji）
ICON_OK   = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/2705.png"   # ✅
ICON_WARN = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/26a0.png"  # ⚠
ICON_ERR  = "https://raw.githubusercontent.com/twitter/twemoji/master/assets/72x72/274c.png"  # ❌


def fetch_page_info(name: str, url: str):
    """
    从 Yahoo diainfo 页面解析单一线路的信息：

    返回:
      updated: '11月28日 8時59分更新'  22+23 行
      status:  '平常運転' / '遅延' / '運転状況' / '列車遅延' 等  25 行
      reason:  有事故时的红字说明（26+27 行合在一起）；平常運転则为 None
    """
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    strings = [s.strip() for s in soup.stripped_strings if s.strip()]

    updated = "更新時刻不明"
    status = "状態不明"
    reason = None

    title_idx = None
    for i, t in enumerate(strings):
        if t == name:
            title_idx = i
            break

    # ---------- 更新时间：标题下一行 + “更新” ----------
    if title_idx is not None and title_idx + 1 < len(strings):
        # 22 行：日期时间
        updated = strings[title_idx + 1]
        # 23 行：几乎总是 “更新”
        if title_idx + 2 < len(strings) and "更新" in strings[title_idx + 2]:
            updated = updated + strings[title_idx + 2]
        elif "更新" not in updated:
            updated = updated + "更新"

    # 如果标题没找到，兜底用 “xx月xx日 xx時xx分”
    if updated == "更新時刻不明":
        dt_pattern = re.compile(r"\d{1,2}月\d{1,2}日\s+\d{1,2}時\d{1,2}分")
        for t in strings:
            if dt_pattern.search(t):
                updated = t + "更新"
                break

    # ---------- 状態：标题后面往下扫 ----------
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
        if len(t) <= 10 and any(w in t for w in status_words):
            status = t
            status_idx = j
            break

    # ---------- 原因：状态下一两行（事故时才有） ----------
    if status_idx is not None and "平常運転" not in status:
        # 规则：从状态行后面开始，抓到遇到“迂回ルート検索 / 路線を登録 / つぶやき”等为止
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
            # 一般情况：
            # 25 行：状態（比如 列車遅延）
            # 26 行：红字具体原因
            # 27 行：括号里的“（11月28日 6時55分掲載）”
            # 这里干脆把 26+27 行拼在一起
            reason_text = " ".join(detail_lines)
            # 如果只是“現在､事故･遅延に関する情報はありません。”之类，就当没有
            if "事故･遅延に関する情報はありません" not in reason_text:
                reason = reason_text

    return updated, status, reason


def collect_all_lines():
    """抓取 4 段常磐線的全部信息"""
    results = []
    for name, url in LINES:
        try:
            updated, status, reason = fetch_page_info(name, url)
            results.append(
                {
                    "name": name,
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

    - key = (status, reason_text)
    - 完全相同的一组线路合并成一块，第一行是
      【常磐線(快速)[品川～取手] / 常磐線(各停) / …】
    - 内容格式：

      【…】
      状態：平常運転 / 遅延 / …
      更新：11月27日 15時47分更新
      原因：……（有事故时才有这一行）
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
        updated = items[0]["updated"]  # 同组认为更新时间相同

        lines = [
            f"",
            f"状態：{status}",
            f"更新：{updated}",
        ]

        # 有不是“平常運転”的就算异常
        if "平常運転" not in status and "情報取得エラー" not in status:
            has_abnormal = True
        if any(w in status for w in ["運転見合わせ", "運休", "脱線"]):
            has_severe = True

        if reason and "情報取得エラー" not in status:
            lines.append(f"原因：{reason}")

        blocks.append("\n".join(lines))

    body = "\n\n".join(blocks)
    return has_abnormal, has_severe, body


def choose_icon(has_abnormal: bool, has_severe: bool) -> str:
    """根据是否有事故选择不同图标"""
    if not has_abnormal:
        return ICON_OK
    if has_severe:
        return ICON_ERR
    return ICON_WARN


def send_bark(title: str, body: str, icon_url: str | None = None):
    bark_key = os.environ.get("BARK_KEY")
    if not bark_key:
        raise RuntimeError("環境変数 BARK_KEY が設定されていません。")

    url = f"https://api.day.app/{bark_key}/{quote(title)}/{quote(body)}"
    if icon_url:
        url += "?icon=" + quote(icon_url, safe=":/")

    requests.get(url, timeout=10)


def main():
    results = collect_all_lines()
    has_abnormal, has_severe, body = build_grouped_message(results)

    title = "常磐線運行情報"  # Bark 通知标题
    icon = choose_icon(has_abnormal, has_severe)

    send_bark(title, body, icon)


if __name__ == "__main__":
    main()
