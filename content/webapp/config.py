#!/usr/bin/env python3
"""系统与 AI 模型配置管理
支持持久化保存至 config.json，兼顾系统环境变量
"""
import os
import json
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")

DEFAULT_CONFIG = {
    "ai": {
        "provider": "deepseek",
        "api_key": "",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "temperature": 0.4
    },
    "settings": {
        "filter_politics": True,
        "cache_expire_sec": 300,
        "default_template": "C",
        "default_platforms": ["x", "binance", "wechat", "xiaohongshu", "telegram"]
    }
}

PRESET_PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "doc": "性价比极高，推荐使用 deepseek-chat 或 deepseek-reasoner"
    },
    "zhipu": {
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "doc": "国内高速稳定，glm-4-flash 免费/高性价比"
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "doc": "推荐 gpt-4o-mini 或 gpt-4o"
    },
    "kimi": {
        "name": "月之暗面 Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
        "doc": "擅长长文本与中文自媒体文案"
    },
    "qwen": {
        "name": "阿里通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-turbo",
        "doc": "阿里云 Compatible Mode"
    },
    "siliconflow": {
        "name": "硅基流动 SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "deepseek-ai/DeepSeek-V3",
        "doc": "提供 DeepSeek 等聚合模型接入"
    },
    "ollama": {
        "name": "本地 Ollama",
        "base_url": "http://127.0.0.1:11434/v1",
        "model": "qwen2.5:7b",
        "doc": "本地离线模型，无需 API Key"
    },
    "custom": {
        "name": "自定义 OpenAI 兼容接口",
        "base_url": "",
        "model": "",
        "doc": "任何兼容 OpenAI Chat Completion 协议的端点"
    }
}


def load_config():
    """读取配置，若不存在则返回默认配置并尝试整合环境变量"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    if "ai" in saved and isinstance(saved["ai"], dict):
                        cfg["ai"].update(saved["ai"])
                    if "settings" in saved and isinstance(saved["settings"], dict):
                        cfg["settings"].update(saved["settings"])
        except Exception:
            pass

    # 环境变量降级补充
    if not cfg["ai"]["api_key"]:
        if os.environ.get("DEEPSEEK_API_KEY"):
            cfg["ai"]["api_key"] = os.environ["DEEPSEEK_API_KEY"]
            cfg["ai"]["provider"] = "deepseek"
            cfg["ai"]["base_url"] = "https://api.deepseek.com/v1"
            cfg["ai"]["model"] = "deepseek-chat"
        elif os.environ.get("ZHIPU_API_KEY"):
            cfg["ai"]["api_key"] = os.environ["ZHIPU_API_KEY"]
            cfg["ai"]["provider"] = "zhipu"
            cfg["ai"]["base_url"] = "https://open.bigmodel.cn/api/paas/v4"
            cfg["ai"]["model"] = os.environ.get("GLM_MODEL", "glm-4-flash")
        elif os.environ.get("OPENAI_API_KEY"):
            cfg["ai"]["api_key"] = os.environ["OPENAI_API_KEY"]
            cfg["ai"]["provider"] = "openai"
            cfg["ai"]["base_url"] = "https://api.openai.com/v1"
            cfg["ai"]["model"] = "gpt-4o-mini"

    return cfg


def save_config(cfg):
    """保存配置到 config.json"""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return True


def get_safe_config():
    """返回对前端展示的安全配置（掩码 API Key）"""
    cfg = load_config()
    safe = json.loads(json.dumps(cfg))
    key = safe.get("ai", {}).get("api_key", "")
    if key:
        if len(key) > 8:
            safe["ai"]["masked_key"] = key[:3] + "..." + key[-4:]
        else:
            safe["ai"]["masked_key"] = "***"
        safe["ai"]["has_key"] = True
    else:
        safe["ai"]["masked_key"] = ""
        safe["ai"]["has_key"] = False
    safe["ai"]["api_key"] = ""  # 不在明文中传回完整 key
    safe["preset_providers"] = PRESET_PROVIDERS
    return safe


def test_ai_connection(api_key, base_url, model):
    """测试 AI 模型 API 是否可用"""
    if not base_url:
        return False, "Base URL 不能为空"
    if not model:
        return False, "Model 不能为空"
    
    url = base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "crypto-posts-tester/1.0"
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "user", "content": "你好，请回复【OK】"}
        ],
        "max_tokens": 10,
        "temperature": 0.1
    }).encode("utf-8")

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            return True, f"连接成功！模型响应: {content}"
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8")
        except Exception:
            pass
        return False, f"HTTP {e.code} 错误: {e.reason} ({body[:120]})"
    except Exception as e:
        return False, f"请求异常: {str(e)}"
