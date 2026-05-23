# deepseek_news

**上游**由 `deepseek_news_filter.py` 统一编排：**RSS**（`feeds.txt`）或 **Tavily / SerpAPI 搜索** 获取候选 → DeepSeek 按「中国医药 AI 数字化」筛选分层；可选 **`NEWS_INTL_SEARCH_QUERY`** 再跑一轮海外搜索，经单独提示词只保留**美国/欧洲/亚太**语境下的**高优先级**条目。**下游**由 `digest_email.py` 排版为 HTML（国内高中低 + 海外要闻）并 **每日发送一封邮件**（`run_daily_digest.py` 只做编排）。

## 准备

1. Python 3.10+（建议使用 `python3`）。
2. 建议在本项目目录创建虚拟环境并安装依赖：

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate   # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. 配置密钥与邮箱（复制 `.env.example` 为 `.env` 并填写）：
   - `DEEPSEEK_API_KEY`
   - `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `EMAIL_FROM` / `EMAIL_TO`
   - Gmail 示例：`SMTP_HOST=smtp.gmail.com`，`SMTP_PORT=587`，`SMTP_ENCRYPTION=starttls`，发件账号需开启「应用专用密码」。

4. **候选来源二选一**  
   - **RSS**：复制 `feeds.example.txt` 为 `feeds.txt`，每行一个订阅地址（`#` 为注释）。  
   - **搜索 API**：在项目根目录 **`.env`** 里填写 **`SERPAPI_API_KEY`**（见下方 SerpAPI 小节）。使用 **`--search-query "关键词"`** 或环境变量 **`NEWS_SEARCH_QUERY`** 时将**不再读取** `feeds.txt`。

## 搜索 API 模式（SerpAPI 免费档 / Tavily）

### SerpAPI（你当前用的免费 plan，约每月 250 次 search）

1. 打开项目根目录下的 **`.env`**（与 `deepseek_news_filter.py` 同级；没有则从 `.env.example` 复制一份改名）。  
2. 写入你的 Key（二选一变量名即可）：

   ```env
   SERPAPI_API_KEY=这里粘贴SerpAPI控制台里的API_KEY
   ```

   可选（推荐写上，避免歧义）：

   ```env
   NEWS_SEARCH_PROVIDER=serpapi
   ```

   若**只**配置了 `SERPAPI_API_KEY`、**没有**配置 `TAVILY_API_KEY`，即使不写 `NEWS_SEARCH_PROVIDER`，程序也会**自动选用 SerpAPI**。

3. **时间与召回（默认已优化）**  
   - **48 小时内**：环境变量 **`NEWS_MAX_AGE_HOURS=48`**（默认）对 SerpAPI / RSS 结果按 `pub_date` 解析后过滤；无可靠日期的条目默认丢弃（`NEWS_DROP_UNDATED=1`），以免混进旧闻。  
   - SerpAPI 会在搜索词后追加 **`after:YYYY-MM-DD`**（按 `NEWS_MAX_AGE_HOURS` 回推，可用 `NEWS_QUERY_APPEND_WHEN_2D=0` 关闭）。**不要默认开启** `SERPAPI_SORT_DATE=1`（`so=1` 在部分纯 `q` 查询下会触发 SerpAPI **HTTP 400**）。精确 48 小时仍靠后端按 `pub_date` 过滤。  
   - 可用 **`NEWS_SEARCH_QUERY_SUFFIX`** 追加 `OR 药监局 OR …` 一类关键词，扩大政策类召回，减轻「high 全空」（还与当日是否真有政策稿有关）。

