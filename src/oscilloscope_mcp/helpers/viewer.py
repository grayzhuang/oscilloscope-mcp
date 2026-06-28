"""Generate a self-contained interactive HTML waveform viewer.

Produces a single HTML file with embedded waveform data that renders
an oscilloscope-like analog waveform display in the browser. Features:

- Per-channel ON/OFF toggle
- Per-channel V/div dropdown
- Draggable GND offset markers (▶)
- Time-axis zoom (scroll) and pan (drag)
- Time-axis scale dropdown (1-2-5 sequence)
- Vertical cursor line snapped to sample points
- Fixed readout panel showing time + per-channel voltage
- Adaptive rendering: min/max envelope when zoomed out, raw samples when zoomed in
- No downsampling of data — full raw samples embedded

Usage::

    from oscilloscope_mcp.helpers.viewer import generate_viewer_html
    html = generate_viewer_html(channels_data, title="My Capture")
    Path("capture.html").write_text(html)
"""

from __future__ import annotations

import json
from typing import Any


def generate_viewer_html(
    channels: dict[str, dict[str, Any]],
    colors: dict[str, str] | None = None,
    title: str = "Oscilloscope Capture",
) -> str:
    """Generate a self-contained HTML viewer.

    Parameters
    ----------
    channels : dict
        Mapping of channel name to channel data::

            {"CHAN1": {"volts": [...], "t0_us": float, "dt_us": float,
                       "n_samples": int, "scale_v_per_div": float,
                       "offset_v": float}, ...}

    colors : dict or None
        Mapping of channel name to CSS color. Defaults provided if None.
    title : str
        Page title.

    Returns
    -------
    str
        Complete HTML document as a string.
    """
    channel_list = list(channels.keys())
    if colors is None:
        default_colors = ["#ffca28", "#4fc3f7", "#ff4081", "#2979ff",
                          "#66bb6a", "#ab47bc"]
        colors = {ch: default_colors[i % len(default_colors)]
                  for i, ch in enumerate(channel_list)}

    data_json = json.dumps(channels)
    channels_json = json.dumps(channel_list)
    colors_json = json.dumps(colors)

    return _TEMPLATE.format(
        title=title,
        data_json=data_json,
        channels_json=channels_json,
        colors_json=colors_json,
    )


