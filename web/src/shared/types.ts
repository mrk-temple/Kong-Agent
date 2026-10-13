export type Mode="fast"|"auto"|"plan";
export interface MemoryRecord {record_id:string;revision:number;state:string;kind:string;category:string;sensitivity:string;current:{version:number;content:string;created_at:string;sources:{origin:string;excerpt:string;source_id:string}[]};memberships?:Bottle[];visible?:boolean;visible_reason?:string}
export interface Bottle {bottle_id:string;title:string;revision:number;active:boolean}
export interface AppEvent {version:number;sequence:number;event_id:string;kind:string;timestamp:string;run_id:string|null;request_id:string|null;data:any;replayed?:boolean}
export interface Message {role:string;content:string;run_id:string}
export interface Run {run_id:string;status:string;mode:Mode;intent:string;driver_active:boolean;outcome_kind:string;pending_question:string|null;pending_approval:{revision:number}|null;plan:any;unknown_actions:string[];fresh_required:boolean;can_resume:boolean;observations:any[];metrics:any;report:any}
export interface Thread {thread_id:string;title:string;runs?:Run[];accepted_messages?:Message[]}
export interface ModelStatus {provider:string;model:string|null;base_url:string|null;configured:boolean;api_key_configured:boolean;stream:boolean}
