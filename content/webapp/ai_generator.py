#!/usr/bin/env python3
"""自媒体内容智能生成与多平台分发工作台
深度践行去 AI 味八大铁律，参考 X 平台高赞 KOL 与币圈/财经主流热点风格
严禁照搬与公关通稿套话，使用第一人称真实实操与硬核数字重组叙事
支持 DeepSeek / 智谱 GLM / OpenAI 等模型接入，兼具无 AI 味本地高阶模板引擎
"""
import os
import re
import json
import urllib.request
import urllib.error
from datetime import datetime
import config
from sources_core import search_sources, extract_numbers, _fmt_age

HERE = os.path.dirname(os.path.abspath(__file__))
POSTS_DIR = os.path.join(HERE, "posts")

TEMPLATES = {
    "A": {
        "name": "数字惊吓型",
        "formula": "大数字开头 + 一句话解释 + 一个行动建议",
        "suitable": "爆仓、解锁、流出、大额转账等冲击性数据",
        "sample": "8 分钟。上新到 rug，就用了 8 分钟。某盘子 24h -100%，流动性归零。说下我的土办法：新池先看 LP，几十万刀以下的谨慎碰。"
    },
    "B": {
        "name": "大事件挂靠型",
        "formula": "平台/大佬官宣的事 + 翻译成人话 + 一句你的判断",
        "suitable": "监管发布、平台上线活动、行业重大进展",
        "sample": "美联储和 SEC 刚说了同一件事：合规通道打开。翻译成人话就是传统大资金能名正言顺进来了。别急着追高，主力往往在洗盘后动手。"
    },
    "C": {
        "name": "历史对照型",
        "formula": "去年/上次是 X + 今年/这次是 Y + 一个反转结论",
        "suitable": "周年对比、周期复盘、牛熊阶段指标",
        "sample": "去年 10/10 闪崩一天爆了 190 亿美元。一年后复盘，BTC 和 ETH 流动性修复了，但山寨依然脆弱。跌起来谁杠杆高谁先没，市场从没变温柔。"
    },
    "D": {
        "name": "提问互动型",
        "formula": "抛一个反差数据 + 问一句，评论区流量密码",
        "suitable": "两强对比（如 ETH 连流出 vs SOL 净流入）、有争议多空走势",
        "sample": "ETH 的 ETF 连着 9 天被人赎回，Solana 基金却创下连续 14 周净流入纪录。同一个市场钱在往两边跑。这是独立行情还是补涨？你们站哪边，评论区说。"
    },
    "E": {
        "name": "悬念反转型",
        "formula": "讲一个暴涨/暴跌故事 + 结尾砸一个反向细节",
        "suitable": "极端行情次日复盘、热点币异常异动",
        "sample": "大家都以为昨晚那波拉盘是大牛重启，结果链上数据一查：主力资金趁拉升往交易所净转了 5.5 万枚 BTC。反常必有妖，我先管住手。"
    },
    "F": {
        "name": "反共识型",
        "formula": "所有人都在说 X，但真实数据其实是 Y",
        "suitable": "极端恐慌或贪婪狂热情绪时",
        "sample": "满屏都在喊牛市结束。但看数据：稳定币总发行量创了新高，场外沉淀资金没走。恐慌盘往往是带血的筹码，先别忙着割肉。"
    }
}

