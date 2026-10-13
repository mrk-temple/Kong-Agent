import {describe,it,expect} from "vitest";
import {render,screen,cleanup} from "@testing-library/react";
import {Bottle} from "./Bottle";
import {layout,label,projectTrace,appendEvent} from "./projection";
import {parseFrame} from "../../shared/api";
import type {MemoryRecord,AppEvent} from "../../shared/types";
const record=(id="r",version=1):MemoryRecord=>({record_id:id,revision:version,state:"active",kind:"semantic",category:"preference",sensitivity:"personal",current:{version,content:"中文偏好",created_at:"2026-10-12T00:00:00Z",sources:[]}});
const event=(kind:string,version=1,replayed=false):AppEvent=>({version:1,sequence:1,event_id:"local-profile:1",kind,timestamp:new Date().toISOString(),run_id:"run",request_id:"req",data:{selected:[{record_id:"r",version}],records:[{record_id:"r",version}]},replayed});
describe("Memory facts",()=>{
 it("stable bounded layout and equivalent list count",()=>{const records=Array.from({length:40},(_,i)=>record(String(i)));expect(layout(records)).toEqual(layout([...records].reverse()));expect(layout(records)).toHaveLength(24);expect(new Set(layout(records).map(r=>r.x+":"+r.y)).size).toBe(24);});
 it("sensitive labels are hidden",()=>expect(label({...record(),sensitivity:"secret"})).toBe("私人记忆"));
 it("retrieval and assembly never mean delivery",()=>{expect(projectTrace(record(),[event("memory.retrieved")]).stage).toBe("检索命中");expect(projectTrace(record(),[event("memory.context_assembled")]).animate).toBe(false);});
 it("delivery copies only current versions and fresh events",()=>{expect(projectTrace(record(),[event("memory.request_delivered")]).animate).toBe(true);expect(projectTrace(record( "r",2),[event("memory.request_delivered")]).stage).toBe("已保存");expect(projectTrace(record(),[event("memory.request_delivered",1,true)]).animate).toBe(false);});
 it("failed request is explicit and metadata deduplicates",()=>{const first=event("memory.request_delivered");const last={...event("memory.request_failed"),event_id:"local-profile:2"};expect(projectTrace(record(),[first,last]).failed).toBe(true);expect(appendEvent([first],first)).toHaveLength(1);});
 it("bottle has keyboard fragments and real empty state",()=>{render(<Bottle records={[]} onSelect={()=>{}}/>);expect(screen.getByText("还没有保存的记忆")).toBeTruthy();cleanup();render(<Bottle records={[record()]} onSelect={()=>{}}/>);expect(screen.getByRole("button",{name:/中文偏好/})).toBeTruthy();cleanup();});
 it("event frame preserves payload and ignores heartbeat/invalid JSON",()=>{expect(parseFrame("id: local-profile:3\nevent: kong\ndata: {\"version\":1}")?.data.version).toBe(1);expect(parseFrame(": heartbeat")).toBe(null);expect(parseFrame("data: {bad}")).toBe(null);});
});