4. 每次完整跑一遍「搜索 → DeepSeek →（发信）」对 SerpAPI 通常是 **1 次** `google_news` 请求，计 **1 次 search**（以 [SerpAPI 控制台](https://serpapi.com/dashboard) 用量为准）。`--dry-run` 也会发起搜索，同样会计数。  
5. 运行示例：

```bash
python3 run_daily_digest.py --search-query "中国 医药 人工智能 数字化" --dry-run
```

可选环境变量：`NEWS_SEARCH_MAX_RESULTS`（默认 **15**）、`SERPAPI_HL`、`SERPAPI_GL`（见 `.env.example`）。

### 海外分区（可选）

在 **`.env`** 中增加 **`NEWS_INTL_SEARCH_QUERY`**（英文关键词通常效果更好），例如含 `pharmaceutical`、`FDA`、`EMA`、`digital`、`AI` 等；程序会**额外发起 1 次**搜索（SerpAPI/Tavily 与与国内同源），再用**单独**的 DeepSeek 提示词筛选，邮件末尾追加 **「海外要闻 · 美国 / 欧洲 / 亚太（仅高优先级）」**，每条标注区域（美国/欧洲/亚太）。国际搜索默认使用 **`NEWS_INTL_SERPAPI_HL=en`**、**`NEWS_INTL_SERPAPI_GL=us`**，避免与国内 `gl=cn` 混用；详见 `.env.example` 中国际相关注释。命令行可覆盖：`python3 run_daily_digest.py --intl-search-query "..."`。

### Tavily

1. 在 [Tavily](https://tavily.com/) 申请 Key，在 **`.env`** 中设置 `TAVILY_API_KEY=...`。  
2. 设置 `NEWS_SEARCH_PROVIDER=tavily`（若与 SerpAPI Key 同时存在，请显式指定 provider）。

## 手动跑一天

```bash
python3 run_daily_digest.py
```

若使用虚拟环境：

```bash
.venv/bin/python run_daily_digest.py
```

仅测试抓取与模型、不发邮件：

```bash
python3 run_daily_digest.py --dry-run
```

## 定时发邮件（每天约 11:00）

**物理限制**：只要程序跑在你自己的笔记本上，**关机或长时间睡眠时任务不会执行**；用户级 `launchd` 还通常要求**至少登录过该用户会话**。若要**笔记本合盖/关机仍能每天收到邮件**，请用下面 **GitHub Actions（或其它云主机 cron）**，在云端执行同一套 `run_daily_digest.py`。

### 推荐：GitHub Actions（无需本机开机、无需登录）

1. 把本仓库推送到 **GitHub**（私有仓库即可）。
2. 打开 **Settings → Secrets and variables → Actions → New repository secret**  
   - **Name**：`DIGEST_DOTENV`  
   - **Secret**：把你本机项目根目录 **`.env` 文件全文**复制进去（多行粘贴；与本地一致即可，含 `NEWS_SEARCH_QUERY`、SMTP、SerpAPI 等）。GitHub 会加密保存。
3. 确认 **Actions** 未被禁用；工作流见 **`.github/workflows/daily-digest.yml`**。  
   - 当前为 **`America/New_York` 每天 11:05** = **美国东部当地上午 11:05**（含夏令时处理）。邮件标题里的日期也按 `TZ` 与之一致。  
   - **若你改回「北京时间上午 11 点」**：把同一文件里的 `timezone:` 与 `TZ:` 都改为 **`Asia/Shanghai`**。  
   - 你曾遇到「美东凌晨 2 点、中国下午 2 点」收到：约等于 **UTC 6 点** 触发，与「北京上午 11 点」（约 UTC 3 点）不一致，多半是 GitHub 上仍是旧 workflow 或未 push；以 Actions 里该次运行的 **Start time** 为准核对。
4. 在 **Actions** 页选中 **Daily digest email** → **Run workflow**：可勾选 **dry_run** 试跑（不发邮件）；不勾选则真实发信。定时任务始终会发信（不走 dry_run）。

**若昨天没收到邮件，请按顺序自查**

1. **Actions 里有没有昨天的运行记录**  
   打开 **Actions → Daily digest email**，看对应日期是否有 **绿色成功** 或 **红色失败**。若**根本没有记录**：常见原因是仓库是 **fork**（定时默认关）、**workflow 文件不在默认分支**，或 **Actions 被仓库设置关掉**。若**有记录但失败**：点开看最后一步报错（SMTP、SerpAPI、缺密钥等）。
2. **GitHub 官方说明**：定时任务在 Actions 高峰时**可能被推迟**；极端情况下整点队列过重**有丢跑可能**（见 [schedule 文档](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#schedule)）。已把工作流改为 **带 timezone 的 11:05**，降低整点拥堵影响。
3. **公开仓库 60 天无提交**：GitHub 会**自动停用**定时 workflow，需到 Actions 里 **Enable** 或随便推一个 commit。
4. **本机 launchd 与云端二选一**：若两边都开着，可能发两封或你只注意到其中一封；若只用云端，请 `./scripts/uninstall_macos_launchd.sh`。
5. **垃圾箱 / 推广邮件**里搜 `医药 AI 数字化` 或发件邮箱。

**「本地每天 11:00」与 UTC cron 对照（仅在不使用 `timezone` 字段时参考；现已用 `Asia/Shanghai` 可忽略本表）**

| 常居地（示例） | 与 UTC 差 | 若要当地 11:00，cron（分 时 * * *） |
|----------------|-----------|--------------------------------------|
| 中国 / 香港 / 新加坡 | +8 | `0 3 * * *`（旧写法，等价于上海 11:00） |
| 日本 | +9 | `0 2 * * *` |
| 美东（纽约等，标准时） | −5 | `0 16 * * *` |
| 美西（洛杉矶等，标准时） | −8 | `0 19 * * *` |
| 英国（标准时） | +0 | `0 11 * * *` |

有夏令时的地区每年要改两次 cron，或改用 workflow 里的 **`timezone:`**（见 [GitHub 文档示例](https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#schedule)）。也可用 [crontab.guru](https://crontab.guru) 核对。

5. **若已装过本机 launchd**：请执行 `./scripts/uninstall_macos_launchd.sh`，否则云端与本机各跑一封，会**重复发两封**。

说明：免费额度内一般足够；每次会安装依赖并访问 SerpAPI / DeepSeek / SMTP，与本地一次完整运行相当。

### 本机 macOS（仅适合「电脑每天 11 点左右开机且已登录」）

1. 确认项目路径为 **`/Users/cccjoyyy/deepseek_news`**（若你移动了仓库，需用编辑器改 `launchd/com.deepseek-news-digest.plist` 里两处绝对路径，与 `scripts/install_macos_launchd.sh` 无关，脚本会自动定位项目根目录）。
2. 在终端执行（只需一次）：

   ```bash
   cd /Users/cccjoyyy/deepseek_news
   chmod +x scripts/install_macos_launchd.sh scripts/run_daily_digest_scheduled.sh
   ./scripts/install_macos_launchd.sh
   ```

3. **立即试跑一封**（不必等到 11 点）：

   ```bash
   launchctl start com.deepseek-news-digest
   tail -f logs/scheduled-digest.log
   ```

4. **取消本机定时**：

   ```bash
   ./scripts/uninstall_macos_launchd.sh
   ```

   或手动：`launchctl unload ~/Library/LaunchAgents/com.deepseek-news-digest.plist` 后删除该 plist。

若要改成 **10:30** 等其它时间：编辑 `launchd/com.deepseek-news-digest.plist` 里 `StartCalendarInterval` 的 `Hour` / `Minute`，再执行一次 `./scripts/install_macos_launchd.sh`。

### 通用：crontab（任意 Unix，系统本地时区）

```cron
0 11 * * * /Users/cccjoyyy/deepseek_news/scripts/run_daily_digest_scheduled.sh
```

首次请 `chmod +x scripts/run_daily_digest_scheduled.sh`。若未使用虚拟环境，脚本会退回 `python3`，请保证 PATH 里能找到依赖。同样依赖机器开机。

## 故障排查

- **`缺少 SMTP_HOST` 但 `.env` 里已填写**：请确认 `.env` 与 `deepseek_news_filter.py` 在同一项目根目录；代码会从该目录加载 `.env`（不依赖你在终端里的 `cd`）。若仍失败，在终端执行 `unset SMTP_HOST`（以及其它误导出的空 `SMTP_*`）后再运行。
- **邮件里条目很少或几乎为空**：筛选主题是「中国医药 AI 数字化」；若 `feeds.txt` 里全是英文泛健康 RSS（如 BBC Health），模型会大量丢弃，属正常。请换成国内药监局、医药科技、医疗信息化等相关 RSS。
- **邮件里链接为空**：已在新版本里用原始 RSS 按标题回填 `url`；若模型改写了标题导致对不上，请升级后重跑；仍建议换更贴近主题的源。
- **使用 `--search-query` 时报缺少 API Key**：在**项目根目录的 `.env`** 中填写 `SERPAPI_API_KEY`（或 `SERPAPI_KEY`）或 `TAVILY_API_KEY`，保存后重跑；不要用错工作目录导致读到空的 `.env`。

## 其它脚本

- `run_deepseek_demo.py`：不经过 RSS，用内置或 JSON 文件测新闻筛选。
- `run_deepseek_roundtrip.py`：每次运行自动（或可指定）向 DeepSeek 发送 **system + user** 一段文字，并把返回体 **按 JSON 解析** 打印；加 `--raw` 则只打印模型原文。代码中也可直接调用 `deepseek_chat_text` / `deepseek_chat_json`（见 `deepseek_news_filter.py`）。