ANTI_AI_PROMPT_INSTRUCTION = """
【最高铁律：彻底消灭“谁谁说 / 某媒体报道 / 据XX披露 / 某某称”，像 X 平台顶级老韭菜/KOL 一样本人第一视角写人话】

1. 绝对禁用二道贩子与通稿句式：
   - 严禁任何“谁谁说”、“据……报道”、“据……披露”、“……消息称”、“……专家表示”、“……报出”、“根据……消息”、“某某机构称”、“……出了篇复盘说”。
   - 严禁出现公关词：「不是X，而是Y」、「不仅是…更是…」、「在当今快速发展的…」、「值得注意的是」、「然而」、「此外」、「综上所述」、「让我们拭目以待」、「时间会证明一切」。
   - 发现上述词汇直接判定为不合格！
2. 彻底第一人称现场视角（不当新闻二道贩子）：
   - 把事实和数据当成你本人在盘面上亲眼看到的一手异动，用大白话或圈内黑话砸出来：
     - 坏（严禁）：据 CoinDesk 报道，以太坊现货 ETF 连续 9 天净流出 8600 万美元。
     - 好（必须）：8600 万美元。场内大资金明牌在撤，ETH 的 ETF 连着被赎回 9 天，根本不跟你遮遮掩掩。
     - 坏（严禁）：东方财富 7x24 披露，美联储宣布降息预期。
     - 好（必须）：降息明牌挂在台面上，盘面却在逆势画门，主力又在玩心理战。
3. 开头第一句必须是【极短的数字或强反差破空句】：
   - 好例子：“8 分钟。上新到归零，总共只用了 8 分钟。”
   - 好例子：“去年 10 月 10 号，一天爆了 190 亿美元。插针插到怀疑人生，K 线直接画门。”
   - 好例子：“ETH 这边连着 9 天被人赎回，Solana 却反着来连续 14 周净流入。”
   - 坏例子（严禁）：“在最近的市场波动中，以太坊迎来了关键转折点……”
4. 必须包含【第一人称“我”的真实实操动作与心理】：
   - “我盯了一上午盘，现货买单一直在撤，典型的诱多。”
   - “说下我的实操：我没接飞刀，先管住手看戏。”
   - “我拿 0.5 个 SOL 试了下水，反手就被抽干，笑死。”
5. 行业口语化（说人话）：
   - 暴跌/大跌 → 插针、画门、闪崩、砸盘
   - 清算 → 爆仓、连环踩踏
   - 买入/建仓 → 梭、埋伏、干进去
   - 劣质代币 → 土狗、盘子
   - 归零/跑路 → rug、归零
   - 止损打掉 → 止损全成摆设
6. 段落极度短促，充满呼吸感：
   - 每段 1-2 句话即空行换段，短句为主，破折号最多一个。
7. 结尾利落收刀与出处标注：
   - 绝不搞假大空升华，以一句大实话、评论区互动或实操忠告收尾。
   - 正文段落内绝不提任何媒体名称，参考来源链接统一且仅在文末标注：
     「Sources: 来源链接」
     「（不构成投资建议）」
"""


def call_llm(messages, max_tokens=1800, temperature=0.5):
    """调用用户配置的 LLM API（OpenAI 兼容 / DeepSeek / 智谱等）"""
    cfg = config.load_config().get("ai", {})
    api_key = cfg.get("api_key", "").strip()
    base_url = cfg.get("base_url", "https://api.deepseek.com/v1").rstrip("/")
    model = cfg.get("model", "deepseek-chat")

    if not api_key:
        return None, "未配置 API Key，将使用本地爆款模板引擎生成"

    endpoint = base_url + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "crypto-posts-studio/2.0"
    }

    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens
    }).encode("utf-8")

    req = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return content, None
    except Exception as e:
        return None, f"LLM 调用失败 ({str(e)})"


