#!/usr/bin/env python3
"""自媒体矩阵 · 聚合创作中心 (crypto-posts)
设计理念：
- Google & Apple 极简大搜索框首页：搜关键词直接出多平台爆款成稿（去 AI 味）
- 统一设置中心：数据源配置与 AI 模型配置合并为一处，拒绝杂乱
- 极简干净：无多余归档入口与复杂仪表盘，专注核心创作
- 零依赖：纯 Python 3 标准库构建
"""
import os
import re
import json
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "fetcher"))

import config
import sources_core as sc
import hot_radar as hr
import ai_generator as aig
import render_site as rs

DAILY_DIR = os.path.join(HERE, "daily")
POSTS_DIR = os.path.join(HERE, "posts")
PORT = int(os.environ.get("PORT", "8765"))


def list_days():
    days = {}
    for d, key in ((DAILY_DIR, "daily"), (POSTS_DIR, "posts")):
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", f):
                days.setdefault(f[:-3], {})[key] = os.path.join(d, f)
    return sorted(days.items(), reverse=True)


def read_file(path):
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- 极简 Google / Apple 风格界面

APP_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>素材出稿雷达</title>
<style>
:root {
  --bg-app: #ffffff;
  --bg-subtle: #f8f9fa;
  --border: #dadce0;
  --border-subtle: #ebecef;
  --text-main: #202124;
  --text-secondary: #5f6368;
  --text-tertiary: #80868b;
  --google-blue: #1a73e8;
  --google-blue-hover: #174ea6;
  --apple-green: #34a853;
  --radius-pill: 980px;
  --radius-lg: 16px;
  --radius-md: 10px;
  --shadow-search: 0 2px 10px rgba(0, 0, 0, 0.08), 0 1px 3px rgba(0, 0, 0, 0.04);
  --shadow-hover: 0 4px 18px rgba(0, 0, 0, 0.12), 0 1px 4px rgba(0, 0, 0, 0.06);
  --shadow-card: 0 1px 4px rgba(0, 0, 0, 0.05);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", "PingFang SC", "Hiragino Sans GB", "Helvetica Neue", Arial, sans-serif;
  background: var(--bg-app);
  color: var(--text-main);
  line-height: 1.6;
  font-size: 14px;
  -webkit-font-smoothing: antialiased;
  padding-bottom: 80px;
}
a { color: var(--google-blue); text-decoration: none; }
a:hover { text-decoration: underline; }

/* 顶部极简设置入口 */
header {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  z-index: 100;
  background: transparent;
  border-bottom: none;
}
.header-inner {
  max-width: 1320px;
  margin: 0 auto;
  padding: 24px 32px;
  display: flex;
  align-items: center;
  justify-content: flex-end;
}
.brand { display: none; }
.header-btns {
  display: flex;
  align-items: center;
}
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 18px;
  border-radius: var(--radius-pill);
  font-size: 13.5px;
  font-weight: 500;
  cursor: pointer;
  border: 1px solid var(--border);
  background: #fff;
  color: var(--text-main);
  box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  transition: all 0.15s ease;
}
.btn:hover { background: var(--bg-subtle); border-color: #bdc1c6; }
.btn-primary {
  background: var(--google-blue);
  border-color: var(--google-blue);
  color: #fff;
  font-weight: 600;
}
.btn-primary:hover { background: var(--google-blue-hover); border-color: var(--google-blue-hover); }
.btn-sm { padding: 7px 16px; font-size: 13px; }

/* 页面主体：宽屏饱满 */
.container {
  max-width: 1240px;
  width: 92%;
  margin: 0 auto;
  padding: 0 20px;
}

/* 经典 Google 风格大搜索框 Hero 区域 */
.hero-box {
  text-align: center;
  padding: 90px 0 32px;
  transition: all 0.3s ease;
}
.hero-box.compact {
  padding: 20px 0 16px;
}
.hero-title {
  font-size: 50px;
  font-weight: 800;
  letter-spacing: -0.04em;
  color: var(--text-main);
  margin-bottom: 26px;
}

/* 巨幅大搜索框：尺寸放大，饱满高级 */
.big-search-card {
  max-width: 960px;
  margin: 0 auto;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: var(--radius-pill);
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08), 0 1px 4px rgba(0, 0, 0, 0.04);
  padding: 8px 10px 8px 24px;
  min-height: 64px;
  display: flex;
  align-items: center;
  gap: 14px;
  transition: all 0.2s ease;
}
.big-search-card:hover, .big-search-card:focus-within {
  box-shadow: 0 6px 26px rgba(0, 0, 0, 0.12), 0 2px 6px rgba(0, 0, 0, 0.06);
  border-color: #bdc1c6;
}
.search-icon svg {
  width: 22px;
  height: 22px;
  fill: var(--text-tertiary);
}
.big-search-input {
  flex: 1;
  border: none;
  background: transparent;
  font-size: 18px;
  color: var(--text-main);
  outline: none;
  padding: 8px 0;
}
.big-search-input::placeholder {
  color: var(--text-tertiary);
  font-size: 16px;
}
.big-search-btn {
  padding: 12px 34px;
  background: var(--google-blue);
  color: #fff;
  border: none;
  border-radius: var(--radius-pill);
  font-size: 16px;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s;
}
.big-search-btn:hover { background: var(--google-blue-hover); }

