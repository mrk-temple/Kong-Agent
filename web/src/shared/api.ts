let csrf="";
export const command=()=>crypto.randomUUID();
export class ApiError extends Error {constructor(public code:string,message:string,public command_id?:string){super(message)}}
export async function api<T=any>(path:string,method="GET",body?:unknown):Promise<T>{
  let response:Response;
  try {response=await fetch("/v1"+path,{method,credentials:"same-origin",headers:{"Content-Type":"application/json","X-Kong-CSRF":csrf},body:body===undefined?undefined:JSON.stringify(body)});}
  catch {throw new ApiError("connection_lost","连接中断，后端可能已处理。请先刷新状态或查询操作收据。",(body as any)?.command_id)}
  const data=await response.json();
  if(!response.ok)throw new ApiError(data.code??"request_failed",data.message??"操作失败。",(body as any)?.command_id);
  return data;
}
export async function pair(){
  const token=new URLSearchParams(location.hash.slice(1)).get("pair");
  if(token){
    history.replaceState(null,"",location.pathname+location.search);
    const response=await fetch("/v1/session",{method:"POST",credentials:"same-origin",headers:{"Authorization":"Bearer "+token,"Content-Type":"application/json"},body:"{}"});
    if(!response.ok)throw new ApiError("session_required","请使用本地启动入口打开 Kong。 ");
    csrf=(await response.json()).csrf_token;
  }else{csrf=(await api("/session")).csrf_token;}
}
export function parseFrame(frame:string):{event:string;data:any;id?:string}|null{
  let event="message",id:string|undefined;const lines:string[]=[];
  for(const line of frame.split("\n")){if(line.startsWith("event:"))event=line.slice(6).trim();if(line.startsWith("id:"))id=line.slice(3).trim();if(line.startsWith("data:"))lines.push(line.slice(5).trimStart());}
  if(!lines.length)return null;
  try{return {event,id,data:JSON.parse(lines.join("\n"))}}catch{return null;}
}
export async function streamEvents(cursor:string,onEvent:(event:any)=>void,onState:(state:string)=>void,signal:AbortSignal){
 let reconnected=false;
 while(!signal.aborted){
  try{
   const response=await fetch("/v1/events?after="+encodeURIComponent(cursor),{credentials:"same-origin",signal});
   if(!response.ok){if(response.status===410){cursor=(await api("/health")).cursor;onState("历史已裁剪，刷新当前状态");continue;}throw Error("stream");}
   onState("已连接");const reader=response.body!.getReader();const decoder=new TextDecoder();let buffer="";
   while(!signal.aborted){const timeout=setTimeout(()=>{void reader.cancel();},15000);let chunk:ReadableStreamReadResult<Uint8Array>;try{chunk=await reader.read();}finally{clearTimeout(timeout);}const {value,done}=chunk;if(done)break;onState("已连接");buffer+=decoder.decode(value,{stream:true}).replace(/\r\n/g,"\n");let boundary:number;
    while((boundary=buffer.indexOf("\n\n"))>=0){const frame=parseFrame(buffer.slice(0,boundary));buffer=buffer.slice(boundary+2);if(frame?.event==="snapshot")cursor=frame.data.cursor;
     if(frame?.event==="kong"&&frame.data.version===1){if(frame.id)cursor=frame.id;onEvent({...frame.data,replayed:reconnected});}}
   }
  }catch(error){if(signal.aborted)return;onState("连接中断，正在重连");}
  reconnected=true;await new Promise<void>(resolve=>{const end=()=>{clearTimeout(timer);signal.removeEventListener("abort",end);resolve();};const timer=setTimeout(end,1500);signal.addEventListener("abort",end,{once:true});});
 }
}
