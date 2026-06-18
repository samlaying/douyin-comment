#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小红书评论采集 — browser-harness (CDP) 版（含回复 / 带层级）

给一个搜索关键词，自动跑完整流程：
    打开搜索页 → 点击前 N 个笔记卡片（必须 click，直接 goto /explore/{id} 会被风控 404）
    → 滚动加载全部评论 → 点击“展开N条回复 / 展开更多回复 / 更多” → 区分一级/二级 → 存 CSV

用法：
    python3 小红书评论爬虫_browser-harness版.py <关键词> [数量]
    python3 小红书评论爬虫_browser-harness版.py ai产品面经 5
    python3 小红书评论爬虫_browser-harness版.py             # 默认 ai产品面经 / 前5个

前提：
    1. 已安装 browser-harness（uv tool install -e ~/browser-harness）
    2. 已连上真实 Chrome（chrome://inspect 勾选“允许远程调试”）
    3. 小红书已登录（评论默认登录可见）

输出：crawled_comments/小红书_{关键词}_{时间戳}.csv（UTF-8 BOM）
      层级 1 = 顶层评论，层级 2 = 回复（reply-container 内）

关键经验（已固化）：
    - 反爬关键：笔记必须从【搜索结果页点击卡片】打开，URL 才带 xsec_token；
      直接 goto /explore/{id} 会 404（error_code=300031）。所以全程 click，不跳转。
    - 小红书评论容器 .note-scroller 用 IntersectionObserver 懒加载：直接设 scrollTop 就能
      加载更多（这点和抖音不同，抖音只认真实按键）。评论区底部有 `- THE END -` 结束标记（
      .end-container，等价于抖音“暂时没有更多评论”），但它常驻 DOM，所以脚本仍靠【连续 5 轮
      数量不变】判定到底（到底即 stall、同时 THE END 进入视图），二者等价。
    - 回复分批加载：点“展开N条回复”先出 ~5 条，再点“展开更多回复”逐批拉满；
      一级/二级靠【祖先有无 .reply-container】区分（回复在 reply-container 里，不是嵌在
      comment-item 里）。
    - 展开按钮（show-more 311×32）需先 mouseMoved(hover) 再 press/release 才响应，
      click_at_xy 不发 hover 会点空（详见 memory）。
    - 每个 js() 以 `return <基本类型>` 结尾；browser-harness 装在隔离 venv，本脚本是薄壳：
      解析参数 → 设环境变量 → 把下面 PROGRAM pipe 给 `browser-harness` CLI（每篇笔记一次，
      短调用，避免长会话 CDP 掉线）。