/* 热门推荐词标签 */
.hot-tags-row {
  max-width: 960px;
  margin: 20px auto 0;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 10px;
}
.hot-tag-lbl {
  font-size: 13px;
  color: var(--text-secondary);
  font-weight: 500;
}
.hot-chip {
  padding: 6px 18px;
  background: var(--bg-subtle);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-pill);
  font-size: 13.5px;
  color: var(--text-main);
  cursor: pointer;
  transition: all 0.15s;
}
.hot-chip:hover {
  background: #e8f0fe;
  color: var(--google-blue);
  border-color: #d2e3fc;
}

/* 搜索结果大区 */
#resultsSection {
  display: none;
  margin-top: 32px;
  animation: fadeIn 0.25s ease;
}
@keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }

/* 模块 1: 平台稿件成文（核心重点，饱满大气） */
.result-card-main {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 20px;
  box-shadow: 0 4px 24px rgba(0, 0, 0, 0.06);
  padding: 32px 36px;
  margin-bottom: 28px;
}
.res-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
  border-bottom: 1px solid var(--border-subtle);
  padding-bottom: 16px;
}
.res-title-text {
  font-size: 21px;
  font-weight: 700;
  color: var(--text-main);
  letter-spacing: -0.01em;
}
.res-badges-box { display: flex; align-items: center; gap: 8px; }
.badge-clean {
  padding: 4px 10px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 600;
}
.badge-blue { background: #e8f0fe; color: #1a73e8; }
.badge-green { background: #e6f4ea; color: #137333; }

/* 平台切换胶囊 */
.platform-switch-bar {
  display: flex;
  gap: 8px;
  background: var(--bg-subtle);
  padding: 6px;
  border-radius: var(--radius-pill);
  margin-bottom: 22px;
  overflow-x: auto;
}
.plat-btn {
  flex: 1;
  padding: 11px 20px;
  border-radius: var(--radius-pill);
  font-size: 14.5px;
  font-weight: 600;
  cursor: pointer;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  transition: all 0.15s;
  white-space: nowrap;
}
.plat-btn:hover { color: var(--text-main); }
.plat-btn.active {
  background: #fff;
  color: var(--text-main);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}

/* 稿件正文展现区 */
.post-paper {
  background: #fbfbfd;
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 26px 30px;
  min-height: 440px;
  display: flex;
  flex-direction: column;
}
.post-textarea {
  flex: 1;
  width: 100%;
  background: transparent;
  border: none;
  color: var(--text-main);
  font-size: 16.5px;
  line-height: 1.88;
  font-family: inherit;
  resize: vertical;
  outline: none;
  min-height: 360px;
}
.post-foot-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-top: 16px;
  border-top: 1px solid var(--border-subtle);
  margin-top: 16px;
}

/* 微信公众号备选标题栏 */
#wechatTitleBox {
  display: none;
  background: #e6f4ea;
  border: 1px dashed #34a853;
  border-radius: var(--radius-md);
  padding: 12px 16px;
  margin-bottom: 14px;
}
.wechat-title-lbl {
  font-size: 12px;
  font-weight: 700;
  color: #137333;
  margin-bottom: 4px;
}

/* 提取硬核数据行 */
.extracted-nums-box {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 16px 20px;
  margin-bottom: 24px;
}
.ext-lbl {
  font-weight: 600;
  font-size: 13px;
  color: var(--text-main);
  margin-bottom: 10px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.num-tag-item {
  display: inline-block;
  background: #f1f3f4;
  border: 1px solid #dadce0;
  color: #1a73e8;
  padding: 3px 10px;
  border-radius: 6px;
  font-size: 12.5px;
  margin: 3px 6px 3px 0;
  font-family: ui-monospace, SFMono-Regular, monospace;
}

/* 模块 2: 参考的数据源新闻卡片 */
.sources-news-box {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-card);
  padding: 24px;
}
.news-item {
  padding: 14px 0;
  border-bottom: 1px solid var(--border-subtle);
}
.news-item:last-child { border-bottom: none; }
.news-item-head {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  margin-bottom: 6px;
}
.news-src-pill {
  font-weight: 600;
  color: var(--text-main);
  background: #f1f3f4;
  padding: 2px 8px;
  border-radius: 4px;
}
.news-item-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-main);
  line-height: 1.5;
  margin-bottom: 4px;
}

/* 未搜索时的“今日热点参考”区 */
#defaultExploreBox {
  margin-top: 36px;
}
.explore-head {
  font-size: 14.5px;
  font-weight: 700;
  color: var(--text-secondary);
  margin-bottom: 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.explore-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}
.explore-card {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  padding: 14px 16px;
  cursor: pointer;
  transition: all 0.15s;
}
.explore-card:hover {
  border-color: var(--google-blue);
  box-shadow: 0 4px 12px rgba(26, 115, 232, 0.08);
  transform: translateY(-1px);
}
.exp-title { font-weight: 600; font-size: 13.5px; margin-bottom: 6px; line-height: 1.4; color: var(--text-main); }
.exp-meta { font-size: 12px; color: var(--text-tertiary); display: flex; justify-content: space-between; }

