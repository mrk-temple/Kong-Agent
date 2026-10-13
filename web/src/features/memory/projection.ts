import type {MemoryRecord,AppEvent} from "../../shared/types";
export const hash=(value:string)=>Array.from(value).reduce((result,ch)=>(Math.imul(result,31)+ch.charCodeAt(0))>>>0,7);
export const label=(record:MemoryRecord)=>["sensitive","secret"].includes(record.sensitivity)?"私人记忆":record.current.content.slice(0,8);
export function layout(records:MemoryRecord[]){
 const used=new Set<number>();return [...records].sort((a,b)=>a.record_id.localeCompare(b.record_id)).slice(0,24).map(record=>{
  let slot=hash(record.record_id)%24;while(used.has(slot))slot=(slot+1)%24;used.add(slot);
  return {record,x:32+(slot%3)*34,y:78+Math.floor(slot/3)*17,angle:(hash(record.record_id)%9)-4};});
}
export type TraceState={stage:string;animate:boolean;request_id:string|null;failed:boolean};
export function projectTrace(record:MemoryRecord,events:AppEvent[],now=Date.now()):TraceState{
 let state:TraceState={stage:"已保存",animate:false,request_id:null,failed:false};
 for(const event of events){const records=event.kind==="memory.retrieved"?event.data.records:event.data.selected;
  if(!Array.isArray(records)||!records.some((r:any)=>r.record_id===record.record_id&&r.version===record.current.version))continue;
  const stages:Record<string,string>={"memory.retrieved":"检索命中","memory.selected":"本轮选中","memory.context_assembled":"已组装上下文","memory.request_dispatched":"请求已发出","memory.request_delivered":"已进入请求","memory.request_failed":"请求失败"};
  if(stages[event.kind])state={stage:stages[event.kind],request_id:event.request_id,failed:event.kind==="memory.request_failed",animate:event.kind==="memory.request_delivered"&&!event.replayed&&now-Date.parse(event.timestamp)<5000};
 }
 return state;
}
export function appendEvent(events:AppEvent[],event:AppEvent){return events.some(e=>e.event_id===event.event_id)?events:[...events,event].slice(-1000);}
