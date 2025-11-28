import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import quote

# 監控的 4 段常磐線
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


def pick_title_index(name: str, strings: list[str]) -> int | None:
    """
    在所有等于 name 的行中，找到"下一行是日期"的那个索引。
    """
    date_re = re.compile(r"\d{1,2}月\d{1,2}日\s+\d{1,2}時\d{1,2}分")
    candidates = [i for i, t in enumerate(strings) if t == name]
    for i in candidates:
        if i + 1 < len(strings) and date_re.fullmatch(strings[i + 1]):
            return i
    return candidates[0] if candidates else None


def fetch_page_info(name: str, url: str):
    """
    爬取页面信息，返回：更新时间、状态、原因
    """
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    strings = [s.strip() for s in soup.stripped_strings if s.strip()]

    updated = "更新時刻不明"
    status = "状態不明"
    reason = None

    # 1. 找标题行索引
    title_idx = pick_title_index(name, strings)

    # 2. 更新时间
    if title_idx is not None and title_idx + 1 < len(strings):
        updated = strings[title_idx + 1]
        if title_idx + 2 < len(strings) and "更新" in strings[title_idx + 2]:
            updated += strings[title_idx + 2]
        elif "更新" not in updated:
            updated += "更新"

    if updated == "更新時刻不明":
        dt_re = re.compile(r"\d{1,2}月\d{1,2}日\s+\d{1,2}時\d{1,2}分")
        for t in strings:
            if dt_re.fullmatch(t):
                updated = t + "更新"
                break

    # 3. 状态
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
        if t in status_words:
            status = t
            status_idx = j
            break

    # 4. 原因（只在非平常運転时抓取）
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
    """收集所有线路信息"""
    results = []
    for name, url in LINES:
        try:
            updated, status, reason = fetch_page_info(name, url)
            results.append({
                "name": name,
                "updated": updated,
                "status": status,
                "reason": reason,
            })
        except Exception as e:
            results.append({
                "name": name,
                "updated": "取得失敗",
                "status": "情報取得エラー",
                "reason": str(e),
            })
    return results


def build_grouped_message(results):
    """
    分组规则：相同状态和原因的线路合并
    返回：是否有异常、是否严重、消息内容
    """
    groups = {}
    for r in results:
        key = (r["status"] or "", r["reason"] or "")
        groups.setdefault(key, []).append(r)

    blocks = []
    has_abnormal = False
    has_severe = False

    for (status, reason), items in groups.items():
        # 合并标题
        names = " / ".join(i["name"] for i in items)
        updated = items[0]["updated"]

        # 构建每个分组的内容块
        block_lines = [
            f"【{names}】",
            f"状態：{status}",
            f"更新：{updated}",
        ]

        # 判断是否异常
        if "平常運転" not in status and "情報取得エラー" not in status:
            has_abnormal = True
        if any(w in status for w in ["運転見合わせ", "運休", "脱線"]):
            has_severe = True

        # 添加原因（只在有原因且非错误时）
        if reason and "情報取得エラー" not in status:
            block_lines.append(f"原因：{reason}")

        blocks.append("\n".join(block_lines))

    # 用两个换行分隔不同分组
    body = "\n\n".join(blocks)
    return has_abnormal, has_severe, body


def choose_icon(has_abnormal: bool, has_severe: bool) -> str:
    """根据状态选择图标"""
    if not has_abnormal:
        return ICON_OK
    if has_severe:
        return ICON_ERR
    return ICON_WARN


def send_bark(title: str, body: str, icon_url: str | None = None):
    """发送 Bark 推送"""
    bark_key = os.environ.get("BARK_KEY")
    if not bark_key:
        raise RuntimeError("環境変数 BARK_KEY が設定されていません。")

    # 清理 bark_key，移除可能的空格或换行
    bark_key = bark_key.strip()
    
    # Bark URL 格式：https://api.day.app/{key}/{title}/{body}
    # 使用 POST 方法更可靠
    url = f"https://api.day.app/{bark_key}"
    
    payload = {
        "title": title,
        "body": body,
        "icon": icon_url if icon_url else ""
    }
    
    print(f"Sending to Bark...")
    print(f"Title: {title}")
    print(f"Body preview: {body[:100]}...")
    print(f"URL: {url}")
    
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"✓ Bark 推送成功！ (status: {resp.status_code})")
        return True
    except requests.exceptions.HTTPError as e:
        print(f"✗ Bark 推送失败: {e}")
        print(f"Response: {resp.text if resp else 'No response'}")
        # 尝试 GET 方法作为备选
        print("嘗試使用 GET 方法...")
        try:
            url_get = f"https://api.day.app/{bark_key}/{quote(title)}/{quote(body)}"
            if icon_url:
                url_get += f"?icon={quote(icon_url, safe=':/')}"
            resp = requests.get(url_get, timeout=10)
            resp.raise_for_status()
            print(f"✓ Bark 推送成功 (GET方法)！")
            return True
        except Exception as e2:
            print(f"✗ GET 方法也失败: {e2}")
            raise
    except Exception as e:
        print(f"✗ 网络错误: {e}")
        raise


def main():
    print("開始監視常磐線運行情報...")
    results = collect_all_lines()
    has_abnormal, has_severe, body = build_grouped_message(results)

    title = "常磐線運行情報"
    icon = choose_icon(has_abnormal, has_severe)

    send_bark(title, body, icon)
    print("完了！")


if __name__ == "__main__":
    main()