/* 模态弹窗: 统一设置中心 */
.modal-overlay {
  display: none;
  position: fixed;
  top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0, 0, 0, 0.35);
  backdrop-filter: blur(8px);
  z-index: 999;
  align-items: center;
  justify-content: center;
}
.modal-overlay.active { display: flex; }
.modal-card {
  background: #fff;
  border-radius: var(--radius-lg);
  width: 90%;
  max-width: 680px;
  max-height: 85vh;
  box-shadow: 0 10px 30px rgba(0, 0, 0, 0.18);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.modal-head {
  padding: 16px 24px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.modal-tab-bar {
  display: inline-flex;
  background: #f1f3f4;
  padding: 3px;
  border-radius: var(--radius-pill);
}
.modal-tab-btn {
  padding: 5px 14px;
  border-radius: var(--radius-pill);
  font-size: 12.5px;
  font-weight: 500;
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.15s;
}
.modal-tab-btn.active {
  background: #fff;
  color: var(--text-main);
  font-weight: 600;
  box-shadow: 0 1px 3px rgba(0,0,0,0.1);
}
.modal-body {
  padding: 24px;
  overflow-y: auto;
  flex: 1;
}
.modal-foot {
  padding: 14px 24px;
  border-top: 1px solid var(--border);
  background: var(--bg-subtle);
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

/* 表单与条目 */
.form-grp { margin-bottom: 14px; }
.form-lbl { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; color: var(--text-main); }
.form-inp {
  width: 100%;
  padding: 9px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 13.5px;
  outline: none;
}
.form-inp:focus { border-color: var(--google-blue); }

.source-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 0;
  border-bottom: 1px solid var(--border-subtle);
}
.source-info { flex: 1; margin-right: 14px; }
.source-name-txt { font-weight: 600; font-size: 13.5px; }
.source-url-txt { font-size: 12px; color: var(--text-secondary); max-width: 380px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 开关 */
.switch { position: relative; display: inline-block; width: 38px; height: 22px; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider {
  position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0;
  background-color: #dadce0; transition: .2s; border-radius: 20px;
}
.slider:before {
  position: absolute; content: ""; height: 16px; width: 16px; left: 3px; bottom: 3px;
  background-color: white; transition: .2s; border-radius: 50%;
}
input:checked + .slider { background-color: var(--apple-green); }
input:checked + .slider:before { transform: translateX(16px); }

#toast {
  position: fixed;
  bottom: 24px;
  right: 24px;
  background: #202124;
  color: #fff;
  padding: 11px 20px;
  border-radius: var(--radius-pill);
  font-size: 13px;
  box-shadow: 0 4px 14px rgba(0,0,0,0.2);
  z-index: 10000;
  display: none;
}
</style>
</head>
<body>

<header>
  <div class="header-inner">
    <button class="btn btn-sm" onclick="openSettingsModal()">⚙️ 设置</button>
  </div>
</header>

<div class="container">
  <!-- ================= 核心：极简 Google / Apple 风格大搜索框 ================= -->
  <div class="hero-box" id="heroBox">
    <h1 class="hero-title">Crypto Posts</h1>

    <!-- 巨幅大搜索框 -->
    <div class="big-search-card">
      <div class="search-icon">
        <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z"/></svg>
      </div>
      <input id="mainSearchInput" class="big-search-input" placeholder="搜索关键词..." autofocus onkeydown="if(event.key==='Enter') executeSearch()">
      <button id="btnSearch" class="big-search-btn" onclick="executeSearch()">搜索</button>
    </div>
  </div>

  <!-- ================= 搜索结果展示区 ================= -->
  <div id="resultsSection">
    <!-- 1. 多平台成文卡片 -->
    <div class="result-card-main">
      <div class="res-card-head">
        <div>
          <span class="res-title-text" id="resTopicTitle">出稿结果</span>
        </div>
        <button class="btn btn-sm btn-primary" onclick="copyResultText()">📋 复制文案</button>
      </div>

      <!-- 平台切换器：页签上方先说明用途 -->
      <div style="font-size:12.5px;color:var(--text-secondary);margin:0 0 8px">下面 5 个页签 = 同一条新闻写成 5 个平台各自的发帖版本（X 要短、公众号是长文、小红书口语化、TG 是频道版）。点页签切换版本，点「复制文案」拿去对应平台手动发布。</div>
      <div id="modeHintBox" style="display:none;font-size:12.5px;color:#8a6d1a;background:#fdf6e3;border:1px solid #e8d48a;border-radius:6px;padding:7px 12px;margin:0 0 10px">当前是「本地模板」模式：句式固定、只会往里填数字和标题。设置环境变量 <b>ZHIPU_API_KEY</b>（智谱开放平台申请）后重启，出稿将走真 AI 大模型，按你的主题重新创作。</div>
      <div class="platform-switch-bar">
        <button class="plat-btn active" onclick="switchPlat('x')">𝕏 (Twitter)</button>
        <button class="plat-btn" onclick="switchPlat('binance')">币安 / Gate 广场</button>
        <button class="plat-btn" onclick="switchPlat('wechat')">微信公众号</button>
        <button class="plat-btn" onclick="switchPlat('xiaohongshu')">小红书</button>
        <button class="plat-btn" onclick="switchPlat('telegram')">Telegram</button>
      </div>

      <!-- 微信公众号备选标题 -->
      <div id="wechatTitleBox">
        <div class="wechat-title-lbl">💡 爆款备选标题：</div>
        <div id="wechatTitleList"></div>
      </div>

      <!-- 成文文本展示与复制 -->
      <div class="post-paper">
        <textarea id="postOutputText" class="post-textarea" placeholder="生成的文案将在此展现..."></textarea>
        <div class="post-foot-bar">
          <span id="charCountLabel" style="font-size:12.5px;color:var(--text-secondary)">字数: 0</span>
          <button class="btn btn-sm btn-primary" onclick="copyResultText()">复制文案</button>
        </div>
      </div>
    </div>

    <!-- 2. 提取的核心数据指标 -->
    <div class="extracted-nums-box" id="extractedNumsBox" style="display:none">
      <div class="ext-lbl">📊 盘面核心量化指标：</div>
      <div id="extractedNumsList"></div>
    </div>

    <!-- 3. 参考的原始数据源事实 -->
    <div class="sources-news-box">
      <div style="font-size:13px;font-weight:600;margin-bottom:10px;color:var(--text-secondary)">参考一手数据源：</div>
      <div id="matchedSourcesList"></div>
    </div>
  </div>
</div>

<!-- ================= 模态弹窗: 统一设置中心 (数据源 + AI配置) ================= -->
<div id="settingsModal" class="modal-overlay">
  <div class="modal-card">
    <div class="modal-head">
      <div style="display:flex;align-items:center;gap:14px">
        <span style="font-weight:700;font-size:15px">⚙️ 系统设置</span>
        <div class="modal-tab-bar">
          <button class="modal-tab-btn active" id="tabBtnSources" onclick="switchSettingsTab('sources')">📡 数据源配置 (<span id="activeSourcesCount">0</span>)</button>
          <button class="modal-tab-btn" id="tabBtnAi" onclick="switchSettingsTab('ai')">🤖 AI 模型配置</button>
        </div>
      </div>
      <span style="cursor:pointer;font-size:18px;color:#5f6368" onclick="closeSettingsModal()">✕</span>
    </div>

    <div class="modal-body">
      <!-- 设置 Tab 1: 数据源配置 -->
      <div id="settingsTabSources">
        <!-- 常用主流源一键加入 -->
        <div style="background:#f8f9fa;border:1px dashed #dadce0;border-radius:10px;padding:14px;margin-bottom:18px">
          <div style="font-weight:600;font-size:13px;margin-bottom:8px">🌟 一键快速启用常用一手资讯源：</div>
          <div style="display:flex;flex-wrap:wrap;gap:6px">
            <span class="hot-chip" onclick="quickAddSource('PANews', 'rss', 'https://www.panewslab.com/rss', '币圈')">+ PANews 快讯</span>
            <span class="hot-chip" onclick="quickAddSource('Odaily星球日报', 'rss', 'https://rss.odaily.news/', '币圈')">+ Odaily 星球日报</span>
            <span class="hot-chip" onclick="quickAddSource('深潮 TechFlow', 'rss', 'https://www.techflowpost.com/rss', '币圈')">+ 深潮 TechFlow</span>
            <span class="hot-chip" onclick="quickAddSource('Blockworks', 'rss', 'https://blockworks.co/feed', '币圈')">+ Blockworks</span>
            <span class="hot-chip" onclick="quickAddSource('东方财富7x24', 'eastmoney', '', '宏观')">+ 东方财富</span>
            <span class="hot-chip" onclick="quickAddSource('新浪财经直播', 'sina_fast', '', '宏观')">+ 新浪财经</span>
            <span class="hot-chip" onclick="quickAddSource('Hacker News', 'hn', '', 'AI')">+ Hacker News</span>
          </div>
        </div>

        <!-- 添加自定义源折叠表单 -->
        <div style="margin-bottom:18px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
            <span style="font-weight:700;font-size:13.5px">自定义新源</span>
            <button class="btn btn-sm btn-primary" onclick="toggleAddForm()">+ 新增 RSS / JSON 接口</button>
          </div>
          <div id="addSourceForm" style="display:none;background:#f8f9fa;padding:16px;border-radius:10px;border:1px solid #dadce0">
            <div class="form-grp">
              <label class="form-lbl">名称</label>
              <input id="addSrcName" class="form-inp" placeholder="如：我的自选博客 / 金色财经快讯">
            </div>
            <div class="form-grp">
              <label class="form-lbl">类型</label>
              <select id="addSrcType" class="form-inp">
                <option value="rss">RSS / Atom 订阅流</option>
                <option value="json">通用 JSON API 接口</option>
                <option value="google_news">Google 新闻搜索</option>
              </select>
            </div>
            <div class="form-grp">
              <label class="form-lbl">URL 地址</label>
              <input id="addSrcUrl" class="form-inp" placeholder="https://...">
            </div>
            <div id="addSrcTestMsg" style="font-size:12.5px;padding:8px 12px;border-radius:6px;display:none;margin-bottom:10px"></div>
            <div style="display:flex;gap:8px;justify-content:flex-end">
              <button class="btn btn-sm" onclick="testAddSource()">🔍 测试连接</button>
              <button class="btn btn-sm btn-primary" onclick="saveNewSource()">保存并启用</button>
            </div>
          </div>
        </div>

        <!-- 已配置数据源列表 -->
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <span style="font-weight:700;font-size:13.5px">已配置的数据源清单</span>
          <span style="font-size:12px;color:#1a73e8;cursor:pointer" onclick="resetDefaults()">恢复默认配置</span>
        </div>
        <div id="sourcesListContainer"></div>
      </div>

      <!-- 设置 Tab 2: AI 模型配置 -->
      <div id="settingsTabAi" style="display:none">
        <div class="form-grp">
          <label class="form-lbl">快速选择服务商</label>
          <select id="aiPresetSelect" class="form-inp" onchange="applyPreset()">
            <option value="deepseek">DeepSeek (推荐 deepseek-chat)</option>
            <option value="zhipu">智谱 GLM (glm-4-flash)</option>
            <option value="openai">OpenAI (GPT-4o-mini)</option>
            <option value="kimi">月之暗面 Kimi</option>
            <option value="ollama">本地 Ollama (完全离线)</option>
            <option value="custom">自定义兼容端点</option>
          </select>
        </div>
        <div class="form-grp">
          <label class="form-lbl">API 密钥 (API Key)</label>
          <input id="aiApiKey" class="form-inp" type="password" placeholder="未配置时将使用高阶去AI味本地模板引擎">
        </div>
        <div class="form-grp">
          <label class="form-lbl">API 端点 (Base URL)</label>
          <input id="aiBaseUrl" class="form-inp" placeholder="https://api.deepseek.com/v1">
        </div>
        <div class="form-grp">
          <label class="form-lbl">模型代号 (Model)</label>
          <input id="aiModel" class="form-inp" placeholder="deepseek-chat">
        </div>
        <div id="aiTestMsg" style="font-size:12.5px;padding:8px 12px;border-radius:6px;display:none;margin-top:10px"></div>
        <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:20px">
          <button class="btn btn-sm" onclick="testAi()">测试连接</button>
          <button class="btn btn-sm btn-primary" onclick="saveAi()">保存 AI 配置</button>
        </div>
      </div>
    </div>

    <div class="modal-foot">
      <button class="btn btn-sm btn-primary" onclick="closeSettingsModal()">完成</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
let currentResults = null;
let currentPlat = 'x';
let allSources = [];
let aiConfigCache = null;

window.addEventListener('DOMContentLoaded', () => {
  loadSourcesCount();
  loadAiConfig();
});

function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => { t.style.display = 'none'; }, 2200);
}

function goHome() {
  document.getElementById('resultsSection').style.display = 'none';
  document.getElementById('heroBox').classList.remove('compact');
  document.getElementById('mainSearchInput').value = '';
}

function searchTopic(kw) {
  document.getElementById('mainSearchInput').value = kw;
  executeSearch();
}

// ---------------- 核心搜索与出稿 ----------------
async function executeSearch() {
  const kw = document.getElementById('mainSearchInput').value.trim();
  if (!kw) {
    toast('请输入关键词');
    return;
  }
  const btn = document.getElementById('btnSearch');
  btn.disabled = true;
  btn.textContent = '出稿中...';

  document.getElementById('heroBox').classList.add('compact');
  const resSec = document.getElementById('resultsSection');
  resSec.style.display = 'block';

  document.getElementById('resTopicTitle').textContent = kw;
  document.getElementById('postOutputText').value = '正在检索一手动态，提取盘面事实与数字并生成成稿...';

  try {
    const res = await fetch('/api/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keyword: kw, template_code: 'auto' })
    });
    const data = await res.json();
    if (data.error) {
      resSec.style.display = 'none';
      document.getElementById('heroBox').classList.remove('compact');
      toast(data.error);
      return;
    }
    const mh = document.getElementById('modeHintBox');
    if (mh) mh.style.display = (data.generated_by === 'llm') ? 'none' : 'block';
    currentResults = data;

    const tmplEl = document.getElementById('resBadgeTmpl');
    if (tmplEl) tmplEl.textContent = `模板 ${data.template_code} (${data.template_name})`;
    const engEl = document.getElementById('resBadgeEngine');
    if (engEl) engEl.textContent = data.generated_by === 'llm' ? 'AI' : '精炼';

    const numsBox = document.getElementById('extractedNumsBox');
    const numsList = document.getElementById('extractedNumsList');
    if (data.extracted_numbers && data.extracted_numbers.length) {
      numsBox.style.display = 'block';
      numsList.innerHTML = data.extracted_numbers.map(n => 
        `<span class="num-tag-item">${escapeHtml(n.num)} (${escapeHtml(n.ctx)})</span>`
      ).join('');
    } else {
      numsBox.style.display = 'none';
    }

    renderCurrentPost();

    const newsList = document.getElementById('matchedSourcesList');
    if (data.sources_used && data.sources_used.length) {
      newsList.innerHTML = data.sources_used.map(s => `
        <div class="news-item">
          <div class="news-item-head">
            <span class="news-src-pill">${escapeHtml(s.src)}</span>
            <span style="color:var(--text-tertiary)">一手数据源</span>
          </div>
          <div class="news-item-title"><a href="${escapeHtml(s.link || '')}" target="_blank">${escapeHtml(s.title)}</a></div>
        </div>
      `).join('');
    } else {
      newsList.innerHTML = '<div style="color:var(--text-secondary);padding:10px 0">围绕该主题未匹配到特定条目，已采用行业通用量化模型生成。</div>';
    }

    toast('稿件生成完成！');
  } catch (e) {
    document.getElementById('postOutputText').value = '生成失败: ' + e;
  } finally {
    btn.disabled = false;
    btn.textContent = '搜索并出稿';
  }
}

