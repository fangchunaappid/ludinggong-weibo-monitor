#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
weibo-monitor — 超级鹿鼎公微博原创帖增量监控
=============================================
功能：
  1. 抓取 m.weibo.cn 时间线（UID 3962719063 = 挖地瓜的超级鹿鼎公）；
  2. 只收本人内容：原创帖 + 「回复@」粉丝帖，纯转发跳过；
  3. 增量去重：优先按微博 id，退化为 (日期, 正文) 键，绝不重复入库；
  4. 数据集 data/posts.json 按「新→旧」追加；
  5. 飞书仅推送「成功入库」通知（不带正文），无新帖不推送。

零第三方依赖：仅使用 Python 标准库。
环境变量：
  WEIBO_UID       可选，默认 3962719063
  WEIBO_COOKIE    可选，m.weibo.cn 需要登录态时填入（GitHub Secrets）
  FEISHU_WEBHOOK  可选，飞书自定义机器人 Webhook（GitHub Secrets）
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(HERE, "data", "posts.json")
UID = os.environ.get("WEIBO_UID", "3962719063")
TZ = timezone(timedelta(hours=8))  # Asia/Shanghai
MAX_PAGES = 30          # 单次运行最多翻页数（安全上限）
PAGE_DELAY = 2.0        # 翻页间隔秒数（防风控）

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"),
    "Referer": "https://m.weibo.cn/",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "MWeibo-Pwa": "1",
}

# ---------------------------------------------------------------- 游客身份（免登录）
_visitor = {"sub": "", "subp": ""}


def _visitor_cookies():
    """genvisitor2 换取游客 SUB/SUBP（进程内缓存，失效后重新生成）。"""
    if _visitor["sub"]:
        return "SUB=%s; SUBP=%s" % (_visitor["sub"], _visitor["subp"])
    url = ("https://visitor.passport.weibo.cn/visitor/genvisitor2"
           "?cb=visitor_callback&from=weibo.cn")
    req = urllib.request.Request(url, headers={"User-Agent": HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=20) as r:
        txt = r.read().decode("utf-8", "replace")
    m = re.search(r"\((\{.*\})\)", txt)
    data = (json.loads(m.group(1)).get("data") or {}) if m else {}
    if not data.get("sub"):
        raise RuntimeError("游客身份获取失败: %s" % txt[:150])
    _visitor["sub"] = data["sub"]
    _visitor["subp"] = data.get("subp", "")
    print("已获取微博游客身份")
    return "SUB=%s; SUBP=%s" % (_visitor["sub"], _visitor["subp"])


def _reset_visitor():
    _visitor["sub"] = _visitor["subp"] = ""


# ---------------------------------------------------------------- 底库读写
def load_posts():
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data


def save_posts(data):
    data["meta"]["count"] = len(data["posts"])
    if data["posts"]:
        data["meta"]["date_max"] = data["posts"][0]["date"]
        data["meta"]["date_min"] = data["posts"][-1]["date"]
    data["meta"]["updated"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DATA_FILE)


# ---------------------------------------------------------------- 抓取
def http_get_json(url, cookie=None):
    headers = dict(HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_timeline_page(page, user_cookie=None):
    """返回 (ok, mblogs)。无登录 Cookie 时自动用游客身份；
    遇 432/ok=-100 自动换游客身份重试（学习自 weibo-monitor 项目）。"""
    url = ("https://m.weibo.cn/api/container/getIndex?type=uid&value=%s"
           "&containerid=107603%s&page=%d" % (UID, UID, page))
    last_err = None
    for attempt in range(3):
        try:
            cookie = user_cookie or _visitor_cookies()
            data = http_get_json(url, cookie)
            if data.get("ok") == 1:
                cards = (data.get("data") or {}).get("cards") or []
                return True, [c.get("mblog") for c in cards
                              if isinstance(c, dict) and c.get("mblog")]
            last_err = "接口返回 ok=%s msg=%s" % (data.get("ok"), str(data)[:120])
            if data.get("ok") == -100 and not user_cookie:
                _reset_visitor()  # 游客会话失效，重新生成
            else:
                break
        except urllib.error.HTTPError as e:
            last_err = "HTTP %s" % e.code
            if e.code == 432 and not user_cookie:
                _reset_visitor()
            else:
                break
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            if attempt == 2:
                break
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("拉取微博失败: %s" % last_err)


def fetch_long_text(wid, cookie=None):
    """长文帖全文：列表页 text 被截到 ~140 字，调 extend 接口取完整正文
    （返回字段是 data.longTextContent，游客身份即可调用）。失败返回空串。"""
    url = "https://m.weibo.cn/statuses/extend?id=%s" % wid
    try:
        data = http_get_json(url, cookie or _visitor_cookies())
    except Exception as e:  # noqa: BLE001
        print("  长文 %s 全文获取失败：%s" % (wid, e))
        return ""
    if data.get("ok") != 1:
        return ""
    return clean_text((data.get("data") or {}).get("longTextContent") or "")


# ---------------------------------------------------------------- 解析
_HTML_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")
_TW_TIME = re.compile(
    r"^[A-Z][a-z]{2}\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+\+0800\s+(\d{4})$")
_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
           "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def clean_text(raw):
    """去掉 HTML 标签/实体，压平空白。"""
    text = _HTML_TAG.sub("", raw or "")
    text = html.unescape(text)
    return _SPACES.sub(" ", text).strip()


def parse_date(created_at, now=None):
    """把 m.weibo.cn 的 created_at 归一化为 YYYY-MM-DD（东八区）。"""
    now = now or datetime.now(TZ)
    s = (created_at or "").strip()
    m = _TW_TIME.match(s)  # Twitter 风格：Tue Oct 28 12:34:56 +0800 2026
    if m:
        month = _MONTHS.get(m.group(1))
        if not month:
            return None
        try:
            return datetime(int(m.group(6)), month, int(m.group(2)),
                            int(m.group(3)), int(m.group(4)), int(m.group(5)),
                            tzinfo=TZ).strftime("%Y-%m-%d")
        except ValueError:
            return None
    if ("刚刚" in s) or ("分钟前" in s) or s.startswith("今天"):
        return now.strftime("%Y-%m-%d")
    if "小时前" in s:
        return now.strftime("%Y-%m-%d")
    if s.startswith("昨天"):
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", s)  # MM-DD：当年；跨年边界按去年处理
    if m:
        try:
            d = datetime(now.year, int(m.group(1)), int(m.group(2)), tzinfo=TZ)
        except ValueError:
            return None
        if d > now + timedelta(days=1):
            d = d.replace(year=now.year - 1)
        return d.strftime("%Y-%m-%d")
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)  # YYYY-MM-DD
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)),
                            int(m.group(3)), tzinfo=TZ).strftime("%Y-%m-%d")
        except ValueError:
            return None
    return None


