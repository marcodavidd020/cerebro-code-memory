"""Visualizations built from the brain: a self-contained interactive HTML
dependency graph, and an Obsidian vault (one note per file, imports as [[links]]).

Both read only the existing index — no new analysis. The HTML graph defaults to the
most central files so it stays responsive on large repos; the Obsidian export writes
every indexed file so its graph view is complete.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import config as cfg
from . import db
from . import graph as graphmod


def _select(conn, limit: int | None, prefix: str | None):
    from . import insights

    ranked = graphmod.rank(conn)  # [(path, score)] desc
    scores = dict(ranked)
    in_scope = [p for p, _ in ranked if not prefix or p.startswith(prefix)]
    files = in_scope[:limit] if limit else list(in_scope)
    keep = set(files)
    # Orphans (in_degree 0) rank lowest by centrality, so the top-N cut drops every
    # one of them — leaving the "Orphans" overlay with nothing to highlight. Pull
    # the overlay nodes (orphans + cycle members, within scope) in explicitly so
    # both buttons actually do something.
    scope = set(in_scope)
    highlight = (set(insights.orphans(conn, prefix)["dead"]) | insights.cycle_members(conn)) & scope
    files += [f for f in in_scope if f in highlight and f not in keep]
    keep = set(files)
    edges = [
        (r["src_path"], r["dst_path"])
        for r in conn.execute("SELECT src_path, dst_path FROM edges")
        if r["src_path"] in keep and r["dst_path"] in keep
    ]
    return files, scores, edges


def graph_html(conn, limit: int = 400, prefix: str | None = None) -> str:
    from . import insights

    files, scores, edges = _select(conn, limit, prefix)
    summ = {
        r["path"]: r["summary_en"]
        for r in conn.execute("SELECT path, summary_en FROM summaries")
    }
    orphan_set = set(insights.orphans(conn).get("dead", []))
    cycle_set = insights.cycle_members(conn)
    nodes = []
    for p in files:
        nodes.append(
            {
                "id": p,
                "label": p.split("/")[-1],
                "group": p.split("/", 1)[0],
                "value": round(scores.get(p, 0.0) * 1000, 3) + 1,
                "score": round(scores.get(p, 0.0), 5),
                "summary": summ.get(p, ""),
                "orphan": p in orphan_set,
                "cycle": p in cycle_set,
            }
        )
    data = {"nodes": nodes, "edges": [{"from": s, "to": d} for s, d in edges]}
    total = conn.execute("SELECT COUNT(*) AS n FROM files WHERE lang IS NOT NULL").fetchone()["n"]
    meta = {
        "shown": len(files),
        "total": total,
        "prefix": prefix or "",
        "orphans": sum(1 for p in files if p in orphan_set),
        "cycles": sum(1 for p in files if p in cycle_set),
    }
    return _TEMPLATE.replace("__DATA__", json.dumps(data)).replace(
        "__META__", json.dumps(meta)
    )


def export_obsidian(config, conn, out_dir: Path) -> dict:
    out = Path(out_dir)
    ranked = dict(graphmod.rank(conn))
    summ = {
        r["path"]: r["summary_en"]
        for r in conn.execute("SELECT path, summary_en FROM summaries")
    }
    deps_by: dict[str, list[str]] = {}
    for r in conn.execute("SELECT src_path, dst_path FROM edges"):
        deps_by.setdefault(r["src_path"], []).append(r["dst_path"])

    files = [
        r["path"] for r in conn.execute("SELECT path FROM files WHERE lang IS NOT NULL")
    ]
    count = 0
    for p in files:
        pkg = p.split("/", 1)[0]
        lang = config.lang_for(p) or "other"
        lines = [
            "---",
            f"tags: [{pkg}, {lang}]",
            f"centrality: {ranked.get(p, 0.0):.4f}",
            "---",
            "",
            f"`{p}`",
            "",
            summ.get(p) or "_No summary yet — run cerebro-summarize._",
            "",
        ]
        syms = db.symbols_for(conn, p)
        if syms:
            lines.append("## Symbols")
            lines += [f"- L{s['line']} {s['kind']} `{s['name']}`" for s in syms[:60]]
            lines.append("")
        deps = sorted(set(deps_by.get(p, [])))
        if deps:
            lines.append("## Imports (depends on)")
            lines += [f"- [[{d}]]" for d in deps]
        note = out / (p + ".md")
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text("\n".join(lines), encoding="utf-8")
        count += 1
    return {"notes": count, "vault": str(out)}


# --- CLI entry points --------------------------------------------------------

def graph_main():
    import argparse

    ap = argparse.ArgumentParser(description="Write an interactive dependency graph HTML")
    ap.add_argument("--limit", type=int, default=400, help="max nodes (by centrality)")
    ap.add_argument("--prefix", default=None, help="only files under this path prefix")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()
    config = cfg.Config.load()
    conn = db.connect(config.db_path)
    out = Path(args.out) if args.out else config.db_path.parent / "cerebro-graph.html"
    out.write_text(graph_html(conn, args.limit, args.prefix), encoding="utf-8")
    print(json.dumps({"html": str(out), "open_with": f"open '{out}'"}))


def obsidian_main():
    import argparse

    ap = argparse.ArgumentParser(description="Export an Obsidian vault of the codebase")
    ap.add_argument("-o", "--out", default=None)
    args = ap.parse_args()
    config = cfg.Config.load()
    conn = db.connect(config.db_path)
    out = Path(args.out) if args.out else config.db_path.parent / "vault"
    result = export_obsidian(config, conn, out)
    result["open"] = "Open this folder as a vault in Obsidian"
    print(json.dumps(result))


_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cerebro · dependency graph</title>
<script src="https://unpkg.com/force-graph"></script>
<style>
  :root{
    --bg:#0d1117; --surface:#161b22; --surface2:#1c2230; --border:#30363d;
    --text:#e6edf3; --muted:#7d8590; --accent:#4c8dff; --radius:10px;
    --font: ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono: ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{background:var(--bg);color:var(--text);font-family:var(--font);font-size:13px;overflow:hidden}
  #app{display:flex;height:100vh}
  #side{width:330px;flex:none;background:var(--surface);border-right:1px solid var(--border);
        display:flex;flex-direction:column;overflow:hidden}
  .brand{display:flex;align-items:baseline;gap:8px;padding:16px 16px 4px}
  .brand b{font-size:15px;font-weight:650;letter-spacing:.2px}
  .brand span{color:var(--muted);font-size:11px}
  #meta{padding:0 16px 12px;color:var(--muted);font-size:11.5px}
  .sec{padding:12px 16px;border-top:1px solid var(--border)}
  .sec.grow{flex:1;overflow:auto}
  .lbl{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px}
  #search{width:100%;padding:9px 11px;border-radius:var(--radius);border:1px solid var(--border);
          background:var(--bg);color:var(--text);font-size:12.5px;outline:none}
  #search:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(76,141,255,.15)}
  #hits{color:var(--muted);font-size:11px;margin-top:6px;min-height:14px}
  #legend{display:flex;flex-wrap:wrap;gap:6px}
  .chip{display:inline-flex;align-items:center;gap:6px;padding:4px 9px;border-radius:999px;
        border:1px solid var(--border);background:var(--bg);cursor:pointer;font-size:11px;
        user-select:none;transition:opacity .15s,border-color .15s}
  .chip.off{opacity:.35}
  .chip .dot{width:9px;height:9px;border-radius:50%}
  #info .empty{color:var(--muted);line-height:1.5}
  #info .path{font-family:var(--mono);font-size:11px;color:#9fb4ff;word-break:break-all;margin-bottom:8px}
  #info .row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px}
  .badge{font-size:10.5px;padding:2px 8px;border-radius:999px;color:#0d1117;font-weight:600}
  .stat{font-size:11px;color:var(--muted)}
  .stat b{color:var(--text);font-weight:600}
  #info .sum{font-size:12.5px;line-height:1.5;margin:4px 0 6px;color:#cdd6e3}
  #info h4{font-size:10px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:14px 0 6px}
  #info ul{list-style:none;margin:0;padding:0}
  #info li{font-family:var(--mono);font-size:11px;padding:4px 8px;border-radius:7px;cursor:pointer;
           color:#c9d1d9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  #info li:hover{background:var(--surface2);color:#fff}
  #info li .pkg{color:var(--muted)}
  #main{position:relative;flex:1}
  #graph{position:absolute;inset:0}
  #bar{position:absolute;top:14px;right:14px;display:flex;gap:6px;z-index:5}
  .btn{background:rgba(22,27,34,.82);backdrop-filter:blur(6px);border:1px solid var(--border);color:var(--text);
       padding:7px 12px;border-radius:8px;font-size:12px;cursor:pointer;font-family:var(--font);
       transition:background .15s,border-color .15s}
  .btn:hover{background:var(--surface2);border-color:#3d4654}
  .btn.active{border-color:var(--accent);color:var(--accent)}
  #load{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
        flex-direction:column;gap:14px;background:var(--bg);z-index:8;transition:opacity .6s}
  #load.hidden{opacity:0;pointer-events:none}
  .spin{width:30px;height:30px;border-radius:50%;border:3px solid var(--border);
        border-top-color:var(--accent);animation:spin .8s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  #load span{color:var(--muted);font-size:12px}
  ::-webkit-scrollbar{width:9px} ::-webkit-scrollbar-thumb{background:#2b3340;border-radius:9px}
</style></head>
<body><div id="app">
  <aside id="side">
    <div class="brand"><b>🧠 cerebro</b><span>dependency graph</span></div>
    <div id="meta"></div>
    <div class="sec">
      <input id="search" placeholder="Search a file…" autocomplete="off" spellcheck="false">
      <div id="hits"></div>
    </div>
    <div class="sec"><div class="lbl">Packages</div><div id="legend"></div></div>
    <div class="sec grow"><div class="lbl">Selection</div><div id="info">
      <div class="empty">Hover a node to light up what it connects to. Drag a node and the web
      follows. Click to pin its details. Size = centrality (PageRank).</div></div></div>
  </aside>
  <div id="main">
    <div id="bar">
      <button class="btn" id="cyc">⚠ Cycles</button>
      <button class="btn" id="orph">○ Orphans</button>
      <button class="btn" id="fit">Fit</button>
      <button class="btn active" id="freeze">❚❚ Pause</button>
    </div>
    <div id="graph"></div>
    <div id="load"><div class="spin"></div><span>simulating layout…</span></div>
  </div>
</div>
<script>
const DATA = __DATA__, META = __META__;
const PALETTE = ["#4c8dff","#3fb950","#db6d28","#e3597b","#a371f7","#1f9ce4",
                 "#f0a02c","#39c5bb","#e2c044","#ec6a5e","#57ab5a","#bc8cff"];
const groups=[...new Set(DATA.nodes.map(n=>n.group))].sort();
const color={}; groups.forEach((g,i)=>color[g]=PALETTE[i%PALETTE.length]);
const byId={}; DATA.nodes.forEach(n=>byId[n.id]=n);

const nbr={}, outM={}, inM={};
DATA.nodes.forEach(n=>nbr[n.id]=new Set());
DATA.edges.forEach(e=>{(outM[e.from]=outM[e.from]||[]).push(e.to);(inM[e.to]=inM[e.to]||[]).push(e.from);
  nbr[e.from].add(e.to); nbr[e.to].add(e.from);});

document.getElementById('meta').innerHTML =
  `<b style="color:var(--text)">${META.shown}</b> of ${META.total} files`+
  `${META.prefix?` · <span style="color:#9fb4ff">${META.prefix}</span>`:''} · ${DATA.edges.length} imports`+
  `${META.cycles?` · <span style="color:#e2c044">⚠ ${META.cycles} in cycles</span>`:''}`+
  `${META.orphans?` · <span style="color:#ec6a5e">○ ${META.orphans} orphans</span>`:''}`;

const hidden=new Set();
const legend=document.getElementById('legend');
groups.forEach(g=>{
  const c=document.createElement('div'); c.className='chip'; c.dataset.g=g;
  c.innerHTML=`<i class="dot" style="background:${color[g]}"></i>${g}`;
  c.onclick=()=>{ c.classList.toggle('off'); hidden.has(g)?hidden.delete(g):hidden.add(g); };
  legend.appendChild(c);
});

let hl=new Set(), focusId=null, pinned=null, overlay=null;
const lid=x=>typeof x==='object'?x.id:x;
const overlaySet=k=>new Set(DATA.nodes.filter(n=>k==='cycle'?n.cycle:n.orphan).map(n=>n.id));
const REL=3.4;
const el=document.getElementById('graph');

const Graph = ForceGraph()(el)
  .backgroundColor('#0d1117')
  .graphData({nodes:DATA.nodes.map(n=>Object.assign({},n)),
              links:DATA.edges.map(e=>({source:e.from,target:e.to}))})
  .nodeId('id').nodeVal(n=>n.value)
  .nodeRelSize(REL)
  .autoPauseRedraw(false)
  .cooldownTicks(Infinity).d3VelocityDecay(0.30).d3AlphaMin(0)
  .linkDirectionalArrowLength(2).linkDirectionalArrowRelPos(1).linkCurvature(0)
  .linkColor(l=>{const s=lid(l.source),t=lid(l.target);
    if(hidden.has(byId[s].group)||hidden.has(byId[t].group))return 'rgba(0,0,0,0)';
    if(focusId)return (s===focusId||t===focusId)?'rgba(76,141,255,.85)':'rgba(48,54,61,.12)';
    return 'rgba(70,80,95,.28)';})
  .linkWidth(l=>{const s=lid(l.source),t=lid(l.target);return focusId&&(s===focusId||t===focusId)?1.4:0.5;})
  .linkDirectionalParticles(l=>{const s=lid(l.source),t=lid(l.target);return focusId&&(s===focusId||t===focusId)?3:0;})
  .linkDirectionalParticleWidth(2.2).linkDirectionalParticleColor(()=>'#7fb0ff').linkDirectionalParticleSpeed(0.012)
  .nodeCanvasObject((node,ctx,scale)=>{
    if(hidden.has(node.group))return;
    const r=Math.sqrt(node.value)*REL, dim=hl.size&&!hl.has(node.id);
    if(node.id===focusId){ctx.shadowColor=color[node.group];ctx.shadowBlur=22;}
    ctx.beginPath(); ctx.arc(node.x,node.y,r,0,6.2832);
    ctx.globalAlpha=dim?0.45:1; ctx.fillStyle=dim?'#6e7681':color[node.group]; ctx.fill();
    ctx.shadowBlur=0;
    if(node.id===pinned){ctx.lineWidth=2/scale;ctx.strokeStyle='#fff';ctx.stroke();}
    if(!dim&&node.cycle){ctx.strokeStyle='rgba(226,192,68,.9)';ctx.lineWidth=1.6/scale;
      ctx.beginPath();ctx.arc(node.x,node.y,r+2.5/scale,0,6.2832);ctx.stroke();}
    if(!dim&&node.orphan){ctx.strokeStyle='rgba(236,106,94,.95)';ctx.lineWidth=1.4/scale;
      ctx.setLineDash([3/scale,2.5/scale]);ctx.beginPath();
      ctx.arc(node.x,node.y,r+(node.cycle?5:2.5)/scale,0,6.2832);ctx.stroke();ctx.setLineDash([]);}
    ctx.globalAlpha=1;
    if(!dim&&(node.value>9||scale>1.8||hl.has(node.id))){
      ctx.font=`${11/scale}px ${getComputedStyle(document.body).fontFamily}`;
      ctx.textAlign='center'; ctx.textBaseline='top';
      ctx.fillStyle=hl.size&&!hl.has(node.id)?'rgba(230,237,243,.2)':'rgba(230,237,243,.92)';
      ctx.fillText(node.label, node.x, node.y+r+2/scale);
    }
  })
  .nodePointerAreaPaint((node,col,ctx)=>{
    if(hidden.has(node.group))return;
    ctx.fillStyle=col; ctx.beginPath();
    ctx.arc(node.x,node.y,Math.sqrt(node.value)*REL+2,0,6.2832); ctx.fill();
  })
  .onNodeHover(node=>{ el.style.cursor=node?'pointer':'default';
    if(node) setHL(node);
    else if(pinned) setHL(live[pinned]||{id:pinned});
    else setHL(null); })
  .onNodeClick(node=>{ pinned=node.id; setHL(node); panel(node.id);
    Graph.centerAt(node.x,node.y,600); Graph.zoom(Math.max(Graph.zoom(),2.4),600); })
  .onBackgroundClick(()=>{ pinned=null; setHL(null);
    document.getElementById('info').innerHTML='<div class="empty">Hover a node to light up what it connects to. Drag a node and the web follows. Click to pin its details.</div>'; });

Graph.d3Force('charge').strength(-260).distanceMax(900);
Graph.d3Force('link').distance(42).strength(.45);

const live={}; Graph.graphData().nodes.forEach(n=>live[n.id]=n);
function setHL(node){ if(node){focusId=node.id; hl=new Set([node.id]); nbr[node.id].forEach(x=>hl.add(x));}
  else if(overlay){focusId=null; hl=overlaySet(overlay);}
  else {focusId=null; hl=new Set();} }
const esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function li(id){const parts=id.split('/');const base=parts.pop();
  return `<li data-id="${esc(id)}" title="${esc(id)}"><span class="pkg">${esc(parts[0]||'')}/…/</span>${esc(base)}</li>`;}
function panel(id){const n=byId[id];if(!n)return;const o=(outM[id]||[]),inn=(inM[id]||[]);
  document.getElementById('info').innerHTML=
    `<div class="path">${esc(id)}</div>`+
    `<div class="row"><span class="badge" style="background:${color[n.group]}">${esc(n.group)}</span>`+
    `<span class="stat">PageRank <b>${n.score}</b></span></div>`+
    `<div class="sum">${esc(n.summary)||'<span style="color:var(--muted)">No summary yet — run cerebro-summarize.</span>'}</div>`+
    `<h4>Imports · ${o.length}</h4><ul>${o.map(li).join('')||'<li style="color:var(--muted);cursor:default">none</li>'}</ul>`+
    `<h4>Imported by · ${inn.length}</h4><ul>${inn.map(li).join('')||'<li style="color:var(--muted);cursor:default">none</li>'}</ul>`;}
function focusNode(id){const node=live[id];if(!node)return;pinned=id;setHL({id});panel(id);
  Graph.centerAt(node.x,node.y,600);Graph.zoom(Math.max(Graph.zoom(),2.4),600);}
document.getElementById('info').addEventListener('click',e=>{const li=e.target.closest('li[data-id]');if(li)focusNode(li.dataset.id);});

const search=document.getElementById('search'),hits=document.getElementById('hits');
search.addEventListener('input',()=>{const q=search.value.toLowerCase().trim();
  hits.textContent=q?`${DATA.nodes.filter(n=>n.id.toLowerCase().includes(q)).length} match`:'';});
search.addEventListener('keydown',e=>{if(e.key!=='Enter')return;const q=search.value.toLowerCase().trim();
  const m=DATA.nodes.find(n=>n.id.toLowerCase().includes(q));if(m)focusNode(m.id);});

const cycB=document.getElementById('cyc'),orphB=document.getElementById('orph');
if(META.cycles)cycB.textContent='⚠ Cycles '+META.cycles; if(META.orphans)orphB.textContent='○ Orphans '+META.orphans;
function setOverlay(k){overlay=overlay===k?null:k;
  cycB.classList.toggle('active',overlay==='cycle'); orphB.classList.toggle('active',overlay==='orphan');
  pinned=null; setHL(null);}
cycB.onclick=()=>setOverlay('cycle'); orphB.onclick=()=>setOverlay('orphan');

document.getElementById('fit').onclick=()=>Graph.zoomToFit(600,60);
let running=true;const fb=document.getElementById('freeze');
fb.onclick=()=>{running=!running; running?Graph.resumeAnimation():Graph.pauseAnimation();
  fb.classList.toggle('active',running); fb.textContent=running?'❚❚ Pause':'▶ Resume';};

function size(){Graph.width(el.clientWidth).height(el.clientHeight);}
size(); window.addEventListener('resize',size);
setTimeout(()=>Graph.zoomToFit(700,70),1200);
setTimeout(()=>document.getElementById('load').classList.add('hidden'),900);
</script></body></html>"""
