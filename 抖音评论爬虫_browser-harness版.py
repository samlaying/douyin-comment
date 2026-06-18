#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抖音评论采集 — browser-harness (CDP) 版（含回复 / 带层级）

给一个抖音视频链接，自动跑完整流程：
    打开页面 → 真实 PageDown 加载评论（滚到“暂时没有更多评论”为止）
    → 真实点击“展开N条回复 / 展开更多” → 区分一级/二级 → 存 CSV

用法：
    python3 抖音评论爬虫_browser-harness版.py <视频链接>
    python3 抖音评论爬虫_browser-harness版.py             # 运行时再输入链接

链接支持两种形式（自动归一成 /video/{id}）：
    https://www.douyin.com/video/7641230262990556479
    搜索页带 modal_id：...?...&modal_id=7641230262990556479&type=general

前提：
    1. 已安装 browser-harness（uv tool install -e ~/browser-harness）
    2. 已连上真实 Chrome（chrome://inspect 勾选“允许远程调试”）
    3. 抖音已登录

输出：crawled_comments/{标题}_{时间戳}_with_replies.csv（UTF-8 BOM）
      层级 1 = 顶层评论，层级 2 = 回复

关键经验（已固化）：
    - 抖音反爬：评论懒加载只认【真实输入事件】。程序改 scrollTop / 合成 wheel 都不触发；
      必须用 press_key("PageDown")（真实按键）滚动，且【每轮重新聚焦容器】防失焦卡住。
    - 展开回复也必须真实点击（click_at_xy），JS .click() 无效。
    - 停止条件用页面里的“暂时没有更多评论”标记（抖音即便显示总数，也常只放出 ~100 条就给此标记）。
    - 每个 js() 必须以 `return <基本类型>` 结尾，否则 CDP returnByValue 序列化 DOM 报错。
    - browser-harness 装在隔离 venv，普通 python 无法 import，所以本脚本是薄壳：
      解析链接 → 设环境变量 → 把下面 PROGRAM pipe 给 `browser-harness` CLI。