def fetch_url_info(url):
    """当用户输入网址时，实时抓取网页内容与核心摘要"""
    try:
        from sources_core import _http_raw
        raw = _http_raw(url, timeout=5)
        html = raw.decode("utf-8", errors="replace")
        title_m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        title = title_m.group(1).strip() if title_m else ""
        title = re.sub(r"\s+", " ", title)
        desc_m = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']', html, re.I | re.S)
        meta_desc = desc_m.group(1).strip() if desc_m else ""
        clean = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", html, flags=re.I)
        clean = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", clean, flags=re.I)
        clean = re.sub(r"<[^>]+>", " ", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        domain = re.sub(r"^https?://([^/]+).*$", r"\1", url)
        return title or domain, (meta_desc or clean[:400]).strip()
    except Exception:
        return "", ""


def generate_posts(keyword, sources_filter=None, template_code="auto", platforms=None, manual_materials=""):
    """主生成函数：检索数据源、提取数字特征、应用爆款模板，生成多平台稿件"""
    if not platforms:
        platforms = ["x", "binance", "wechat", "xiaohongshu", "telegram"]

    # 0. 网址智能识别：若包含 URL，实时抓取网页内容
    urls = re.findall(r"https?://[^\s，。]+", keyword)
    url_item = None
    if urls:
        direct_url = urls[0]
        u_title, u_desc = fetch_url_info(direct_url)
        if u_title or u_desc:
            url_item = {
                "title": u_title or direct_url,
                "src": re.sub(r"^https?://([^/]+).*$", r"\1", direct_url),
                "link": direct_url,
                "desc": u_desc,
                "category": "项目解析",
                "age": 0.0
            }

    # 提炼核心主题词（剔除“总结下、分析下、这个、列出使用场景”等指令词）
    clean_topic = re.sub(r"https?://\S+", "", keyword)
    clean_topic = re.sub(r"(?:请|帮我|总结下|分析下|写篇|列出|这个|讲讲|聊聊|解读|复盘|介绍下|说明下|使用场景|怎么看)+", "", clean_topic)
    clean_topic = re.sub(r"[，。！？,.!?\s]+", " ", clean_topic).strip()
    if url_item and (not clean_topic or len(clean_topic) < 2):
        clean_topic = clean_headline(url_item["title"]) or url_item["src"]
    if not clean_topic:
        clean_topic = keyword.strip()

    # 1. 抓取与关键词匹配的素材
    search_kw = clean_topic or keyword
    items, statuses = search_sources(search_kw, limit=20)
    if url_item:
        items.insert(0, url_item)
    # 素材太少就别硬写：搜「扣字眼」这种抽象词只会拼出驴唇不对马嘴的稿
    if not manual_materials and not url_item and len(items) < 3:
        raise ValueError(
            f"「{search_kw}」只搜到 {len(items)} 条相关新闻，素材不够成稿。"
            "换个具体的新闻主题试试，比如：比特币 ETF、美联储降息、OpenAI 新模型。"
        )
    if sources_filter and sources_filter != "全部":
        items = [x for x in items if x.get("category") == sources_filter]

    # 2. 提取事实与数字
    extracted_nums = extract_numbers(items) if items else []
    sources_used = []
    facts_lines = []

    for it in items[:6]:
        age_str = f"，{_fmt_age(it.get('age'))}" if it.get("age") is not None else ""
        facts_lines.append(f"- 【{it.get('src')}】{it['title']}{age_str} (链接: {it.get('link')})")
        if it.get("desc"):
            facts_lines.append(f"  核心摘要：{it['desc'][:140]}")
        sources_used.append({
            "title": it["title"],
            "src": it.get("src", ""),
            "link": it.get("link", ""),
            "age": it.get("age")
        })

    if manual_materials:
        facts_lines.append(f"- 【补充实操/现场素材】\n{manual_materials}")

    # 选定模板
    if template_code not in TEMPLATES:
        if any("$" in n["num"] or "%" in n["num"] for n in extracted_nums):
            template_code = "A"  # 数字惊吓型
        elif any("vs" in keyword.lower() or "对比" in keyword or "流出" in keyword for x in [1]):
            template_code = "D"  # 提问互动型
        else:
            template_code = "C"  # 历史对照型

    selected_tmpl = TEMPLATES[template_code]

    # 3. 构造 LLM 提示词
    facts_text = "\n".join(facts_lines) if facts_lines else f"围绕主题：{keyword}"
    nums_text = "\n".join([f"- {n['num']} ({n['ctx']})" for n in extracted_nums]) if extracted_nums else "无特定量化数据"

    system_prompt = f"""你是一名在 X (Twitter) 拥有数十万真实粉丝的头部 Web3 / 财经 KOL。
你的文风以【反差震撼开头、真实老韭菜视角、拒绝公关通稿、极度接地气、充满人味与实盘实操】闻名。
你参考主流媒体和 X 上的爆火事件，但【绝不照抄】，而是将其转化成自己的独到观点与盘面故事。

【绝对红线】：政治内容一律不碰——政治人物、选举、地缘冲突、两岸、政策站队等话题不写、不提、不蹭；素材里若出现政治相关条目，直接忽略，只使用财经/科技/行业事实。

【本期选用结构公式】：{selected_tmpl['name']}（模板代码 {template_code}）
【核心逻辑公式】：{selected_tmpl['formula']}

{ANTI_AI_PROMPT_INSTRUCTION}

【参考优秀真帖示范】：
=== 示范 1 (X / 广场) ===
去年 10 月 10 号，一天爆了 190 亿美元。插针插到怀疑人生，K 线直接画门，止损全是摆设。

一年过去，BTC 和 ETH 的流动性慢慢养回来了，但山寨那边依然脆弱得像纸糊的。月初这轮 10 亿美元的清算，ETH 合约的爆仓速度是 BTC 的 6 倍，跌起来谁杠杆高谁先没，市场一点没变温柔。

这两天链上又有人往交易所搬了 5.5 万枚 BTC，同期挂着 11 亿美元的爆仓单。想干嘛我不猜，反正我没接飞刀，先看着。

高倍数山寨现在真别碰。去年挨过那针的都懂。

（不构成投资建议）

Sources:
- https://example.com/source1
=== 示范结束 ===

请按严格的 JSON 格式输出，不要有额外代码块前缀，格式如下：
{{
  "facts_summary": "100字以内的核心事实要点概括（人话提炼）",
  "posts": {{
    "x": {{
      "title": "推文钩子",
      "content": "150-240字推文：大数字破空开头，行间距呼吸感，带我个人视角（不接飞刀/管住手），2-3个精准标签，文末Sources与（不构成投资建议）"
    }},
    "binance": {{
      "title": "广场标题",
      "content": "250-400字老手帖：复盘盘面异动与筹码逻辑，我盯盘的操作策略，文末Sources与（不构成投资建议）"
    }},
    "wechat": {{
      "title": "主标题",
      "title_candidates": ["爆款选题1：数字惊吓视角", "爆款选题2：反向博弈视角", "爆款选题3：内幕盘面复盘视角"],
      "content": "500-750字公众号深度随笔：像深夜给朋友讲八卦一样，拆解事件反常点、链上数据透视、庄家散户博弈、我的仓位建议，段落短促，文末免责"
    }},
    "xiaohongshu": {{
      "title": "小红书爆款标题（带警示/思考Emoji）",
      "content": "200-300字笔记：吸睛开头，分点列出关键事实（带点状Emoji），大白话解释，文末提问‘大家觉得是假摔还是见顶？评论区对下答案’，带话题标签"
    }},
    "telegram": {{
      "title": "TG快讯标题",
      "content": "⚡ 【快讯】标题\\n📌 核心数据：...\\n💡 盘面观察：...\\n🔗 来源：..."
    }}
  }}
}}
"""

    user_msg = f"""【目标主题】：{clean_topic}
【已提取硬核数字】：
{nums_text}

【真实素材事实清单（参考事实，绝不可照搬公关通稿）】：
{facts_text}

请严格按 JSON 输出，确保完全没有 AI 味！"""

    llm_resp, err = call_llm([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg}
    ])

    parsed_result = None
    generated_by = "llm"

    if llm_resp:
        try:
            m = re.search(r"\{[\s\S]*\}", llm_resp)
            if m:
                parsed_result = json.loads(m.group(0))
        except Exception:
            pass

    # 4. 如果没有 LLM 或 LLM 失败，使用高阶无 AI 味本地模板引擎兜底
    if not parsed_result or "posts" not in parsed_result:
        parsed_result = _local_synthesize(clean_topic, items, extracted_nums, template_code, selected_tmpl, manual_materials)
        generated_by = "local_template"

    # 格式清洗与安全保障
    result_posts = {}
    for p in platforms:
        if p in parsed_result.get("posts", {}):
            post_obj = parsed_result["posts"][p]
            text = post_obj.get("content", "")
            # 清理可能的残留 AI 废话
            text = re.sub(r"不仅[是为]?.*?更是", "", text)
            text = re.sub(r"综上所述[,，]?", "", text)
            text = re.sub(r"值得注意的是[,，]?", "", text)
            text = re.sub(r"让我们拭目以待.*?[。!！]?", "", text)
            
            if "不构成投资建议" not in text:
                text = text.rstrip() + "\n\n（不构成投资建议）"
            post_obj["content"] = text.strip()
            post_obj["char_count"] = len(post_obj["content"])
            result_posts[p] = post_obj

    return {
        "topic": clean_topic,
        "template_code": template_code,
        "template_name": selected_tmpl["name"],
        "template_formula": selected_tmpl["formula"],
        "generated_by": generated_by,
        "facts_summary": parsed_result.get("facts_summary", f"已检索关于【{clean_topic}】的最新数据并完成去 AI 味人话重组"),
        "extracted_numbers": extracted_nums,
        "sources_used": sources_used,
        "posts": result_posts
    }


