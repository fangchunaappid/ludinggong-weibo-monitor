# ludinggong-weibo-monitor — 超级鹿鼎公微博原创帖增量监控

只针对**微博**（不含雪球），增量抓取「超级鹿鼎公」(UID `3962719063`) 的**原创帖**，
追加进 `data/posts.json` 底库（新→旧），并在有新帖时向飞书推送一条**仅成功入库**的通知。

- 主程序：[monitor.py](monitor.py)，**零第三方依赖**（纯标准库）
- 触发：**cron-job.org 外部触发（推荐，准时）** + GitHub Actions schedule 兜底（可能有延迟）
- 去重：优先按**微博 id**，退化为 **(日期, 正文)** 键，历史绝不重复
- 只收本人内容：原创帖 + 「回复@」粉丝帖，纯转发跳过
- 飞书通知格式（不含正文、无新帖不推）：
  > ✅ 已成功将 N 条超级鹿鼎公的微博原创帖加入帖子库（posts.json）（日期区间）

## 数据格式（data/posts.json）

```json
{
  "meta": { "source": "...", "uid": "3962719063", "count": 4063, ... },
  "posts": [
    { "id": null, "date": "2026-10-28", "text": "…" },
    { "id": "5178…", "date": "2026-10-27", "text": "…" }
  ]
}
```

- `id: null` = 站点爬取的历史底库帖（无微博 id），按 (日期, 正文) 参与去重
- 增量帖由 monitor.py 写入真实微博 id

## 本地运行

```bash
python3 monitor.py                       # 默认 UID
WEIBO_COOKIE='SUB=…' python3 monitor.py  # 可选：需要登录态时
```

## 手机上查看帖子

两种方式：

**方式一：离线全量版（推荐先看这个）**
独立文件 `鹿鼎公帖子_手机版.html`（不在本仓库，7704 条雪球+微博全量数据内嵌），
发到手机（微信/QQ 文件传输助手、云盘均可）后用浏览器打开即可，无需联网：

- 手机优先排版：大字号、日期分组、深色模式（跟随系统+可切换）
- 筛选：平台（全部/雪球/微博）、年份、雪球分类、关键词搜索（命中高亮）
- 每条可一键复制；滚动自动加载，7000+ 条不卡

**方式二：GitHub Pages 在线版（随监控自动更新）**
本仓库自带 [index.html](index.html)，读取 `data/posts.json` 渲染微博帖——
monitor 每次增量入库后页面自动更新，手机收藏一个网址即可：

1. 推送本仓库后：Settings → Pages → Build and deployment →
   Source 选 **Deploy from a branch** → Branch 选 **main / (root)** → Save
2. 稍等 1-2 分钟，访问 `https://<你的用户名>.github.io/ludinggong-weibo-monitor/`
3. 以后无需任何操作：monitor 入库 → Actions 提交 → Pages 自动重新发布

## 三步推送到 GitHub

1. 在 GitHub 上新建**空仓库**（不要初始化 README），名称如 `ludinggong-weibo-monitor`
2. 本机执行：

   ```bash
   cd ludinggong-weibo-monitor
   git remote add origin https://github.com/<你的用户名>/ludinggong-weibo-monitor.git
   git push -u origin main
   ```

3. 仓库 → Settings → Secrets and variables → Actions → New repository secret：

   | Secret 名称      | 必填 | 说明 |
   |------------------|------|------|
   | `FEISHU_WEBHOOK` | 可选 | 飞书自定义机器人 Webhook，不配则只入库不通知 |
   | `WEIBO_COOKIE`   | 可选 | m.weibo.cn 登录态（SUB=…），被风控时再配 |

   最后到 Actions 页启用 workflow（若提示），并用 `Run workflow` 手动验证一次。

## 外部定时触发（推荐：cron-job.org，避开 schedule 延迟）

GitHub Actions 的 schedule 在高峰期经常延迟几分钟到几小时。用免费外部 cron 服务
[cron-job.org](https://cron-job.org) 调 GitHub API 触发 `repository_dispatch`，到点即跑：

1. 准备一个 PAT（classic 勾 `repo`；fine-grained 勾 Contents 读写）——可与推送用同一个
2. 在 cron-job.org 注册后 **Create cronjob**：
   - **URL**：`https://api.github.com/repos/<你的用户名>/ludinggong-weibo-monitor/dispatches`
   - **Method**：`POST`
   - **Headers** 添加两条：
     - `Authorization`: `Bearer <你的PAT>`
     - `Accept`: `application/vnd.github+json`
   - **Body**（JSON）：`{"event_type":"monitor"}`
   - **Schedule**：每 30 分钟（`*/30`）
3. 保存并 Run once 验证：GitHub 仓库 Actions 页应出现一次 `ludinggong-weibo-monitor` 运行

> 说明：API 返回 `204` 即触发成功；workflow 里的 `schedule` 保留为兜底，两者都触发也只会
> 各自跑一次，去重逻辑保证不会重复入库。