function switchPlat(p) {
  currentPlat = p;
  const btns = document.querySelectorAll('.plat-btn');
  btns.forEach(b => b.classList.remove('active'));
  const map = { 'x': 0, 'binance': 1, 'wechat': 2, 'xiaohongshu': 3, 'telegram': 4 };
  if (btns[map[p]]) btns[map[p]].classList.add('active');
  renderCurrentPost();
}

function renderCurrentPost() {
  if (!currentResults || !currentResults.posts) return;
  const post = currentResults.posts[currentPlat];
  const textarea = document.getElementById('postOutputText');
  const countEl = document.getElementById('charCountLabel');
  const wxBox = document.getElementById('wechatTitleBox');

  if (currentPlat === 'wechat' && post && post.title_candidates) {
    wxBox.style.display = 'block';
    document.getElementById('wechatTitleList').innerHTML = post.title_candidates.map(t => `<div style="margin:2px 0">• ${escapeHtml(t)}</div>`).join('');
  } else {
    wxBox.style.display = 'none';
  }

  if (post) {
    textarea.value = post.content || '';
    countEl.textContent = `字数: ${textarea.value.length}`;
  } else {
    textarea.value = '暂无该平台稿件';
    countEl.textContent = '字数: 0';
  }
}

document.getElementById('postOutputText').addEventListener('input', (e) => {
  document.getElementById('charCountLabel').textContent = `字数: ${e.target.value.length}`;
  if (currentResults && currentResults.posts && currentResults.posts[currentPlat]) {
    currentResults.posts[currentPlat].content = e.target.value;
  }
});