def norm(text):
    """去重键用的正文归一化：去全部空白。"""
    return re.sub(r"\s+", "", text or "")


def collect_new(existing_posts, cookie=None):
    """翻页抓取直到撞上已知旧帖；返回(新帖列表, 修复老帖数, 翻页数, 首页是否正常)。"""
    known_ids = {p.get("id") for p in existing_posts if p.get("id")}
    known_keys = {(p["date"], norm(p["text"])) for p in existing_posts}
    newest_known = max(p["date"] for p in existing_posts) if existing_posts else ""

    new, repaired = [], []
    seen_ids, seen_keys = set(), set()
    pages_used = 0
    for page in range(1, MAX_PAGES + 1):
        pages_used = page
        try:
            ok, mblogs = fetch_timeline_page(page, cookie)
        except RuntimeError as e:
            if page == 1:
                raise
            print("  第 %d 页失败，停止：%s" % (page, e))
            break
        if not ok or not mblogs:
            print("  第 %d 页无数据，停止" % page)
            if page == 1:
                return new, repaired, pages_used, False
            break

        hit_known = False
        for mb in mblogs:
            # 只收鹿鼎公本人的内容：原创帖，或他回复粉丝的帖子（正文以「回复@」开头）
            text = clean_text(mb.get("text") or "")
            # 列表页截断的帖子尾部带「...全文」链接字样，去掉
            text = re.sub(r"(?:\.{3}|…)\s*全文$", "", text).strip()
            if mb.get("retweeted_status") and not text.startswith("回复@"):
                continue
            wid = str(mb.get("id") or mb.get("mid") or "")
            date = parse_date(mb.get("created_at"))
            if not date or not text:
                continue
            # 长文帖：列表页 text 被截到 ~140 字，调 extend 接口取完整正文
            if mb.get("isLongText") and wid:
                full = fetch_long_text(wid, cookie)
                if len(full) > len(text):
                    text = full
            key = (date, norm(text))
            if wid and wid in known_ids:
                hit_known = True
                # 修复老帖：本次正文比库内长（历史截断）就更新；
                # 库内尾部带「...全文」残渣而本次已剥掉的，等长也更新
                for p in existing_posts:
                    if str(p.get("id")) == wid:
                        old = re.sub(r"(?:\.{3}|…)\s*全文$", "", p["text"]).strip()
                        if len(text) > len(old) or (text != old and len(text) == len(old)):
                            p["text"] = text
                            repaired.append(wid)
                        break
                continue
            if key in known_keys:
                hit_known = True
                continue
            if wid and wid in seen_ids:
                continue
            if key in seen_keys:
                continue
            seen_ids.add(wid)
            seen_keys.add(key)
            new.append({"id": wid or None, "date": date, "text": text})

        page_newest = max(
            (parse_date(mb.get("created_at")) or "" for mb in mblogs), default="")
        if hit_known:
            # 时间线严格新→旧：本页已撞上已知旧帖，后续页必然更旧，立即停止
            print("  第 %d 页撞上已知旧帖，停止" % page)
            break
        if newest_known and page_newest and page_newest < min(
                p["date"] for p in existing_posts):
            print("  第 %d 页已早于库内最早帖，停止" % page)
            break
        time.sleep(PAGE_DELAY)
    return new, repaired, pages_used, True


