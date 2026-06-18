# 抖音 & 小红书 评论采集工具集

基于 **browser-harness (CDP)** 的评论采集脚本，连接你**真实的 Chrome**（已登录态），
像真人一样点击/滚动/展开，把帖子/视频下的全部评论（含多级回复）抓下来存成 CSV。

> 同时保留了一套早期基于 DrissionPage 的抖音脚本和可视化分析工具（见文末）。

---

## 一、两套采集器一览

| 平台 | 脚本 | 输入 | 输出 |
|------|------|------|------|
| 🎵 **抖音** | `抖音评论爬虫_browser-harness版.py` | 单个**视频链接** | `{标题}_{时间戳}_with_replies.csv` |
| 📕 **小红书** | `小红书评论爬虫_browser-harness版.py` | **搜索关键词** + 数量 N | `小红书_{关键词}_{时间戳}.csv` |

两者都：连真实 Chrome → 模拟真人滚动/点击加载评论 → 展开回复 → 区分一级/二级 → 存 UTF-8-BOM CSV（Excel 直开不乱码）。

---

## 二、抖音 vs 小红书：每个流程的差异 ⭐（核心）

两个平台反爬机制完全不同，下面逐流程对照。**这些差异是踩坑总结，改脚本前必看。**

### 总览表

| 流程 | 🎵 抖音 | 📕 小红书 |
|------|---------|-----------|
| **打开目标** | `goto_url(/video/{id})` **可直接跳转** | 直接 `goto /explore/{id}` → **404**（`error_code=300031`）；**必须从搜索结果点击卡片**，URL 才带 `xsec_token` |
| **评论容器** | `.route-scroll-container`（含 `[data-e2e="comment-item"]`） | `.note-scroller`（含 `.comment-item`） |
| **加载机制** | 只认**真实按键** `press_key("PageDown")`；程序改 `scrollTop`/合成 wheel **不触发**懒加载；每轮需重新锁定+聚焦容器 | IntersectionObserver，**直接设 `scrollTop = scrollHeight` 就能加载**更多 |
| **停止条件** | 页面出现 **「暂时没有更多评论」** 文字标记（兜底：连续 5 轮无增长） | 评论区底部 **`- THE END -`** 结束标记（`.end-container`，常驻 DOM，滚到底可见）；脚本里因标记常驻、实际靠**连续 5 轮数量不变**判定（到底即 stall、同时能看到 THE END）；用「共 N 条评论」header 校验总量 |
| **展开回复** | 点「展开 N 条回复 / 展开更多」；点过的打 `data-bh-tried` 标记跳过 | 点「展开 N 条回复」**先加载约 5 条**，再反复点「展开更多回复」**每批 +5** 直到该按钮消失拉满；「更多/展开」展开被截断的正文（排除左侧 AI 侧栏的「更多」） |
| **展开点击方式** | hover-aware：`mouseMoved → press → release`（`click_at_xy` 不发 hover 会点空） | 同上（`show-more` 311×32 按钮同样要 hover 才响应） |
| **层级判定** | 祖先链有无 `[data-e2e="comment-item"]`（回复**嵌套在** comment-item 里） | 祖先链有无 **`.reply-container`**（回复在 reply-container 里，是 comment-item 的**兄弟**，不嵌套） |
| **风控节奏** | `jitter`：PageDown 0.35–0.6s / 每轮 1.2–1.8s / 展开 1.5–2.2s | `jitter`：加载 1.0–1.4s / 展开 1.0–1.5s |
| **会话稳定性** | 一次大 PROGRAM 跑完整流程 | 长会话 CDP 易掉线 → **每篇笔记单独调用一次** browser-harness（短任务），`safe_js/safe_cdp` 异常自动 `ensure_daemon` 重连 |

### 逐流程详解

**① 打开目标 —— 小红书最大的反爬差异**
- 抖音：拿到视频 id（优先 URL 里的 `modal_id`，其次 `/video/{id}`），直接 `goto_url` 打开即可。
- 小红书：笔记**只能从搜索结果点击卡片进入**。直接访问 `https://www.xiaohongshu.com/explore/{id}` 会被风控挡回 404（「当前笔记暂时无法浏览」）。原因是授权 token（`xsec_token`）只在点击时拼到 URL 上。
  → 所以脚本流程是：`window.location.href = 搜索页` → 找到 `a[href*="/explore/{id}"]` 卡片 → **坐标点击卡片中心** → 笔记以 `.note-detail-mask` 浮层打开。

