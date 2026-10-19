"use strict";
const $ = id => document.getElementById(id);
let busy = false, lastQuery = null;
const now = new Date();
$("end").value = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,"0")}-${String(now.getDate()).padStart(2,"0")}`;
function controls() {
  const history = $("kind").value === "history", fund = $("asset").value === "fund";
  $("dates").hidden = !history;
  $("market-options").hidden = !history || fund;
  $("kind").options[0].textContent = fund ? "历史净值" : "历史行情";
  $("kind").options[1].textContent = fund ? "估算涨幅" : "最新行情";
}
$("asset").addEventListener("change", controls); $("kind").addEventListener("change", controls); controls();
function state(loading, message, error=false) {
  busy = loading; $("submit").disabled = loading; $("download").disabled = loading || !lastQuery;
  $("submit").firstChild.textContent = loading ? "正在查询… " : "查询数据 ";
  $("status").textContent = message; $("status").className = error ? "error" : "";
}
function node(tag, text, cls) { const n=document.createElement(tag); if(text!==undefined) n.textContent=text; if(cls)n.className=cls; return n; }
async function post(url, query) {
  const response=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(query)});
  if(!response.ok){ let text=`查询失败 (${response.status})`; try{const data=await response.json();const messages={NetworkError:"数据源连接失败，请稍后重试。",TimeoutError:"数据源响应超时，请稍后重试。",RateLimitError:"数据源请求受限，请稍后重试。",SymbolNotFoundError:"未找到标的，请检查代码与市场。",ResponseError:"数据源返回格式异常，请稍后重试或检查接口。"};text=messages[data.error?.type] || data.error?.message || text;}catch(_){} throw new Error(text); }
  return response;
}
function render(results) {
  $("result-content").replaceChildren();
  for(const result of results){
    const block=node("section",undefined,"result-block"); block.append(node("h3",result.code));
    block.append(node("p",`${result.metadata.label} · ${result.metadata.adjustment} · ${result.queried_at}`,"meta"));
    block.append(node("p",`${result.metadata.units}；${result.metadata.timezone}`,"meta"));
    if(!result.records.length){block.append(node("p","该标的在查询范围内没有数据。","meta"));$("result-content").append(block);continue;}
    const records=result.records;
    if(records[0]["日期"]!==undefined){
      const ordered=[...records].sort((a,b)=>String(a["日期"]).localeCompare(String(b["日期"])));
      const chart=node("div",undefined,"chart");block.append(chart);
      const x=ordered.map(r=>r["日期"]), traces=[];
      if(result.columns.includes("单位净值")){traces.push({x,y:ordered.map(r=>r["单位净值"]),type:"scatter",mode:"lines",line:{color:"#126859"},name:"单位净值"});}
      else if(["开盘","收盘","最高","最低"].every(c=>result.columns.includes(c))){traces.push({x,open:ordered.map(r=>r["开盘"]),close:ordered.map(r=>r["收盘"]),high:ordered.map(r=>r["最高"]),low:ordered.map(r=>r["最低"]),type:"candlestick",name:"K线",increasing:{line:{color:"#d55c54"}},decreasing:{line:{color:"#258a70"}}});if(result.columns.includes("成交量"))traces.push({x,y:ordered.map(r=>r["成交量"]),type:"bar",yaxis:"y2",name:"成交量",marker:{color:"#aacabd"}});}
      $("result-content").append(block);
      if(traces.length)Plotly.newPlot(chart,traces,{margin:{l:55,r:15,t:15,b:35},font:{family:"Segoe UI,Microsoft YaHei",size:11,color:"#617568"},paper_bgcolor:"#fff",plot_bgcolor:"#fff",showlegend:false,xaxis:{rangeslider:{visible:false}},yaxis:{domain:traces.length>1?[.3,1]:[0,1],gridcolor:"#edf1ee"},yaxis2:{domain:[0,.2],gridcolor:"#edf1ee"}},{responsive:true,displayModeBar:false}); else chart.remove();
    }
    const wrap=node("div",undefined,"table-wrap"), table=node("table"), head=node("thead"), tr=node("tr");
    result.columns.forEach(c=>{const th=node("th",c);th.scope="col";tr.append(th);});head.append(tr);table.append(head);
    const tbody=node("tbody"); for(const row of records){const tr=node("tr");result.columns.forEach(c=>tr.append(node("td",row[c]===null?"—":String(row[c]))));tbody.append(tr);}table.append(tbody);wrap.append(table);block.append(wrap);$("result-content").append(block);
  }
}
$("query-form").addEventListener("submit",async event=>{
  event.preventDefault();if(busy)return;
  const query={asset:$("asset").value,codes:$("codes").value.split(/[,，\s]+/).filter(Boolean),kind:$("kind").value};
  if(query.codes.length<1 || query.codes.length>10){state(false,"请输入 1 到 10 个标的。",true);return;}
  if(query.kind==="history"){
    if($("beg").value)query.beg=$("beg").value.replaceAll("-","");if($("end").value)query.end=$("end").value.replaceAll("-","");
    if(query.asset!=="fund"){query.klt=Number($("klt").value);query.fqt=Number($("fqt").value);}
  }
  lastQuery=null;state(true,"正在向数据源查询，请稍候…");
  try{const data=await(await post(`/api/${query.kind}`,query)).json();render(data.results);lastQuery=query;const count=data.results.reduce((n,r)=>n+r.records.length,0);state(false,`查询完成 · ${data.results.length} 个标的 · ${count} 条记录`);}catch(error){state(false,error.message,true);}
});
$("download").addEventListener("click",async()=>{
  if(busy || !lastQuery)return;state(true,"正在重新查询并生成 CSV…");
  try{const response=await post("/api/export",lastQuery),blob=await response.blob(),url=URL.createObjectURL(blob),a=node("a");a.href=url;a.download="efinance.csv";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);state(false,"CSV 已生成。下载会重新查询，数据可能与上次查询略有差异。");}catch(error){state(false,error.message,true);}
});