function copyResultText() {
  const t = document.getElementById('postOutputText').value;
  if (!t) return;
  copyText(t);
  toast('已复制当前平台文案！直接粘贴发布');
}

// ---------------- 今日探索推荐 ----------------
async function loadExploreHot() {
  const grid = document.getElementById('exploreGrid');
  grid.innerHTML = '<div style="color:var(--text-secondary)">正在聚合全网热点...</div>';
  try {
    const res = await fetch('/api/hot');
    const d = await res.json();
    grid.innerHTML = (d.topics || []).slice(0, 6).map(t => `
      <div class="explore-card" onclick="searchTopic('${escapeJs(t.title)}')">
        <div class="exp-title">${escapeHtml(t.title)}</div>
        <div class="exp-meta">
          <span style="color:#1a73e8;font-weight:600">热度 ${Math.round(t.heat)}</span>
          <span>${(t.board_names || []).join(' / ')}</span>
        </div>
      </div>
    `).join('');
  } catch (e) {
    grid.innerHTML = '<div style="color:var(--text-secondary)">暂无法加载热点</div>';
  }
}

// ---------------- 统一设置中心 (数据源 + AI) ----------------
async function loadSourcesCount() {
  try {
    const res = await fetch('/api/sources');
    const d = await res.json();
    allSources = d.sources || [];
    const active = allSources.filter(x => x.enabled).length;
    document.getElementById('activeSourcesCount').textContent = active;
  } catch (e) {}
}