_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>{title}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:monospace;background:#1a1a2e;color:#e0e0e0;height:100vh;overflow:hidden}}
.layout{{display:grid;grid-template-rows:auto 1fr;height:100vh}}
.header{{padding:6px 16px;background:#16213e;border-bottom:1px solid #0f3460;display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
.header h1{{font-size:13px;color:#4fc3f7}}
.ch-ctrl{{display:flex;gap:6px;align-items:center;font-size:11px}}
.ch-ctrl label{{cursor:pointer;padding:2px 6px;border-radius:3px;border:1px solid #37474f}}
.ch-ctrl label.on{{border-color:currentColor;font-weight:bold}}
.ch-ctrl select{{font-size:10px;background:#0d1117;color:#e0e0e0;border:1px solid #37474f;border-radius:3px;padding:1px 3px;width:60px}}
.tb-ctrl{{display:flex;gap:4px;align-items:center;font-size:11px;margin-left:auto}}
.tb-ctrl label{{color:#90a4ae}}
.tb-ctrl select{{font-size:10px;background:#0d1117;color:#e0e0e0;border:1px solid #37474f;border-radius:3px;padding:1px 3px;width:70px}}
.panel{{position:relative;overflow:hidden;background:#0d1117}}
canvas{{display:block;width:100%;height:100%}}
.cursor-line{{position:absolute;top:10px;width:1px;background:#ffffff80;pointer-events:none;z-index:50}}
.readout{{position:absolute;top:12px;left:60px;font-size:12px;pointer-events:none;z-index:50;display:none;gap:12px;align-items:center;font-family:monospace}}
.readout span{{padding:1px 6px;border-radius:3px;background:rgba(13,17,23,0.85);min-width:100px;text-align:right}}
.readout .time-val{{color:#a0aec0}}
.gnd-marker{{position:absolute;left:2px;font-size:14px;cursor:ns-resize;user-select:none;font-weight:bold;background:rgba(13,17,23,0.85);padding:1px 4px;border-radius:3px;border:1px solid currentColor;line-height:1.2}}
</style></head><body>
<div class="layout">
  <div class="header">
    <h1>{title}</h1>
    <div class="ch-ctrl" id="ch-ctrl"></div>
    <div class="tb-ctrl">
      <label>T/div:</label>
      <select id="tdiv-select"></select>
    </div>
  </div>
  <div class="panel" id="panel">
    <canvas id="canvas"></canvas>
    <div class="cursor-line" id="cursor-line"></div>
    <div class="readout" id="readout"></div>
  </div>
</div>
<script>
const DATA={data_json};
const CHANNELS={channels_json};
const COLORS={colors_json};
(function(){{
const canvas=document.getElementById("canvas"),ctx=canvas.getContext("2d"),panel=document.getElementById("panel"),chCtrl=document.getElementById("ch-ctrl"),tdivSel=document.getElementById("tdiv-select"),cursorLine=document.getElementById("cursor-line"),readout=document.getElementById("readout");
const LW=50,MT=10,MB=25,NVD=8;
const VDIV_OPTIONS=[0.01,0.02,0.05,0.1,0.2,0.5,1,2,5,10,20,50,100];
const TDIV_OPTIONS=[0.001,0.002,0.005,0.01,0.02,0.05,0.1,0.2,0.5,1,2,5,10,20,50,100,200,500,1000,2000,5000,10000,20000,50000,100000];
function vdivLabel(v){{return v>=1?v+"V":(v*1000)+"mV";}}
function tdivLabel(t){{if(t>=1000)return(t/1000).toFixed(0)+"ms";if(t>=1)return t+"µs";return(t*1000)+"ns";}}

let chState={{}};
CHANNELS.forEach(ch=>{{chState[ch]={{on:true,vPerDiv:DATA[ch].scale_v_per_div,gnd:-DATA[ch].offset_v}};}});

let dS=Infinity,dE=-Infinity;
CHANNELS.forEach(ch=>{{const w=DATA[ch],s=w.t0_us,e=s+w.dt_us*w.n_samples;if(s<dS)dS=s;if(e>dE)dE=e;}});
let vS=dS,vE=dE;
let drag=false,dX=0,dVS=0,gDrag=null;

// T/div state: grid always draws at tDiv intervals (12 divs visible)
let tDiv=nS((dE-dS)/12);  // initial T/div from data span

function buildTdivOptions(){{
  tdivSel.innerHTML="";
  TDIV_OPTIONS.forEach(t=>{{
    const o=document.createElement("option");o.value=t;o.textContent=tdivLabel(t);
    if(Math.abs(t-tDiv)/tDiv<0.01)o.selected=true;
    tdivSel.appendChild(o);
  }});
}}
buildTdivOptions();
tdivSel.onchange=function(){{tDiv=parseFloat(this.value);const mid=(vS+vE)/2;vS=mid-tDiv*6;vE=mid+tDiv*6;rnd();}};

// After zoom, check if tDiv should step up/down
function syncTdiv(){{
  const viewSpan=vE-vS;
  const pxPerDiv=(canvas.width-LW-10)/12;
  const actualPxPerDiv=(canvas.width-LW-10)/(viewSpan/tDiv);
  // If one div is too wide (>200px) or too narrow (<40px), step
  if(actualPxPerDiv>200){{
    // Zoom in too much for current tDiv — go to finer step
    const idx=TDIV_OPTIONS.indexOf(tDiv);
    if(idx>0){{tDiv=TDIV_OPTIONS[idx-1];buildTdivOptions();}}
  }} else if(actualPxPerDiv<40){{
    // Zoom out too much — go to coarser step
    const idx=TDIV_OPTIONS.indexOf(tDiv);
    if(idx<TDIV_OPTIONS.length-1){{tDiv=TDIV_OPTIONS[idx+1];buildTdivOptions();}}
  }}
}}

function buildControls(){{
  chCtrl.innerHTML="";
  CHANNELS.forEach(ch=>{{
    const st=chState[ch],wrap=document.createElement("span");wrap.style.color=COLORS[ch];
    const lbl=document.createElement("label");lbl.textContent=ch.slice(-1);lbl.className=st.on?"on":"";lbl.style.color=COLORS[ch];
    lbl.onclick=()=>{{st.on=!st.on;buildControls();rnd();rGnd();}};wrap.appendChild(lbl);
    const sel=document.createElement("select");sel.style.color=COLORS[ch];
    VDIV_OPTIONS.forEach(v=>{{const o=document.createElement("option");o.value=v;o.textContent=vdivLabel(v);if(Math.abs(v-st.vPerDiv)<0.0001)o.selected=true;sel.appendChild(o);}});
    sel.onchange=()=>{{st.vPerDiv=parseFloat(sel.value);rnd();rGnd();}};wrap.appendChild(sel);chCtrl.appendChild(wrap);
  }});
}}
buildControls();

function rsz(){{canvas.width=panel.clientWidth;canvas.height=panel.clientHeight;rnd();rGnd();}}
function t2x(t){{return LW+(t-vS)/(vE-vS)*(canvas.width-LW-10);}}
function x2t(x){{return vS+(x-LW)/(canvas.width-LW-10)*(vE-vS);}}

function rnd(){{
  const w=canvas.width,h=canvas.height,pH=h-MT-MB;
  ctx.clearRect(0,0,w,h);
  let gridVpd=0;CHANNELS.forEach(ch=>{{if(chState[ch].on&&chState[ch].vPerDiv>gridVpd)gridVpd=chState[ch].vPerDiv;}});
  if(!gridVpd)gridVpd=2;
  const vMn=-(NVD/2)*gridVpd,vMx=(NVD/2)*gridVpd;
  function gV2y(v){{return MT+pH-((v-vMn)/(vMx-vMn))*pH;}}
  // V grid (no voltage labels — scale differs per channel)
  for(let d=0;d<=NVD;d++){{const v=vMn+d*gridVpd,y=gV2y(v);ctx.strokeStyle=d===NVD/2?"#4a5568":"#2d3748";ctx.lineWidth=d===NVD/2?1:0.5;ctx.beginPath();ctx.moveTo(LW,y);ctx.lineTo(w-10,y);ctx.stroke();}}
  // T grid: always at tDiv intervals (like an oscilloscope)
  const gSt=Math.ceil(vS/tDiv)*tDiv;
  ctx.textAlign="center";
  for(let t=gSt;t<=vE;t+=tDiv){{const x=t2x(t);if(x<LW||x>w-10)continue;ctx.strokeStyle="#2d3748";ctx.lineWidth=0.5;ctx.beginPath();ctx.moveTo(x,MT);ctx.lineTo(x,h-MB);ctx.stroke();ctx.fillStyle="#718096";ctx.fillText(fT(t),x,h-6);}}
  // T/div indicator
  ctx.textAlign="left";ctx.fillStyle="#4a5568";ctx.fillText(tdivLabel(tDiv)+"/div",LW+4,h-6);

  // Trigger position marker (t=0) — vertical dashed line + ▼ at top
  const trigX=t2x(0);
  if(trigX>=LW&&trigX<=w-10){{
    ctx.strokeStyle="#ff6f00";ctx.lineWidth=1;ctx.setLineDash([4,3]);
    ctx.beginPath();ctx.moveTo(trigX,MT);ctx.lineTo(trigX,h-MB);ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle="#ff6f00";ctx.font="10px monospace";ctx.textAlign="center";
    ctx.fillText("▼",trigX,MT-1);
    ctx.fillText("T",trigX,MT+10);
  }}

  const pxW=w-LW-10;
  CHANNELS.forEach(ch=>{{
    const st=chState[ch];if(!st.on)return;
    const wf=DATA[ch],volts=wf.volts,chVmn=-(NVD/2)*st.vPerDiv-st.gnd,chVmx=(NVD/2)*st.vPerDiv-st.gnd;
    function chV2y(v){{return MT+pH-((v-chVmn)/(chVmx-chVmn))*pH;}}
    const si=Math.max(0,Math.floor((vS-wf.t0_us)/wf.dt_us)),ei=Math.min(wf.n_samples,Math.ceil((vE-wf.t0_us)/wf.dt_us));
    const vis=ei-si,spp=vis/pxW;
    if(spp<=2){{
      ctx.strokeStyle=COLORS[ch];ctx.lineWidth=1.5;ctx.beginPath();let first=true;const pts=[];
      for(let s=si;s<ei;s++){{const x=t2x(wf.t0_us+s*wf.dt_us),y=chV2y(volts[s]);if(first){{ctx.moveTo(x,y);first=false;}}else ctx.lineTo(x,y);pts.push({{x,y}});}}
      ctx.stroke();
      if(spp<0.5&&pts.length<300){{ctx.fillStyle=COLORS[ch];pts.forEach(p=>{{ctx.beginPath();ctx.arc(p.x,p.y,2,0,Math.PI*2);ctx.fill();}});}}
    }} else {{
      ctx.fillStyle=COLORS[ch]+"25";ctx.strokeStyle=COLORS[ch];ctx.lineWidth=0.8;
      const tops=[],bots=[];
      for(let px=0;px<pxW;px++){{
        const tL=vS+(px/pxW)*(vE-vS),tR=vS+((px+1)/pxW)*(vE-vS);
        const iL=Math.max(si,Math.floor((tL-wf.t0_us)/wf.dt_us)),iR=Math.min(ei,Math.ceil((tR-wf.t0_us)/wf.dt_us));
        if(iL>=iR)continue;let mn=Infinity,mx=-Infinity;
        for(let i=iL;i<iR;i++){{const v=volts[i];if(v<mn)mn=v;if(v>mx)mx=v;}}
        tops.push({{x:LW+px,y:chV2y(mx)}});bots.push({{x:LW+px,y:chV2y(mn)}});
      }}
      if(tops.length>1){{
        ctx.beginPath();ctx.moveTo(tops[0].x,tops[0].y);for(let i=1;i<tops.length;i++)ctx.lineTo(tops[i].x,tops[i].y);for(let i=bots.length-1;i>=0;i--)ctx.lineTo(bots[i].x,bots[i].y);ctx.closePath();ctx.fill();
        ctx.beginPath();for(let i=0;i<tops.length;i++){{if(i===0)ctx.moveTo(tops[i].x,tops[i].y);else ctx.lineTo(tops[i].x,tops[i].y);}}ctx.stroke();
        ctx.beginPath();for(let i=0;i<bots.length;i++){{if(i===0)ctx.moveTo(bots[i].x,bots[i].y);else ctx.lineTo(bots[i].x,bots[i].y);}}ctx.stroke();
      }}
    }}
  }});
  ctx.strokeStyle="#4a5568";ctx.lineWidth=1;ctx.strokeRect(LW,MT,pxW,pH);
}}

function rGnd(){{
  document.querySelectorAll(".gnd-marker").forEach(e=>e.remove());
  const pH=canvas.height-MT-MB;
  let gridVpd=0;CHANNELS.forEach(ch=>{{if(chState[ch].on&&chState[ch].vPerDiv>gridVpd)gridVpd=chState[ch].vPerDiv;}});
  if(!gridVpd)gridVpd=2;
  const vMn=-(NVD/2)*gridVpd,vMx=(NVD/2)*gridVpd;
  CHANNELS.forEach(ch=>{{const st=chState[ch];if(!st.on)return;
    let y=MT+pH-((st.gnd-vMn)/(vMx-vMn))*pH;
    y=Math.max(MT,Math.min(MT+pH,y));
    const m=document.createElement("div");m.className="gnd-marker";m.style.top=(y-10)+"px";m.style.color=COLORS[ch];m.textContent=ch.slice(-1)+"▶";m.style.fontSize="14px";
    m.addEventListener("mousedown",function(e){{e.stopPropagation();gDrag={{ch,sY:e.clientY,sO:st.gnd}};document.body.style.cursor="ns-resize";}});panel.appendChild(m);
  }});
}}

panel.addEventListener("wheel",function(e){{e.preventDefault();const t=x2t(e.clientX-panel.getBoundingClientRect().left),f=e.deltaY>0?1.3:1/1.3;vS=t-(t-vS)*f;vE=t+(vE-t)*f;syncTdiv();rnd();}},{{passive:false}});
panel.addEventListener("mousedown",function(e){{if(gDrag)return;drag=true;dX=e.clientX;dVS=vS;panel.style.cursor="grabbing";}});
document.addEventListener("mousemove",function(e){{
  if(gDrag){{const ch=gDrag.ch,st=chState[ch],dy=e.clientY-gDrag.sY,pH=canvas.height-MT-MB;let gridVpd=0;CHANNELS.forEach(c=>{{if(chState[c].on&&chState[c].vPerDiv>gridVpd)gridVpd=chState[c].vPerDiv;}});if(!gridVpd)gridVpd=2;const vPx=(NVD*gridVpd)/pH;st.gnd=gDrag.sO-dy*vPx;rnd();rGnd();return;}}
  if(drag){{const dx=e.clientX-dX,s=vE-vS;vS=dVS-dx*((vE-vS)/(canvas.width-LW-10));vE=vS+s;rnd();return;}}
  const rect=panel.getBoundingClientRect(),mx=e.clientX-rect.left,my=e.clientY-rect.top;
  if(mx>LW&&mx<canvas.width-10){{
    const t=x2t(mx);
    const firstCh=CHANNELS.find(ch=>chState[ch].on);
    let snapT=t;if(firstCh){{const wf=DATA[firstCh];const idx=Math.round((t-wf.t0_us)/wf.dt_us);snapT=wf.t0_us+idx*wf.dt_us;}}
    const snapX=t2x(snapT);
    cursorLine.style.left=snapX+"px";cursorLine.style.height=(canvas.height-MT-MB)+"px";cursorLine.style.top=MT+"px";cursorLine.style.display="block";
    let html='<span class="time-val">'+fT(snapT)+'</span>';
    CHANNELS.forEach(ch=>{{if(!chState[ch].on)return;const wf=DATA[ch],idx=Math.round((snapT-wf.t0_us)/wf.dt_us);if(idx>=0&&idx<wf.n_samples){{const v=wf.volts[idx];const vs=(v>=0?" ":"")+v.toFixed(3);html+='<span style="color:'+COLORS[ch]+'">'+ch.slice(-1)+':'+vs+'V</span>';}}}});
    readout.innerHTML=html;readout.style.display="flex";
  }}else{{cursorLine.style.display="none";readout.style.display="none";}}
}});
document.addEventListener("mouseup",function(){{if(gDrag){{gDrag=null;document.body.style.cursor="default";return;}}drag=false;panel.style.cursor="default";}});
function nS(r){{const p=Math.pow(10,Math.floor(Math.log10(Math.abs(r)||1))),n=r/p;if(n<=1)return p;if(n<=2)return 2*p;if(n<=5)return 5*p;return 10*p;}}
function fT(t){{if(Math.abs(t)>=1000)return(t/1000).toFixed(1)+"ms";if(Math.abs(t)>=1)return t.toFixed(2)+"µs";return(t*1000).toFixed(1)+"ns";}}
rsz();window.addEventListener("resize",rsz);
}})();
</script></body></html>"""