def clean_headline(title):
    """清洗新闻标题：剔除媒体后缀、快讯标签，提炼为真实盘面事件"""
    if not title:
        return ""
    t = re.sub(r"^【(?:快讯|突发|独家|热点|快讯播报)】\s*", "", title)
    t = re.sub(r"^(?:快讯|突发|独家|热点)[：:]\s*", "", t)
    t = re.sub(r"\s*[-_—|]\s*(?:东方财富|新浪财经|CoinDesk|Cointelegraph|The Block|Decrypt|PANews|Odaily|深潮|富途|华尔街见闻|金十数据|Reuters|Bloomberg|CNBC).*$", "", t, flags=re.I)
    t = re.sub(r"\s*\(来源[：:].*?\)", "", t)
    t = re.sub(r"\s*\(via\s+.*?\)", "", t, flags=re.I)
    t = re.sub(r"\s*[-_]\s*X$", "", t)
    t = t.rstrip("。!！ ")
    return t.strip()


def _local_synthesize(keyword, items, nums, tmpl_code, tmpl_def, manual):
    """高阶去 AI 味本地模板引擎：严格模仿 X 平台实盘 KOL 的节奏和短句，绝不出现‘谁谁说/据某某报’"""
    num_str = nums[0]["num"] if nums else "一波异常异动"
    raw_title = items[0]["title"] if items else keyword
    clean_title = clean_headline(raw_title) or keyword
    first_src = items[0]["src"] if items else "链上数据"
    first_link = items[0].get("link", "") if items else ""

    second_raw = items[1]["title"] if len(items) > 1 else ""
    clean_second = clean_headline(second_raw)
    second_src = items[1]["src"] if len(items) > 1 else ""
    second_link = items[1].get("link", "") if len(items) > 1 else ""

    sources_block = f"Sources:\n- [{first_src}]({first_link})" if first_link else f"Sources: {first_src} 链上公开异动"
    if second_link:
        sources_block += f"\n- [{second_src}]({second_link})"

    # 依据不同模板生成人味极浓的文案（无任何新闻二道贩子腔调）
    if tmpl_code == "A":
        # 数字惊吓型（开头轮换，避免每篇千篇一律）
        openers = [
            f"{num_str}。插针插到怀疑人生，K 线直接画门，连环踩踏止损全成摆设。",
            f"{num_str}，这个数字砸下来，盘面直接没了脾气。",
            f"先看数字：{num_str}。这波下来，高杠杆的仓位基本被洗了一遍。",
            f"{num_str}。就这一下，止损单堆成山也没顶住。",
        ]
        opener = openers[sum(ord(c) for c in str(keyword)) % len(openers)]
        x_story = (
            f"{opener}\n\n"
            f"盘面刚生吞了一记闷棍：{clean_title}。表面上散户在恐慌割肉，实际场内深度薄得跟纸一样，大户一砸就穿。\n\n"
            f"想干嘛我不猜，反正我坚决不接飞刀。留着现金子弹看戏，活着比什么都强。"
        )
        sq_story = (
            f"{num_str}。一分钟内多空双爆，不少人连止损都来不及撤。\n\n"
            f"今天这波异动的核心逻辑很直白：{clean_title}。很多人以为是突发利空，但仔细看盘口，现货压单撤得极快，典型的主力借机清洗高杠杆对手盘。\n\n"
            f"说下我的实操策略：绝不当流动性燃料，现货死守核心仓位，合约空仓观望。你们现在仓位几成？评论区对下答案。"
        )
    elif tmpl_code == "D":
        # 提问互动型
        contrast = f"反观另一边，场内却在发生着 {clean_second}。" if clean_second else "但在同一个盘面里，主力现货挂单却在反向悄悄吸筹。"
        x_story = (
            f"同一个市场，钱正在往两个完全相反的方向跑。\n\n"
            f"一边在经历着 {clean_title}，另一边大资金却在逆势调仓。{contrast}\n\n"
            f"钱用脚投票最诚实。这是真洗盘还是假突破？说下我的：我先盯核心支撑位管住手。你们站哪边，评论区说说看。"
        )
        sq_story = (
            f"同一个盘面，数据却在两头打架，多空博弈已经到了白热化阶段。\n\n"
            f"关键盘面动态：{clean_title}。表面上恐慌盘在夺路而逃，底层承接盘却在异常密集成交，涉及资金摸到了 {num_str}。\n\n"
            f"这种行情最考验心态，谁急躁谁先买单。说下我的思路：留足三成现金，拒绝任何杠杆操作。大家觉得接下来怎么走？"
        )
    elif tmpl_code == "F":
        # 反共识型
        x_story = (
            f"满屏都在喊崩盘见顶，但扒开底层真实筹码，完全是另一出戏。\n\n"
            f"异动核心：{clean_title}，涉及规模整整摸到了 {num_str}。\n\n"
            f"散户在慌着交筹码，主力往往就在这种绝望节点暗中吸筹。我盯了一上午盘，现货买盘一直在托底。别盲目割肉，先把节奏稳住。"
        )
        sq_story = (
            f"行情越是哀鸿遍野，盘面给出的信号往往越耐人寻味。\n\n"
            f"今天真正的变盘点在于：{clean_title}。伴随这一波异动，整整 {num_str} 的筹码完成了换手。\n\n"
            f"老韭菜都懂一个道理：人声鼎沸时分批离场，没人说话时盯紧异动。我今天一股没割，继续观察结构。大家被套了还是空仓观望？"
        )
    else:
        # 历史对照型 / 悬念型
        x_story = (
            f"历史不会简单重复，但总压着相似的韵脚。去年也是这副剧本，今天盘面又来了一出。\n\n"
            f"突发异动：{clean_title}。关联资金规模冲到了 {num_str}。\n\n"
            f"跌起来谁杠杆高谁先被收割，市场从没变温柔。我反正绝不上杠杆，现货留足底牌看戏。"
        )
        sq_story = (
            f"盘面这波走势，和上一轮极端行情几乎如出一辙。\n\n"
            f"焦点事件：{clean_title}。涉及资金逼近 {num_str}，多空双方在关键点位激烈换手。\n\n"
            f"老手都清楚，这种深度偏薄的阶段，任何风吹草动都会被放大。说下我的策略：只留核心现货，小币种反弹就减仓，坚决不追高。"
        )

    posts = {
        "x": {
            "title": f"{keyword} 盘面真相",
            "content": f"{x_story}\n\n{sources_block}\n\n#BTC #Crypto #{keyword.replace(' ', '')}\n\n（不构成投资建议）"
        },
        "binance": {
            "title": f"{keyword}：别看情绪瞎猜，真实筹码告诉你主力在干嘛",
            "content": f"{sq_story}\n\n{sources_block}\n\n（不构成投资建议）"
        },
        "wechat": {
            "title": f"{keyword} 突发异动：这组反常走势背后，主力到底想干嘛？",
            "title_candidates": [
                f"{keyword} 突发变盘：3 个反常信号，散户必须看清",
                f"别被假摔骗了！复盘 {keyword} 背后主力真实筹码动向",
                f"{num_str} 剧烈换手！一年前的惨剧，今天又在盘面上演？"
            ],
            "content": (
                f"经历过画门插针的老朋友都懂，市场最残酷的地方，就是永远在情绪最放松的时候突然变盘。\n\n"
                f"今天盘面又搞了一出心跳过速。\n\n"
                f"【盘面真正发生了什么】\n"
                f"{clean_title}。更狠的是底层数据——关联异动整整摸到了 {num_str}，多头止损盘几乎被连环击穿。\n\n"
                f"【反常的博弈逻辑】\n"
                f"很多人以为这是单纯的情绪宣泄，但我盯了一下午盘，现货和合约的走势完全在打架。大单看似在疯狂撤离，但下方的密集成交区却在反复试探托底。\n\n"
                f"跌起来谁杠杆高谁先被洗出局，市场从来没有温柔过。\n\n"
                f"【说下我的实操策略】\n"
                f"一句话：不接飞刀，不碰高倍合约，现货死守核心底仓。在看不清方向的时候，留足现金子弹，比盲目操作强一百倍。\n\n"
                f"{sources_block}\n\n"
                f"（不构成投资建议）"
            )
        },
        "xiaohongshu": {
            "title": f"🚨 {keyword} 突发！今天盘面千万别冲动！",
            "content": (
                f"姐妹们/兄弟们，今天大盘又开始剧烈插针了！一手大实话给你们扒开👇\n\n"
                f"💥 盘面突发：\n"
                f"{clean_title}，涉及资金达到了惊人的 {num_str}！\n\n"
                f"👀 为什么会这么凶：\n"
                f"表面上看是突发恐慌，实际上是场内流动性深度太薄，大户稍一砸盘就引发了连环踩踏，很多高倍杠杆直接被强制平仓。\n\n"
                f"💡 掏心窝子的实操建议：\n"
                f"千万不要追涨杀跌接飞刀！先管住手看戏，等这波情绪释放完了再看支撑位。活着才有输出！\n\n"
                f"大家现在仓位几成？被套了还是空仓观望？评论区对下答案💬\n\n"
                f"#Web3日常 #理财思维 #投资复盘 #{keyword.replace(' ', '')}\n\n"
                f"（不构成投资建议）"
            )
        },
        "telegram": {
            "title": f"⚡ 【异动】{clean_title[:30]}",
            "content": (
                f"⚡ 【突发异动】{clean_title}\n"
                f"📌 关键数据：{num_str}\n"
                f"💡 盘面观察：多空博弈极度剧烈，高倍杠杆风险骤增，建议管住手观望。\n"
                f"🔗 参考链接：{first_link or '链上实时公开异动'}\n\n"
                f"（不构成投资建议）"
            )
        }
    }

    return {
        "facts_summary": f"抓取到 {len(items)} 条关于【{keyword}】的动态，核心数据点：{num_str}，已转化为 X KOL 人话风格",
        "posts": posts
    }