function openSettingsModal(tab = 'sources') {
  renderSourcesList();
  switchSettingsTab(tab);
  document.getElementById('settingsModal').classList.add('active');
}
function closeSettingsModal() {
  document.getElementById('settingsModal').classList.remove('active');
  loadSourcesCount();
}
function switchSettingsTab(tab) {
  const isSources = tab === 'sources';
  document.getElementById('settingsTabSources').style.display = isSources ? 'block' : 'none';
  document.getElementById('settingsTabAi').style.display = isSources ? 'none' : 'block';
  document.getElementById('tabBtnSources').className = 'modal-tab-btn ' + (isSources ? 'active' : '');
  document.getElementById('tabBtnAi').className = 'modal-tab-btn ' + (isSources ? '' : 'active');
}

function renderSourcesList() {
  const c = document.getElementById('sourcesListContainer');
  c.innerHTML = allSources.map(s => `
    <div class="source-row">
      <div class="source-info">
        <div class="source-name-txt">${escapeHtml(s.name)} <span style="font-size:11px;color:#1a73e8;background:#e8f0fe;padding:1px 6px;border-radius:4px">${escapeHtml(s.type)}</span></div>
        <div class="source-url-txt">${escapeHtml(s.url || s.note || '系统预设数据源')}</div>
      </div>
      <div style="display:flex;align-items:center;gap:12px">
        <label class="switch">
          <input type="checkbox" ${s.enabled ? 'checked' : ''} onchange="toggleSrc('${s.id}', this.checked)">
          <span class="slider"></span>
        </label>
        <span style="color:#e11d48;font-size:12px;cursor:pointer" onclick="deleteSrc('${s.id}')">删除</span>
      </div>
    </div>
  `).join('');
}

async function toggleSrc(id, enabled) {
  await fetch('/api/sources/toggle', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, enabled })
  });
  const s = allSources.find(x => x.id === id);
  if (s) s.enabled = enabled;
  loadSourcesCount();
}