"""

import os, sys, json, csv, shutil, subprocess, datetime, glob
from urllib.parse import quote

BH = shutil.which("browser-harness") or os.path.expanduser("~/.local/bin/browser-harness")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(SCRIPT_DIR, "crawled_comments")
RAW_DIR = os.path.join(OUT_DIR, "_xhs_raw")

DEFAULT_KEYWORD = "ai产品面经"
DEFAULT_N = 5
SEARCH_URL_TPL = "https://www.xiaohongshu.com/search_result?keyword=%s&source=web_explore_feed"

# ── 阶段一：取前 N 个笔记卡片 ID（一次 browser-harness 调用）────────────────────
COLLECT_PROGRAM = r'''
import os, json, time
ensure_daemon(); ensure_real_tab()
SEARCH = os.environ["XHS_SEARCH_URL"]; N = int(os.environ["XHS_N"])
safe_js('window.location.href=%s;return 1;' % json.dumps(SEARCH))
time.sleep(2.5)
try: wait_for_load()
except Exception: pass
time.sleep(1.5)
# 滚几下让卡片渲染出来（搜索结果是瀑布流）
for _ in range(4):
    try: js("window.scrollBy(0,1200);return 1;")
    except Exception: pass
    time.sleep(0.8)
ids = js(r'''
var as=document.querySelectorAll('a[href*="/explore/"]');
var seen={},out=[];
for(var i=0;i<as.length;i++){
  var m=/\/explore\/([a-z0-9]+)/i.exec(as[i].href);
  if(!m||seen[m[1]])continue; seen[m[1]]=1;
  out.push(m[1]);
}
return out;
''')
ids = (ids or [])[:N]
print("IDS_JSON:" + json.dumps(ids, ensure_ascii=False))
'''

# ── 阶段二：单篇笔记采集（每篇一次 browser-harness 调用）────────────────────────
NOTE_PROGRAM = r'''
import os, time, random, json
ensure_daemon(); ensure_real_tab()
def jitter(a,b): return random.uniform(a,b)
def safe_js(s):
    try: return js(s)
    except Exception:
        ensure_daemon(); ensure_real_tab(); time.sleep(1.0)
        try: return js(s)
        except Exception as ex: print("JSERR",ex); return None
def safe_cdp(method,**kw):
    try: return cdp(method,**kw)
    except Exception:
        ensure_daemon(); ensure_real_tab(); time.sleep(1.0)
        try: return cdp(method,**kw)
        except Exception as ex: print("CDPERR",ex)

NID   = os.environ["XHS_NOTE_ID"]
SEARCH= os.environ["XHS_SEARCH_URL"]
OUT   = os.environ["XHS_OUT"]
IDX   = os.environ["XHS_NOTE_IDX"]

# 1) 打开搜索页（每篇都回到搜索，保证卡片带 token）
safe_js('window.location.href=%s;return 1;' % json.dumps(SEARCH))
time.sleep(2.5)
try: wait_for_load()
except Exception: ensure_daemon(); ensure_real_tab()
time.sleep(1.5)

# 2) 点击该笔记卡片（必须 click，不能 goto）
clicked=False
for _attempt in range(3):
    rect=safe_js(('''var a=document.querySelector('a[href*="/explore/IDID"]');
if(!a) return null;
var card=a,p=a;
for(var i=0;i<6&&p;i++){var r=p.getBoundingClientRect();if(r.width>100&&r.height>100){card=p;break;}p=p.parentElement;}
card.scrollIntoView({block:"center"});
var rr=card.getBoundingClientRect();
return {x:Math.round(rr.x),y:Math.round(rr.y),w:Math.round(rr.width),h:Math.round(rr.height)};''').replace("IDID",NID))
    if rect:
        cx=int(rect["x"]+rect["w"]/2); cy=int(rect["y"]+rect["h"]/2)
        safe_cdp("Input.dispatchMouseEvent",type="mouseMoved",x=cx,y=cy); time.sleep(0.4)
        safe_cdp("Input.dispatchMouseEvent",type="mousePressed",x=cx,y=cy,button="left",clickCount=1)
        safe_cdp("Input.dispatchMouseEvent",type="mouseReleased",x=cx,y=cy,button="left",clickCount=1)
        time.sleep(2.5)
        if safe_js('return document.querySelector(".note-detail-mask")?1:0;'): clicked=True; break
    time.sleep(1.5)
print("[%s] clicked=%s" % (IDX,clicked))

# 3) 等首批评论
for _ in range(30):
    if (safe_js('return document.querySelectorAll(".comment-item").length;') or 0)>0: break
    time.sleep(0.7)

# 4) 滚动 .note-scroller 加载全部一级评论（靠 stall 判定，无文字标记）
last=0; stall=0
for _ in range(60):
    safe_js('var s=document.querySelector(".note-scroller");if(s){s.scrollTop=s.scrollHeight;}return 1;')
    time.sleep(jitter(1.0,1.4))
    c=safe_js('return document.querySelectorAll(".comment-item").length;') or 0
    if c==last: stall+=1
    else: stall=0; last=c
    if stall>=5: break
print("[%s] loaded base=%d" % (IDX,last))

# 5) 展开引擎：点击 展开N条回复 / 展开更多回复 / 更多（hover-aware，时间盒 130s）
PICK=r'''
function inComment(e){var p=e.parentElement;while(p){if(p.classList&&p.classList.contains("comment-item"))return true;p=p.parentElement;}return false;}
function notSkipped(e){var p=e;while(p){if(p.getAttribute&&p.getAttribute("data-bh-skip"))return false;p=p.parentElement;}return true;}
var best=null,bestY=Infinity;var all=document.querySelectorAll("span,div,a,p");
for(var i=0;i<all.length;i++){var e=all[i];if(!notSkipped(e))continue;var t=(e.textContent||"").replace(/\s+/g,"").trim();if(!t||t.length>16)continue;var type=null;
 if(/^展开\d+条回复$/.test(t)||/^查看\d+条回复$/.test(t))type="reply-open";else if(t==="展开更多回复")type="reply-more";else if((t==="更多"||t==="展开")&&inComment(e))type="text";
 if(!type)continue;var r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)continue;if(r.y<bestY){bestY=r.y;best={type:type,t:t,_el:e};}}
if(!best)return null;best._el.scrollIntoView({block:"center"});var rr=best._el.getBoundingClientRect();return{type:best.type,t:best.t,x:Math.round(rr.x),y:Math.round(rr.y),w:Math.round(rr.width),h:Math.round(rr.height)};'''
def skip_text(t):
    safe_js(('''var w=%s;var all=document.querySelectorAll("span,div,a,p");
for(var i=0;i<all.length;i++){var e=all[i];var t=(e.textContent||"").replace(/\\s+/g,"").trim();if(t===w)e.setAttribute("data-bh-skip","1");}return 1;''') % json.dumps(t))
def adv():
    return safe_js(r'''return (function(){var s=document.querySelector(".note-scroller");if(!s)return{atend:1};
var was=Math.round(s.scrollTop);var ns=Math.min(was+Math.round(s.clientHeight*0.8),s.scrollHeight);s.scrollTop=ns;
return{atend:(ns+10>=s.scrollHeight)};})();''')

start=time.time(); clicks=0; lastc=safe_js('return document.querySelectorAll(".comment-item").length;') or 0
last_key=None; stuck=0; finished=False; empty_rounds=0
while time.time()-start<130:
    b=safe_js(PICK)
    if not b:
        info=adv(); time.sleep(jitter(0.4,0.7))
        if not safe_js(PICK):
            empty_rounds+=1
            if (info and info.get("atend") and empty_rounds>=1) or empty_rounds>=3: finished=True; break
        else: empty_rounds=0
        continue
    empty_rounds=0
    cx=int(b["x"]+b["w"]/2); cy=int(b["y"]+b["h"]/2)
    safe_cdp("Input.dispatchMouseEvent",type="mouseMoved",x=cx,y=cy); time.sleep(0.35)
    safe_cdp("Input.dispatchMouseEvent",type="mousePressed",x=cx,y=cy,button="left",clickCount=1)
    safe_cdp("Input.dispatchMouseEvent",type="mouseReleased",x=cx,y=cy,button="left",clickCount=1)
    clicks+=1; time.sleep(jitter(1.0,1.5))
    key=(b["type"],b["t"]); c=safe_js('return document.querySelectorAll(".comment-item").length;') or 0
    if c==lastc and key==last_key: stuck+=1
    else: stuck=0; last_key=key
    lastc=c
    if stuck>=4: skip_text(b["t"]); stuck=0
    if clicks>=400: finished=True; break
print("[%s] expand clicks=%d final=%d finished=%s" % (IDX,clicks,safe_js('return document.querySelectorAll(".comment-item").length;'),finished))

# 6) 提取 + 存盘（一级/二级靠祖先有无 reply-container）
EXTRACT=('''var out=[];var items=document.querySelectorAll(".comment-item");
items.forEach(function(it){try{
 var p=it.parentElement,lvl=1;while(p){if(p.classList&&p.classList.contains("reply-container")){lvl=2;break;}p=p.parentElement;}
 var nick=(it.querySelector(".name")||{}).textContent||""; nick=nick.replace(/\\s+/g," ").trim();
 var content=it.querySelector(".note-text")?(it.querySelector(".note-text").innerText||"").replace(/\\s+/g," ").trim():"";
 var locEl=it.querySelector(".location"); var ip=locEl?(locEl.textContent||"").replace(/\\s+/g," ").trim():"";
 var dateEl=it.querySelector(".date"); var dateText=""; if(dateEl){var d=(dateEl.textContent||"").replace(/\\s+/g," ").trim(); dateText=ip?d.replace(ip,"").trim():d;}
 function cnt(sel){var e=it.querySelector(sel);if(!e)return "0";var t=(e.textContent||"").replace(/\\s+/g,"").trim();return(/^\\d+$/.test(t))?t:"0";}
 var likes=cnt(".interactions .like .count")||cnt(".like .count"); var replies=cnt(".interactions .reply .count")||cnt(".reply .count");
 var ua=it.querySelector('a[href*="/user/"]'); var user=ua?ua.href:"";
 if(nick||content)out.push({lvl:lvl,nick:nick,content:content,date:dateText,ip:ip,likes:likes,replies:replies,user:user});
}catch(e){}});
var title=(document.querySelector("#detail-title")||{}).textContent||document.title||"";
var descEl=document.querySelector("#detail-desc,.desc"); var desc=descEl?(descEl.innerText||"").replace(/\\s+/g," ").trim():"";
return {id:"IDID",comments:out,title:title,desc:desc,url:location.href};''').replace("IDID",NID)
data=safe_js(EXTRACT)
os.makedirs(OUT,exist_ok=True)
fp=os.path.join(OUT,"note%s_raw.json" % IDX)
with open(fp,"w",encoding="utf-8") as f: json.dump(data,f,ensure_ascii=False,indent=2)
cs=data["comments"] if data else []
print("[%s] DONE total=%d l1=%d l2=%d -> %s" % (IDX,len(cs),sum(1 for c in cs if c["lvl"]==1),sum(1 for c in cs if c["lvl"]==2),fp))
'''


def run_bh(program, env_extra):
    env = dict(os.environ, **env_extra)
    return subprocess.run([BH], input=program, text=True, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def collect_note_ids(keyword, n):
    env = {"XHS_SEARCH_URL": SEARCH_URL_TPL % quote(keyword), "XHS_N": str(n)}
    r = run_bh(COLLECT_PROGRAM, env)
    ids = []
    for line in r.stdout.splitlines():
        if line.startswith("IDS_JSON:"):
            ids = json.loads(line[len("IDS_JSON:"):])
            break
    print("收集到 %d 个笔记：%s" % (len(ids), ids))
    return ids


def scrape_note(idx, note_id, keyword):
    env = {
        "XHS_SEARCH_URL": SEARCH_URL_TPL % quote(keyword),
        "XHS_NOTE_ID": note_id, "XHS_NOTE_IDX": str(idx),
        "XHS_OUT": RAW_DIR,
    }
    r = run_bh(NOTE_PROGRAM, env)
    print(r.stdout)


def merge_to_csv(keyword):
    files = sorted(glob.glob(os.path.join(RAW_DIR, "note*_raw.json")))
    rows, summary = [], []
    for fp in files:
        d = json.load(open(fp, encoding="utf-8"))
        nid = d.get("id", ""); title = (d.get("title") or "").strip()
        cs = d.get("comments", [])
        l1 = sum(1 for c in cs if c.get("lvl") == 1); l2 = sum(1 for c in cs if c.get("lvl") == 2)
        summary.append((nid, title[:30], len(cs), l1, l2, d.get("url", "")))
        for i, c in enumerate(cs):
            rows.append({
                "笔记ID": nid, "笔记标题": title, "笔记链接": d.get("url", ""),
                "层级": c.get("lvl"), "序号": i + 1,
                "昵称": c.get("nick", ""), "地区": c.get("ip", ""),
                "时间": c.get("date", ""), "评论内容": c.get("content", ""),
                "点赞数": c.get("likes", ""), "回复数": c.get("replies", ""),
                "用户链接": c.get("user", ""),
            })
    safe_kw = "".join(c if c.isalnum() or "一" <= c <= "鿿" else "_" for c in keyword).strip("_") or "xiaohongshu"
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(OUT_DIR, "小红书_%s_%s.csv" % (safe_kw, ts))
    cols = ["笔记ID", "笔记标题", "笔记链接", "层级", "序号", "昵称", "地区",
            "时间", "评论内容", "点赞数", "回复数", "用户链接"]
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader()
        for r in rows: w.writerow(r)
    print("\n=== 汇总 ===")
    for nid, t, total, l1, l2, url in summary:
        print("  %-24s %s total=%-4d l1=%-4d l2=%-4d" % (nid, t, total, l1, l2))
    print("总评论数：%d" % len(rows))
    print("输出：%s" % out)
    return out


def main():
    if not os.path.exists(BH):
        sys.exit("找不到 browser-harness，请先安装：uv tool install -e ~/browser-harness")
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    keyword = args[0] if len(args) >= 1 else DEFAULT_KEYWORD
    try:
        n = int(args[1]) if len(args) >= 2 else DEFAULT_N
    except ValueError:
        n = DEFAULT_N

    os.makedirs(RAW_DIR, exist_ok=True)
    # 清掉旧的单篇 raw，避免合并进上次的结果
    for old in glob.glob(os.path.join(RAW_DIR, "note*_raw.json")):
        os.remove(old)

    print("关键词：%s   数量：%d" % (keyword, n))
    ids = collect_note_ids(keyword, n)
    if not ids:
        sys.exit("未收集到笔记卡片，检查登录状态 / 搜索结果")

    for i, nid in enumerate(ids, 1):
        print("\n===== 笔记 %d/%d  %s =====" % (i, len(ids), nid))
        scrape_note(i, nid, keyword)

    merge_to_csv(keyword)


if __name__ == "__main__":
    main()