**② 加载评论 —— 滚动机制完全相反**
- 抖音：评论懒加载**只认真实输入事件**。程序里改 `scrollTop`、合成 `wheel` 都不动；必须 `press_key("PageDown")` 真按键滚动，而且每轮要重新「锁定容器 + 聚焦」防失焦卡住。
- 小红书：评论容器用 IntersectionObserver，**对程序设 `scrollTop` 响应良好**，每轮 `scrollTop = scrollHeight` 即可不断拉出新评论。

**③ 停止条件 —— 小红书的「没有更多」是 `- THE END -`**
- 抖音：滚到底会有「暂时没有更多评论」文字，直接当停止标记（抖音即便显示总数，也常只放出 ~100 条就给此标记）。
- 小红书：评论区底部有 **`- THE END -`** 结束标记（在 `.comments-container` 末尾的 `.end-container` 里），它就是小红书的「没有更多评论」。⚠️ 注意它和**笔记正文末尾**不一样——笔记正文（`.desc`）结尾是 `#话题标签`，而 `- THE END -` 是评论区的结束符，别混。
  - `.end-container` 是**常驻 DOM**（不管滚到哪都在 DOM 里），所以脚本不能只判「在不在 DOM」，而用**滚到底 + 连续 5 轮评论数量不变**来确认到底（到底时 THE END 会进入视图）。两者等价：到底 = stall = 能看到 THE END。页面顶部「共 N 条评论」可校验是否抓全。

**④ 展开回复 —— 小红书分批，且层级结构不同**
- 抖音：点「展开 N 条回复 / 展开更多」一次性展开，用 `data-bh-tried` 标记避免重复点击死按钮。
- 小红书：点「展开 N 条回复」**只先加载约 5 条**，需继续点「展开更多回复」逐批拉满（点过的按钮会变成「收起」或消失，天然防重复）。
- 层级判定差异（**最容易写错的地方**）：
  - 抖音回复**嵌套在**评论项里 → 看祖先有没有 `comment-item`。
  - 小红书回复在 `.reply-container` 里，它是 `.comment-item` 的**兄弟**（都在 `.parent-comment` 下）→ 必须看祖先有没有 **`.reply-container`**，看 `comment-item` 会把所有回复都判成一级。

**⑤ 会话稳定性 —— 小红书要拆短任务**
- browser-harness 的 daemon 在**单次执行超过约 2 分钟**时容易 CDP 掉线。
- 抖音单视频通常够快，一次大 PROGRAM 跑完。
- 小红书要抓多篇笔记、单篇展开可能很久 → 脚本设计成**每篇笔记单独调一次** browser-harness（短任务），且 `safe_js/safe_cdp` 在异常时自动 `ensure_daemon(); ensure_real_tab()` 重连。Chrome 标签页和 DOM 在调用之间持久存在，所以「每篇一次」是干净的隔离方式。

---

## 三、快速开始（browser-harness 版）

### 环境前提（两个脚本通用）

1. **安装 browser-harness**
   ```bash
   uv tool install -e ~/browser-harness
   ```
2. **Chrome 开启远程调试**：访问 `chrome://inspect/#remote-debugging`，勾选「Allow remote debugging for this browser instance」，弹窗点 Allow。
3. **目标网站已登录**：在同一个 Chrome 里登录抖音 / 小红书（评论默认登录可见）。
4. **依赖**：browser-harness 版**不需要** `requirements.txt`（纯 CDP）；旧版 DrissionPage 脚本才需要 `pip install -r requirements.txt`。

### 采集抖音评论

```bash
# 给一个视频链接（支持 /video/{id} 或搜索页带 modal_id 的长链接）
python3 抖音评论爬虫_browser-harness版.py "https://www.douyin.com/video/7641230262990556479"
python3 抖音评论爬虫_browser-harness版.py    # 不带参数则运行时输入
```

### 采集小红书评论

```bash
# 关键词 + 数量（默认 ai产品面经 / 前 5 个）
python3 小红书评论爬虫_browser-harness版.py ai产品面经 5
python3 小红书评论爬虫_browser-harness版.py "求职面经" 10
python3 小红书评论爬虫_browser-harness版.py            # 默认 ai产品面经 / 5
```

脚本会自动：搜索关键词 → 收集前 N 个笔记卡片 → 逐篇点击 → 滚动加载 → 展开回复 → 区分层级 → 合并成一个 CSV。

---

## 四、输出说明

保存目录：`crawled_comments/`

### CSV 字段

**抖音**（`{标题}_{时间戳}_with_replies.csv`）：
`评论ID, 层级, 昵称, 地区, 时间, 评论内容, 点赞数, 回复数, 用户链接`