async function deleteSrc(id) {
  if (!confirm('确定删除该数据源？')) return;
  await fetch('/api/sources/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id })
  });
  allSources = allSources.filter(x => x.id !== id);
  renderSourcesList();
  loadSourcesCount();
  toast('已删除！');
}

async function quickAddSource(name, type, url, category) {
  const payload = { name, type, url, category, enabled: true };
  await fetch('/api/sources/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  const res = await fetch('/api/sources');
  const d = await res.json();
  allSources = d.sources || [];
  renderSourcesList();
  loadSourcesCount();
  toast(`已启用：${name}`);
}

function toggleAddForm() {
  const f = document.getElementById('addSourceForm');
  f.style.display = f.style.display === 'none' ? 'block' : 'none';
}

async function testAddSource() {
  const msg = document.getElementById('addSrcTestMsg');
  msg.style.display = 'block';
  msg.style.background = '#f1f3f4';
  msg.style.color = '#5f6368';
  msg.textContent = '⏳ 正在发起网络探测...';

  const res = await fetch('/api/sources/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      name: document.getElementById('addSrcName').value,
      type: document.getElementById('addSrcType').value,
      url: document.getElementById('addSrcUrl').value
    })
  });
  const d = await res.json();
  if (d.success) {
    msg.style.background = '#e6f4ea';
    msg.style.color = '#137333';
    msg.textContent = `✅ 连接成功 (${d.latency_ms}ms)！解析出 ${d.count} 条记录。`;
  } else {
    msg.style.background = '#fce8e6';
    msg.style.color = '#c5221f';
    msg.textContent = `❌ 测试失败: ${d.error || '无法解析'}`;
  }
}

async function saveNewSource() {
  const name = document.getElementById('addSrcName').value.trim();
  const type = document.getElementById('addSrcType').value;
  const url = document.getElementById('addSrcUrl').value.trim();
  if (!name) { toast('请填写名称'); return; }

  await fetch('/api/sources/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, type, url, category: '自定义', enabled: true })
  });
  toggleAddForm();
  const res = await fetch('/api/sources');
  const d = await res.json();
  allSources = d.sources || [];
  renderSourcesList();
  loadSourcesCount();
  toast('已添加自定义数据源！');
}

async function resetDefaults() {
  if (!confirm('重置为系统预设推荐源？')) return;
  await fetch('/api/sources/reset', { method: 'POST' });
  const res = await fetch('/api/sources');
  const d = await res.json();
  allSources = d.sources || [];
  renderSourcesList();
  loadSourcesCount();
  toast('已恢复为默认源！');
}

// ---------------- AI 模型设置 ----------------
async function loadAiConfig() {
  try {
    const res = await fetch('/api/config');
    aiConfigCache = await res.json();
    if (aiConfigCache && aiConfigCache.ai) {
      document.getElementById('aiPresetSelect').value = aiConfigCache.ai.provider || 'deepseek';
      document.getElementById('aiBaseUrl').value = aiConfigCache.ai.base_url || 'https://api.deepseek.com/v1';
      document.getElementById('aiModel').value = aiConfigCache.ai.model || 'deepseek-chat';
      document.getElementById('aiApiKey').placeholder = aiConfigCache.ai.masked_key ? `已配置 (${aiConfigCache.ai.masked_key})` : '未配置时将使用高阶去AI味本地模板引擎';
    }
  } catch (e) {}
}

function applyPreset() {
  const p = document.getElementById('aiPresetSelect').value;
  if (aiConfigCache && aiConfigCache.preset_providers && aiConfigCache.preset_providers[p]) {
    const info = aiConfigCache.preset_providers[p];
    document.getElementById('aiBaseUrl').value = info.base_url;
    document.getElementById('aiModel').value = info.model;
  }
}

async function testAi() {
  const msg = document.getElementById('aiTestMsg');
  msg.style.display = 'block';
  msg.style.background = '#f1f3f4';
  msg.style.color = '#5f6368';
  msg.textContent = '⏳ 测试连接中...';

  const res = await fetch('/api/config/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      api_key: document.getElementById('aiApiKey').value,
      base_url: document.getElementById('aiBaseUrl').value,
      model: document.getElementById('aiModel').value
    })
  });
  const d = await res.json();
  if (d.success) {
    msg.style.background = '#e6f4ea';
    msg.style.color = '#137333';
    msg.textContent = '✅ ' + d.message;
  } else {
    msg.style.background = '#fce8e6';
    msg.style.color = '#c5221f';
    msg.textContent = '❌ ' + d.message;
  }
}