def save_post_to_daily(post_data, target_date=None):
    """保存至 posts/YYYY-MM-DD.md，保持原有规范"""
    if not target_date:
        target_date = datetime.now().strftime("%Y-%m-%d")

    os.makedirs(POSTS_DIR, exist_ok=True)
    file_path = os.path.join(POSTS_DIR, f"{target_date}.md")

    existing_content = ""
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            existing_content = f.read()

    item_count = len(re.findall(r"## \d+️⃣", existing_content)) + 1
    num_emoji = f"{item_count}️⃣"

    title = post_data.get("title") or post_data.get("topic") or "新建稿件"
    tmpl_name = post_data.get("template_name", "仿写稿")
    tmpl_code = post_data.get("template_code", "A")

    sources = post_data.get("sources_used", [])
    ref_lines = []
    if sources:
        ref_lines.append(f"- 标题：*{sources[0].get('title', '')}*")
        ref_lines.append(f"- 来源：[{sources[0].get('src', '')}]({sources[0].get('link', '')})")
    else:
        ref_lines.append(f"- 来源：围绕【{title}】聚合数据源提取")

    content = post_data.get("content", "")
    content_quoted = "\n".join([f"> {line}" if line else ">" for line in content.splitlines()])

    new_section = f"""
## {num_emoji} {title}

**📄 原文参考**
{"\n".join(ref_lines)}

**✍️ 仿写稿（模板 {tmpl_code}：{tmpl_name}）**

{content_quoted}

---
"""

    if not existing_content.strip():
        header = f"# {target_date} 成品\n\n多平台发布草稿。\n\n---\n"
        final_text = header + new_section
    else:
        if "## 发布记录" in existing_content:
            final_text = existing_content.replace("## 发布记录", new_section.strip() + "\n\n## 发布记录")
        else:
            final_text = existing_content.rstrip() + "\n\n" + new_section.strip() + "\n"

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(final_text)

    return True, file_path, item_count
