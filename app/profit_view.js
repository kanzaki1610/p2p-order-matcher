/* Reporting controls only. No exchange writes or pricing/release switches. */
'use strict';
window.createProfitView=function({api,notify,fmt,when}){
 const $=id=>document.getElementById(id),node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n};
 let generation=0,page=1,current=null,polling=null,running=false,historyEdited=false;
 const today=()=>new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Ho_Chi_Minh',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
 const addDays=(d,n)=>new Date(Date.parse(d+'T00:00:00Z')+n*86400000).toISOString().slice(0,10);
 function period(){
  const mode=$('profitPeriod').value;
  if(mode==='day')return {from_day:$('profitDay').value,to_day:$('profitDay').value};
  if(mode==='month'){
   const m=$('profitMonth').value;if(!/^\d{4}-\d{2}$/.test(m))throw Error('Chọn tháng cần thống kê.');
   const [y,mm]=m.split('-').map(Number),end=new Date(Date.UTC(y,mm,0)).toISOString().slice(0,10);
   return {from_day:m+'-01',to_day:end>today()?today():end};
  }
  return {from_day:$('profitFrom').value,to_day:$('profitTo').value};
 }
 function fields(){
  const m=$('profitPeriod').value;
  $('profitDayField').hidden=m!=='day';$('profitMonthField').hidden=m!=='month';
  $('profitFromField').hidden=m!=='range';$('profitToField').hidden=m!=='range';
 }
 function metric(title,value,sub,cls){const d=node('div',undefined,'profitMetric');d.append(node('small',title),node('strong',value,cls),node('span',sub||''));return d;}
 function signed(v){return v===null?'Thiếu giá vốn':fmt(v)+'đ';}
 function color(v){return v===null?'warn':Number(v)<0?'bad':'good';}
 function table(head,rows){
  const t=node('table',undefined,'profitTable'),thead=node('thead'),tr=node('tr');
  for(const h of head)tr.append(node('th',h));thead.append(tr);t.append(thead);const body=node('tbody');
  for(const values of rows){const r=node('tr');for(const v of values){const td=node('td');if(v instanceof Node)td.append(v);else td.textContent=v;r.append(td);}body.append(r);}t.append(body);return t;
 }
 function profitCell(value,missing){const p=node('span',signed(value),color(value));if(Number(missing)>0)p.title='Thiếu giá vốn cho '+fmt(missing)+' USDT';return p;}
 function syncText(job){
  if(!job)return 'Chưa đồng bộ lịch sử.';
  const result=Object.entries(job.results||{}).map(([ex,v])=>ex+': '+(v.ok?`${v.count} lệnh`:v.message)).join(' · ');
  const state={RUNNING:'Đang đồng bộ',DONE:'Đã đồng bộ',FAILED:'Đồng bộ chưa thành công',INTERRUPTED:'Đồng bộ bị gián đoạn'}[job.state]||job.state;
  return `${state} ${job.from} → ${job.to} · ${when(job.started)}${result?' · '+result:''}`;
 }
 function render(r){
  current=r;const t=r.totals;
  $('profitMetrics').replaceChildren(
   metric('Mua vào',fmt(t.buy_qty)+' USDT',fmt(t.buy_fiat)+'đ · '+t.buy_count+' lệnh'),
   metric('Bán ra',fmt(t.sell_qty)+' USDT',fmt(t.sell_fiat)+'đ · '+t.sell_count+' lệnh'),
   metric('Lãi gộp tạm tính',r.row_count||Object.values(r.coverage).every(Boolean)?signed(t.profit):'Chưa có dữ liệu','FIFO · chưa trừ phí',color(t.profit)),
   metric('Lãi phần đủ giá vốn',fmt(t.known_profit)+'đ','Thiếu vốn: '+fmt(t.missing_qty)+' USDT',color(t.known_profit)),
   metric('USDT tồn sổ cuối kỳ',fmt(t.inventory_qty)+' USDT','Sổ chung 2 sàn, không phải số dư ví'),
   metric('Giá vốn tồn sổ',fmt(t.inventory_cost)+'đ','Chưa tính lãi/lỗ do biến động giá'));
  $('profitWarnings').replaceChildren(...r.warnings.map(x=>node('p',x,'hint')));
  $('profitSyncStatus').textContent=syncText(r.sync);
  $('profitDaily').replaceChildren(table(['Ngày VN','Lệnh','Tiền mua','Tiền bán','Lãi gộp tạm tính'],r.daily.map(d=>[
   d.date,String(d.orders),fmt(d.buy_fiat)+'đ',fmt(d.sell_fiat)+'đ',profitCell(d.profit,d.missing_qty)])));
  $('profitOrders').replaceChildren(table(['Hoàn tất VN','Sàn','Chiều','Mã lệnh','USDT','Đơn giá','Thành tiền','Giá vốn bán','Lãi gộp'],r.rows.map(o=>[
   when(o.completed),o.exchange,o.side==='BUY'?'Mua':'Bán',o.code,fmt(o.quantity),fmt(o.price)+'đ',fmt(o.fiat)+'đ',
   o.side==='SELL'?fmt(o.cost)+'đ':'—',o.side==='SELL'?profitCell(o.profit,o.missing_qty):'—'])));
  $('profitEmpty').hidden=r.row_count>0;
  $('profitPage').textContent=`Trang ${r.page}/${Math.max(1,Math.ceil(r.row_count/100))} · ${r.row_count} lệnh`;
  $('profitPrevious').disabled=r.page<=1;$('profitNext').disabled=r.page*100>=r.row_count;
  if(r.opening&&!$('profitOpeningForm').contains(document.activeElement)){
   $('profitOpeningDay').value=r.opening.day;$('profitOpeningQuantity').value=r.opening.quantity;$('profitOpeningCost').value=r.opening.unit_cost;
  }
  if(r.sync?.state==='RUNNING')watch(r.sync.id);
 }
 async function load(){
  const n=++generation;
  try{
   const p=period();if(!p.from_day||!p.to_day||p.from_day>p.to_day)throw Error('Chọn ngày hợp lệ, không ở tương lai.');
   if(!historyEdited)$('profitHistoryFrom').value=[addDays(p.from_day,-90),addDays(p.to_day,-366)].sort().pop();
   $('profitLoadStatus').textContent='Đang tính từ lịch sử đã lưu…';
   const r=await api('profit?'+new URLSearchParams({...p,exchange:$('profitExchange').value,page}));
   if(n!==generation)return;
   render(r);$('profitLoadStatus').textContent='Giờ Việt Nam · Chỉ lệnh đã hoàn tất · Theo ngày hoàn tất';
  }catch(e){if(n===generation){$('profitLoadStatus').textContent=e.message;notify(e.message,true);}}
 }
 function watch(id){
  if(polling)return;running=true;$('profitSync').disabled=true;
  polling=setTimeout(async()=>{
   polling=null;
   try{const j=await api('profit/sync/'+encodeURIComponent(id));$('profitSyncStatus').textContent=syncText(j);
    if(j.state==='RUNNING'){watch(id);return;}
    running=false;$('profitSync').disabled=false;await load();
   }catch(e){running=false;$('profitSync').disabled=false;notify(e.message,true);}
  },3000);
 }
 $('profitFilter').onsubmit=e=>{e.preventDefault();page=1;load();};
 $('profitPeriod').onchange=()=>{fields();page=1;load();};
 $('profitDay').onchange=$('profitMonth').onchange=$('profitExchange').onchange=()=>{page=1;load();};
 $('profitToday').onclick=()=>{$('profitPeriod').value='day';$('profitDay').value=today();fields();page=1;load();};
 $('profitThisMonth').onclick=()=>{$('profitPeriod').value='month';$('profitMonth').value=today().slice(0,7);fields();page=1;load();};
 $('profitPrevious').onclick=()=>{if(page>1){page--;load();}};
 $('profitHistoryFrom').onchange=()=>{historyEdited=true;};
 $('profitNext').onclick=()=>{if(current&&page*100<current.row_count){page++;load();}};
 $('profitSync').onclick=async()=>{
  if(running)return;
  try{const p=period(),from=$('profitHistoryFrom').value;
   if(!from||from>p.from_day)throw Error('Ngày bắt đầu đồng bộ phải trước hoặc bằng ngày đầu kỳ thống kê.');
   running=true;$('profitSync').disabled=true;
   const j=await api('profit/sync',{method:'POST',body:JSON.stringify({from_day:from,to_day:p.to_day})});
   $('profitSyncStatus').textContent=syncText(j);watch(j.id);
  }catch(e){running=false;$('profitSync').disabled=false;notify(e.message,true);}
 };
 $('profitOpeningForm').onsubmit=e=>{
  e.preventDefault();
  const day=$('profitOpeningDay').value,q=$('profitOpeningQuantity').value,c=$('profitOpeningCost').value;
  if(!confirm(`Lưu vốn đầu kỳ ${day}: ${q} USDT, giá vốn ${c}đ/USDT? Mốc này đặt lại sổ FIFO từ đầu ngày, không thay đổi lệnh hoặc giá vốn bám giá.`))return;
  api('profit/opening',{method:'PUT',body:JSON.stringify({day,quantity:q,unit_cost:c})}).then(r=>{notify(r.message);page=1;load();}).catch(e=>notify(e.message,true));
 };
 const d=today();$('profitDay').value=d;$('profitMonth').value=d.slice(0,7);$('profitFrom').value=d.slice(0,7)+'-01';$('profitTo').value=d;
 $('profitHistoryFrom').value=addDays(d.slice(0,7)+'-01',-90);$('profitOpeningDay').value=d.slice(0,7)+'-01';fields();
 return {open(){load();}};
};