async function saveAi() {
  const key = document.getElementById('aiApiKey').value.trim();
  const payload = {
    provider: document.getElementById('aiPresetSelect').value,
    base_url: document.getElementById('aiBaseUrl').value.trim(),
    model: document.getElementById('aiModel').value.trim()
  };
  if (key) payload.api_key = key;

  await fetch('/api/config/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  loadAiConfig();
  toast('AI 配置已更新！');
}

// ---------------- 辅助函数 ----------------
function escapeHtml(s) {
  if (!s) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function escapeJs(s) {
  if (!s) return '';
  return String(s).replace(/\\\\/g, '\\\\\\\\').replace(/'/g, "\\\\'").replace(/"/g, '&quot;').replace(/\\n/g, ' ');
}
function copyText(t) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(t).then(() => toast('已复制到剪贴板！'));
  } else {
    const ta = document.createElement('textarea');
    ta.value = t;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    toast('已复制到剪贴板！');
  }
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------- HTTP 请求处理器

class Handler(BaseHTTPRequestHandler):
    def reply_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def reply_html(self, html_text, code=200):
        body = html_text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def read_post_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        content_type = self.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                return json.loads(raw)
            except Exception:
                return {}
        else:
            parsed = parse_qs(raw)
            return {k: v[0] if len(v) == 1 else v for k, v in parsed.items()}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/feed":
            cat = qs.get("cat", [""])[0]
            force = bool(qs.get("force", [""])[0])
            items, statuses = sc.fetch_aggregated_feed(category=cat, force_refresh=force)
            return self.reply_json({"items": items, "statuses": statuses})

        if path == "/api/hot":
            cat = qs.get("cat", [""])[0]
            return self.reply_json(hr.get_hot_topics(cat_filter=cat))

        if path == "/api/sources":
            return self.reply_json(sc.load_sources())

        if path == "/api/config":
            return self.reply_json(config.get_safe_config())

        if path == "/api/days":
            return self.reply_json({"days": list_days()})

        if path in ("/", "/index.html"):
            return self.reply_html(APP_HTML)

        m = re.fullmatch(r"/(\d{4}-\d{2}-\d{2})", path)
        if m:
            date = m.group(1)
            days = dict(list_days())
            if date in days:
                files = days[date]
                daily_md = read_file(files.get("daily", ""))
                posts_md = read_file(files.get("posts", ""))
                daily_html = rs.render_blocks(rs.parse_blocks(daily_md)) if daily_md else "<p>无日报</p>"
                posts_html = rs.render_blocks(rs.parse_blocks(posts_md)) if posts_md else "<p>无成品稿</p>"
                full_page = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
                <title>{date} 日报与稿件</title><style>{rs.CSS}</style></head><body>
                <p><a href="/">← 返回搜索</a></p>
                <h1>{date} 发布稿件</h1>{posts_html}
                <hr><h1>{date} 素材日报</h1>{daily_html}
                </body></html>"""
                return self.reply_html(full_page)

        if path == "/archive":
            days = list_days()
            items_li = "".join([f'<li><a href="/{d}">{d}</a></li>' for d, _ in days])
            page = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>日报存档</title>
            <style>{rs.CSS}</style></head><body><p><a href="/">← 返回搜索</a></p>
            <h1>历史存档</h1><ul>{items_li}</ul></body></html>"""
            return self.reply_html(page)

        self.reply_html("<h1>404 Not Found</h1><p><a href='/'>返回首页</a></p>", code=404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self.read_post_body()

        if path == "/api/sources/save":
            saved = sc.save_source_item(body)
            return self.reply_json({"success": True, "source": saved})

        if path == "/api/sources/toggle":
            src_id = body.get("id")
            en = body.get("enabled")
            ok, cur = sc.toggle_source(src_id, en)
            return self.reply_json({"success": ok, "enabled": cur})

        if path == "/api/sources/delete":
            ok = sc.delete_source(body.get("id"))
            return self.reply_json({"success": ok})

        if path == "/api/sources/test":
            res = sc.test_source(
                src_type=body.get("type", "rss"),
                url=body.get("url", ""),
                name=body.get("name", "测试源"),
                subs=body.get("subs")
            )
            return self.reply_json(res)

        if path == "/api/sources/reset":
            sc.reset_sources_to_default()
            return self.reply_json({"success": True})

        if path == "/api/config/save":
            cfg = config.load_config()
            cfg["ai"]["provider"] = body.get("provider", cfg["ai"]["provider"])
            cfg["ai"]["base_url"] = body.get("base_url", cfg["ai"]["base_url"])
            cfg["ai"]["model"] = body.get("model", cfg["ai"]["model"])
            if body.get("api_key"):
                cfg["ai"]["api_key"] = body["api_key"]
            config.save_config(cfg)
            return self.reply_json({"success": True})

        if path == "/api/config/test":
            key = body.get("api_key") or config.load_config().get("ai", {}).get("api_key")
            ok, msg = config.test_ai_connection(
                api_key=key,
                base_url=body.get("base_url"),
                model=body.get("model")
            )
            return self.reply_json({"success": ok, "message": msg})

        if path == "/api/generate":
            kw = body.get("keyword", "")
            src_filter = body.get("sources_filter", "全部")
            tmpl_code = body.get("template_code", "auto")
            manual = body.get("manual_materials", "")
            try:
                res = aig.generate_posts(
                    keyword=kw,
                    sources_filter=src_filter,
                    template_code=tmpl_code,
                    manual_materials=manual
                )
            except ValueError as e:
                return self.reply_json({"error": str(e)})
            return self.reply_json(res)

        if path == "/api/save_post":
            ok, target_path, item_num = aig.save_post_to_daily(body)
            return self.reply_json({"success": ok, "path": target_path, "item_num": item_num})

        self.reply_json({"error": "Unknown endpoint"}, code=404)

    def log_message(self, *args):
        pass


def main():
    print(f"=========================================================")
    print(f"⚡ 素材出稿雷达 已启动")
    print(f"📍 本地访问地址: http://localhost:{PORT}")
    print(f"💡 采用 Google 极简大搜索框交互，统一设置中心已整合")
    print(f"=========================================================")
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()