**小红书**（`小红书_{关键词}_{时间戳}.csv`）：
`笔记ID, 笔记标题, 笔记链接, 层级, 序号, 昵称, 地区, 时间, 评论内容, 点赞数, 回复数, 用户链接`

- **层级**：`1` = 顶层评论，`2` = 回复。
- 小红书「时间/地区」从 `.date`（合并了时间+IP，如 `04-14北京`）拆出来；点赞/回复占位符 `赞`/`回复` 自动转 `0`。

---

## 五、旧版：DrissionPage 抖音脚本 + 可视化分析

除 browser-harness 版外，仓库还保留了一套基于 **DrissionPage** 的抖音采集器和分析工具（Windows 优先，本机 macOS 跑需注意下方坑）。

### 采集（3 个爬虫）

| 文件名 | 特点 | 推荐场景 |
|-------|------|---------|
| `抖音评论爬虫_DOM提取版.py` | DOM 解析，速度快 | 日常使用（最推荐） |
| `抖音评论爬虫_API监听版.py` | 监听 API，字段最全（sec_id/头像/时间戳等） | 需要完整用户信息，建议登录 |
| `抖音评论爬虫_DOM提取版_含回复.py` | 含多级回复 | 需要回复关系 |

```bash
pip install -r requirements.txt
python 抖音评论爬虫_DOM提取版.py          # 运行后输入视频 URL（回车=测试链接）
```

### 数据分析

```bash
python 评论可视化分析工具.py    # 选 CSV（回车=最新），报告输出到 analysis_results/
```

生成 `{标题}_完整分析报告.html` + `{标题}_词云图.png`，含：情感分析、用户活跃时段、语言风格、互动频率、地理热力地图、影响力排行、TF-IDF 关键词、24 小时趋势、地区分布。

### 调参

- **滚动次数**（影响评论量）：脚本里 `range(200)` → 100 次≈400-500 条，200 次≈800-1000 条。
- **滚动速度**：`time.sleep(0.2)` 快 / `0.3` 适中（推荐）/ `0.5` 稳定。
- **切换 Edge**：见旧版 README 历史（修改 `ChromiumOptions().set_browser_path(...)`）。

### ⚠️ macOS 运行旧版的已知坑

- `_DOM提取版.py` import 了 Windows 专属的 `msvcrt`，macOS/Linux 会崩（另两个用 `input()` 没问题）。
- 词云硬编码 `C:/Windows/Fonts/simhei.ttf`、matplotlib 强制 `SimHei`，本机字体是 `STHeiti`。
- 磁盘上 `评论可视化分析工具py` 缺 `.py` 后缀，按文档的 `.py` 名运行会找不到，需用真实文件名或重命名。

---

## 六、常见问题

**Q: 小红书笔记打开是 404 /「当前笔记暂时无法浏览」？**
A: 这是风控。**不能直接 goto 笔记 URL**，脚本已按「点击卡片」处理；若仍 404，确认是从搜索结果页点击、且 Chrome 已登录。

**Q: 抖音评论数量很少 / 滚不动？**
A: 抖音懒加载只认真实按键，脚本用 `press_key("PageDown")`；若仍少，增大每轮按键次数或等待时间。

**Q: 小红书只抓到 1 条评论？**
A: 那篇笔记本身只有 1 条（脚本会打印「共 N 条评论」校验）。低评论笔记会提前结束，不空转。

**Q: 长时间运行中途 CDP 报错 / 掉线？**
A: browser-harness 单次任务别超过约 2 分钟。小红书脚本已按「每篇笔记一次调用」拆分；抖音单视频若卡住，重跑即可（`browser-harness --reload` 可重启 daemon）。

**Q: CSV 在 Excel 乱码？**
A: 已用 `utf-8-sig`（带 BOM）；仍乱码则用 Excel「数据 → 从文本导入」选 UTF-8。

**Q: 两个平台能同时抓吗？**
A: 各自独立脚本，建议分开跑、分开 Chrome 标签页，避免互相干扰。

---

## 七、注意事项

1. **仅供学习研究使用**，不要过于频繁采集，控制好节奏（脚本已内置 `jitter` 随机延迟贴近真人）。
2. 采集到的数据请妥善保管、合规使用。
3. 平台前端结构可能更新导致选择器失效；核心选择器（抖音 `[data-e2e="comment-item"]`、小红书 `.comment-item`/`.note-scroller`/`.reply-container`）相对稳定，但 `.show-more` 等类名变动时需相应调整。
4. 商业使用请标注作者；有 bug 和新需求欢迎提交 PR。

---

**最后更新**：2026-06-18