"""

import os
import sys
import shutil
import subprocess

BH = shutil.which("browser-harness") or os.path.expanduser("~/.local/bin/browser-harness")

PROGRAM = r'''
import os, re, csv, time, datetime, random

URL = os.environ["DOUYIN_URL"]
OUT_DIR = os.environ["DOUYIN_OUT_DIR"]
os.makedirs(OUT_DIR, exist_ok=True)

SCROLL_MAX    = 500   # PageDown 上限
STALL_LIMIT   = 8     # 连续 N 次无增长 → 停（无标记也进入下一步）
EXPAND_MAX    = 120   # 最多点击展开按钮的次数（含“展开更多”）

# 节奏（防风控：偏慢 + 随机抖动，贴近真人操作，避免触发风控）
PD_INTERVAL = (0.35, 0.6)   # PageDown 之间的随机间隔
ROUND_WAIT  = (1.2, 1.8)    # 每轮（一批 PageDown）之后等待
EXPAND_WAIT = (1.5, 2.2)    # 每次展开点击之后等待
def jitter(lo, hi): return random.uniform(lo, hi)

# ── JS 片段（每段都以 return 基本类型结尾）──────────────────────────────
HYDRATED_JS = r"""
var og=(document.querySelector('meta[property="og:title"]')||{}).content||"";
var t=(document.title||"").replace(/[^0-9A-Za-z一-鿿]/g,"");
return (og.length>2||t.length>2)?1:0;
"""
LOCK_JS = r"""
window.__cc=null;
var rs=document.querySelectorAll(".route-scroll-container");
for(var i=0;i<rs.length;i++){if(rs[i].querySelector('[data-e2e="comment-item"]')){window.__cc=rs[i];break;}}
return !!window.__cc;
"""
FOCUS_JS = 'if(window.__cc){window.__cc.setAttribute("tabindex","-1");window.__cc.focus();} return window.__cc?Math.round(window.__cc.scrollTop):-1;'
COUNT_JS  = 'return document.querySelectorAll("[data-e2e=comment-item]").length;'
MARKER_JS = 'return (document.body.innerText.indexOf("暂时没有更多评论")>=0)?1:0;'

# 找下一个未展开按钮的【叶子文本元素】（不是 756px 宽的整行 wrapper）。
# 点过的打 data-bh-tried 标记 → 无论成功失败下次都跳过，保证逐个推进、不卡在死按钮上。
FINDBTN_JS = r"""
var all=document.querySelectorAll('span,div,button,a,p');
var bestEl=null, bestArea=Infinity;
for(var i=0;i<all.length;i++){
  var el=all[i];
  if(el.getAttribute && el.getAttribute('data-bh-tried')) continue;
  var t=(el.textContent||"").replace(/\s+/g," ").trim();
  if(t==="展开更多" || /^展开\d+条回复$/.test(t)){
    var r=el.getBoundingClientRect();
    var area=r.width*r.height;
    if(r.width>0 && r.width<300 && r.height<60 && area<bestArea){ bestArea=area; bestEl=el; }
  }
}
if(!bestEl) return null;
bestEl.scrollIntoView({block:"center"});
bestEl.setAttribute('data-bh-tried','1');
var rr=bestEl.getBoundingClientRect();
return (rr.width<2 || rr.height<2) ? {gone:1} : {x:rr.x, y:rr.y, w:rr.width, h:rr.height};
"""

# 提取全部评论，层级=是否有 comment-item 祖先（嵌套=2 否则=1）
EXTRACT_JS = r"""
var items=document.querySelectorAll('[data-e2e="comment-item"]');
var out=[];var seen={};
items.forEach(function(it,idx){
  try{
    var id=it.getAttribute("data-id")||("c_"+idx);
    if(seen[id])return;seen[id]=1;
    var p=it.parentElement,lvl=1;
    while(p){if(p.getAttribute&&p.getAttribute("data-e2e")==="comment-item"){lvl=2;break;} p=p.parentElement;}
    var nickEl=it.querySelector('[data-click-from="title"]');
    var nickname=nickEl?nickEl.textContent.replace(/\s+/g," ").trim():"";
    var ua=it.querySelector('a[href*="/user/"]');
    var userLink=ua?ua.getAttribute("href"):"";
    if(userLink&&userLink.indexOf("//")===0)userLink="https:"+userLink;
    var infoWrap=it.querySelector(".comment-item-info-wrap");
    var node=infoWrap?infoWrap.nextElementSibling:null;
    var content=node?node.innerText.replace(/\s+/g," ").trim():"";
    var timeNode=node?node.nextElementSibling:null;var timeText="",ip="";
    if(timeNode){var t=timeNode.innerText.replace(/\s+/g," ").trim();var pp=t.split("·");timeText=(pp[0]||"").trim();ip=pp[1]?(pp[1].trim()):"";}
    var stats=it.querySelector(".comment-item-stats-container");var likes="0";
    if(stats){var pe=stats.querySelector("p");if(pe){var m=pe.innerText.replace(/\s+/g,"").match(/\d+/);likes=m?m[0]:"0";}}
    if(content)out.push({id:id,level:lvl,nickname:nickname,userLink:userLink,content:content,time:timeText,ip:ip,likes:likes});
  }catch(e){}
});
var title=(document.querySelector('meta[property="og:title"]')||{}).content||document.title||"";
title=title.replace(/\s*[-–—]\s*抖音.*$/,"").trim();
return {comments: out, title: title};
"""


def resolve_video_url(raw):
    # modal_id 优先：搜索页 URL 里 modal_id 才是要看的视频
    # （URL 可能同时含 /video/{别的id}/search/...&modal_id={真视频}）
    m = re.search(r"[?&]modal_id=(\d+)", raw)
    if m: return "https://www.douyin.com/video/" + m.group(1)
    m = re.search(r"/video/(\d+)", raw)
    if m: return "https://www.douyin.com/video/" + m.group(1)
    m = re.search(r"(\d{15,})(?:$|[?&/])", raw)
    if m: return "https://www.douyin.com/video/" + m.group(1)
    return raw


def wait_for(pred, timeout, step=0.7):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred(): return True
        time.sleep(step)
    return pred()


print("[1/5] daemon + 真实标签页")
ensure_daemon()
ensure_real_tab()

target = resolve_video_url(URL)
print("[2/5] 打开视频页：" + target)
goto_url(target)
wait_for_load()
wait_for(lambda: js(HYDRATED_JS), 40)
if not js(HYDRATED_JS):
    print("  未水合，刷新一次 …")
    js("location.reload(); return 1")
    wait_for_load()
    wait_for(lambda: js(HYDRATED_JS), 20)
wait_for(lambda: js(COUNT_JS) > 0, 25, 0.5)   # 等首批评论渲染
time.sleep(1.0)
js(LOCK_JS)

print("[3/5] PageDown 加载评论（到'暂时没有更多评论'为止）")
last = 0; stall = 0; hit = False
for rnd in range(60):
    js(LOCK_JS)               # 每轮重新锁定容器（DOM 重挂后旧引用会失效）
    js(FOCUS_JS)
    for _ in range(6):        # 一轮按若干下到底
        press_key("PageDown")
        time.sleep(jitter(*PD_INTERVAL))
    time.sleep(jitter(*ROUND_WAIT))   # 等这一批懒加载渲染
    if js(MARKER_JS):
        hit = True
        print("  到达'暂时没有更多评论'（round=%d）" % rnd)
        break
    c = js(COUNT_JS)
    if c == last:
        stall += 1
    else:
        stall = 0; last = c
    if stall >= 5:
        print("  加载停滞（count=%d），进入展开步骤" % last)
        break
print("  当前已加载评论：%d 条%s" % (js(COUNT_JS), "（已到底）" if hit else ""))

print("[4/5] 展开回复（真实点击 展开 / 展开更多）")
clicked = 0
for _ in range(EXPAND_MAX):
    rect = js(FINDBTN_JS)         # 每个按钮只点一次（FINDBTN 内打 data-bh-tried 标记）
    if not rect or rect.get("gone"):
        break
    cx = int(rect["x"] + rect["w"] / 2); cy = int(rect["y"] + rect["h"] / 2)
    if cx <= 0 or cy <= 0:
        break
    # 必须先 mouseMoved（hover）再 press/release —— 抖音按钮要 hover 才响应，
    # click_at_xy 不发 hover 所以经常点空。这里手动发完整三事件。
    cdp("Input.dispatchMouseEvent", type="mouseMoved", x=cx, y=cy)
    cdp("Input.dispatchMouseEvent", type="mousePressed", x=cx, y=cy, button="left", clickCount=1)
    cdp("Input.dispatchMouseEvent", type="mouseReleased", x=cx, y=cy, button="left", clickCount=1)
    clicked += 1
    time.sleep(jitter(*EXPAND_WAIT))   # 等回复渲染，并放慢节奏避免触发风控
    if clicked % 20 == 0:
        print("  已展开 %d 个，当前评论数 %d" % (clicked, js(COUNT_JS)))
print("  展开完成：展开 %d 个，当前 %d 条" % (clicked, js(COUNT_JS)))

print("[5/5] 提取 + 保存（带层级）")
data = js(EXTRACT_JS)
comments = data["comments"]
l1 = sum(1 for c in comments if c["level"] == 1)
l2 = sum(1 for c in comments if c["level"] == 2)
title = data.get("title") or "douyin_video"
title = re.sub(r"^[^0-9A-Za-z一-鿿]+", "", title)         # 去开头 emoji
safe = re.sub(r"[<>:\"/\\|?*\s]+", "_", title).strip("_")[:40] or "douyin_video"
ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
fpath = os.path.join(OUT_DIR, safe + "_" + ts + "_with_replies.csv")

cols = ["评论ID", "层级", "昵称", "地区", "时间", "评论内容", "点赞数", "回复数", "用户链接"]
with open(fpath, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=cols)
    w.writeheader()
    for c in comments:
        w.writerow({
            "评论ID": c["id"], "层级": c["level"], "昵称": c["nickname"],
            "地区": c["ip"], "时间": c["time"], "评论内容": c["content"],
            "点赞数": c["likes"], "回复数": "", "用户链接": c["userLink"],
        })
print("")
print("✅ 完成：共 %d 条（一级 %d / 二级 %d）→ %s" % (len(comments), l1, l2, fpath))
'''


def main():
    if not os.path.exists(BH):
        sys.exit("找不到 browser-harness，请先安装：uv tool install -e ~/browser-harness")
    url = (sys.argv[1] if len(sys.argv) > 1 else input("视频链接：")).strip()
    if not url:
        sys.exit("未提供链接")
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "crawled_comments")
    env = dict(os.environ, DOUYIN_URL=url, DOUYIN_OUT_DIR=out_dir)
    subprocess.run([BH], input=PROGRAM, text=True, env=env)


if __name__ == "__main__":
    main()
