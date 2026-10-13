import {useId} from "react";
import {layout,label,projectTrace} from "./projection";
import type {MemoryRecord,AppEvent} from "../../shared/types";
export function Bottle({records,events=[],onSelect,small=false,motion=true}:{records:MemoryRecord[];events?:AppEvent[];onSelect:(record:MemoryRecord)=>void;small?:boolean;motion?:boolean}){
 const uid=useId().replace(/:/g,"");const fragments=layout(records);
 return <div className={"bottle-scene "+(small?"bottle-small":"")}>
  <svg viewBox="0 0 160 240" role="img" aria-label={`记忆漂流瓶，已载入 ${records.length} 条记忆`}>
   <defs><linearGradient id={uid+"glass"} x1="0" x2="1"><stop offset="0" stopColor="#dbe7ed" stopOpacity=".5"/><stop offset=".23" stopColor="white" stopOpacity=".95"/><stop offset=".65" stopColor="white" stopOpacity=".3"/><stop offset="1" stopColor="#b8ccd8" stopOpacity=".45"/></linearGradient><linearGradient id={uid+"light"} x1="0" y1="0" x2="1" y2="1"><stop stopColor="white"/><stop offset="1" stopColor="#ecf3f6"/></linearGradient><clipPath id={uid+"clip"}><path d="M59 31H101V51C101 59 116 68 122 76Q132 87 132 105V207Q132 223 115 224H45Q28 223 28 207V105Q28 87 38 76C44 68 59 59 59 51Z"/></clipPath></defs>
   <ellipse cx="80" cy="231" rx="49" ry="5" fill="#cddbe2" opacity=".18"/>
   <path d="M59 31H101V51C101 59 116 68 122 76Q132 87 132 105V207Q132 223 115 224H45Q28 223 28 207V105Q28 87 38 76C44 68 59 59 59 51Z" fill={`url(#${uid}glass)`} stroke="#a7bece" strokeOpacity=".6" strokeWidth="1.2"/>
   <rect x="56" y="20" width="48" height="13" rx="4" fill="#ecedeb" stroke="#d3d8d8"/>
   <path d="M44 83Q37 91 37 109V198" fill="none" stroke="white" strokeOpacity=".9" strokeWidth="4" strokeLinecap="round"/>
   <path d="M47 217H112" stroke="white" strokeWidth="3" opacity=".8"/>
   <g clipPath={`url(#${uid}clip)`}>{fragments.map(({record,x,y,angle})=>{const trace=projectTrace(record,events);return <g key={record.record_id} transform={`translate(${x},${y}) rotate(${angle},12,6)`} role="button" tabIndex={small?-1:0} aria-label={`查看记忆：${label(record)}，${trace.stage}`} onClick={()=>onSelect(record)} onKeyDown={event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();onSelect(record);}}} className={"memory-fragment "+(trace.stage!=="已保存"?"fragment-active":"")}>
    <rect width="28" height="12" rx="3" fill={`url(#${uid}light)`} stroke="#c2d2db" strokeWidth=".7"/>
    <text x="14" y="8" fontSize="3.6" textAnchor="middle" fill="#536572">{label(record).slice(0,6)}</text>
   </g>})}</g>
   {!records.length&&<g><path d="M69 143Q80 134 91 143" fill="none" stroke="#d0dbe1"/><text x="80" y="158" textAnchor="middle" fontSize="6" fill="#94a3ad">留一点空间，给未来</text></g>}
  </svg>
  {!small&&<div className="bottle-caption">{records.length?`${records.length} 条真实记忆 · 安静地保存在这里`:"还没有保存的记忆"}<span>已保存的内容，与一次回答分开记录。</span></div>}
  {motion&&records.some(r=>projectTrace(r,events).animate)&&<span className="injection-copy" aria-hidden="true">记忆进入本轮请求</span>}
 </div>
}