# ---------------------------------------------------------------- 飞书通知
def send_feishu(text):
    webhook = os.environ.get("FEISHU_WEBHOOK", "").strip()
    if not webhook:
        print("未配置 FEISHU_WEBHOOK，跳过通知")
        return
    payload = json.dumps({"msg_type": "text", "content": {"text": text}}).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", "replace")
        print("飞书通知已发送：%s" % body[:120])
    except Exception as e:  # noqa: BLE001
        print("飞书通知发送失败（不影响主流程）：%s" % e)


def notify_feishu(n, date_min, date_max):
    send_feishu("✅ 已成功将 %d 条超级鹿鼎公的微博原创帖加入帖子库（posts.json）"
                "（%s ~ %s）" % (n, date_min, date_max))


# ---------------------------------------------------------------- 主流程
def main():
    data = load_posts()
    posts = data.get("posts", [])
    print("底库：%d 条（%s ~ %s）" % (
        len(posts),
        data.get("meta", {}).get("date_min", "?"),
        data.get("meta", {}).get("date_max", "?")))

    cookie = os.environ.get("WEIBO_COOKIE", "").strip() or None
    try:
        new, repaired, pages, first_ok = collect_new(posts, cookie)
    except RuntimeError as e:
        print("抓取失败：%s" % e)
        send_feishu("⚠️ 鹿鼎公微博监控抓取失败（请求异常：%s），本次未入库，请检查 WEIBO_COOKIE。" % str(e)[:80])
        return 1
    print("抓取完成：翻 %d 页，新增原创帖 %d 条" % (pages, len(new)))

    if not first_ok:
        print("抓取失败：m.weibo.cn 首页无数据（432 反爬拦截或 Cookie 失效）。")
        send_feishu("⚠️ 鹿鼎公微博监控抓取失败：m.weibo.cn 返回无数据（反爬拦截或 WEIBO_COOKIE 失效），"
                    "本次未入库，请更新仓库 Secrets 中的 WEIBO_COOKIE。")
        return 1

    if repaired:
        print("修复历史截断帖 %d 条" % len(repaired))
    if not new:
        if repaired:
            save_posts(data)
            print("已修复历史截断帖，无新帖，不推送。")
        else:
            print("无新帖，不推送。")
        return 0

    new.sort(key=lambda p: p["date"], reverse=True)  # 新→旧
    posts[:0] = new                                   # 追加到头部
    save_posts(data)
    print("已入库：%d 条（现共 %d 条，%s ~ %s）" % (
        len(new), len(posts),
        data["meta"]["date_min"], data["meta"]["date_max"]))

    notify_feishu(len(new), new[-1]["date"], new[0]["date"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

