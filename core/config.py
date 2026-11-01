"""应用配置：所有配置项都来自环境变量或 `.env`，这里只声明默认值与形状。

导入期不做强制校验 —— 缺 key 也能启动、能跑单测，只有真发起调用时才报错并给出提示。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # 绝对路径：pydantic-settings 对相对路径是按**进程 CWD** 解析的，
        # 从别的目录启动（任务计划、IDE 的自定义 workdir）就会读不到 .env，
        # 症状是"服务起得来但所有联网工具都报没配 key"（评价11 P4-3）。
        env_file=str(Path(__file__).resolve().parent.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- 人设 ----
    #: 桌宠怎么称呼你（第 143 步 / 人设.md 的 {{称呼}}）。
    #: 默认「哥哥」；想改成「姐姐」或昵称就改 .env 里的 PET_ADDRESS。
    #: 为什么做成配置而不是首次启动让用户选：省掉一个流程，
    #: 而改一行 .env 的代价远小于为此加一个首启对话框。
    pet_address: str = "哥哥"

    # ---- 预算 ----
    #: 聊天一轮的服务端总预算（秒）。评价11 P3-1：食谱/出行/识图都有，
    #: 只有聊天没有 —— 而它的最坏情况是"单次 60s 超时 × 最多 4 步"≈4 分钟。
    #: 桌宠聊天只有 1-2 步，90 秒足够，且能挡住"网关每步都磨满超时"。
    chat_deadline_seconds: float = 90.0

    # ---- 出行：外部 MCP 服务器 ----
    #: 12306 车次查询的 MCP 服务器地址（第 215 步）。**留空 = 不接车次**。
    #: 两种传输按 URL 后缀自动选：`/sse` 走 SSE（ModelScope 广场给的地址长这样），
    #: 其余走 streamable HTTP（自托管 `mcp-server-12306` 之类常见 `/mcp`）。
    #: 为什么做成"可选、默认关"：它是唯一一个会把我们连到**别人家服务**的工具，
    #: 连不上时必须只是"这次没拿到车次"，不能让出行窗口整体失败（失败语义见
    #: `core/trip_planner.py` 模块 docstring）。
    mcp_12306_url: str = ""
    #: 单次 MCP 调用的超时（秒）。车次查询本身 5~15 秒，给 20 秒；
    #: 再长就不如直接告诉用户"这次没取到"。
    mcp_12306_timeout_seconds: float = 20.0
    #: 12306 那台服务器的令牌（可选）。ModelScope 广场上的托管服务都要
    #: `Authorization` 头；自托管的一般不要，所以默认空。
    mcp_12306_token: str = ""
    #: 12306 那台服务器上**额外**要暴露的工具（逗号分隔，第 217 步）。
    #: 默认只要这两件：票价（`query-ticket-price`）与当前时间/相对日期
    #: （`get-current-time`，模型据此把「下周六」算成具体日期）。
    #: 写 `all` = 暴露它声明的全部工具，写 `none` = 只要手写的那一个车次工具。
    #: 为什么不默认全开：多给一个工具就多一次选错的机会，而「车次号转官方编号」
    #: 这种中间步骤工具对回答用户的问题没有直接帮助。
    mcp_12306_extra_tools: str = "query-ticket-price,get-current-time"
    #: 其余 MCP 服务器（第 217 步），JSON 数组，一项一台：
    #: `[{"name":"bazi","url":"https://…/sse","token":"…","timeout_seconds":20}]`
    #: 为什么用一段 JSON 而不是每台一组 `MCP_XXX_URL` 设置项：接一台新服务器
    #: 不该再改一次代码和 README 的配置表；名字、地址、令牌都在同一个地方看得到。
    #: `name` 会变成工具名的前缀（`bazi_…`），所以只允许字母/数字/下划线/连字符。
    mcp_servers: str = ""
    #: 哪台 MCP 服务器负责黄历（第 221 步）：填 `MCP_SERVERS` 里那台的 `name`。
    #: 命中的那台，它的 `getChineseCalendar`（一次只回一天的 16 个字段）会被换成
    #: `search_almanac`（按 start_date/end_date 区间一次查完，只留宜/忌/冲煞/节气）。
    #: 为什么默认空而不是猜一个名字：没配的人不该看到"黄历不可用"的启动警告。
    mcp_almanac_server: str = ""
    #: 哪台 MCP 服务器负责联网搜索（第 222 步）：填 `MCP_SERVERS` 里那台的 `name`。
    #: 命中的那台，它的 `bing_search`（默认 10 条、每条 5 个字段，实测一次 3192 字）
    #: 会被换成 `search_web`（只回标题/摘要/链接，默认 5 条）；同台的 `crawl_webpage`
    #: 直接摘掉 —— 实测抓百度百科 `HTTP 403`，而能抓到的整页一次就吃掉大半轮预算。
    #: 默认空：没配的人不该在启动日志里看到"搜索不可用"。
    mcp_web_search_server: str = ""
    #: 远端工具清单的缓存文件（第 217 步）。启动时是同步的，而"问远端有哪些工具"
    #: 必须发网络请求 —— 所以拆成两步：`scripts/mcp_remote_probe.py` 去问一次并
    #: 写进这个文件，启动只读文件。**改地址就要重跑 probe**，地址变了会自动失效。
    mcp_tools_cache: str = "data/mcp_tools.json"

    # ---- 安全 ----
    #: Host 头白名单（逗号分隔；第 192 步 / 评价15 T0-1）。
    #: 只认本机回环，挡的是 DNS rebinding：恶意网页把自己的域名解析到 127.0.0.1 之后，
    #: 浏览器认为「我在跟自己的域名说话」，于是不发预检、也不看 CORS，
    #: Host 头却带着攻击者的域名打进本地服务 —— 这时唯一还能分辨真假的只剩 Host。
    #: `testserver` 是 FastAPI 的 TestClient 用的名字，不是可解析域名；
    #: 而能自己造 Host 头的客户端本来就有权直连端口，留着它不构成新洞。
    #: 将来要把服务绑到 0.0.0.0（手机/平板访问），在这里补上那个 IP 或域名。
    allowed_hosts: str = "127.0.0.1,localhost,[::1],testserver"

    # ---- LLM ----
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    #: `deepseek-chat` 是官方长期保留的**别名**，别再"顺手升"成 deepseek-flash（第 207 步实测）：
    #: 2026-10-28 真调用两者都 HTTP 200，但发 `deepseek-chat` 时**响应的 model 字段回的是
    #: `deepseek-flash`** —— 服务端已经把我们转到新模型上了，本地改名只是改自己的记录。
    #: 唯一会变的是 `core/llm_usage` 里记下的模型名，改了会让历史数据不可比。
    deepseek_model: str = "deepseek-chat"
    #: 智谱 GLM（第 98 步）：v4 接口 OpenAI 兼容，实测工具调用与流式都通。
    #: 默认 glm-4-flash：免费档，够桌宠用。换模型只改 .env。
    glm_api_key: str = ""
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    glm_model: str = "glm-4-flash"
    #: 嵌入模型（第 182 步，评价14 的向量召回）：`/embeddings` 端点，
    #: 用来给菜谱和查询算向量。没有 GLM key 时整条向量路自动关闭（不是报错）。
    glm_embedding_model: str = "embedding-3"
    #: 向量召回的开关。（评价16 更正：这里曾写"默认关"，但第 188 步起实际默认**开**，
    #: 注释与代码矛盾过一阵子。）默认开的前提是下面第 188 步那条：向量只做召回，
    #: 要不要由模型裁决 —— 这一条才是"零命中正确"没被破坏的原因。
    #: 第 183 步的否决结论仍然有效：**裸余弦排序**不行。在 96 道菜的库上，GLM `embedding-3`
    #: 给"不同查询"的余弦相似度全挤在 0.38~0.57，连"松露炖汤"（库里根本没有）都有 0.45，
    #: 比"发汗"这类真实语义查询还高 —— 任何门槛都分不开"相关"和"不相关"。
    #: 所以向量**只当召回器**（top-10 交给人/模型看），不直接决定排序，也不独自决定"有没有"。
    #: 补语义缺口另有两条路：① 服务端场景词表（第 184 步，`core/recipe_scene_terms.py`，
    #: 确定、可复现）；② `search_terms`（让模型拆同义/场景词）—— 后者真模型下只出过 2/4
    #: （第 193 步），所以保留在 schema 里但不当主力。
    #: 第 188 步起默认**开**：向量只做召回（top-10），要不要由**模型裁决**
    #: （`core/semantic_judge.py`）—— 裁决可以直接说"都不合适"，所以"零命中正确"不被破坏。
    #: 没有裁决者时工具会退化为纯字面检索（不会放未经裁决的向量结果出去）。
    embedding_enabled: bool = True
    #: MiMo（第 98 步）：只有 key，没有接口地址与模型名，账号也没充值 ——
    #: 界面上**列出但不可选**。以后填上这两项就能用，不用改代码。
    mimo_api_key: str = ""
    mimo_base_url: str = ""
    mimo_model: str = ""

    # ---- 和风天气 ----
    # 账号专属 host，形如 h2a9cf3mhs.xy.qweatherapi.com
    hefeng_api_host: str = ""
    hefeng_api_key: str = ""

    # ---- 高德地图（用于「附近好玩的地方」）----
    # 在 https://console.amap.com/dev/key 创建，
    # 【服务平台】必须选「Web 服务」，否则 Web 接口会报 key 无效
    gaode_api_key: str = ""

    # ---- 高德地图 JS API（第 224 步：出行窗口里的可交互地图）----
    # 和上面那把是**两种平台**，不能互换：这把要选「Web端(JS API)」。
    # 同页还有一串「安全密钥」（jscode），两者都是 32 位十六进制。
    # 密钥为什么放服务端：JS API 的数据请求都要带 jscode，明文塞进前端等于公开；
    # 所以前端只请求同源的 /_AMapService/*，由服务端补上 jscode 再转发（官方给的代理接法）。
    # 留空 = 地图不可用，出行窗口不显示地图按钮，其它功能完全不受影响。
    # 兼容旧名：老版本 .env 里这两行叫 GAODE2_API_KEY / GAODE_SAFE_KEY。
    gaode_js_key: str = Field(
        default="", validation_alias=AliasChoices("GAODE_JS_KEY", "GAODE2_API_KEY")
    )
    gaode_js_security_code: str = Field(
        default="",
        validation_alias=AliasChoices("GAODE_JS_SECURE_CODE", "GAODE_SAFE_KEY"),
    )

    # ---- Unsplash（出行计划里的景点配图）----
    # 在 https://unsplash.com/oauth/applications 创建应用后取 **Access Key**。
    # 注意：控制台里还有一把 Secret Key，长得一样但用途不同，
    # 填错会一律返回 401 "The access token is invalid"（已实测踩过）。
    unsplash_api_key: str = ""

    # ---- SerpAPI（第 60 步：上传图片识菜）----
    # 在 https://serpapi.com/manage-api-key 取 key，写进 .env 的 SERPAPI_API_KEY。
    # 用途**只有一个**：Google Lens 以图搜图（把用户上传的照片认成菜名）。
    # 免费额度 250 次/月，**一次识图 + 一次找做法 = 2 次**，所以文本搜索不用它
    # （那个走 DeepSeek 的 Search 接口，省额度）。
    # 留空不影响启动：识图功能会直接告诉用户"暂时查不了"。
    serpapi_api_key: str = ""

    # ---- 存储（第 47 步）----
    # SQLite 库文件位置。留空 = 默认 <项目>/data/pet.db；
    # 也可以用环境变量 PET_DB_PATH 覆盖（pydantic-settings 大小写不敏感，
    # 会自动映射到这个字段），最终解释者是 core/storage.resolve_db_path。
    pet_db_path: str = ""

    # ---- 其它 ----
    default_city_lat: float = 39.92
    default_city_lon: float = 116.41

    # ---- Agent ----
    # 单轮对话内允许的最大工具调用轮数，防止模型陷入死循环
    agent_max_steps: int = 4
    #: 一次行程生成的**总预算**（秒，第 70 步）。
    #: 出行是串行的 6~7 次外部调用，每个都有自己的超时与重试；
    #: 没有总预算时，最坏情况（每次都要重试）会拖到几十秒，用户只能干等。
    trip_deadline_seconds: float = 25.0
    #: 一次食谱问答的**总预算**（秒，第 83 步）。
    #: 前端 45 秒的静默 abort 只是**客户端放弃**，服务端并不会因此停下 ——
    #: 所以服务端要自己有预算，并且先于前端触发，才能给出可读的收尾。
    recipe_deadline_seconds: float = 35.0
    #: 出行「聊天式规划」的步数与总预算（第 216 步）。
    #: 为什么比 `agent_max_steps` 大：一轮里可能要 定位 → 天气 → 景点 → 餐馆 →
    #: 车次 → 作答，正好 6 次往返，而 4 步会在拿到数据前就被截断。
    #: 为什么给 150 秒：这 6 次里有 5 次是真实外部 API（含 12306），每次都可能慢。
    trip_chat_max_steps: int = 6
    trip_chat_deadline_seconds: float = 150.0
    #: 出行聊天的**单轮 token 预算**（第 256 步），与聊天的 `turn_token_budget` 分开。
    #: 实测（2026-10-31，上海→北京，4 次调用）：5,571 + 6,006 + 6,776 + 7,599
    #: = **25,952 token**，而 20K 的通用预算在第 5 步之前就把这一轮掐了。
    #: 6 步 + 收尾按同一量级估约 50K，这里给 60K 留余量；超了仍然会被拦（不放开）。
    trip_chat_turn_token_budget: int = 60000
    # 每个会话保留的历史消息条数
    session_max_messages: int = 40
    #: 第 112 步：单次输出上限。食谱实测 completion 59–147，1000 很宽裕。
    #: 它只是**输出**上限，不是上下文上限 —— 输入靠 40 条历史 + max_steps 限住。
    max_output_tokens: int = 1000
    #: 一轮（Agent 多步 + 工具结果）的软预算：超了就不再发起下一次调用。
    #: 0 表示不限。实测一轮约 2.3K–3.2K，20K 是给多步工具循环留的余量。
    turn_token_budget: int = 20000
    #: 单日用量告警阈值；0 表示关闭。**只告警不拒绝** —— 个人单用户，
    #: 误伤正常对话的代价比多花几毛钱大。跑一周按 P95×2 再定。
    daily_token_warn: int = 0

    # ---- 最小可交付性（第 251 步）----
    #: 多用户模式的 token 表，形如 `alice:token-a,bob:token-b`。
    #: **空 = 关闭**（本机模式，行为与以前完全一致）；配了之后所有 /api/* 都要 Bearer。
    #: 格式错会直接启动失败 —— 鉴权配置写错不能悄悄退化成「没有鉴权」。
    auth_tokens: str = ""
    #: 每个 owner 每 60 秒的最大请求数；0 = 不限。单进程内存计数。
    auth_rate_limit: int = 0
    #: 允许的浏览器 Origin（逗号分隔）；空 = 不校验。带 Origin 且不在列表里 → 403。
    auth_allowed_origins: str = ""

    @property
    def hefeng_base_url(self) -> str:
        """规范化为 https://<host> 形式，容忍用户填了协议头或结尾斜杠。"""
        host = self.hefeng_api_host.strip()
        if not host:
            raise ValueError(
                "未配置 HEFENG_API_HOST。请在 https://console.qweather.com/setting "
                "查看你的账号专属 API Host，并写入 .env，例如：\n"
                "    HEFENG_API_HOST = h2a9cf3mhs.xy.qweatherapi.com"
            )
        if host.startswith(("http://", "https://")):
            return host.rstrip("/")
        return f"https://{host.rstrip('/')}"

    def require_deepseek_key(self) -> str:
        if not self.deepseek_api_key.strip():
            raise ValueError(
                "未配置 DEEPSEEK_API_KEY。请在 https://platform.deepseek.com/api_keys "
                "创建 key 并写入 .env。"
            )
        return self.deepseek_api_key.strip()

    def require_hefeng_key(self) -> str:
        if not self.hefeng_api_key.strip():
            raise ValueError(
                "未配置 HEFENG_API_KEY。请在 https://console.qweather.com 创建凭据并写入 .env。"
            )
        return self.hefeng_api_key.strip()

    def require_gaode_key(self) -> str:
        if not self.gaode_api_key.strip():
            raise ValueError(
                "未配置 GAODE_API_KEY。请在 https://console.amap.com/dev/key 创建应用，"
                "添加 Key 时【服务平台】选「Web 服务」，然后写入 .env。"
            )
        return self.gaode_api_key.strip()

    def require_unsplash_key(self) -> str:
        if not self.unsplash_api_key.strip():
            raise ValueError(
                "未配置 UNSPLASH_API_KEY。请在 https://unsplash.com/oauth/applications "
                "创建应用，取其中的 **Access Key**（不是 Secret Key）写入 .env。"
            )
        return self.unsplash_api_key.strip()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
